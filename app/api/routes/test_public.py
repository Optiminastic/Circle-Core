"""Public (no-login) candidate test/assessment endpoints.

The candidate reaches these with the unguessable invite token in the URL. The
server owns BOTH the question set and the grading:

  * `GET /{token}` returns the invite plus its questions **without the answer
    key**, so the correct answers never reach the browser. (The generic
    `GET /api/test-invites/{id}` used to be public and returned the raw
    document — answers included — which let a candidate read the key straight
    out of the Network tab.)
  * `POST /{token}/submit` accepts only the candidate's chosen options. The
    server grades against the stored key and computes correct/total/score/
    passed/disqualified itself; nothing score-related is trusted from the
    client (previously the browser POSTed its own `score`/`passed`).
  * WRITE-ONCE — a finished attempt can't be resubmitted or overwritten.
  * The IQ result row is created here, bound to the invite's candidate so it
    can't be injected against someone else.

Question source, in order: the snapshot HR picked when sending the test
(`assessmentQuestions` on the invite) → the shared IQ bank → the role's
assessment bank. All three live server-side.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, File, HTTPException, UploadFile

from app.api.dependencies import get_repository, get_resource_service, get_storage
from app.api.routes.documents import _safe_name
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.domain.registry import get_resource
from app.repositories.base import DocumentRepository
from app.services.resource_service import ResourceService
from app.storage.base import FileStorage

router = APIRouter(prefix="/api/public/test", tags=["public-test"])

logger = get_logger("curcle.test_public")

_INVITES = "test-invites"
_IQ = "iq-tests"
_IQ_BANK = "iq-bank"
_ASSESSMENT_BANKS = "assessment-banks"
_TERMINAL = {"Completed", "Auto-Submitted", "Graded"}
# Take-home has its own lifecycle — no score at submit time, graded manually
# by HR afterwards (see the candidate page's gradeAssignment mutation).
_TAKE_HOME_TERMINAL = {"Submitted", "Graded"}
_DOCS_TABLE = "documents"

# Scoring rules — must stay in step with the candidate-facing copy in
# circle-fe/data/test-banks.ts (marks/pass thresholds shown on the result card).
IQ_MARKS_PER_QUESTION = 4
IQ_PASS_SCORE = 100  # out of 200 (50 questions x 4 marks)
ASSESSMENT_PASS_PERCENT = 35
MAX_VIOLATIONS = 3  # tab/window switches before the attempt is void


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load(service: ResourceService, token: str) -> dict[str, Any]:
    # NotFoundError -> 404, same as a bad/expired link.
    return service.get(get_resource(_INVITES), token)


def _questions_for(service: ResourceService, invite: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve this invite's question set, each as {key, text, options, answer}.

    `answer` stays server-side — `_public_questions` strips it before the
    payload is returned to the candidate.
    """
    # Take-home is a file submission, not a question set — never fall through
    # to the role-name assessment-bank lookup below (a same-named bank would
    # otherwise silently attach unrelated MCQ questions to it).
    if invite.get("kind") == "take-home":
        return []

    # 1. The exact questions HR picked when sending the test (already snapshotted
    #    onto the invite), so a later bank edit can't change a live attempt.
    snapshot = invite.get("assessmentQuestions") or []
    if snapshot:
        return [
            {
                "key": f"asm-{index}",
                "text": str(question.get("text") or ""),
                "options": list(question.get("options") or []),
                "answer": question.get("answer"),
            }
            for index, question in enumerate(snapshot)
        ]

    # 2. IQ tests use the single shared bank.
    if invite.get("kind") == "iq":
        banks = service.list(get_resource(_IQ_BANK))
        questions = (banks[0].get("questions") if banks else []) or []
    else:
        # 3. Otherwise the assessment bank for this candidate's role.
        role = str(invite.get("position") or "").strip().lower()
        banks = service.list(get_resource(_ASSESSMENT_BANKS))
        match = next(
            (b for b in banks if str(b.get("roleName") or "").strip().lower() == role),
            None,
        )
        questions = (match or {}).get("questions") or []

    return [
        {
            "key": str(question.get("id") or f"q-{index}"),
            "text": str(question.get("q") or question.get("text") or ""),
            "options": list(question.get("options") or []),
            "answer": question.get("answer"),
        }
        for index, question in enumerate(questions)
    ]


