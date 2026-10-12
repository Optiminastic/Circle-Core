"""Sarvam's call result, from the webhook through to a scored verdict.

The webhook is unsigned, so two things carry the weight: the secret is the
URL, and a result is applied only to a call we placed. Both are tested here,
because between them they are the only thing stopping a stranger writing a fit
rating onto a real candidate.

No database and no network: an in-memory repo, and the result is posted by
hand the way Sarvam would.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import current_user, get_screening_call_service
from app.core.config import get_settings
from app.services.screening_calls import ScreeningCallService

TOKEN = "a-long-unguessable-token"
CANDIDATE_ID = "cand-1"
ATTEMPT = "att-xyz"

QUESTIONS = [
    {"id": "m1", "text": "Do you have 2+ years experience?", "type": "yesno",
     "importance": "Must Have", "expectedAnswer": True},
    {"id": "g1", "text": "Which tools do you use?", "type": "choice",
     "importance": "Good to Have", "options": ["Canva", "Figma"], "expectedOption": "Figma"},
]


class _Repo:
    """Just enough DocumentRepository for the service."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, dict[str, Any]]] = {
            "screening_calls": {}, "candidates": {}, "jobs": {},
        }

    def get(self, table: str, key: str) -> dict[str, Any] | None:
        return self.rows.setdefault(table, {}).get(key)

    def find(self, table: str, where: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            row for row in self.rows.setdefault(table, {}).values()
            if all(row.get(k) == v for k, v in where.items())
        ]

    def upsert(self, table: str, key: str, data: dict[str, Any]) -> dict[str, Any]:
        self.rows.setdefault(table, {})[key] = data
        return data


def _call_row(status: str = "queued") -> dict[str, Any]:
    return {
        "id": "SC-1", "candidateId": CANDIDATE_ID, "mode": "phone", "status": status,
        "providerCallId": ATTEMPT, "vapiCallId": ATTEMPT, "questions": QUESTIONS,
        "startedAt": "2026-10-12T09:00:00Z", "answers": [], "needsReview": False,
    }


@pytest.fixture
def repo() -> _Repo:
    r = _Repo()
    r.rows["screening_calls"]["SC-1"] = _call_row()
    # What they said on the application form, so a contradiction can be seen.
    r.rows["candidates"][CANDIDATE_ID] = {
        "id": CANDIDATE_ID,
        "screeningAnswers": [{"questionId": "m1", "answer": "No", "passed": False}],
    }
    return r


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, repo: _Repo) -> Iterator[TestClient]:
    monkeypatch.setenv("SARVAM_WEBHOOK_TOKEN", TOKEN)
    get_settings.cache_clear()
    from app.main import create_app

    app = create_app()
    service = ScreeningCallService(
        repo=repo, provider=None, assistant_settings=None, phone_enabled=False, audit=None,
    )
    app.dependency_overrides[get_screening_call_service] = lambda: service
    app.dependency_overrides[current_user] = lambda: {"email": "hr@x", "role": "hr"}
    yield TestClient(app)
    get_settings.cache_clear()


def result(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "attempt_id": ATTEMPT,
        "status": "connected",
        "duration": 142.0,
        "interaction_id": "int-1",
        "channel_info": {"channel_type": "v2v", "channel_provider": "plivo"},
        "webhook_config": {"url": "https://x/y", "metadata": {"screeningCallId": "SC-1"}},
        "interaction_transcript": [
            {"role": "agent", "en_text": "Is this Tanya?"},
            {"role": "user", "en_text": "Yes speaking"},
        ],
        "final_agent_variables": {
            "q1_answer": "Yes", "q1_followup": "strong",
            "q1_evidence": "I ran the Nykaa account for two years",
            "q2_answer": "Figma", "q2_followup": "weak", "q2_evidence": "mostly Figma",
            "screening_status": "completed", "call_outcome": "completed",
            "call_summary": "Two years on social, comfortable on camera.",
        },
    }
    body.update(over)
    return body


def post(client: TestClient, body: dict[str, Any], token: str = TOKEN):
    return client.post(f"/api/sarvam/webhook/{token}", content=json.dumps(body),
                       headers={"content-type": "application/json"})


# -- the gate ----------------------------------------------------------------


@pytest.mark.parametrize("token", ["wrong-token", "a-long-unguessable-toke"])
def test_a_wrong_token_is_a_404_not_a_401(client: TestClient, repo: _Repo, token: str) -> None:
    # 404 so a prober cannot even confirm the endpoint is here, and the same
    # answer as an unconfigured webhook. The near-miss catches a comparison
    # that stops at a prefix.
    response = post(client, result(), token=token)
    assert response.status_code == 404
    assert repo.rows["screening_calls"]["SC-1"]["status"] == "queued"


def test_an_empty_token_never_reaches_the_handler(client: TestClient, repo: _Repo) -> None:
    # /api/sarvam/webhook/ matches no route, so this is refused by routing
    # before any code runs. Fine either way - just not a 404.
    response = post(client, result(), token="")
    assert response.status_code in (404, 405)
    assert repo.rows["screening_calls"]["SC-1"]["status"] == "queued"


def test_an_unconfigured_webhook_accepts_nothing(
    monkeypatch: pytest.MonkeyPatch, repo: _Repo
) -> None:
    monkeypatch.setenv("SARVAM_WEBHOOK_TOKEN", "")
    get_settings.cache_clear()
    from app.main import create_app

    app = create_app()
    service = ScreeningCallService(
        repo=repo, provider=None, assistant_settings=None, phone_enabled=False, audit=None,
    )
    app.dependency_overrides[get_screening_call_service] = lambda: service
    assert TestClient(app).post("/api/sarvam/webhook/", json=result()).status_code in (404, 405)
    get_settings.cache_clear()


