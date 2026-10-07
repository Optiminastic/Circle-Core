"""Server-side screening scoring for public job applications.

A Python port of the frontend `lib/screening.ts`. The public apply endpoint must
NOT trust a client-supplied fit rating (an applicant could mark themselves "Fit"),
so it recomputes the answers and rating here from the job's own questions.
"""

from __future__ import annotations

from typing import Any

# Good-to-have pass rate at/above which a candidate is rated Fit.
FIT_THRESHOLD = 0.6


def build_answers(
    questions: list[dict[str, Any]], responses: dict[str, str]
) -> list[dict[str, Any]]:
    """Score raw responses (keyed by question id) against the job's questions."""
    answers: list[dict[str, Any]] = []
    for q in questions:
        qid = str(q.get("id", ""))
        qtype = q.get("type") or "yesno"
        answer = str(responses.get(qid, ""))
        if qtype == "yesno":
            passed = (answer == "Yes") == bool(q.get("expectedAnswer"))
        elif qtype == "choice":
            expected = q.get("expectedOption")
            passed = (answer == expected) if expected else True
        else:  # text — informational, never auto-fails
            passed = True
        answers.append(
            {
                "questionId": qid,
                "text": q.get("text", ""),
                "category": q.get("category"),
                "importance": q.get("importance"),
                "type": qtype,
                "answer": answer,
                "passed": passed,
            }
        )
    return answers


def compute_fit(answers: list[dict[str, Any]]) -> str:
    """Fit / Borderline / Unfit from scored answers (mirrors the frontend rule)."""
    if not answers:
        return "Fit"
    must_haves = [a for a in answers if a.get("importance") == "Must Have"]
    if any(not a["passed"] for a in must_haves):
        return "Unfit"
    good_to_haves = [a for a in answers if a.get("importance") == "Good to Have"]
    if not good_to_haves:
        return "Fit"
    rate = sum(1 for a in good_to_haves if a["passed"]) / len(good_to_haves)
    return "Fit" if rate >= FIT_THRESHOLD else "Borderline"


def build_extra_answers(
    questions: list[dict[str, Any]], responses: dict[str, str]
) -> list[dict[str, Any]]:
    """Snapshot raw extra-question responses against the job's own questions.

    Unlike build_answers/compute_fit, these are purely informational — no
    pass/fail scoring, and they never feed Fit/Borderline/Unfit.
    """
    answers: list[dict[str, Any]] = []
    for q in questions:
        qid = str(q.get("id", ""))
        answers.append(
            {
                "questionId": qid,
                "text": q.get("text", ""),
                "type": q.get("type", "text"),
                "answer": str(responses.get(qid, "")),
            }
        )
    return answers


def missing_required_extra(questions: list[dict[str, Any]], responses: dict[str, str]) -> bool:
    """True if any question marked `required` has no non-blank answer."""
    for q in questions:
        if not q.get("required"):
            continue
        qid = str(q.get("id", ""))
        if not str(responses.get(qid, "")).strip():
            return True
    return False
