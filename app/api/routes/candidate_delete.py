"""Cascade delete for a candidate.

Deleting a candidate must also remove every record that hangs off them —
interviews, schedules, test invites/results, doc requests, BGV, onboarding, the
handoff-feed entry, and uploaded documents (metadata + S3 blob). Otherwise those
orphaned rows keep surfacing (e.g. a deleted candidate still showing under the
dashboard's "Upcoming Interviews", which is derived from the interviews table).

This dedicated DELETE is registered before the generic resources router so it
wins for `/api/candidates/{id}`; all other candidate methods still fall through
to the generic router.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from app.api.dependencies import (
    get_audit_service,
    get_google_calendar_service,
    get_repository,
    get_storage,
    require_user,
)
from app.api.routes.calendar import cleanup_calendar_events
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.repositories.base import DocumentRepository
from app.services.audit_service import AuditService
from app.services.google_calendar import GoogleCalendarService
from app.storage.base import FileStorage

# Deleting a candidate is destructive + cascades — require a dashboard session.
router = APIRouter(prefix="/api/candidates", tags=["candidates"], dependencies=[Depends(require_user)])

logger = get_logger("curcle.candidate_delete")

# Tables whose rows carry a `candidateId` field — found by that field, then
# deleted by their own primary key (`id`).
_CHILD_BY_FIELD = (
    "schedules",
    "test_invites",
    "doc_requests",
    "interviews",
    "iq_tests",
    "assignments",
)
# Tables keyed directly by candidateId — deleted by the candidate id itself.
_CHILD_BY_KEY = ("bgvs", "onboarding", "candidate_handoffs")


@router.delete("/{candidate_id}", status_code=204, response_class=Response)
def delete_candidate(
    candidate_id: str,
    repo: DocumentRepository = Depends(get_repository),
    storage: FileStorage = Depends(get_storage),
    calendar: GoogleCalendarService = Depends(get_google_calendar_service),
    settings: Settings = Depends(get_settings),
    user: dict = Depends(require_user),
    audit: AuditService = Depends(get_audit_service),
) -> Response:
    removed = 0
    # Capture the candidate's name before the cascade removes the record, so the
    # audit entry reads "deleted candidate <name>" rather than a bare id.
    existing = repo.get("candidates", candidate_id) or {}
    candidate_label = existing.get("fullName") or existing.get("name") or candidate_id
    # Google Calendar app-event ids to remove (interview = the event itself + the
    # interviewer's +1h event; schedules = the round's event). Collected here as
    # rows are found, then cleaned up so nothing lingers on the HR / interviewer /
    # candidate calendars.
    calendar_event_ids: list[str] = []
    # Pipeline records that reference the candidate by a candidateId field.
    for table in _CHILD_BY_FIELD:
        for rec in repo.find(table, {"candidateId": candidate_id}):
            rid = rec.get("id")
            if not rid:
                continue
            if table == "interviews":
                calendar_event_ids.append(rid)
                calendar_event_ids.append(f"{rid}-interviewer")
            elif table == "schedules":
                calendar_event_ids.append(rid)
            if repo.delete(table, rid):
                removed += 1
    # Remove the linked Google Calendar events (best-effort — never blocks delete).
    try:
        cleanup_calendar_events(repo, calendar, settings, calendar_event_ids)
    except Exception:  # noqa: BLE001
        logger.warning("Calendar cleanup failed during delete of candidate %s", candidate_id)
    # Records keyed directly by candidateId (no-op when absent).
    for table in _CHILD_BY_KEY:
        if repo.delete(table, candidate_id):
            removed += 1
    # Uploaded documents — remove the S3 blob (best-effort) then the metadata row.
    for doc in repo.find("documents", {"entityType": "candidate", "entityId": candidate_id}):
        key = doc.get("storageKey")
        if key:
            try:
                storage.delete(key)
            except Exception:  # noqa: BLE001 - a missing blob must not block the delete
                logger.warning("Could not delete blob for document %s", doc.get("id"))
        if doc.get("id"):
            repo.delete("documents", doc["id"])
            removed += 1
    # Finally the candidate itself.
    repo.delete("candidates", candidate_id)
    logger.info("Cascade-deleted candidate %s (+%d related records).", candidate_id, removed)
    audit.record(
        actor=user,
        action="candidate.deleted",
        summary=f"Deleted candidate {candidate_label}",
        entity_type="candidate",
        entity_id=candidate_id,
        entity_label=candidate_label,
        metadata={"related_removed": removed},
    )
    return Response(status_code=204)
