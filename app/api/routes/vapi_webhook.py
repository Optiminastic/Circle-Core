"""Vapi server messages for AI screening calls.

Vapi posts call events here (status updates and the end-of-call report). Each
assistant is built with this endpoint's URL and an X-Vapi-Secret header. The
secret is checked by a route dependency, which FastAPI runs before validating
the body or opening a database session for the service.
Replays are harmless: once a call is finished, later events for it are ignored
(ScreeningCallService.apply_event), and the only thing this route can change is
that call's own screening_calls row.
"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict

from app.api.dependencies import get_screening_call_service
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.services.screening_calls import ScreeningCallService

router = APIRouter(prefix="/api/vapi", tags=["vapi"])

logger = get_logger("curcle.vapi_webhook")


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


def require_vapi_secret(
    x_vapi_secret: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    check_vapi_secret(x_vapi_secret, settings.vapi_webhook_secret)


@router.post("/webhook", dependencies=[Depends(require_vapi_secret)])
def vapi_webhook(
    payload: VapiWebhookIn,
    service: ScreeningCallService = Depends(get_screening_call_service),
) -> dict[str, bool]:
    applied = service.apply_event(payload.message)
    # Only the event type and outcome: transcripts and numbers are candidate PII.
    logger.info("Vapi event %s applied=%s", payload.message.get("type"), applied)
    return {"ok": True}
