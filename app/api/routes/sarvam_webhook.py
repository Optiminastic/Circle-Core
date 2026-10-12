"""Sarvam's call result for an AI screening call.

Sarvam reports once, when the call ends. There is no progress event, so this
is the entire lifecycle after dialling.

**It is not signed.** The payload reference documents no signature, no
timestamp and no signing secret, so the HMAC used for the other provider has
nothing to verify. Two things stand in for it:

* the secret is the URL - `/api/sarvam/webhook/{token}` - the same shape as
  the candidate feed, and a wrong or unset token answers 404 rather than 401
  so the endpoint's existence is never confirmed to a prober;
* the result is applied only to a call we placed, matched on the `attempt_id`
  Sarvam gave us when we placed it. A caller who guesses the URL still cannot
  write a verdict onto a candidate without also holding a live attempt id.

https://docs.sarvam.ai/conversations/api/instant-outbound/webhook-payload.md
"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from app.api.dependencies import get_screening_call_service
from app.core.config import Settings, get_settings
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.services.screening_calls import ScreeningCallService

router = APIRouter(prefix="/api/sarvam", tags=["sarvam"])

logger = get_logger("curcle.sarvam_webhook")


class SarvamWebhookIn(BaseModel):
    """Only `attempt_id` is relied on here; the service reads the rest.

    `extra="allow"` on purpose - Sarvam adds fields over time and a stricter
    model would start rejecting real results for carrying something new.
    """

    model_config = ConfigDict(extra="allow")

    attempt_id: str = ""


@router.post("/webhook/{token}")
def sarvam_webhook(
    token: str,
    payload: SarvamWebhookIn,
    settings: Settings = Depends(get_settings),
    service: ScreeningCallService = Depends(get_screening_call_service),
) -> dict[str, bool]:
    expected = settings.sarvam_webhook_token.strip()
    # 404, not 401, and the same for an unset token as for a wrong one: an
    # unconfigured or mis-addressed webhook should look like nothing is here.
    if not expected or not secrets.compare_digest(token, expected):
        raise NotFoundError("Not found")

    applied = service.apply_sarvam_event(payload.model_dump())
    # The attempt id is Sarvam's, not a candidate's - safe to log, and the one
    # thing worth having when a result does not show up. Never the transcript.
    logger.info("Sarvam result %s applied=%s", payload.attempt_id or "?", applied)
    return {"ok": True}
