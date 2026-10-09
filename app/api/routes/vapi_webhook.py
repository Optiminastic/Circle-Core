"""Vapi server messages for AI screening calls.

Vapi posts call events here (status updates and the end-of-call report).

Two ways in, and the better one wins when it is configured:

* **HMAC** (`vapi_webhook_signing_secret`) - the signature covers the body, so
  the payload cannot be altered, and the secret itself never crosses the wire.
  With a timestamp header the signature covers the age too, so a captured
  request stops working within the freshness window, and each signature is
  accepted once.
* **Shared secret** (`vapi_webhook_secret`, `X-Vapi-Secret`) - the fallback, and
  what is configured today. It proves only that the caller knows the secret,
  which it hands over in full on every request. Kept so the switch to HMAC can
  happen in Vapi's dashboard without downtime, not because it is sufficient.

What is at stake is why this matters: an accepted event writes a `fitRating`,
the answers, the transcript and the recording URL onto a real candidate.
"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Depends, Request, status
from fastapi.exceptions import HTTPException
from pydantic import BaseModel, ConfigDict

from app.api.dependencies import get_screening_call_service
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.services.screening_calls import ScreeningCallService
from app.services.webhook_auth import SeenSignatures, verify_signature

router = APIRouter(prefix="/api/vapi", tags=["vapi"])

logger = get_logger("curcle.vapi_webhook")

#: Signatures already acted on. Module-level so it outlives a request, like the
#: rate limiters in `core.rate_limit`, and with the same single-instance caveat.
_seen = SeenSignatures()


class VapiWebhookIn(BaseModel):
    """Vapi wraps every event in {"message": {...}}; the service validates the inside."""

    model_config = ConfigDict(extra="ignore")

    message: dict[str, Any]


def check_vapi_secret(provided: str | None, expected: str) -> None:
    """Raise unless Vapi presented the configured shared secret.

    Compared as bytes: compare_digest raises TypeError on non-ASCII str, which
    would turn a junk header into a 500 instead of a 401.
    """
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Vapi webhook is not configured.",
        )
    if not provided or not secrets.compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook secret.")


async def require_vapi_auth(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> None:
    """Authenticate the sender before the body is trusted for anything."""
    signing_secret = settings.vapi_webhook_signing_secret.strip()
    if not signing_secret:
        check_vapi_secret(request.headers.get("x-vapi-secret"), settings.vapi_webhook_secret)
        return

    signature = request.headers.get(settings.vapi_webhook_signature_header.lower())
    timestamp_header = settings.vapi_webhook_timestamp_header.strip()
    timestamp = request.headers.get(timestamp_header.lower()) if timestamp_header else None

    # The bytes as received. Re-serialising the parsed JSON would reorder keys
    # and drop whitespace, and the digest would never match.
    body = await request.body()
    check = verify_signature(
        body=body,
        signature=signature,
        timestamp=timestamp,
        secret=signing_secret,
        require_timestamp=bool(timestamp_header),
    )
    if not check:
        # The reason is logged, never returned: telling a caller which part of
        # their forgery failed is free help.
        logger.warning("Vapi webhook rejected: %s", check.reason)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature."
        )
    # Remembered only once the event has actually been handled, further down
    # in the route. Recording it here would turn our own 500 into lost data:
    # Vapi retries a failed delivery with the same body, and that retry would
    # come back as a replay.
    if _seen.was_used(signature or ""):
        logger.warning("Vapi webhook rejected: signature already used")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="This event has already been received."
        )
    request.state.vapi_signature = signature


@router.post("/webhook", dependencies=[Depends(require_vapi_auth)])
def vapi_webhook(
    request: Request,
    payload: VapiWebhookIn,
    service: ScreeningCallService = Depends(get_screening_call_service),
) -> dict[str, bool]:
    applied = service.apply_event(payload.message)
    # Now it has been acted on, a second copy is a replay rather than a retry.
    signature = getattr(request.state, "vapi_signature", None)
    if signature:
        _seen.remember(signature)
    # Only the event type and outcome: transcripts and numbers are candidate PII.
    logger.info("Vapi event %s applied=%s", payload.message.get("type"), applied)
    return {"ok": True}
