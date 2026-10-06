from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.services.screening_call_assistant import AssistantSettings
from app.services.screening_calls import (
    CallAlreadyActive,
    CallingNotConfigured,
    CandidateNotFound,
    CannotCall,
    ProviderRejected,
    ScreeningCallService,
)
from tests.fakes import FakeVoiceCallProvider, InMemoryDocumentRepository

HR = {"email": "hr@example.com", "name": "HR Person", "role": "hr"}
SETTINGS = AssistantSettings("https://api/hook", "s", "https://voice", "b", "openai", "gpt-4o-mini")
QUESTIONS = [
    {"id": "m1", "text": "Use Instagram?", "type": "yesno", "importance": "Must Have", "expectedAnswer": True},
    {"id": "g1", "text": "Internship?", "type": "yesno", "importance": "Good to Have", "expectedAnswer": True},
]
ALL_YES = {
    "q1": {"answer": "Yes", "evidence": "daily", "followUpStrength": "strong"},
    "q2": {"answer": "Yes", "evidence": "yes", "followUpStrength": "weak"},
}


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def repo() -> InMemoryDocumentRepository:
    r = InMemoryDocumentRepository()
    r.upsert("jobs", "job1", {"id": "job1", "title": "Social Media Intern", "screeningQuestions": QUESTIONS})
    r.upsert(
        "candidates",
        "c1",
        {
            "id": "c1",
            "fullName": "Priya Sharma",
            "jobId": "job1",
            "phone": "+91 9876543210",
            "screeningAnswers": [{"questionId": "m1", "answer": "Yes"}],
        },
    )
    return r


def service(repo: Any, provider: Any = None, phone_enabled: bool = True, clock: Any = None) -> ScreeningCallService:
    return ScreeningCallService(
        repo=repo,
        provider=provider if provider is not None else FakeVoiceCallProvider(),
        assistant_settings=SETTINGS,
        phone_enabled=phone_enabled,
        clock=clock or Clock(),
    )


def report(call: dict[str, Any], reason: str, structured: Any = None) -> dict[str, Any]:
    message: dict[str, Any] = {
        "type": "end-of-call-report",
        "endedReason": reason,
        "call": {"id": call["vapiCallId"]},
        "transcript": "AI: hi\nUser: haan",
        "recordingUrl": "https://storage.vapi.ai/rec.wav",
        "durationSeconds": 182.5,
        "cost": 0.42,
    }
    if structured is not None:
        message["analysis"] = {"structuredData": structured}
    return message


# -- start_call --------------------------------------------------------------


def test_phone_call_is_persisted_and_placed(repo: Any) -> None:
    provider = FakeVoiceCallProvider()
    call = service(repo, provider).start_call("c1", "phone", HR)
    assert provider.phone_calls[0][1] == "+919876543210"
    stored = repo.get("screening_calls", call["id"])
    assert stored["status"] == "queued"
    assert stored["vapiCallId"] == "vapi-1"
    assert stored["startedAt"] == "2026-10-06T10:00:00Z"
    assert [q["id"] for q in stored["questions"]] == ["m1", "g1"]
    assert stored["startedBy"] == {"email": "hr@example.com", "name": "HR Person"}


def test_web_call_returns_link_and_skips_phone_check(repo: Any) -> None:
    repo.upsert("candidates", "c1", {**repo.get("candidates", "c1"), "phone": "not a phone"})
    call = service(repo, phone_enabled=False).start_call("c1", "web", HR)
    assert call["webCallUrl"] == "https://vapi.daily.co/r"


def test_unknown_candidate_is_404(repo: Any) -> None:
    with pytest.raises(CandidateNotFound):
        service(repo).start_call("nope", "web", HR)


def test_job_without_questions_is_422(repo: Any) -> None:
    repo.upsert("jobs", "job1", {"id": "job1", "title": "X", "screeningQuestions": []})
    with pytest.raises(CannotCall):
        service(repo).start_call("c1", "web", HR)


def test_bad_phone_is_422_for_phone_calls(repo: Any) -> None:
    repo.upsert("candidates", "c1", {**repo.get("candidates", "c1"), "phone": "+1 415 555 0100"})
    with pytest.raises(CannotCall):
        service(repo).start_call("c1", "phone", HR)


def test_phone_mode_without_number_is_503(repo: Any) -> None:
    with pytest.raises(CallingNotConfigured):
        service(repo, phone_enabled=False).start_call("c1", "phone", HR)


def test_unconfigured_service_is_503(repo: Any) -> None:
    svc = ScreeningCallService(repo=repo, provider=None, assistant_settings=None, phone_enabled=False)
    with pytest.raises(CallingNotConfigured):
        svc.start_call("c1", "web", HR)


def test_second_call_while_active_is_409_but_stale_calls_do_not_block(repo: Any) -> None:
    clock = Clock()
    svc = service(repo, clock=clock)
    svc.start_call("c1", "web", HR)
    with pytest.raises(CallAlreadyActive):
        svc.start_call("c1", "web", HR)
    clock.now += timedelta(hours=1)
    svc.start_call("c1", "web", HR)  # the first call never reported back


