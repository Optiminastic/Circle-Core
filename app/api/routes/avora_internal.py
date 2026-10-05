"""Server-to-server endpoints for Avora: one employee's pay, bank and documents.

Avora's backend calls these with `X-Avora-Secret`; no browser ever does. Avora
applies its own, stricter access rules (only the employee, HR and admin) and
audits every read before anything reaches a person. Employees are looked up by
work email because that is the key both systems already share.
"""

from __future__ import annotations

import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status

from app.api.dependencies import get_repository, get_storage
from app.api.routes.documents import _safe_name
from app.core.config import Settings, get_settings
from app.domain.registry import get_resource
from app.repositories.base import DocumentRepository
from app.services import avora_export
from app.storage.base import FileStorage

SECRET_HEADER = "X-Avora-Secret"
_EMPLOYEES = "employees"
_DOCUMENTS = "documents"


def require_avora_secret(
    settings: Settings = Depends(get_settings),
    provided: Annotated[str | None, Header(alias=SECRET_HEADER)] = None,
) -> None:
    expected = settings.avora_api_secret
    if not expected:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Not configured.")
    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid secret.")


router = APIRouter(
    prefix="/api/internal/avora",
    tags=["internal"],
    dependencies=[Depends(require_avora_secret)],
)


def _employee_by_email(repo: DocumentRepository, email: str) -> dict[str, Any]:
    wanted = email.strip().lower()
    for doc in repo.list(get_resource(_EMPLOYEES).table):
        if (doc.get("email") or "").strip().lower() == wanted:
            return doc
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such employee.")


EmailQuery = Annotated[str, Query(min_length=3, max_length=320)]


@router.get("/compensation")
def employee_compensation(
    email: EmailQuery, repo: DocumentRepository = Depends(get_repository)
) -> dict[str, Any]:
    return avora_export.compensation_for(_employee_by_email(repo, email))


@router.get("/documents")
def employee_documents(
    email: EmailQuery, repo: DocumentRepository = Depends(get_repository)
) -> dict[str, Any]:
    employee = _employee_by_email(repo, email)
    code = str(employee.get("id") or "")
    docs = repo.find(_DOCUMENTS, {"entityType": "employee", "entityId": code})
    shareable = [d for d in docs if avora_export.is_shareable_document(d, code)]
    return {"employee_code": code, "documents": [avora_export.document_entry(d) for d in shareable]}


@router.get("/documents/{doc_id}/content")
def employee_document_content(
    doc_id: str,
    email: EmailQuery,
    repo: DocumentRepository = Depends(get_repository),
    storage: FileStorage = Depends(get_storage),
) -> Response:
    """The file's bytes - only if it belongs to the employee Avora named, so a
    known document id cannot be used to reach anyone else's file."""
    employee = _employee_by_email(repo, email)
    doc = repo.get(_DOCUMENTS, doc_id)
    if doc is None or not avora_export.is_shareable_document(doc, str(employee.get("id") or "")):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such document.")
    data, stored_type = storage.get(doc["storageKey"])
    return Response(
        content=data,
        media_type=doc.get("contentType") or stored_type or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{_safe_name(doc.get("fileName") or "file")}"'},
    )
