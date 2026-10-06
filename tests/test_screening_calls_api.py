"""HTTP layer for AI screening calls: auth, error mapping, webhook secret, rate limits.

No database: the service is replaced by a stub, and the app's lifespan (which
connects to Postgres) is never entered.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import current_user, get_screening_call_service
from app.core.config import get_settings
from app.services.screening_calls import CallAlreadyActive

WEBHOOK_SECRET = "hook-secret"
HR = {"email": "hr@example.com", "name": "HR", "role": "hr"}
STORED_CALL = {
    "id": "SC-1",
    "candidateId": "c1",
    "mode": "web",
    "status": "queued",
    "webCallUrl": "https://vapi.daily.co/r",
    "vapiCallId": "secret-provider-id",
    "questions": [{"id": "m1"}],
    "startedAt": "2026-10-06T10:00:00Z",
    "answers": [],
}


class StubService:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.error: Exception | None = None

    def start_call(self, candidate_id: str, mode: str, actor: dict[str, Any]) -> dict[str, Any]:
        if self.error:
            raise self.error
        return {**STORED_CALL, "candidateId": candidate_id, "mode": mode}

    def list_for_candidate(self, candidate_id: str) -> list[dict[str, Any]]:
        return [STORED_CALL]

    def apply_event(self, message: dict[str, Any]) -> bool:
        self.events.append(message)
        return True


def make_client(monkeypatch: pytest.MonkeyPatch, stub: StubService, *, user: Any, **env: str) -> TestClient:
    monkeypatch.setenv("VAPI_WEBHOOK_SECRET", WEBHOOK_SECRET)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    from app.main import create_app

    app = create_app()
    app.dependency_overrides[get_screening_call_service] = lambda: stub
    app.dependency_overrides[current_user] = lambda: user
    return TestClient(app)  # not used as a context manager: lifespan (DB) never runs


@pytest.fixture
def stub() -> StubService:
    return StubService()


@pytest.fixture(autouse=True)
def _reset_settings() -> Iterator[None]:
    yield
    get_settings.cache_clear()


# -- HR endpoints ------------------------------------------------------------


def test_start_requires_a_session(monkeypatch: pytest.MonkeyPatch, stub: StubService) -> None:
    client = make_client(monkeypatch, stub, user=None)
    assert client.post("/api/screening-calls", json={"candidateId": "c1", "mode": "web"}).status_code == 401
    assert client.get("/api/screening-calls", params={"candidateId": "c1"}).status_code == 401


def test_start_returns_201_without_internal_fields(monkeypatch: pytest.MonkeyPatch, stub: StubService) -> None:
    client = make_client(monkeypatch, stub, user=HR)
    response = client.post("/api/screening-calls", json={"candidateId": "c1", "mode": "web"})
    assert response.status_code == 201
    body = response.json()
    assert body["webCallUrl"] == "https://vapi.daily.co/r"
    assert "vapiCallId" not in body and "questions" not in body


def test_start_rejects_unknown_fields_and_modes(monkeypatch: pytest.MonkeyPatch, stub: StubService) -> None:
    client = make_client(monkeypatch, stub, user=HR)
    assert client.post("/api/screening-calls", json={"candidateId": "c1", "mode": "sms"}).status_code == 422
    forged = {"candidateId": "c1", "mode": "web", "fitRating": "Fit"}
    assert client.post("/api/screening-calls", json=forged).status_code == 422


def test_service_errors_map_to_status_codes(monkeypatch: pytest.MonkeyPatch, stub: StubService) -> None:
    stub.error = CallAlreadyActive("busy")
    client = make_client(monkeypatch, stub, user=HR)
    response = client.post("/api/screening-calls", json={"candidateId": "c1", "mode": "web"})
    assert response.status_code == 409
    assert response.json()["detail"] == "busy"


def test_list_returns_calls(monkeypatch: pytest.MonkeyPatch, stub: StubService) -> None:
    client = make_client(monkeypatch, stub, user=HR)
    response = client.get("/api/screening-calls", params={"candidateId": "c1"})
    assert response.status_code == 200
    assert [c["id"] for c in response.json()] == ["SC-1"]


def test_start_is_rate_limited(monkeypatch: pytest.MonkeyPatch, stub: StubService) -> None:
    client = make_client(monkeypatch, stub, user=HR, PUBLIC_RATE_LIMIT_PER_MINUTE="2")
    payload = {"candidateId": "c1", "mode": "web"}
    codes = [client.post("/api/screening-calls", json=payload).status_code for _ in range(3)]
    assert codes == [201, 201, 429]


# -- Webhook -----------------------------------------------------------------

EVENT = {"message": {"type": "status-update", "status": "ringing", "call": {"id": "v1"}}}


def test_webhook_rejects_missing_or_wrong_secret(monkeypatch: pytest.MonkeyPatch, stub: StubService) -> None:
    client = make_client(monkeypatch, stub, user=None)
    assert client.post("/api/vapi/webhook", json=EVENT).status_code == 401
    assert client.post("/api/vapi/webhook", json=EVENT, headers={"X-Vapi-Secret": "nope"}).status_code == 401
    assert stub.events == []


def test_webhook_is_503_when_not_configured(monkeypatch: pytest.MonkeyPatch, stub: StubService) -> None:
    client = make_client(monkeypatch, stub, user=None, VAPI_WEBHOOK_SECRET="")
    assert client.post("/api/vapi/webhook", json=EVENT, headers={"X-Vapi-Secret": ""}).status_code == 503


def test_webhook_applies_event_with_right_secret(monkeypatch: pytest.MonkeyPatch, stub: StubService) -> None:
    client = make_client(monkeypatch, stub, user=None)
    response = client.post("/api/vapi/webhook", json=EVENT, headers={"X-Vapi-Secret": WEBHOOK_SECRET})
    assert response.status_code == 200
    assert stub.events == [EVENT["message"]]


def test_webhook_rejects_payload_without_message(monkeypatch: pytest.MonkeyPatch, stub: StubService) -> None:
    client = make_client(monkeypatch, stub, user=None)
    response = client.post("/api/vapi/webhook", json={"nope": 1}, headers={"X-Vapi-Secret": WEBHOOK_SECRET})
    assert response.status_code == 422
    assert stub.events == []
