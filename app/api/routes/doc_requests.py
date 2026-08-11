"""Public onboarding document-upload portal endpoints.

A "doc request" is an unguessable, 24-hour link HR sends to a hired candidate so
they can upload their joining documents (Aadhaar, PAN, bank details, etc.). The
request record itself is created/read/verified through the generic resources
router (`/api/doc-requests`); this module adds the two operations that need
special handling:

  * POST /api/doc-requests/{token}/upload — public, multipart. Validates the
    token + expiry, stores the blob in S3, records it in the `documents` table
    (so HR can pull a presigned URL later) and stamps the submission onto the
    request.

Expiry is enforced here on the server so an old link can never accept files,
regardless of what the client does.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, File, Form, UploadFile

from app.api.dependencies import get_repository, get_storage
from app.core.config import Settings, get_settings
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.repositories.base import DocumentRepository
from app.storage.base import FileStorage
from app.api.routes.documents import _safe_name

router = APIRouter(prefix="/api/doc-requests", tags=["doc-requests"])

logger = get_logger("curcle.doc_requests")

TABLE = "doc_requests"
DOCS_TABLE = "documents"


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _is_expired(request: dict[str, Any]) -> bool:
    expires = _parse_iso(request.get("expiresAt"))
    if expires is None:
        return False  # no expiry set — treat as open
    return datetime.now(timezone.utc) > expires


@router.post("/{token}/upload", status_code=201)
async def upload_request_document(
    token: str,
    docType: str = Form(...),
    file: UploadFile = File(...),
    repo: DocumentRepository = Depends(get_repository),
    storage: FileStorage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    request = repo.get(TABLE, token)
    if request is None:
        raise NotFoundError("This upload link is invalid.")
    if _is_expired(request):
        raise ValidationError("This upload link has expired. Please ask HR for a new one.")

    required = request.get("requiredDocs") or []
    if required and docType not in required:
        raise ValidationError(f"'{docType}' is not a requested document for this link.")

    # A verified document is locked — HR has approved it, so it can never be
    # overwritten (even via a fresh link that carried the approval forward).
    existing = next(
        (s for s in (request.get("submissions") or []) if s.get("docType") == docType),
        None,
    )
    if existing and existing.get("status") == "Verified":
        raise ValidationError(
            "This document has already been verified and locked. It can no longer be replaced."
        )

    data = await file.read()
    if not data:
        raise ValidationError("Empty file.")
    limit = settings.max_upload_mb * 1024 * 1024
    if len(data) > limit:
        raise ValidationError(f"File exceeds the {settings.max_upload_mb} MB limit.")

    # Signed letters (offer / appointment) are stricter: PDF/Word only, under 5 MB.
    if docType in ("Signed Offer Letter", "Signed Appointment Letter"):
        name = (file.filename or "").lower()
        allowed_types = {
            "application/pdf",
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }
        if (file.content_type or "") not in allowed_types and not name.endswith((".pdf", ".doc", ".docx")):
            raise ValidationError("Please upload a PDF or Word document (.pdf, .doc, .docx).")
        if len(data) > 5 * 1024 * 1024:
            raise ValidationError("File must be under 5 MB.")

    candidate_id = request.get("candidateId") or token
    doc_id = uuid.uuid4().hex[:12]
    filename = file.filename or "file"
    key = f"documents/candidate/{candidate_id}/{doc_id}_{_safe_name(filename)}"
    storage.put(key, data, file.content_type or "application/octet-stream")

    now = datetime.now(timezone.utc).isoformat()
    # Record in the documents table so HR can fetch a presigned download URL.
    repo.upsert(
        DOCS_TABLE,
        doc_id,
        {
            "id": doc_id,
            "entityType": "candidate",
            "entityId": candidate_id,
            "category": docType,
            "fileName": filename,
            "contentType": file.content_type,
            "size": len(data),
            "storageKey": key,
            "uploadedAt": now,
        },
    )

    submission = {
        "docType": docType,
        "documentId": doc_id,
        "fileName": filename,
        "size": len(data),
        "uploadedAt": now,
        "status": "Submitted",
    }
    # Replace any prior submission for the same docType (re-uploads overwrite).
    submissions = [s for s in (request.get("submissions") or []) if s.get("docType") != docType]
    submissions.append(submission)
    request["submissions"] = submissions

    # Overall request status: Submitted once every required doc is present.
    uploaded_types = {s["docType"] for s in submissions}
    bank = request.get("bankDetails") or {}
    bank_complete = bool(bank.get("accountNumber")) and bool(bank.get("ifscCode"))
    if required and uploaded_types.issuperset(set(required)) and bank_complete:
        if request.get("status") not in ("Verified",):
            request["status"] = "Submitted"
    request["updatedAt"] = now
    repo.upsert(TABLE, token, request)

    logger.info("Doc '%s' uploaded for request %s.", docType, token)
    return submission
