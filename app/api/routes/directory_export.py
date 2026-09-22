"""Read-only employee roster for the shared identity service.

Circle is the source of truth for the company directory, so id-sync pulls the
roster from here and every other app reads it from id-sync - no app talks to
Circle directly.

Deliberately narrow: only the fields other apps may see. PAN, Aadhaar, salary,
CTC, bank details and appraisal history stay inside Circle and never cross this
boundary, even though they sit on the same employee document.

The route is wired up INLINE in `app.main.create_app` rather than via an
APIRouter included here. On FastAPI 0.141 a router defined in this module was not
registered in time when included at import, so the logic lives in the pure
`build_directory` function below and `create_app` mounts it directly on the app,
before the generic /api/{resource} router. `build_directory` is import-safe and
easy to unit test.
"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import HTTPException, status

from app.domain.registry import get_resource
from app.services.resource_service import ResourceService

INTERNAL_SECRET_HEADER = "X-Internal-Secret"


def check_internal_secret(provided: str | None, expected: str) -> None:
    """Raise unless the caller presented the configured shared secret."""
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Directory export is not configured.",
        )
    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid internal secret.")


def _entry(doc: dict[str, Any]) -> dict[str, Any] | None:
    email = (doc.get("email") or "").strip().lower()
    code = (doc.get("id") or "").strip()  # Circle keys employees by their EMP-#### code
    if not email or not code:
        return None
    return {
        "employee_code": code,
        "name": doc.get("fullName") or email,
        "email": email,
        "designation": doc.get("role") or None,   # Circle's "role" is the job title
        "department": doc.get("department") or None,
        "status": doc.get("status") or None,      # Active | On Leave | Suspended | Offboarded
    }


def build_directory(service: ResourceService) -> list[dict[str, Any]]:
    """The full employee roster, minimal safe fields only."""
    docs = service.list(get_resource("employees"))
    return [e for doc in docs if (e := _entry(doc)) is not None]
