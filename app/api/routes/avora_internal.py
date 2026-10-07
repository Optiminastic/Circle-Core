"""Server-to-server endpoints for Avora: one employee's pay, bank and documents.

Avora's backend calls these with `X-Avora-Secret`; no browser ever does. Avora
applies its own, stricter access rules (only the employee, HR and admin) and
audits every read before anything reaches a person. Employees are looked up by
work email because that is the key both systems already share.
"""

from __future__ import annotations

import hmac
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
_ONBOARDING = "onboarding"
_DOC_REQUESTS = "doc_requests"


def require_avora_secret(
    settings: Settings = Depends(get_settings),
    provided: Annotated[str | None, Header(alias=SECRET_HEADER)] = None,
) -> None:
    expected = settings.avora_api_secret
    if not expected:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Not configured.")
    if not provided or not hmac.compare_digest(provided.encode("utf-8", "replace"), expected.encode()):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid secret.")


router = APIRouter(
    prefix="/api/internal/avora",
    tags=["internal"],
    dependencies=[Depends(require_avora_secret)],
)


def _employee_by_email(repo: DocumentRepository, email: str) -> dict[str, Any]:
    """The one employee with this work email. Nothing makes emails unique in
    Circle, so a rehire can leave an old record behind: a current record wins
    over an Offboarded one, and two current records are a conflict (409)
    rather than a guess - guessing would hand Avora someone else's pay."""
    matches = repo.find_text_ci(get_resource(_EMPLOYEES).table, "email", email)
    current = [doc for doc in matches if doc.get("status") != "Offboarded"]
    candidates = current or matches
    if not candidates:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such employee.")
    if len(candidates) > 1:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Several employees share this email.")
    return candidates[0]


EmailQuery = Annotated[str, Query(min_length=3, max_length=320)]


@router.get("/compensation")
def employee_compensation(
    email: EmailQuery, repo: DocumentRepository = Depends(get_repository)
) -> dict[str, Any]:
    employee = _employee_by_email(repo, email)
    onboarding = repo.get(_ONBOARDING, str(employee["candidateId"])) if employee.get("candidateId") else None
    offer_letter = (onboarding or {}).get("offerLetter")
    return avora_export.compensation_for(employee, offer_letter if isinstance(offer_letter, dict) else None)


def _shareable_documents(repo: DocumentRepository, employee: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """This person's shareable documents by id - the single rule used by both
    the listing and the download, so nothing unlisted can be fetched."""
    refs = avora_export.owner_refs(employee)
    owner_ids = sorted({entity_id for _, entity_id in refs})
    review = avora_export.review_of(
        [req for owner in owner_ids for req in repo.find(_DOC_REQUESTS, {"candidateId": owner})]
    )
    docs = [
        doc
        for entity_type, entity_id in sorted(refs)
        for doc in repo.find(_DOCUMENTS, {"entityType": entity_type, "entityId": entity_id})
    ]
    return {
        str(doc["id"]): doc
        for doc in docs
        if doc.get("id") and avora_export.is_shareable_document(doc, refs, review)
    }


@router.get("/profile")
def employee_profile(
    email: EmailQuery, repo: DocumentRepository = Depends(get_repository)
) -> dict[str, Any]:
    return avora_export.profile_for(_employee_by_email(repo, email))


@router.get("/avatar")
def employee_avatar(
    email: EmailQuery,
    repo: DocumentRepository = Depends(get_repository),
    storage: FileStorage = Depends(get_storage),
) -> Response:
    """The profile photo's bytes - only when that document belongs to this
    person (their employee or candidate record)."""
    employee = _employee_by_email(repo, email)
    doc_id = avora_export.avatar_document_id(employee)
    doc = repo.get(_DOCUMENTS, doc_id) if doc_id else None
    owned = doc is not None and (doc.get("entityType"), doc.get("entityId")) in avora_export.owner_refs(employee)
    if doc is None or not owned or not doc.get("storageKey"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No photo.")
    data, stored_type = storage.get(doc["storageKey"])
    return Response(content=data, media_type=doc.get("contentType") or stored_type or "application/octet-stream")


@router.get("/documents")
def employee_documents(
    email: EmailQuery, repo: DocumentRepository = Depends(get_repository)
) -> dict[str, Any]:
    employee = _employee_by_email(repo, email)
    shareable = _shareable_documents(repo, employee)
    return {
        "employee_code": employee.get("id"),
        "documents": [avora_export.document_entry(d) for d in shareable.values()],
    }


@router.get("/documents/{doc_id}/content")
def employee_document_content(
    doc_id: str,
    email: EmailQuery,
    repo: DocumentRepository = Depends(get_repository),
    storage: FileStorage = Depends(get_storage),
) -> Response:
    """The file's bytes - only if it belongs to the employee Avora named, so a
    known document id cannot be used to reach anyone else's file."""
    doc = _shareable_documents(repo, _employee_by_email(repo, email)).get(doc_id)
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such document.")
    data, stored_type = storage.get(doc["storageKey"])
    return Response(
        content=data,
        media_type=doc.get("contentType") or stored_type or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{_safe_name(doc.get("fileName") or "file")}"'},
    )
