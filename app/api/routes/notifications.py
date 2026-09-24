"""Candidate notification endpoints.

The email is sent synchronously and the endpoint returns {sent: bool, reason?},
so the frontend can show the real "sent / failed" outcome. Sends never raise
(failures are logged and reported as sent:false), so a mail problem never breaks
the scheduling flow — it just surfaces as a failed-to-send result in the UI.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.dependencies import get_audit_service, get_repository, require_user
from app.core.config import Settings, get_settings
from app.repositories.base import DocumentRepository
from app.services.audit_service import AuditService
from app.services.email_sender import send_custom_email, send_schedule_email, send_test_email
from app.services.email_templates import resolve as resolve_template

# Sending mail is an HR action — require a session so this can't be used as an
# open relay to send arbitrary email from the org's address.
router = APIRouter(
    prefix="/api/notifications", tags=["notifications"], dependencies=[Depends(require_user)]
)


class ScheduleEmailIn(BaseModel):
    to: str
    candidateName: str
    type: str  # 'HR Call' | 'IQ Test' | 'Assessment' | 'Interview'
    dateTimeIso: str
    notes: str | None = None


@router.post("/schedule-email")
def schedule_email(
    payload: ScheduleEmailIn,
    settings: Settings = Depends(get_settings),
    user: dict[str, Any] = Depends(require_user),
    audit: AuditService = Depends(get_audit_service),
) -> dict[str, Any]:
    if not payload.to.strip():
        return {"sent": False, "reason": "no_recipient"}
    if not settings.has_smtp:
        return {"sent": False, "reason": "not_configured"}

    sent = send_schedule_email(
        settings=settings,
        to=payload.to.strip(),
        candidate_name=payload.candidateName,
        schedule_type=payload.type,
        date_time_iso=payload.dateTimeIso,
        notes=payload.notes,
    )
    if sent:
        audit.record(
            actor=user,
            action="email.sent",
            summary=f"Sent a {payload.type} email to {payload.candidateName}",
            entity_type="email",
            entity_label=payload.candidateName,
            metadata={"kind": payload.type, "to": payload.to.strip()},
        )
    return {"sent": sent} if sent else {"sent": False, "reason": "send_failed"}


class TestEmailIn(BaseModel):
    to: str
    candidateName: str
    # 'iq_invite' | 'iq_passed' | 'iq_failed' | 'assessment_passed' | 'assessment_failed'
    template: str
    testUrl: str | None = None
    position: str | None = None
    score: str | None = None
    durationMin: int | None = None
    dateTimeIso: str | None = None
    salary: str | None = None


@router.post("/test-email")
def test_email(
    payload: TestEmailIn,
    settings: Settings = Depends(get_settings),
    repo: DocumentRepository = Depends(get_repository),
    user: dict[str, Any] = Depends(require_user),
    audit: AuditService = Depends(get_audit_service),
) -> dict[str, Any]:
    if not payload.to.strip():
        return {"sent": False, "reason": "no_recipient"}
    if not settings.has_smtp:
        return {"sent": False, "reason": "not_configured"}

    def _log(sent: bool) -> None:
        if sent:
            audit.record(
                actor=user,
                action="email.sent",
                summary=f"Sent a {payload.template} email to {payload.candidateName}",
                entity_type="email",
                entity_label=payload.candidateName,
                metadata={"kind": payload.template, "to": payload.to.strip()},
            )

    # If HR has saved an override for this template, send that instead. The body
    # is plain text, so it goes out through the same branded renderer as an
    # HR-composed email (including [[label|url]] link buttons).
    override = resolve_template(
        repo,
        payload.template,
        {
            "candidate_name": payload.candidateName,
            "role": payload.position or "",
            "position": payload.position or "",
            "test_url": payload.testUrl or "",
            "link": payload.testUrl or "",
            "score": payload.score or "",
            "duration_min": payload.durationMin or "",
            "salary": payload.salary or "",
        },
    )
    if override:
        sent = send_custom_email(
            settings=settings,
            to=payload.to.strip(),
            subject=override["subject"],
            body=override["body"],
        )
        _log(sent)
        return {"sent": sent} if sent else {"sent": False, "reason": "send_failed"}

    sent = send_test_email(
        settings=settings,
        to=payload.to.strip(),
        candidate_name=payload.candidateName,
        template=payload.template,
        test_url=payload.testUrl,
        position=payload.position,
        score=payload.score,
        duration_min=payload.durationMin,
        date_time_iso=payload.dateTimeIso,
        salary=payload.salary,
    )
    _log(sent)
    return {"sent": sent} if sent else {"sent": False, "reason": "send_failed"}


class EmailLinkIn(BaseModel):
    label: str
    url: str


class CustomEmailIn(BaseModel):
    to: str
    subject: str
    body: str
    # Optional calendar invite (.ics, METHOD:REQUEST) attached when eventStartIso is set.
    eventStartIso: str | None = None
    eventDurationMin: int = 45
    eventSummary: str | None = None
    eventLocation: str | None = None
    eventDescription: str | None = None
    organizerEmail: str | None = None
    organizerName: str | None = None
    attendees: list[str] | None = None
    eventUid: str | None = None
    # Rendered as labelled buttons (e.g. resume / interview-questions links).
    links: list[EmailLinkIn] | None = None
    # Optional file attachment (e.g. the offer-letter PDF).
    attachmentName: str | None = None
    attachmentBase64: str | None = None
    attachmentType: str | None = None


@router.post("/custom-email")
def custom_email(
    payload: CustomEmailIn,
    settings: Settings = Depends(get_settings),
    user: dict[str, Any] = Depends(require_user),
    audit: AuditService = Depends(get_audit_service),
) -> dict[str, Any]:
    """Send an HR-composed email (e.g. an interview invitation the HR edited),
    optionally with a Google Calendar invite attached.

    Returns {sent, reason?} reflecting the real delivery result; never raises.
    """
    if not payload.to.strip():
        return {"sent": False, "reason": "no_recipient"}
    if not payload.subject.strip() or not payload.body.strip():
        return {"sent": False, "reason": "empty"}
    if not settings.has_smtp:
        return {"sent": False, "reason": "not_configured"}

    sent = send_custom_email(
        settings=settings,
        to=payload.to.strip(),
        subject=payload.subject,
        body=payload.body,
        event_start_iso=payload.eventStartIso,
        event_duration_min=payload.eventDurationMin,
        event_summary=payload.eventSummary,
        event_location=payload.eventLocation,
        event_description=payload.eventDescription,
        organizer_email=payload.organizerEmail,
        organizer_name=payload.organizerName,
        attendees=payload.attendees,
        event_uid=payload.eventUid,
        links=[l.model_dump() for l in payload.links] if payload.links else None,
        attachment=(
            {
                "filename": payload.attachmentName,
                "contentBase64": payload.attachmentBase64,
                "contentType": payload.attachmentType,
            }
            if payload.attachmentBase64
            else None
        ),
    )
    if sent:
        audit.record(
            actor=user,
            action="email.sent",
            summary=f"Sent an email '{payload.subject}' to {payload.to.strip()}",
            entity_type="email",
            entity_label=payload.to.strip(),
            metadata={"kind": "custom", "subject": payload.subject, "to": payload.to.strip()},
        )
    return {"sent": sent} if sent else {"sent": False, "reason": "send_failed"}
