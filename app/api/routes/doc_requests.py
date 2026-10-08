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

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from pydantic import BaseModel

from app.api.dependencies import get_repository, get_storage, require_user
from app.core.config import Settings, get_settings
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.repositories.base import DocumentRepository
from app.services import doc_extraction, doc_fields, ocr
from app.storage.base import FileStorage
from app.api.routes.documents import _safe_name

router = APIRouter(prefix="/api/doc-requests", tags=["doc-requests"])

logger = get_logger("curcle.doc_requests")

TABLE = "doc_requests"
DOCS_TABLE = "documents"
BGVS_TABLE = "bgvs"

REVIEW_STATUSES = ("Verified", "Rejected")


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


def has_consent(request: dict[str, Any]) -> bool:
    """Has the candidate given the background-verification consent?

    Both halves matter: `agreed` is the candidate's answer, and `text` is the
    wording they agreed to, which is sent verbatim as OnGrid's `consentText`.
    A tick with no recorded wording is not something we can stand behind.

    Shared with the OnGrid onboard (`bgv_ongrid.ongrid_onboard`) so the portal
    and the point of sharing cannot drift apart on what consent means.
    """
    consent = request.get("consent") or {}
    return bool(consent.get("agreed")) and bool(str(consent.get("text") or "").strip())


