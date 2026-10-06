"""AI screening calls: start a call for a candidate and record what Vapi reports.

Business rules only - no FastAPI. Depends on the DocumentRepository and
VoiceCallProvider ports, so tests run on in-memory fakes. Each call is one
document in the internal `screening_calls` table (not exposed through the
generic /api/{resource} router).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from app.repositories.base import DocumentRepository
from app.services.audit_service import AuditService
from app.services.phone import InvalidPhoneError, normalize_indian_mobile
from app.services.screening_call_assistant import (
    MAX_CALL_SECONDS,
    AssistantSettings,
    build_screening_assistant,
)
from app.services.screening_call_results import score_call
from app.services.voice_call_provider import CreatedCall, VoiceCallError, VoiceCallProvider

TABLE = "screening_calls"
CallMode = Literal["phone", "web"]

QUEUED = "queued"
RINGING = "ringing"
IN_PROGRESS = "in-progress"
COMPLETED = "completed"
FAILED = "failed"
NO_ANSWER = "no-answer"
DECLINED = "declined"
ACTIVE_STATUSES = frozenset({QUEUED, RINGING, IN_PROGRESS})
TERMINAL_STATUSES = frozenset({COMPLETED, FAILED, NO_ANSWER, DECLINED})
# Vapi status-update values we mirror; "ended" waits for the end-of-call report.
_PROVIDER_STATUSES = {"queued": QUEUED, "ringing": RINGING, "in-progress": IN_PROGRESS}
_NO_ANSWER_REASONS = ("did-not-answer", "busy", "voicemail")
_FAILURE_MARKERS = ("error", "failed")
# A call whose webhook never arrived must not block new calls forever.
_STALE_AFTER = timedelta(seconds=MAX_CALL_SECONDS * 2)
_MAX_TRANSCRIPT_CHARS = 50_000
_MAX_REASON_CHARS = 120
_QUESTION_FIELDS = ("id", "text", "type", "importance", "category", "expectedAnswer", "options", "expectedOption")


class ScreeningCallError(Exception):
    """Base for errors the route maps to an HTTP status."""

    status_code = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class CandidateNotFound(ScreeningCallError):
    status_code = 404


class CallAlreadyActive(ScreeningCallError):
    status_code = 409


class CannotCall(ScreeningCallError):
    status_code = 422


class CallingNotConfigured(ScreeningCallError):
    status_code = 503


class ProviderRejected(ScreeningCallError):
    status_code = 502


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


class ScreeningCallService:
    def __init__(
        self,
        *,
        repo: DocumentRepository,
        provider: VoiceCallProvider | None,
        assistant_settings: AssistantSettings | None,
        phone_enabled: bool,
        audit: AuditService | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._repo = repo
        self._provider = provider
        self._assistant_settings = assistant_settings
        self._phone_enabled = phone_enabled
        self._audit = audit
        self._clock = clock

    # -- Start -------------------------------------------------------------

    def start_call(self, candidate_id: str, mode: CallMode, actor: dict[str, Any]) -> dict[str, Any]:
        provider, settings = self._require_configured(mode)
        candidate = self._repo.get("candidates", candidate_id)
        if candidate is None:
            raise CandidateNotFound("Candidate not found")
        job = self._repo.get("jobs", str(candidate.get("jobId") or ""))
        if job is None:
            raise CannotCall("This candidate has no job to screen against")
        questions = [_snapshot(q) for q in job.get("screeningQuestions") or [] if isinstance(q, dict)]
        if not questions:
            raise CannotCall("This job has no screening questions")
        to_number = self._phone_for(candidate) if mode == "phone" else None
        self._ensure_no_active_call(candidate_id)

        call = self._new_call(candidate, job, questions, mode, actor)
        self._repo.upsert(TABLE, call["id"], call)
        assistant = build_screening_assistant(
            screening_call_id=call["id"],
            candidate_name=str(candidate.get("fullName") or ""),
            job_title=str(job.get("title") or ""),
            questions=questions,
            settings=settings,
        )
        created = self._place_call(call, provider, assistant, to_number)
        call.update(vapiCallId=created.provider_call_id, webCallUrl=created.web_call_url)
        self._repo.upsert(TABLE, call["id"], call)
        self._record("screening_call.started", f"Started AI screening call ({mode})", call, actor)
        return call

    def list_for_candidate(self, candidate_id: str) -> list[dict[str, Any]]:
        calls = self._repo.find(TABLE, {"candidateId": candidate_id})
        return sorted(calls, key=lambda c: c.get("startedAt") or "", reverse=True)

    def _require_configured(self, mode: CallMode) -> tuple[VoiceCallProvider, AssistantSettings]:
        if self._provider is None or self._assistant_settings is None:
            raise CallingNotConfigured("AI screening calls are not configured")
        if mode == "phone" and not self._phone_enabled:
            raise CallingNotConfigured("Phone calls are not set up yet - use the browser test call")
        return self._provider, self._assistant_settings

    def _phone_for(self, candidate: dict[str, Any]) -> str:
        try:
            return normalize_indian_mobile(candidate.get("phone"))  # type: ignore[arg-type]
        except InvalidPhoneError as exc:
            raise CannotCall("The candidate's phone number is not a valid Indian mobile") from exc

    def _ensure_no_active_call(self, candidate_id: str) -> None:
        stale_before = _iso(self._clock() - _STALE_AFTER)
        for call in self._repo.find(TABLE, {"candidateId": candidate_id}):
            if call.get("status") in ACTIVE_STATUSES and (call.get("startedAt") or "") > stale_before:
                raise CallAlreadyActive("A screening call is already in progress for this candidate")

    def _new_call(
        self,
        candidate: dict[str, Any],
        job: dict[str, Any],
        questions: list[dict[str, Any]],
        mode: CallMode,
        actor: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "id": f"SC-{uuid.uuid4().hex[:12]}",
            "candidateId": candidate["id"],
            "jobId": job.get("id"),
            "mode": mode,
            "status": QUEUED,
            "vapiCallId": None,
            "webCallUrl": None,
            "questions": questions,
            "startedBy": {"email": actor.get("email"), "name": actor.get("name")},
            "startedAt": _iso(self._clock()),
            "endedAt": None,
            "endedReason": None,
            "durationSeconds": None,
            "transcript": None,
            "recordingUrl": None,
            "answers": [],
            "fitRating": None,
            "needsReview": False,
            "costUsd": None,
        }

    def _place_call(
        self,
        call: dict[str, Any],
        provider: VoiceCallProvider,
        assistant: dict[str, Any],
        to_number: str | None,
    ) -> CreatedCall:
        try:
            if to_number is None:
                return provider.create_web_call(assistant)
            return provider.create_phone_call(assistant, to_number)
        except VoiceCallError as exc:
            call.update(status=FAILED, endedAt=_iso(self._clock()), endedReason="provider-rejected")
            self._repo.upsert(TABLE, call["id"], call)
            raise ProviderRejected("The calling service rejected the call. Try again shortly.") from exc

    # -- Webhook events ----------------------------------------------------

    def apply_event(self, message: dict[str, Any]) -> bool:
        """Apply one Vapi server message. Returns False when it was ignored."""
        kind = message.get("type")
        call = self._find_call(message.get("call"))
        if call is None or call.get("status") in TERMINAL_STATUSES:
            return False  # unknown call, or a replay after completion
        if kind == "status-update":
            return self._apply_status(call, message)
        if kind == "end-of-call-report":
            self._apply_report(call, message)
            return True
        return False

    def _find_call(self, provider_call: Any) -> dict[str, Any] | None:
        if not isinstance(provider_call, dict):
            return None
        provider_id = provider_call.get("id")
        if isinstance(provider_id, str) and provider_id:
            matches = self._repo.find(TABLE, {"vapiCallId": provider_id})
            if matches:
                return matches[0]
        # A status update can beat our own write of vapiCallId; fall back to
        # the id we put in the assistant metadata, but never re-bind a call.
        our_id = _metadata_call_id(provider_call)
        call = self._repo.get(TABLE, our_id) if our_id else None
        if call is None or call.get("vapiCallId") not in (None, provider_id):
            return None
        return call

    def _apply_status(self, call: dict[str, Any], message: dict[str, Any]) -> bool:
        status = _PROVIDER_STATUSES.get(str(message.get("status")))
        if status is None:
            return False
        call["status"] = status
        self._repo.upsert(TABLE, call["id"], call)
        return True

    def _apply_report(self, call: dict[str, Any], message: dict[str, Any]) -> None:
        reason = str(message.get("endedReason") or "")[:_MAX_REASON_CHARS]
        analysis = message.get("analysis") if isinstance(message.get("analysis"), dict) else {}
        structured = analysis.get("structuredData")
        call.update(
            endedAt=_iso(self._clock()),
            endedReason=reason or None,
            durationSeconds=_number(message.get("durationSeconds")),
            costUsd=_number(message.get("cost")),
            transcript=_text(message.get("transcript"), _MAX_TRANSCRIPT_CHARS),
            recordingUrl=_recording_url(message),
        )
        if any(marker in reason for marker in _NO_ANSWER_REASONS):
            call["status"] = NO_ANSWER
        elif not isinstance(structured, dict) and any(m in reason for m in _FAILURE_MARKERS):
            call["status"] = FAILED
        else:
            self._score(call, structured)
        self._repo.upsert(TABLE, call["id"], call)
        self._record(
            "screening_call.completed",
            f"AI screening call ended: {call['status']}",
            call,
            actor=None,
            extra={"fitRating": call.get("fitRating")},
        )

    def _score(self, call: dict[str, Any], structured: Any) -> None:
        candidate = self._repo.get("candidates", call["candidateId"]) or {}
        score = score_call(call["questions"], structured, candidate.get("screeningAnswers"))
        call.update(
            status=DECLINED if score.declined else COMPLETED,
            answers=score.answers,
            fitRating=score.fit_rating,
            needsReview=score.needs_review,
        )

    def _record(
        self,
        action: str,
        summary: str,
        call: dict[str, Any],
        actor: dict[str, Any] | None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if self._audit is None:
            return
        self._audit.record(
            actor=actor,
            action=action,
            summary=summary,
            entity_type="candidate",
            entity_id=call["candidateId"],
            metadata={"screeningCallId": call["id"], "mode": call["mode"], "status": call["status"], **(extra or {})},
        )


def _snapshot(question: dict[str, Any]) -> dict[str, Any]:
    """Copy of the question as asked, so later job edits cannot change old results."""
    return {k: question[k] for k in _QUESTION_FIELDS if k in question}


def _metadata_call_id(provider_call: dict[str, Any]) -> str | None:
    for holder in (provider_call, provider_call.get("assistant")):
        metadata = holder.get("metadata") if isinstance(holder, dict) else None
        if isinstance(metadata, dict) and isinstance(metadata.get("screeningCallId"), str):
            return metadata["screeningCallId"]
    return None


def _recording_url(message: dict[str, Any]) -> str | None:
    artifact = message.get("artifact") if isinstance(message.get("artifact"), dict) else {}
    for value in (message.get("recordingUrl"), artifact.get("recordingUrl")):
        if isinstance(value, str) and value.startswith("https://"):
            return value
    return None


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _text(value: Any, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) else None