def test_a_result_for_a_call_we_never_placed_is_ignored(
    client: TestClient, repo: _Repo
) -> None:
    # Holding the URL is not enough: the attempt id has to be one we minted.
    response = post(client, result(attempt_id="att-someone-elses"))
    assert response.status_code == 200          # Sarvam must not be made to retry
    assert repo.rows["screening_calls"]["SC-1"]["status"] == "queued"


def test_metadata_cannot_redirect_a_result_onto_another_call(
    client: TestClient, repo: _Repo
) -> None:
    # The echoed id confirms a match; it must never choose the call.
    repo.rows["screening_calls"]["SC-2"] = {**_call_row(), "id": "SC-2", "providerCallId": "other"}
    body = result(webhook_config={"url": "https://x", "metadata": {"screeningCallId": "SC-2"}})
    post(client, body)
    assert repo.rows["screening_calls"]["SC-1"]["status"] == "queued"
    assert repo.rows["screening_calls"]["SC-2"]["status"] == "queued"


def test_a_repeat_of_a_finished_call_changes_nothing(client: TestClient, repo: _Repo) -> None:
    assert post(client, result()).status_code == 200
    first = dict(repo.rows["screening_calls"]["SC-1"])
    post(client, result(final_agent_variables={"q1_answer": "No", "call_outcome": "completed"}))
    assert repo.rows["screening_calls"]["SC-1"]["fitRating"] == first["fitRating"]


# -- the result ---------------------------------------------------------------


def test_a_connected_call_is_scored(client: TestClient, repo: _Repo) -> None:
    assert post(client, result()).status_code == 200
    call = repo.rows["screening_calls"]["SC-1"]
    assert call["status"] == "completed"
    assert call["fitRating"] == "Fit"
    assert call["durationSeconds"] == 142.0
    assert call["interactionId"] == "int-1"
    assert call["summary"].startswith("Two years on social")


def test_the_candidates_own_words_survive_into_the_report(
    client: TestClient, repo: _Repo
) -> None:
    post(client, result())
    first = repo.rows["screening_calls"]["SC-1"]["answers"][0]
    assert first["evidence"] == "I ran the Nykaa account for two years"
    assert first["followUpStrength"] == "strong"


def test_a_call_that_contradicts_the_form_is_flagged(client: TestClient, repo: _Repo) -> None:
    # The form said No to the must-have; the call said Yes. HR has to see that.
    post(client, result())
    first = repo.rows["screening_calls"]["SC-1"]["answers"][0]
    assert first["contradictsForm"] is True
    assert first["formAnswer"] == "No"


def test_the_transcript_is_turned_into_something_readable(
    client: TestClient, repo: _Repo
) -> None:
    post(client, result())
    assert repo.rows["screening_calls"]["SC-1"]["transcript"] == (
        "Agent: Is this Tanya?\nCandidate: Yes speaking"
    )


@pytest.mark.parametrize(
    "status,expected", [("no_answer", "no-answer"), ("busy", "no-answer"), ("failed", "failed")],
)
def test_a_call_that_never_connected_is_not_scored(
    client: TestClient, repo: _Repo, status: str, expected: str
) -> None:
    post(client, result(status=status, duration=None, final_agent_variables=None,
                        failure_reason="plivo: subscriber busy"))
    call = repo.rows["screening_calls"]["SC-1"]
    assert call["status"] == expected
    assert call.get("fitRating") is None
    assert call["answers"] == []


def test_refusing_the_call_is_recorded_without_a_rating(
    client: TestClient, repo: _Repo
) -> None:
    post(client, result(final_agent_variables={"call_outcome": "declined"}))
    call = repo.rows["screening_calls"]["SC-1"]
    assert call["status"] == "declined"
    assert call.get("fitRating") is None


def test_reaching_the_wrong_person_scores_nobody(client: TestClient, repo: _Repo) -> None:
    # The line was answered, so the carrier says connected - but not by them.
    post(client, result(final_agent_variables={"call_outcome": "wrong_person",
                                               "q1_answer": "Yes"}))
    call = repo.rows["screening_calls"]["SC-1"]
    assert call["status"] == "declined"
    assert call.get("fitRating") is None


def test_voicemail_is_a_no_answer_even_though_the_line_picked_up(
    client: TestClient, repo: _Repo
) -> None:
    post(client, result(final_agent_variables={"call_outcome": "no_response"}))
    assert repo.rows["screening_calls"]["SC-1"]["status"] == "no-answer"


def test_an_answer_the_agent_could_not_make_out_asks_for_review(
    client: TestClient, repo: _Repo
) -> None:
    post(client, result(final_agent_variables={
        "q1_answer": "Unclear", "q2_answer": "Figma", "call_outcome": "completed",
    }))
    call = repo.rows["screening_calls"]["SC-1"]
    assert call["needsReview"] is True
    # An unclear must-have cannot pass, so it cannot be a Fit.
    assert call["fitRating"] == "Unfit"


def test_an_unexpected_extra_field_does_not_break_the_result(
    client: TestClient, repo: _Repo
) -> None:
    # Sarvam adds fields over time; a stricter model would start rejecting
    # real results for carrying something new.
    assert post(client, result(some_future_field={"a": 1})).status_code == 200
    assert repo.rows["screening_calls"]["SC-1"]["status"] == "completed"
