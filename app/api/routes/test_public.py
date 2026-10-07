"""Public (no-login) candidate test/assessment endpoints.

The candidate reaches these with the unguessable invite token in the URL. They
replace the old pattern where the public test page PATCHed the generic
`/api/test-invites/{id}` with arbitrary fields (letting a candidate overwrite a
finished result, flip fail→pass, or touch unrelated fields). Here the server:

  * enforces WRITE-ONCE — a finished attempt can't be resubmitted/overwritten,
  * accepts only the known result fields (a field allowlist),
  * stamps the completion time server-side,
  * creates the IQ result row itself (was an open `POST /api/iq-tests`), binding
    it to the invite's candidate so it can't be injected against someone else.

NOTE: the questions + answer key still live in the client bundle, so the *raw*
score is computed on the client for now. Making scoring fully tamper-proof means
moving the banks + scoring server-side (a separate change). These endpoints stop
the result being overwritten/replayed and block arbitrary-field tampering.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, File, HTTPException, UploadFile

from app.api.dependencies import get_repository, get_resource_service, get_storage
from app.core.config import Settings, get_settings
from app.core.errors import ValidationError
from app.domain.registry import get_resource
from app.repositories.base import DocumentRepository
from app.storage.base import FileStorage
from app.services.resource_service import ResourceService

router = APIRouter(prefix="/api/public/test", tags=["public-test"])

_INVITES = "test-invites"
_IQ = "iq-tests"
_DOCS = "documents"
# Statuses a candidate can no longer act on. "Submitted" is a take-home
# handed in and awaiting grading - terminal for them, even though HR has
# not finished with it, and it has to be here or a second upload replaces
# the first.
_TERMINAL = {"Completed", "Auto-Submitted", "Submitted", "Graded"}
# The only fields a candidate's submit may set on the invite.
_RESULT_FIELDS = (
    "status", "correct", "total", "score", "passed", "disqualified", "violations", "answers",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_name(name: str) -> str:
    """A storage-safe filename. Submissions arrive named anything at all."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return cleaned[:120] or "file"


def _seconds_left(started_at: str, duration_min: int) -> float:
    """How long the candidate still has. Negative once the window has closed.

    A bad timestamp yields 0 rather than an exception: the answer to "can they
    still submit" must never be a 500.
    """
    try:
        started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    ends = started + timedelta(minutes=max(0, duration_min))
    return (ends - datetime.now(timezone.utc)).total_seconds()


def _load(service: ResourceService, token: str) -> dict[str, Any]:
    # NotFoundError -> 404, same as a bad/expired link.
    return service.get(get_resource(_INVITES), token)


@router.post("/{token}/start")
def start(token: str, service: ResourceService = Depends(get_resource_service)) -> dict[str, Any]:
    invite = _load(service, token)
    if invite.get("status") in _TERMINAL:
        raise HTTPException(status_code=409, detail="This test has already been submitted.")
    return service.patch(
        get_resource(_INVITES),
        token,
        {"status": "In Progress", "startedAt": invite.get("startedAt") or _now()},
    )


@router.post("/{token}/violation")
def violation(token: str, service: ResourceService = Depends(get_resource_service)) -> dict[str, Any]:
    invite = _load(service, token)
    if invite.get("status") in _TERMINAL:
        return {"violations": int(invite.get("violations") or 0)}
    count = int(invite.get("violations") or 0) + 1
    service.patch(get_resource(_INVITES), token, {"violations": count})
    return {"violations": count}


@router.post("/{token}/assignment")
async def submit_assignment(
    token: str,
    file: UploadFile = File(...),
    service: ResourceService = Depends(get_resource_service),
    repo: DocumentRepository = Depends(get_repository),
    storage: FileStorage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """The candidate's finished take-home, uploaded against their invite.

    Separate from `/submit` because nothing here is scored: the work is a file,
    and HR grades it by hand afterwards.

    The time limit is enforced here rather than in the browser. The page shows a
    countdown, but a countdown is a label on a clock the candidate controls -
    this is the only place that decides whether an upload is still allowed.
    """
    invite = _load(service, token)
    if invite.get("kind") != "assignment":
        raise HTTPException(status_code=409, detail="This link is not a take-home assignment.")
    if invite.get("status") in _TERMINAL:
        raise HTTPException(status_code=409, detail="This assignment has already been submitted.")

    started = invite.get("startedAt")
    if not started:
        raise HTTPException(status_code=409, detail="Open the assignment before submitting it.")
    if _seconds_left(started, int(invite.get("durationMin") or 0)) <= 0:
        raise HTTPException(
            status_code=409,
            detail="The time limit for this assignment has passed.",
        )

    data = await file.read()
    if not data:
        raise ValidationError("Empty file.")
    # Its own, larger limit: a submission may be a screen recording, which the
    # 15 MB that suits an identity document would reject outright.
    limit = settings.max_assignment_upload_mb * 1024 * 1024
    if len(data) > limit:
        raise ValidationError(
            f"File exceeds the {settings.max_assignment_upload_mb} MB limit. "
            "For a large video, share a link in the notes instead."
        )

    candidate_id = str(invite.get("candidateId") or token)
    doc_id = uuid.uuid4().hex[:12]
    filename = file.filename or "submission"
    key = f"assignments/{candidate_id}/{doc_id}_{_safe_name(filename)}"
    storage.put(key, data, file.content_type or "application/octet-stream")

    now = _now()
    repo.upsert(
        _DOCS,
        doc_id,
        {
            "id": doc_id,
            "entityType": "candidate",
            "entityId": candidate_id,
            "category": "Assignment submission",
            "fileName": filename,
            "contentType": file.content_type,
            "size": len(data),
            "storageKey": key,
            "uploadedAt": now,
        },
    )
    service.patch(
        get_resource(_INVITES),
        token,
        {
            "submissionDocId": doc_id,
            "submissionFileName": filename,
            "status": "Submitted",
            "completedAt": now,
        },
    )
    return {"ok": True, "fileName": filename}


@router.post("/{token}/submit")
def submit(
    token: str,
    payload: dict[str, Any] = Body(...),
    service: ResourceService = Depends(get_resource_service),
) -> dict[str, Any]:
    invite = _load(service, token)
    if invite.get("status") in _TERMINAL:
        raise HTTPException(status_code=409, detail="This test has already been submitted.")

    changes = {key: payload[key] for key in _RESULT_FIELDS if key in payload}
    # Server owns the terminal status + timestamp.
    if changes.get("status") not in _TERMINAL:
        changes["status"] = "Completed"
    changes["completedAt"] = _now()
    service.patch(get_resource(_INVITES), token, changes)

    # Persist the IQ result row here (previously an open POST /api/iq-tests),
    # binding the candidate identity to the invite so it can't be spoofed.
    if invite.get("kind") == "iq" and isinstance(payload.get("iqRecord"), dict):
        record = {
            **payload["iqRecord"],
            "candidateId": invite.get("candidateId"),
            "candidateName": invite.get("candidateName"),
        }
        service.create(get_resource(_IQ), record)

    return {"ok": True, "status": changes["status"]}
