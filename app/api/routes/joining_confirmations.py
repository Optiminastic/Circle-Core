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

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends

from app.api.dependencies import get_repository
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.repositories.base import DocumentRepository

router = APIRouter(prefix="/api/joining-confirmations", tags=["joining-confirmations"])

logger = get_logger("curcle.joining_confirmations")

TABLE = "joining_confirmations"


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
_CANDIDATE_WRITABLE = {"canJoin", "suggestedDate", "meal", "plantChoice", "respondedAt"}


@router.patch("/{token}")
def update_joining_confirmation(
    token: str,
    changes: dict[str, Any] = Body(...),
    repo: DocumentRepository = Depends(get_repository),
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
    return record
