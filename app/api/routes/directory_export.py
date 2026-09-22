"""Read-only employee roster for the shared identity service.

Circle is the source of truth for the company directory, so id-sync pulls the
roster from here and every other app reads it from id-sync - no app talks to
Circle directly.

Deliberately narrow: this returns ONLY the fields other apps may see. PAN,
Aadhaar, salary, CTC, bank details and appraisal history stay inside Circle and
never cross this boundary, even though they sit on the same employee document.

Authenticated by a shared secret, not a dashboard session, because the caller is
a service. Constant-time comparison, and 503 rather than an open door when the
secret is unconfigured.
"""

from __future__ import annotations

import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel

from app.api.dependencies import get_resource_service
from app.core.config import Settings, get_settings
from app.domain.registry import get_resource
from app.services.resource_service import ResourceService

router = APIRouter(prefix="/api/directory", tags=["directory"])

INTERNAL_SECRET_HEADER = "X-Internal-Secret"


class DirectoryEntry(BaseModel):
    employee_code: str
    name: str
    email: str
    designation: str | None = None
    department: str | None = None
    # Active | On Leave | Suspended | Offboarded, straight from Circle.
    status: str | None = None


def require_internal_secret(
    settings: Annotated[Settings, Depends(get_settings)],
    x_internal_secret: Annotated[str | None, Header(alias=INTERNAL_SECRET_HEADER)] = None,
) -> None:
    expected = getattr(settings, "internal_api_secret", "") or ""
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Directory export is not configured.",
        )
    if not x_internal_secret or not secrets.compare_digest(x_internal_secret, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid internal secret.")


def _entry(doc: dict[str, Any]) -> DirectoryEntry | None:
    email = (doc.get("email") or "").strip().lower()
    code = (doc.get("id") or "").strip()  # Circle keys employees by their EMP-#### code
    if not email or not code:
        return None
    return DirectoryEntry(
        employee_code=code,
        name=doc.get("fullName") or email,
        email=email,
        designation=doc.get("role") or None,     # Circle's "role" is the job title
        department=doc.get("department") or None,
        status=doc.get("status") or None,
    )


@router.get("/export", response_model=list[DirectoryEntry], dependencies=[Depends(require_internal_secret)])
def export_directory(
    service: Annotated[ResourceService, Depends(get_resource_service)],
) -> list[DirectoryEntry]:
    """The full employee roster, minimal fields only."""
    docs = service.list(get_resource("employees"))
    entries = [e for doc in docs if (e := _entry(doc)) is not None]
    return entries