def _public_questions(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The candidate-safe projection — key/text/options only, never `answer`."""
    return [{"key": q["key"], "text": q["text"], "options": q["options"]} for q in questions]


def _grade(
    invite: dict[str, Any],
    questions: list[dict[str, Any]],
    answers: dict[str, Any],
    violations: int,
) -> dict[str, Any]:
    """Score the attempt server-side. The client's only input is `answers`."""
    total = len(questions)
    correct = 0
    for question in questions:
        chosen = answers.get(question["key"])
        if isinstance(chosen, int) and chosen == question.get("answer"):
            correct += 1

    is_iq = invite.get("kind") == "iq"
    if is_iq:
        score = correct * IQ_MARKS_PER_QUESTION
        pass_mark = IQ_PASS_SCORE
    else:
        score = round((correct / total) * 100) if total else 0
        pass_mark = ASSESSMENT_PASS_PERCENT

    disqualified = violations >= MAX_VIOLATIONS
    return {
        "correct": correct,
        "total": total,
        "score": score,
        "passed": bool(not disqualified and score >= pass_mark),
        "disqualified": disqualified,
    }


@router.get("/{token}")
def get_test(token: str, service: ResourceService = Depends(get_resource_service)) -> dict[str, Any]:
    """Everything the test page needs — with the answer key withheld."""
    invite = _load(service, token)
    questions = _questions_for(service, invite)
    return {
        "id": invite.get("id"),
        "kind": invite.get("kind"),
        "status": invite.get("status"),
        "candidateName": invite.get("candidateName"),
        "position": invite.get("position"),
        "department": invite.get("department"),
        "durationMin": invite.get("durationMin"),
        "startedAt": invite.get("startedAt"),
        "completedAt": invite.get("completedAt"),
        "violations": int(invite.get("violations") or 0),
        "instructions": invite.get("instructions"),
        "deadlineIso": invite.get("deadlineIso"),
        # Take-home only — the brief file the candidate downloads, and
        # (once submitted) what they uploaded back.
        "briefDocId": invite.get("briefDocId"),
        "briefFileName": invite.get("briefFileName"),
        # Where to put work too large to upload here. Safe to echo: it is a
        # folder HR deliberately opened for this candidate.
        "driveUploadUrl": invite.get("driveUploadUrl"),
        "submissionDocId": invite.get("submissionDocId"),
        "submissionFileName": invite.get("submissionFileName"),
        "submissionUrl": invite.get("submissionUrl"),
        # Result fields are only meaningful once finished; safe to echo then.
        "score": invite.get("score") if invite.get("status") in _TERMINAL else None,
        "passed": invite.get("passed") if invite.get("status") in _TERMINAL else None,
        "disqualified": invite.get("disqualified") if invite.get("status") in _TERMINAL else None,
        "questions": _public_questions(questions),
        "passMark": IQ_PASS_SCORE if invite.get("kind") == "iq" else ASSESSMENT_PASS_PERCENT,
        "maxViolations": MAX_VIOLATIONS,
    }


@router.post("/{token}/start")
def start(token: str, service: ResourceService = Depends(get_resource_service)) -> dict[str, Any]:
    invite = _load(service, token)
    if invite.get("status") in _TERMINAL:
        raise HTTPException(status_code=409, detail="This test has already been submitted.")
    updated = service.patch(
        get_resource(_INVITES),
        token,
        {"status": "In Progress", "startedAt": invite.get("startedAt") or _now()},
    )
    return {"status": updated.get("status"), "startedAt": updated.get("startedAt")}


@router.post("/{token}/violation")
def violation(token: str, service: ResourceService = Depends(get_resource_service)) -> dict[str, Any]:
    invite = _load(service, token)
    if invite.get("status") in _TERMINAL:
        return {"violations": int(invite.get("violations") or 0)}
    count = int(invite.get("violations") or 0) + 1
    service.patch(get_resource(_INVITES), token, {"violations": count})
    return {"violations": count}


@router.post("/{token}/submit")
def submit(
    token: str,
    payload: dict[str, Any] = Body(...),
    service: ResourceService = Depends(get_resource_service),
) -> dict[str, Any]:
    invite = _load(service, token)
    if invite.get("status") in _TERMINAL:
        raise HTTPException(status_code=409, detail="This test has already been submitted.")

    raw_answers = payload.get("answers")
    answers: dict[str, Any] = raw_answers if isinstance(raw_answers, dict) else {}

    questions = _questions_for(service, invite)
    # Violations are counted server-side via /violation; the client can only ever
    # raise the count it reports, never lower the stored one.
    violations = max(int(invite.get("violations") or 0), int(payload.get("violations") or 0))
    result = _grade(invite, questions, answers, violations)

    changes = {
        **result,
        "answers": answers,
        "violations": violations,
        "status": "Completed",
        "completedAt": _now(),
    }
    service.patch(get_resource(_INVITES), token, changes)
    logger.info(
        "Test %s graded server-side: %s/%s (score %s, passed=%s)",
        token, result["correct"], result["total"], result["score"], result["passed"],
    )

    # The IQ result row is built entirely from server-computed values.
    if invite.get("kind") == "iq":
        service.create(
            get_resource(_IQ),
            {
                "candidateId": invite.get("candidateId"),
                "candidateName": invite.get("candidateName"),
                "testDate": changes["completedAt"],
                "totalQuestions": result["total"],
                "questionsAttempted": len([v for v in answers.values() if isinstance(v, int)]),
                "correctAnswers": result["correct"],
                "scorePercentage": result["score"],
                "qualificationStatus": "Passed" if result["passed"] else "Failed",
                "timeTakenMinutes": int(payload.get("timeTakenMinutes") or 0),
                "remarks": "Disqualified — rule violations." if result["disqualified"] else "",
            },
        )

    return {
        "ok": True,
        "status": changes["status"],
        "score": result["score"],
        "passed": result["passed"],
        "correct": result["correct"],
        "total": result["total"],
        "disqualified": result["disqualified"],
    }


def _take_home_open(invite: dict[str, Any]) -> None:
    """Raise unless this invite is a take-home still accepting a submission.

    Shared by both submission routes so a file and a link are governed by the
    same rules - otherwise closing one door would leave the other open.
    """
    if invite.get("kind") != "take-home":
        raise HTTPException(status_code=400, detail="This invite does not accept a submission.")
    if invite.get("status") in _TAKE_HOME_TERMINAL:
        raise HTTPException(status_code=409, detail="This assignment has already been submitted.")
    deadline = invite.get("deadlineIso")
    if not deadline:
        return
    try:
        expired = datetime.now(timezone.utc) > datetime.fromisoformat(str(deadline))
    except ValueError:
        return
    if expired:
        raise HTTPException(
            status_code=410, detail="The submission window for this assignment has closed."
        )


@router.post("/{token}/submit-link")
def submit_link(
    token: str,
    payload: dict[str, Any] = Body(...),
    service: ResourceService = Depends(get_resource_service),
) -> dict[str, Any]:
    """Take-home only: the candidate hands in a link instead of a file.

    A video answer runs to several hundred MB, which is not worth moving
    through the API and storing. They upload it to the Drive folder HR shared
    and give us the link; the work lives there, and the invite records where.

    Governed by the same window and write-once rules as the file route - the
    way to submit must not change what the deadline means.
    """
    invite = _load(service, token)
    _take_home_open(invite)

    url = str(payload.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="Please paste the link to your work.")
    if len(url) > 2000:
        raise HTTPException(status_code=400, detail="That link is too long.")
    # Only the two schemes a browser will open. Anything else - javascript:,
    # data: - is a link HR would click from their dashboard.
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(
            status_code=400, detail="Enter a full link starting with http:// or https://"
        )

    updated = service.patch(
        get_resource(_INVITES),
        token,
        {"submissionUrl": url, "status": "Submitted", "completedAt": _now()},
    )
    logger.info("Take-home assignment %s submitted as a link", token)
    return {"ok": True, "status": updated.get("status")}


@router.post("/{token}/submit-file")
async def submit_file(
    token: str,
    file: UploadFile = File(...),
    service: ResourceService = Depends(get_resource_service),
    repo: DocumentRepository = Depends(get_repository),
    storage: FileStorage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Take-home only: the candidate uploads their completed work.

    Write-once (like /submit) and additionally time-boxed — the link expires
    once `deadlineIso` passes, matching the email's "the submission link will
    expire" promise. Grading is manual (HR's gradeAssignment action); this
    endpoint only records the file and moves the invite to 'Submitted'.
    """
    invite = _load(service, token)
    _take_home_open(invite)

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Please attach a file.")
    limit = settings.max_upload_mb * 1024 * 1024
    if len(data) > limit:
        raise HTTPException(status_code=400, detail=f"Your file must be {settings.max_upload_mb} MB or smaller.")

    doc_id = uuid.uuid4().hex[:12]
    filename = file.filename or "assignment"
    key = f"documents/test-invite/{token}/{doc_id}_{_safe_name(filename)}"
    storage.put(key, data, file.content_type or "application/octet-stream")
    repo.upsert(
        _DOCS_TABLE,
        doc_id,
        {
            "id": doc_id,
            "entityType": "test-invite",
            "entityId": token,
            "category": "assignment-submission",
            "fileName": filename,
            "contentType": file.content_type,
            "size": len(data),
            "storageKey": key,
            "uploadedAt": _now(),
        },
    )

    updated = service.patch(
        get_resource(_INVITES),
        token,
        {
            "submissionDocId": doc_id,
            "submissionFileName": filename,
            "status": "Submitted",
            "completedAt": _now(),
        },
    )
    logger.info("Take-home assignment %s submitted: doc=%s", token, doc_id)
    return {"ok": True, "status": updated.get("status"), "fileName": filename}
