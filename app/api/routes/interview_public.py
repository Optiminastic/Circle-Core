"""Public (no-login) interviewer sheet endpoints.

An external interviewer opens a link containing an unguessable sheet token. These
endpoints let them read the sheet and submit their feedback WITHOUT exposing the
generic `PATCH /api/interviews/{id}` (whose ids are guessable) to the world — the
target interview id is derived server-side from the token, so a caller can only
write feedback to the interview their token belongs to.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from app.api.dependencies import get_resource_service
from app.domain.registry import get_resource
from app.services.resource_service import ResourceService

router = APIRouter(prefix="/api/public", tags=["public-interview"])

_SHEETS = "interview-sheets"
_INTERVIEWS = "interviews"
_KIT_SENDS = "interview-kit-sends"


@router.get("/interview-sheet/{sheet_id}")
def get_interview_sheet(
    sheet_id: str, service: ResourceService = Depends(get_resource_service)
) -> dict[str, Any]:
    # 404 (via NotFoundError) if the token is wrong — same as a bad link.
    return service.get(get_resource(_SHEETS), sheet_id)


@router.post("/interview-sheet/{sheet_id}/feedback")
def submit_interview_feedback(
    sheet_id: str,
    payload: dict[str, Any] = Body(...),
    service: ResourceService = Depends(get_resource_service),
) -> dict[str, Any]:
    sheet = service.get(get_resource(_SHEETS), sheet_id)  # validates the token
    changes = {
        "questionResponses": payload.get("questionResponses"),
        "grading": payload.get("grading"),
        "status": "Completed",
    }
    # Only write fields the interviewer actually provided.
    changes = {key: value for key, value in changes.items() if value is not None}

    # The write-back target is derived server-side from the unguessable sheet
    # token — the interviewer never names it. A scheduled interview's sheet
    # points at an interview record; a kit sent manually from Settings points at
    # an `interview-kit-sends` record instead (no interview / pipeline change).
    interview_id = sheet.get("interviewId")
    if interview_id:
        return service.patch(get_resource(_INTERVIEWS), interview_id, changes)
    kit_send_id = sheet.get("kitSendId")
    if kit_send_id:
        return service.patch(get_resource(_KIT_SENDS), kit_send_id, changes)
    raise HTTPException(status_code=400, detail="This sheet is not linked to a record.")
