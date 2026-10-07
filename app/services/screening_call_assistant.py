"""Builds the per-call Vapi assistant for an AI screening call.

Pure: no I/O, no settings lookup. The service passes in the job's screening
questions and the candidate; this returns the transient assistant JSON that
Vapi runs for exactly one call. Answers come back through Vapi's
analysisPlan.structuredDataPlan, keyed by answer_key(index), and are scored by
app/services/screening_call_results.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

COMPANY_NAME = "Optiminastic"
UNCLEAR = "Unclear"
YES_NO_ANSWERS = ("Yes", "No")
FOLLOW_UP_STRENGTHS = ("strong", "weak", "n/a")
# Hard stop so a confused call cannot run up cost.
MAX_CALL_SECONDS = 420
# Vapi's default of 15 s to join a browser test call is shorter than getting
# past the join screen (seen live), so the call ended before HR could speak.
CUSTOMER_JOIN_TIMEOUT_SECONDS = 60
# How long Vapi waits for one text-to-speech reply from the bridge.
VOICE_TIMEOUT_SECONDS = 20
_MAX_NAME_CHARS = 40
_MAX_TITLE_CHARS = 80
_MAX_OPTION_CHARS = 100
_SERVER_MESSAGES = ["status-update", "end-of-call-report"]


@dataclass(frozen=True)
class AssistantSettings:
    webhook_url: str
    webhook_secret: str
    bridge_url: str
    bridge_secret: str
    llm_provider: str
    llm_model: str


def answer_key(index: int) -> str:
    """Structured-data key for the question at `index` (stable, id-agnostic)."""
    return f"q{index + 1}"


def question_type(question: dict[str, Any]) -> str:
    """Question type with the legacy default (missing type means yes/no)."""
    return question.get("type") or "yesno"


def build_screening_assistant(
    *,
    screening_call_id: str,
    candidate_name: str,
    job_title: str,
    questions: list[dict[str, Any]],
    settings: AssistantSettings,
) -> dict[str, Any]:
    if not questions:
        raise ValueError("A screening call needs at least one question")
    first_name = _first_name(candidate_name)
    title = _single_line(job_title, _MAX_TITLE_CHARS) or "the role"
    bridge_auth = {"Authorization": f"Bearer {settings.bridge_secret}"}
    return {
        "name": "Circle screening call",
        "firstMessage": _first_message(first_name, title),
        "model": {
            "provider": settings.llm_provider,
            "model": settings.llm_model,
            "messages": [{"role": "system", "content": _system_prompt(first_name, title, questions)}],
            "tools": [{"type": "endCall"}],
        },
        "transcriber": {
            "provider": "custom-transcriber",
            "server": {"url": _websocket_url(settings.bridge_url) + "/transcriber", "headers": bridge_auth},
        },
        "voice": {
            "provider": "custom-voice",
            "server": {
                "url": settings.bridge_url.rstrip("/") + "/synthesize",
                "headers": bridge_auth,
                "timeoutSeconds": VOICE_TIMEOUT_SECONDS,
            },
        },
        "maxDurationSeconds": MAX_CALL_SECONDS,
        "customerJoinTimeoutSeconds": CUSTOMER_JOIN_TIMEOUT_SECONDS,
        "server": {"url": settings.webhook_url, "headers": {"X-Vapi-Secret": settings.webhook_secret}},
        "serverMessages": _SERVER_MESSAGES,
        "analysisPlan": {"structuredDataPlan": {"enabled": True, "schema": _answer_schema(questions)}},
        "metadata": {"screeningCallId": screening_call_id},
    }


def _first_message(first_name: str, title: str) -> str:
    greeting = f"Hi {first_name}" if first_name else "Hi"
    return (
        f"{greeting}, this is an automated AI call from {COMPANY_NAME} about your application "
        f"for {title}. This call is recorded and takes about three to five minutes. "
        "Is now a good time to answer a few quick questions?"
    )


def _system_prompt(first_name: str, title: str, questions: list[dict[str, Any]]) -> str:
    numbered = "\n".join(_describe_question(i, q) for i, q in enumerate(questions))
    who = first_name or "the candidate"
    return f"""You are a friendly screening assistant for {COMPANY_NAME}, phoning {who} about their job application for {title}.

Rules:
- If they say it is not a good time or they do not want an AI call, thank them and end the call.
- Ask the questions below one at a time, in this exact order. Do not skip or reword their meaning.
- Speak in the language the candidate uses (English, Hindi or Hinglish). Keep every turn short.
- After a "Yes" to a yes/no question, ask exactly one short follow-up asking for a concrete example (for example which account, which event, what they made). Then move on.
- For a choice question, read the options and let them pick one.
- If an answer is unclear, ask once to clarify, then move on.
- Never tell the candidate whether they passed, never discuss salary or offers, and never answer questions about the company beyond saying HR will follow up.
- After the last question, thank them, say HR will be in touch, and end the call.

Questions:
{numbered}"""


def _describe_question(index: int, question: dict[str, Any]) -> str:
    text = _single_line(str(question.get("text", "")), 300)
    kind = question_type(question)
    if kind == "choice":
        options = ", ".join(_option(o) for o in question.get("options") or [])
        return f"{index + 1}. {text} (choice: {options})"
    if kind == "text":
        return f"{index + 1}. {text} (open answer)"
    return f"{index + 1}. {text} (yes/no)"


def _answer_schema(questions: list[dict[str, Any]]) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "declined": {
            "type": "boolean",
            "description": "True if the candidate declined the call or hung up before the first question.",
        }
    }
    for index, question in enumerate(questions):
        properties[answer_key(index)] = _question_schema(question)
    return {"type": "object", "properties": properties}


def _question_schema(question: dict[str, Any]) -> dict[str, Any]:
    text = _single_line(str(question.get("text", "")), 300)
    kind = question_type(question)
    answer: dict[str, Any] = {"type": "string"}
    if kind == "choice":
        answer["enum"] = [_option(o) for o in question.get("options") or []] + [UNCLEAR]
        answer["description"] = "The option the candidate picked, copied exactly, or Unclear."
    elif kind == "text":
        answer["description"] = "A short summary of the candidate's answer in English."
    else:
        answer["enum"] = [*YES_NO_ANSWERS, UNCLEAR]
        answer["description"] = "Yes or No as the candidate answered (any language), or Unclear."
    return {
        "type": "object",
        "description": f"Question: {text}",
        "properties": {
            "answer": answer,
            "evidence": {"type": "string", "description": "The candidate's own words, quoted from the transcript."},
            "followUpStrength": {
                "type": "string",
                "enum": list(FOLLOW_UP_STRENGTHS),
                "description": "strong if the follow-up gave a concrete example, weak if vague or none, n/a if no follow-up.",
            },
        },
    }


def _first_name(full_name: str) -> str:
    """First word of the name, letters only, so it cannot carry instructions."""
    words = (full_name or "").split()
    first = words[0] if words else ""
    return re.sub(r"[^\w.'-]", "", first)[:_MAX_NAME_CHARS]


def _option(value: Any) -> str:
    return _single_line(str(value), _MAX_OPTION_CHARS)


def _single_line(value: str, limit: int) -> str:
    return " ".join(value.split())[:limit]


def _websocket_url(http_url: str) -> str:
    base = http_url.rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[len("https://") :]
    if base.startswith("http://"):
        return "ws://" + base[len("http://") :]
    return base