def needs_consent(request: dict[str, Any]) -> bool:
    """An employee request is for someone already hired and already verified, so
    there is no background verification to consent to. Everything else - including
    every request created before `entityType` existed - is a candidate's joining
    documents."""
    return (request.get("entityType") or "candidate") != "employee"


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

    # The portal tells the candidate their documents cannot be accepted without
    # consent, and that has to hold for the API too, not just the UI - these
    # documents exist to be shared with a verification partner who rejects a
    # verification carrying no consent.
    if needs_consent(request) and not has_consent(request):
        raise ValidationError(
            "Please tick the background-verification consent before uploading your documents."
        )

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
    # Defaults to "candidate" — every request created before this field existed
    # (and every existing candidate flow) has no entityType at all.
    entity_type = request.get("entityType") or "candidate"
    doc_id = uuid.uuid4().hex[:12]
    filename = file.filename or "file"
    key = f"documents/{entity_type}/{candidate_id}/{doc_id}_{_safe_name(filename)}"
    storage.put(key, data, file.content_type or "application/octet-stream")

    now = datetime.now(timezone.utc).isoformat()
    # Record in the documents table so HR can fetch a presigned download URL.
    repo.upsert(
        DOCS_TABLE,
        doc_id,
        {
            "id": doc_id,
            "entityType": entity_type,
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

    # Read the document now, while its bytes are already in memory, so the
    # portal can show the candidate what we read and let them confirm it before
    # HR ever sees it. Best-effort in every sense: unsupported type, OCR not
    # installed, engine busy or an unreadable image all just mean no pre-fill.
    # An upload must never fail because we couldn't parse it.
    extraction = _extract_on_upload(docType, data, file.content_type)
    if extraction:
        submission["extraction"] = extraction
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


def _extract_on_upload(doc_type: str, data: bytes, content_type: str | None) -> dict[str, Any] | None:
    """Extraction for a freshly uploaded document, or None if we can't do it.

    Returns None rather than raising for every failure mode, because this runs
    inside the candidate's upload request.
    """
    if not doc_fields.supports(doc_type) or not ocr.ocr_available():
        return None
    try:
        with ocr.slot() as reserved:
            if not reserved:
                logger.info("OCR busy - skipping pre-fill for '%s'.", doc_type)
                return None
            return doc_extraction.extract_bytes(data, content_type, doc_type)
    except Exception:  # noqa: BLE001 - a parse failure must never fail an upload
        logger.exception("Could not extract '%s' on upload.", doc_type)
        return None


class SubmissionConfirmation(BaseModel):
    """Values the candidate corrected, keyed the same as `extraction.fields`."""

    fields: dict[str, str] | None = None


@router.post("/{token}/submissions/{doc_type}/confirm")
def confirm_submission(
    token: str,
    doc_type: str,
    body: SubmissionConfirmation | None = None,
    repo: DocumentRepository = Depends(get_repository),
) -> dict[str, Any]:
    """The candidate confirms the values we read off their own document, and
    may correct the ones OCR could not confirm for itself.

    Public and token-gated, like the upload it follows. It can never set
    `status`: confirming is not verifying, HR still reviews every document, and
    a candidate must not be able to approve their own identity papers.

    Machine-validated fields (an Aadhaar number that satisfies its checksum, a
    well-formed PAN) are NOT editable here - the whole value of those checks is
    that the number came off the document rather than off a keyboard.
    """
    request = _load_request(repo, token)
    if _is_expired(request):
        raise ValidationError("This link has expired. Please ask HR for a new one.")

    submission = _find_submission(request, doc_type)
    if submission.get("status") == "Verified":
        raise ValidationError("This document has already been verified and locked.")

    extraction = submission.get("extraction")
    if not extraction:
        raise ValidationError("There is nothing to confirm for this document.")

    if body and body.fields is not None:
        extraction["fields"] = _apply_candidate_edits(extraction, body.fields)
        extraction["editedByCandidate"] = True

    extraction["candidateConfirmedAt"] = datetime.now(timezone.utc).isoformat()
    request["updatedAt"] = extraction["candidateConfirmedAt"]
    repo.upsert(TABLE, token, request)

    logger.info("Candidate confirmed '%s' for request %s.", doc_type, token)
    return submission


def _apply_candidate_edits(
    extraction: dict[str, Any], submitted: dict[str, str]
) -> dict[str, str]:
    """Merge the candidate's corrections over the extracted values, keeping any
    machine-validated field exactly as it was read."""
    protected = set(extraction.get("validatedFields") or ())
    merged = dict(extraction.get("fields") or {})
    for key, value in submitted.items():
        if key in protected:
            continue
        merged[key] = value.strip()
    return merged


# --- HR-only: OCR extraction + review ----------------------------------------
# Both require a session. They must never be added to a public allowlist: the
# request id doubles as the candidate's portal token, so anything public here
# would let the candidate approve their own identity documents.


def _find_submission(request: dict[str, Any], doc_type: str) -> dict[str, Any]:
    submission = next(
        (s for s in (request.get("submissions") or []) if s.get("docType") == doc_type),
        None,
    )
    if submission is None:
        raise NotFoundError(f"No '{doc_type}' has been uploaded for this request.")
    return submission


def _load_request(repo: DocumentRepository, request_id: str) -> dict[str, Any]:
    request = repo.get(TABLE, request_id)
    if request is None:
        raise NotFoundError("Document request not found.")
    return request


@router.post("/{request_id}/submissions/{doc_type}/extract")
def extract_submission_fields(
    http_request: Request,
    request_id: str,
    doc_type: str,
    repo: DocumentRepository = Depends(get_repository),
    _user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """Read the uploaded document and pre-fill its values for HR to check.

    Runs on demand rather than at upload time: OCR is CPU-heavy, there is no
    queue, and the upload endpoint is public - so this only costs anything for
    documents a human is actually looking at.
    """
    if not ocr.ocr_available():
        # Matches the `not_configured` convention used for OnGrid: a missing
        # optional integration is a 200 with a reason, not an error.
        return {"ok": False, "reason": "ocr_not_configured"}

    # Resolved here rather than as a route dependency: get_storage raises when
    # S3 is unconfigured, and FastAPI would resolve it before the auth check,
    # turning an unauthenticated 401 into a 502 that leaks our storage state.
    storage = get_storage(http_request)

    request = _load_request(repo, request_id)
    submission = _find_submission(request, doc_type)

    extraction = doc_extraction.extract_submission(repo, storage, submission, doc_type)
    submission["extraction"] = extraction
    request["updatedAt"] = datetime.now(timezone.utc).isoformat()
    repo.upsert(TABLE, request_id, request)

    return {"ok": True, "extraction": extraction}


class SubmissionReview(BaseModel):
    status: str
    reason: str | None = None
    fields: dict[str, str] | None = None


@router.post("/{request_id}/submissions/{doc_type}/review")
def review_submission(
    request_id: str,
    doc_type: str,
    body: SubmissionReview,
    repo: DocumentRepository = Depends(get_repository),
    _user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """HR's decision on one document, plus any corrections to its values.

    Correcting and approving are one action, so HR can never approve values they
    have edited but not saved.
    """
    if body.status not in REVIEW_STATUSES:
        raise ValidationError(f"Status must be one of {', '.join(REVIEW_STATUSES)}.")

    request = _load_request(repo, request_id)
    submission = _find_submission(request, doc_type)
    now = datetime.now(timezone.utc).isoformat()

    if body.fields is not None:
        extraction = submission.get("extraction") or doc_extraction.manual_block()
        extraction["fields"] = body.fields
        extraction["editedByHr"] = True
        submission["extraction"] = extraction

    submission["status"] = body.status
    submission["reviewedAt"] = now
    if body.status == "Rejected":
        submission["reviewReason"] = body.reason or ""
    else:
        submission.pop("reviewReason", None)

    request["status"] = _overall_status(request)
    request["updatedAt"] = now
    repo.upsert(TABLE, request_id, request)

    if body.status == "Verified":
        _mirror_to_bgv(repo, request, doc_type, submission)

    logger.info("Doc '%s' marked %s for request %s.", doc_type, body.status, request_id)
    return request


def _overall_status(request: dict[str, Any]) -> str:
    """Verified once every required file doc is verified; otherwise unchanged
    from the upload-time rule. Computed here rather than in the browser."""
    required = request.get("requiredDocs") or []
    submissions = request.get("submissions") or []
    by_type = {s.get("docType"): s for s in submissions}

    # Bank details and reference contacts are not file submissions; they are
    # reviewed separately and must not gate this.
    file_types = [t for t in required if t in by_type]
    if file_types and all(by_type[t].get("status") == "Verified" for t in file_types):
        return "Verified"
    if request.get("status") == "Verified":
        return "Verified"
    return request.get("status") or "Pending"


def _mirror_to_bgv(
    repo: DocumentRepository,
    request: dict[str, Any],
    doc_type: str,
    submission: dict[str, Any],
) -> None:
    """Copy approved values onto the candidate's BGV record, so HR has one
    consolidated view when working through OnGrid's portal.

    Best-effort: a BGV write must never fail the review itself.
    """
    candidate_id = request.get("candidateId")
    fields = (submission.get("extraction") or {}).get("fields")
    if not candidate_id or not fields:
        return
    try:
        bgv = repo.get(BGVS_TABLE, candidate_id) or {
            "id": candidate_id,
            "candidateId": candidate_id,
            "candidateName": request.get("candidateName"),
            "documents": [],
            "overallStatus": "Pending",
            "verificationTimeline": [],
        }
        extracted = bgv.get("extractedFields") or {}
        extracted[doc_type] = fields
        bgv["extractedFields"] = extracted
        repo.upsert(BGVS_TABLE, candidate_id, bgv)
    except Exception:  # noqa: BLE001 - never block HR's approval on this
        logger.exception("Could not mirror extracted fields onto the BGV record.")
