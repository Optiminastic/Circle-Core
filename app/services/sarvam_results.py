"""Turning a Sarvam call result into the shape the scorer already reads.

Sarvam hands back one flat variable per thing - `q1_answer`, `q1_followup`,
`q1_evidence` - while `screening_call_results.score_call` reads a nested entry
per question. This translates between the two and does nothing else: the
scoring, the re-validation of every answer against its question, and the fit
rule stay where they are, shared with the web form, so a call and a form can
never disagree about what counts as a pass.

Pure. No I/O, no provider objects.

Payload reference:
https://docs.sarvam.ai/conversations/api/instant-outbound/webhook-payload.md
"""

from __future__ import annotations

from typing import Any

from app.services.screening_call_assistant import answer_key
from app.services.screening_calls import COMPLETED, FAILED, NO_ANSWER

#: The agent declares outputs for ten questions and no more.
MAX_QUESTIONS = 10

#: Sarvam's own call outcome, from the agent's `call_outcome` enum. Only these
#: say something our status does not already carry.
DECLINED_OUTCOMES = frozenset({"declined", "wrong_person"})
NO_ANSWER_OUTCOMES = frozenset({"no_response"})

#: `status` on the webhook payload: connected | no_answer | busy | failed.
_STATUS = {
    "connected": COMPLETED,
    "no_answer": NO_ANSWER,
    "busy": NO_ANSWER,
    "failed": FAILED,
}


def call_status(payload: dict[str, Any]) -> str:
    """Our status for this result.

    `busy` is reported as no-answer rather than a failure: nobody picked up,
    which is the same thing to HR and the same next action - try again later.
    """
    return _STATUS.get(str(payload.get("status") or "").strip().lower(), FAILED)


def transcript_text(payload: dict[str, Any], *, limit: int = 50_000) -> str | None:
    """The conversation as one readable block.

    Sarvam sends turns of `{role, en_text}` - already English, whatever the
    candidate spoke - so this is a join rather than a fetch.
    """
    turns = payload.get("interaction_transcript")
    if not isinstance(turns, list):
        return None
    lines = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        text = str(turn.get("en_text") or "").strip()
        if not text:
            continue
        who = "Agent" if str(turn.get("role")) == "agent" else "Candidate"
        lines.append(f"{who}: {text}")
    joined = "\n".join(lines)
    return joined[:limit] or None


def to_structured_data(variables: Any, *, question_count: int) -> dict[str, Any]:
    """Sarvam's flat agent variables as the scorer's nested entries.

    Only as many questions as were actually asked: the agent carries ten sets
    of output variables regardless, and reading all ten would invent empty
    answers for questions this candidate was never asked.
    """
    if not isinstance(variables, dict):
        return {}

    structured: dict[str, Any] = {}
    for index in range(min(question_count, MAX_QUESTIONS)):
        number = index + 1
        structured[answer_key(index)] = {
            "answer": _clean(variables.get(f"q{number}_answer")),
            "evidence": _clean(variables.get(f"q{number}_evidence")),
            "followUpStrength": _clean(variables.get(f"q{number}_followup")),
        }

    # The agent records refusal in `call_outcome`; the scorer reads `declined`.
    # `wrong_person` counts too: whoever answered, it was not the candidate, so
    # there is nothing to score and nobody to score it against.
    outcome = _clean(variables.get("call_outcome")).lower()
    structured["declined"] = outcome in DECLINED_OUTCOMES
    return structured


def outcome_status(variables: Any, fallback: str) -> str:
    """Let the agent's own account of the call override the carrier's.

    A call can connect and still reach nobody worth screening - voicemail, the
    wrong person, someone who asked to be called back. The carrier only knows
    the line was answered.
    """
    if not isinstance(variables, dict):
        return fallback
    outcome = _clean(variables.get("call_outcome")).lower()
    if outcome in NO_ANSWER_OUTCOMES:
        return NO_ANSWER
    return fallback


def _clean(value: Any) -> str:
    return str(value).strip() if isinstance(value, (str, int, float)) else ""
