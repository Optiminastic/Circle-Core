"""Vapi server messages for AI screening calls.

Vapi posts call events here (status updates and the end-of-call report). Each
assistant is built with this endpoint's URL and an X-Vapi-Secret header, so a
request without the configured secret is rejected before anything is read.
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
    """Raise unless Vapi presented the configured shared secret."""
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Vapi webhook is not configured.",
        )
    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook secret.")


@router.post("/webhook")
def vapi_webhook(
    payload: VapiWebhookIn,
    x_vapi_secret: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
    service: ScreeningCallService = Depends(get_screening_call_service),
) -> dict[str, bool]:
    check_vapi_secret(x_vapi_secret, settings.vapi_webhook_secret)
    applied = service.apply_event(payload.message)
    # Only the event type and outcome: transcripts and numbers are candidate PII.
    logger.info("Vapi event %s applied=%s", payload.message.get("type"), applied)
    return {"ok": True}
