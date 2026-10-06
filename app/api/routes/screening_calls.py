"""HR endpoints for AI screening calls.

POST starts a call (phone, or a browser test call), GET lists a candidate's
calls with their scored answers. Thin: parse, call ScreeningCallService, map
its typed errors to HTTP statuses.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import get_screening_call_service, require_user
from app.services.screening_calls import ScreeningCallError, ScreeningCallService

router = APIRouter(
    prefix="/api/screening-calls",
    tags=["screening-calls"],
    dependencies=[Depends(require_user)],
)

_MAX_ID_CHARS = 128


class StartScreeningCallIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    candidateId: str = Field(min_length=1, max_length=_MAX_ID_CHARS)
    mode: Literal["phone", "web"]


class ScreeningCallAnswerOut(BaseModel):
    questionId: str
    text: str
    importance: str | None = None
    type: str
    answer: str
    passed: bool
    evidence: str = ""
    followUpStrength: str = "n/a"
    formAnswer: str | None = None
    contradictsForm: bool = False


class ScreeningCallOut(BaseModel):
    """What HR sees. Deliberately omits provider ids and the question snapshot."""

    id: str
    candidateId: str
    mode: str
    status: str
    webCallUrl: str | None = None
    startedBy: dict[str, Any] | None = None
    startedAt: str
    endedAt: str | None = None
    endedReason: str | None = None
    durationSeconds: float | None = None
    transcript: str | None = None
    recordingUrl: str | None = None
    answers: list[ScreeningCallAnswerOut] = []
    fitRating: str | None = None
    needsReview: bool = False
    costUsd: float | None = None


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ScreeningCallOut)
def start_screening_call(
    payload: StartScreeningCallIn,
    user: dict[str, Any] = Depends(require_user),
    service: ScreeningCallService = Depends(get_screening_call_service),
) -> dict[str, Any]:
    try:
        return service.start_call(payload.candidateId, payload.mode, user)
    except ScreeningCallError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.get("", response_model=list[ScreeningCallOut])
def list_screening_calls(
    candidate_id: str = Query(alias="candidateId", min_length=1, max_length=_MAX_ID_CHARS),
    service: ScreeningCallService = Depends(get_screening_call_service),
) -> list[dict[str, Any]]:
    return service.list_for_candidate(candidate_id)
