"""Placing a screening call through Sarvam's Instant Outbound API.

One POST places one call. Unlike the Vapi path this carries no prompt: the
agent already exists in Sarvam, holds its own script, and is handed only the
facts it needs for this candidate as `agent_variables`. There is nothing here
that builds an assistant, and no speech bridge - Sarvam runs the whole call.

    POST /api/outbounds/v1/orgs/{org}/workspaces/{ws}/outbounds   -> attempt_id

stdlib urllib, like `ongrid.py` and `email_sender.py`, to avoid adding an HTTP
dependency for one endpoint.

See https://docs.sarvam.ai/conversations/api/instant-outbound/create.md
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger
from app.services.voice_call_provider import CreatedCall, VoiceCallError

logger = get_logger("curcle.sarvam_calls")

#: Sarvam's own timeout is longer, but a call is placed from inside an HR
#: request - a provider that has not answered in this long has failed.
_TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class SarvamCallSettings:
    """Everything the outbound call needs that is not about the candidate."""

    api_key: str
    org_id: str
    workspace_id: str
    #: The agent, from Sarvam's agent list, and a committed version - never the
    #: draft, which can change under a call that is already running.
    app_id: str
    app_version: int
    #: Which telephony connection to dial from, and the number it shows.
    connection_id: str
    agent_phone_number: str
    #: Where Sarvam reports the result. Carries its own secret, because the
    #: payload is not signed - see `sarvam_webhook.py`.
    webhook_url: str



#: The agent asks at most this many; anything beyond is dropped rather than
#: silently renumbered into a question it has no output variable for.
MAX_QUESTIONS = 10

#: Outputs the agent declares as needing an initial value. Sent empty.
_REQUIRED_EMPTY = (
    "call_summary",
    "screening_status",
    *[f"q{n}_answer" for n in range(1, 6)],
    *[f"q{n}_followup" for n in range(1, 6)],
)


def _first_name(full_name: str) -> str:
    """The agent greets the candidate by this, so it is a first name."""
    return (full_name or "").strip().split(" ")[0] if full_name.strip() else "there"


def format_questions(questions: list[dict[str, Any]]) -> str:
    """The question list as the agent expects it: one numbered line each.

    A string, not a list - the agent reads it straight into its prompt. A
    choice question carries its options in a trailing tag the agent knows to
    speak naturally rather than read out as a tag.
    """
    lines: list[str] = []
    for index, question in enumerate(questions[:MAX_QUESTIONS], start=1):
        # Newlines would break the one-question-per-line contract the agent
        # relies on to number them.
        text = " ".join(str(question.get("text") or "").split())
        if not text:
            continue
        options = [str(o).strip() for o in (question.get("options") or []) if str(o).strip()]
        if options:
            text += " (options: " + " / ".join(options) + ")"
        lines.append(f"{len(lines) + 1}. {text}")
    return "\n".join(lines)


class SarvamCallProvider:
    """Implements `VoiceCallProvider` over Sarvam Instant Outbound."""

    def __init__(self, settings: SarvamCallSettings, *, base_url: str) -> None:
        self._s = settings
        self._base_url = base_url.rstrip("/")

    # -- the payload ---------------------------------------------------------

    def build_payload(
        self,
        *,
        screening_call_id: str,
        candidate_name: str,
        job_title: str,
        questions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """The agent's variables for this one candidate.

        Every name here is one the agent declares - read from its committed
        config, not inferred. Sarvam accepts a variable the agent never
        declared without complaint and it simply arrives as nothing, so a
        plausible-looking guess produces a call that asks the candidate
        nothing at all.

        `screeningCallId` travels in the webhook metadata rather than in a
        variable: metadata is echoed back verbatim, while a variable can be
        rewritten during the call, and the one thing that must survive intact
        is which record the result belongs to.
        """
        variables: dict[str, Any] = {
            "user_name": _first_name(candidate_name),
            "job_title": job_title.strip(),
            "questions": format_questions(questions),
        }
        # The agent marks its own outputs `needs_initial_value`, so they are
        # sent empty and come back filled.
        variables.update({name: "" for name in _REQUIRED_EMPTY})
        return {
            "app_config": {
                "app_type": "agent",
                "app_id": self._s.app_id,
                "app_version": self._s.app_version,
                "connection_config": {
                    "connection_id": self._s.connection_id,
                    "agent_phone_number": self._s.agent_phone_number,
                },
                "agent_variables": variables,
            },
            "webhook_config": {
                "url": self._s.webhook_url,
                "metadata": {"screeningCallId": screening_call_id},
            },
        }

    # -- placing the call ----------------------------------------------------

    def create_phone_call(self, payload: dict[str, Any], to_number: str) -> CreatedCall:
        body = {**payload, "user_config": {"user_phone_number": to_number}}
        answer = self._post("/outbounds/v1/orgs/%s/workspaces/%s/outbounds" % (
            self._s.org_id, self._s.workspace_id,
        ), body)
        attempt_id = str(answer.get("attempt_id") or "")
        if not attempt_id:
            raise VoiceCallError("Sarvam did not return an attempt id.")
        return CreatedCall(provider_call_id=attempt_id)

    def create_web_call(self, payload: dict[str, Any]) -> CreatedCall:
        # Instant Outbound dials a telephone. Sarvam has no browser-call
        # equivalent, so this says so rather than failing somewhere less
        # obvious - the caller turns it into a message HR can act on.
        raise VoiceCallError("Sarvam places phone calls only; there is no browser test call.")

    # -- transport -----------------------------------------------------------

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-API-Key": self._s.api_key,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            # Never the body at warning level in full - it echoes the phone
            # number back. The status and Sarvam's message are enough to act on.
            logger.warning("Sarvam outbound failed: HTTP %s", exc.code)
            raise VoiceCallError(_readable(detail, exc.code), status=exc.code) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logger.warning("Sarvam unreachable: %s", exc.__class__.__name__)
            raise VoiceCallError("Could not reach Sarvam.") from exc

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise VoiceCallError("Sarvam returned something that is not JSON.") from exc


def _readable(detail: str, status: int) -> str:
    """Sarvam's 422 names the field it did not like; surface that, not a code.

    Its shape is FastAPI's: {"detail": [{"loc": [...], "msg": "...", ...}]}.
    """
    try:
        parsed = json.loads(detail)
    except json.JSONDecodeError:
        return f"Sarvam refused the call (HTTP {status})."
    problems = parsed.get("detail")
    if isinstance(problems, list) and problems:
        first = problems[0]
        where = ".".join(str(p) for p in (first.get("loc") or [])[-2:])
        message = str(first.get("msg") or "").strip()
        if message:
            return f"Sarvam refused the call: {message}" + (f" ({where})" if where else "")
    if isinstance(problems, str) and problems.strip():
        return f"Sarvam refused the call: {problems.strip()}"
    return f"Sarvam refused the call (HTTP {status})."
