"""Public joining-date-confirmation portal endpoint.

A "joining confirmation" is an unguessable link HR sends a hired candidate so
they can confirm (or decline + suggest another) their joining date and pick a
meal + welcome-plant preference. The record itself is created/read through the
generic resources router (`/api/joining-confirmations`); this module adds the
one operation that needs field-level write restriction:

  * PATCH /api/joining-confirmations/{token} — public. The candidate may only
    ever write their own response fields; everything else (candidateId,
    proposedDate, expiresAt, email, …) is HR-owned.

Expiry is enforced here on the server so an old link can never accept a
response, regardless of what the client does.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Body, Depends, File, UploadFile

from app.api.dependencies import get_repository, get_storage
from app.core.config import Settings, get_settings
from app.api.routes.documents import _safe_name
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.repositories.base import DocumentRepository
from app.storage.base import FileStorage
from app.services.email_sender import send_custom_email

router = APIRouter(prefix="/api/joining-confirmations", tags=["joining-confirmations"])

logger = get_logger("curcle.joining_confirmations")

TABLE = "joining_confirmations"
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


# The ONLY fields the candidate may write on their own confirmation. Everything
# else (candidateId, candidateName, email, proposedDate, expiresAt, …) is
# HR-owned — without this allowlist a token holder could re-point their link
# at another candidate or extend their own expiry.
_CANDIDATE_WRITABLE = {
    "canJoin",
    "suggestedDate",
    "meal",
    "plantChoice",
    "introduction",
    "respondedAt",
}


def _hr_response_email(record: dict[str, Any]) -> tuple[str, str]:
    """Subject + body summarising what the candidate submitted, for HR."""
    name = record.get("candidateName") or "A candidate"
    lines = [f"{name} has filled in their welcome form."]

    proposed = record.get("proposedDate")
    if proposed:
        lines.append(f"Joining date on file: {proposed}")

    # Kept for records created while the page still asked this question.
    can_join = record.get("canJoin")
    if can_join is True:
        lines.append("Joining date: confirmed.")
    elif can_join is False:
        suggested = record.get("suggestedDate")
        lines.append(f"Joining date: asked for a different one{f' - {suggested}' if suggested else ''}.")

    meal = record.get("meal") or {}
    if meal:
        parts = [p for p in (meal.get("dish"), meal.get("preference")) if p]
        if parts:
            lines.append(f"First meal: {' - '.join(parts)}")
        if meal.get("notes"):
            lines.append(f"Dietary notes: {meal['notes']}")

    if record.get("plantChoice"):
        lines.append(f"Welcome plant: {record['plantChoice']}")

    if record.get("photoFileName"):
        lines.append(f"Photo uploaded: {record['photoFileName']} (on their onboarding page in Circle)")

    if record.get("introduction"):
        lines.append("")
        lines.append("In their words:")
        lines.append(record["introduction"])

    return f"Welcome form submitted - {name}", chr(10).join(lines)


def _notify_hr(settings: Settings, record: dict[str, Any]) -> None:
    """Email HR the candidate's answers. Never raises - this runs in a
    BackgroundTask, and a mail failure must not fail the candidate's submit."""
    recipients = [a.strip() for a in (settings.hr_cc_email or "").split(",") if a.strip()]
    if not recipients:
        logger.warning("No hr_cc_email configured - skipping welcome-form notification.")
        return
    subject, body = _hr_response_email(record)
    try:
        # Send to the first HR address; _add_hr_cc copies the rest.
        send_custom_email(settings, recipients[0], subject, body)
    except Exception:
        logger.exception("Could not email HR the welcome-form response.")

@router.patch("/{token}")
def update_joining_confirmation(
    token: str,
    background_tasks: BackgroundTasks,
    changes: dict[str, Any] = Body(...),
    repo: DocumentRepository = Depends(get_repository),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Candidate-facing save for their joining-date response + preferences."""
    record = repo.get(TABLE, token)
    if record is None:
        raise NotFoundError("This link is not valid.")
    if _is_expired(record):
        raise ValidationError("This link has expired. Please ask HR for a new one.")

    allowed = {key: value for key, value in changes.items() if key in _CANDIDATE_WRITABLE}
    if not allowed:
        raise ValidationError("No editable fields in this request.")

    record.update(allowed)
    record["updatedAt"] = datetime.now(timezone.utc).isoformat()
    repo.upsert(TABLE, token, record)
    logger.info("Joining confirmation %s updated by candidate.", token)

    # Only on the actual submit -- `respondedAt` is set once, when the candidate
    # sends the form. Backgrounded so a slow or failing SMTP never blocks them.
    if "respondedAt" in allowed:
        background_tasks.add_task(_notify_hr, settings, dict(record))

    return record


_PHOTO_MAX_BYTES = 5 * 1024 * 1024
_PHOTO_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}
_PHOTO_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif")


@router.post("/{token}/photo", status_code=201)
async def upload_joining_photo(
    token: str,
    file: UploadFile = File(...),
    repo: DocumentRepository = Depends(get_repository),
    storage: FileStorage = Depends(get_storage),
) -> dict[str, Any]:
    """Candidate's welcome photo, uploaded from the public confirmation page.

    Mirrors POST /api/doc-requests/{token}/upload: the unguessable token is the
    credential, expiry is enforced server-side so an old link can never accept
    files, and the blob is recorded in the documents table so HR can fetch a
    presigned URL. Only the photo id is written back onto the confirmation --
    the candidate never sets it through the PATCH allowlist.
    """
    record = repo.get(TABLE, token)
    if record is None:
        raise NotFoundError("This link is not valid.")
    if _is_expired(record):
        raise ValidationError("This link has expired. Please ask HR for a new one.")

    data = await file.read()
    if not data:
        raise ValidationError("That file appears to be empty.")
    if len(data) > _PHOTO_MAX_BYTES:
        raise ValidationError("Please keep your photo under 5 MB.")
    name = (file.filename or "photo").lower()
    if (file.content_type or "") not in _PHOTO_TYPES and not name.endswith(_PHOTO_EXTS):
        raise ValidationError("Please upload an image (JPG, PNG, WEBP or HEIC).")

    candidate_id = record.get("candidateId") or token
    doc_id = uuid.uuid4().hex[:12]
    filename = file.filename or "photo"
    key = f"documents/candidate/{candidate_id}/{doc_id}_{_safe_name(filename)}"
    storage.put(key, data, file.content_type or "application/octet-stream")

    now = datetime.now(timezone.utc).isoformat()
    repo.upsert(
        DOCS_TABLE,
        doc_id,
        {
            "id": doc_id,
            "entityType": "candidate",
            "entityId": candidate_id,
            "category": "Welcome Photo",
            "fileName": filename,
            "contentType": file.content_type,
            "size": len(data),
            "storageKey": key,
            "uploadedAt": now,
        },
    )

    record["photoDocumentId"] = doc_id
    record["photoFileName"] = filename
    record["updatedAt"] = now
    repo.upsert(TABLE, token, record)
    logger.info("Joining confirmation %s received a welcome photo.", token)
    return {"documentId": doc_id, "fileName": filename, "size": len(data)}
