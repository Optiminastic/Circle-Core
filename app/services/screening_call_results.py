"""Turns a finished screening call into scored answers.

Pure. Vapi's structured data (one entry per question, keyed by
screening_call_assistant.answer_key) is untrusted model output, so every value
is checked against the question before scoring. Scoring itself is the same
build_answers + compute_fit the web form uses, so a call and a form can never
disagree on the rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services.screening import build_answers, compute_fit
from app.services.screening_call_assistant import (
    FOLLOW_UP_STRENGTHS,
    UNCLEAR,
    YES_NO_ANSWERS,
    answer_key,
    question_type,
)

_MAX_EVIDENCE_CHARS = 500
_MAX_TEXT_ANSWER_CHARS = 2000
_NO_FOLLOW_UP = "n/a"


@dataclass(frozen=True)
class CallScore:
    answers: list[dict[str, Any]]
    fit_rating: str | None
    needs_review: bool
    declined: bool


def score_call(
    questions: list[dict[str, Any]],
    structured_data: dict[str, Any] | None,
    form_answers: list[dict[str, Any]] | None,
) -> CallScore:
    data = structured_data if isinstance(structured_data, dict) else {}
    if data.get("declined") is True:
        return CallScore(answers=[], fit_rating=None, needs_review=False, declined=True)

    entries = [_entry(data, index) for index in range(len(questions))]
    responses = {
        str(q.get("id", "")): _validated_answer(q, entry.get("answer"))
        for q, entry in zip(questions, entries)
    }
    form_by_id = _form_answers_by_id(form_answers)
    answers = [
        _enrich(answer, entry, form_by_id)
        for answer, entry in zip(build_answers(questions, responses), entries)
    ]
    needs_review = any(a["answer"] == UNCLEAR for a in answers)
    return CallScore(
        answers=answers,
        fit_rating=compute_fit(answers),
        needs_review=needs_review,
        declined=False,
    )


def _entry(data: dict[str, Any], index: int) -> dict[str, Any]:
    entry = data.get(answer_key(index))
    return entry if isinstance(entry, dict) else {}


def _validated_answer(question: dict[str, Any], raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        return UNCLEAR
    answer = raw.strip()
    kind = question_type(question)
    if kind == "text":
        return answer[:_MAX_TEXT_ANSWER_CHARS]
    allowed = (question.get("options") or []) if kind == "choice" else YES_NO_ANSWERS
    return answer if answer in allowed else UNCLEAR


def _enrich(
    answer: dict[str, Any], entry: dict[str, Any], form_by_id: dict[str, str]
) -> dict[str, Any]:
    is_unclear = answer["answer"] == UNCLEAR
    # build_answers only knows Yes/No/options; an Unclear answer to a question
    # expecting "No" would otherwise count as a pass.
    passed = answer["passed"] and not (is_unclear and answer["type"] != "text")
    form_answer = form_by_id.get(answer["questionId"])
    return {
        **answer,
        "passed": passed,
        "evidence": _text(entry.get("evidence"), _MAX_EVIDENCE_CHARS),
        "followUpStrength": _follow_up_strength(entry.get("followUpStrength")),
        "formAnswer": form_answer,
        "contradictsForm": bool(
            form_answer is not None
            and not is_unclear
            and answer["type"] != "text"
            and form_answer != answer["answer"]
        ),
    }


def _form_answers_by_id(form_answers: list[dict[str, Any]] | None) -> dict[str, str]:
    return {
        str(a.get("questionId")): str(a.get("answer"))
        for a in form_answers or []
        if isinstance(a, dict) and a.get("questionId")
    }


def _follow_up_strength(raw: Any) -> str:
    return raw if raw in FOLLOW_UP_STRENGTHS else _NO_FOLLOW_UP


def _text(raw: Any, limit: int) -> str:
    return raw.strip()[:limit] if isinstance(raw, str) else ""
