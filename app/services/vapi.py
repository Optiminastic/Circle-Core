"""Vapi adapter for VoiceCallProvider.

Stdlib urllib with a short timeout, like app/services/ongrid.py. Every call uses
a transient assistant (built by screening_call_assistant), so nothing has to be
configured in the Vapi dashboard per job. Vapi accepts browser calls
(/call/web) only with the PUBLIC key and phone calls (/call) only with the
private key - verified against the live API.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from app.services.voice_call_provider import CreatedCall, VoiceCallError

_TIMEOUT_SECONDS = 15
_MAX_ERROR_DETAIL = 300


class VapiClient:
    def __init__(self, *, api_key: str, public_key: str, phone_number_id: str, base_url: str) -> None:
        self._api_key = api_key
        self._public_key = public_key
        self._phone_number_id = phone_number_id
        self._base = base_url.rstrip("/")

    def create_phone_call(self, assistant: dict[str, Any], to_number: str) -> CreatedCall:
        if not self._phone_number_id:
            raise VoiceCallError("No phone number is configured for outbound calls")
        body = {
            "assistant": assistant,
            "phoneNumberId": self._phone_number_id,
            "customer": {"number": to_number},
        }
        data = self._post("/call", body, key=self._api_key)
        return CreatedCall(provider_call_id=_call_id(data))

    def create_web_call(self, assistant: dict[str, Any]) -> CreatedCall:
        data = self._post("/call/web", {"assistant": assistant}, key=self._public_key)
        url = data.get("webCallUrl")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise VoiceCallError("Vapi did not return a web call link")
        return CreatedCall(provider_call_id=_call_id(data), web_call_url=url)

    def _post(self, path: str, body: dict[str, Any], *, key: str) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self._base}{path}",
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:  # noqa: S310 (fixed host)
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            # Vapi error bodies describe the request, never echo our key.
            detail = exc.read().decode("utf-8", "replace")[:_MAX_ERROR_DETAIL]
            raise VoiceCallError(f"Vapi {exc.code}: {detail}", status=exc.code) from exc
        except urllib.error.URLError as exc:
            raise VoiceCallError(f"Could not reach Vapi: {exc.reason}") from exc
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise VoiceCallError("Vapi returned a non-JSON response") from exc
        if not isinstance(parsed, dict):
            raise VoiceCallError("Vapi returned an unexpected response")
        return parsed


def _call_id(data: dict[str, Any]) -> str:
    call_id = data.get("id")
    if not isinstance(call_id, str) or not call_id:
        raise VoiceCallError("Vapi response had no call id")
    return call_id