def test_provider_failure_marks_call_failed_and_is_502(repo: Any) -> None:
    with pytest.raises(ProviderRejected):
        service(repo, FakeVoiceCallProvider(fail=True)).start_call("c1", "web", HR)
    [call] = repo.find("screening_calls", {"candidateId": "c1"})
    assert call["status"] == "failed"
    service(repo).start_call("c1", "web", HR)  # a failed call does not block a retry


def test_list_is_newest_first(repo: Any) -> None:
    clock = Clock()
    svc = service(repo, clock=clock)
    first = svc.start_call("c1", "web", HR)
    svc.apply_event(report(first, "customer-ended-call", {"declined": True}))
    clock.now += timedelta(minutes=5)
    second = svc.start_call("c1", "web", HR)
    assert [c["id"] for c in svc.list_for_candidate("c1")] == [second["id"], first["id"]]


# -- apply_event -------------------------------------------------------------


def test_report_scores_the_call(repo: Any) -> None:
    svc = service(repo)
    call = svc.start_call("c1", "web", HR)
    assert svc.apply_event(report(call, "assistant-ended-call", ALL_YES)) is True
    stored = repo.get("screening_calls", call["id"])
    assert stored["status"] == "completed"
    assert stored["fitRating"] == "Fit"
    assert stored["needsReview"] is False
    assert stored["answers"][0]["formAnswer"] == "Yes"
    assert stored["recordingUrl"] == "https://storage.vapi.ai/rec.wav"
    assert stored["durationSeconds"] == 182.5
    assert stored["costUsd"] == 0.42


def test_scoring_uses_the_questions_as_asked(repo: Any) -> None:
    svc = service(repo)
    call = svc.start_call("c1", "web", HR)
    repo.upsert("jobs", "job1", {"id": "job1", "title": "X", "screeningQuestions": []})  # edited mid-call
    svc.apply_event(report(call, "assistant-ended-call", ALL_YES))
    assert len(repo.get("screening_calls", call["id"])["answers"]) == 2


def test_duplicate_report_is_ignored(repo: Any) -> None:
    svc = service(repo)
    call = svc.start_call("c1", "web", HR)
    svc.apply_event(report(call, "assistant-ended-call", ALL_YES))
    all_no = {"q1": {"answer": "No"}, "q2": {"answer": "No"}}
    assert svc.apply_event(report(call, "assistant-ended-call", all_no)) is False
    assert repo.get("screening_calls", call["id"])["fitRating"] == "Fit"


def test_no_answer_has_no_score(repo: Any) -> None:
    svc = service(repo)
    call = svc.start_call("c1", "phone", HR)
    svc.apply_event(report(call, "customer-did-not-answer"))
    stored = repo.get("screening_calls", call["id"])
    assert stored["status"] == "no-answer"
    assert stored["fitRating"] is None


def test_pipeline_error_without_answers_is_failed(repo: Any) -> None:
    svc = service(repo)
    call = svc.start_call("c1", "web", HR)
    svc.apply_event(report(call, "pipeline-error-custom-transcriber-failed"))
    assert repo.get("screening_calls", call["id"])["status"] == "failed"


def test_declined_call(repo: Any) -> None:
    svc = service(repo)
    call = svc.start_call("c1", "web", HR)
    svc.apply_event(report(call, "customer-ended-call", {"declined": True}))
    stored = repo.get("screening_calls", call["id"])
    assert stored["status"] == "declined"
    assert stored["fitRating"] is None


def test_status_updates_move_forward_and_stop_after_completion(repo: Any) -> None:
    svc = service(repo)
    call = svc.start_call("c1", "web", HR)
    status = {"type": "status-update", "status": "in-progress", "call": {"id": call["vapiCallId"]}}
    assert svc.apply_event(status) is True
    assert repo.get("screening_calls", call["id"])["status"] == "in-progress"
    svc.apply_event(report(call, "assistant-ended-call", ALL_YES))
    assert svc.apply_event({**status, "status": "ringing"}) is False
    assert repo.get("screening_calls", call["id"])["status"] == "completed"


def test_event_found_by_metadata_before_call_id_is_stored(repo: Any) -> None:
    svc = service(repo)
    call = svc.start_call("c1", "web", HR)
    repo.upsert("screening_calls", call["id"], {**repo.get("screening_calls", call["id"]), "vapiCallId": None})
    event = {
        "type": "status-update",
        "status": "ringing",
        "call": {"id": "web-1", "assistant": {"metadata": {"screeningCallId": call["id"]}}},
    }
    assert svc.apply_event(event) is True


def test_metadata_cannot_hijack_a_call_bound_to_another_provider_id(repo: Any) -> None:
    svc = service(repo)
    call = svc.start_call("c1", "web", HR)
    event = {
        "type": "end-of-call-report",
        "endedReason": "assistant-ended-call",
        "call": {"id": "someone-else", "metadata": {"screeningCallId": call["id"]}},
        "analysis": {"structuredData": ALL_YES},
    }
    assert svc.apply_event(event) is False


def test_unknown_calls_and_types_are_ignored(repo: Any) -> None:
    svc = service(repo)
    assert svc.apply_event({"type": "end-of-call-report", "call": {"id": "nope"}}) is False
    assert svc.apply_event({"type": "end-of-call-report"}) is False
    call = svc.start_call("c1", "web", HR)
    assert svc.apply_event({"type": "transcript", "call": {"id": call["vapiCallId"]}}) is False
