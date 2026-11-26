"""Regression test for a production incident (2026-11-26, found while
investigating the same symptom the Phase 3 job-worker fix had already
addressed once): IdentitySyncWorker._deliver_due() held one DB connection
open across the ENTIRE batch - the claim AND every item's
client.send() (external HTTP, up to 10s each, CLAIM_BATCH_SIZE=10) - in a
single `with database.session()` block. Observed live: a single
db_connection_held of 371.9 seconds. Fixed by claiming with a short-lived
session, sending with no session held at all, then marking each result in
its own fresh short session - same shape as
tests/test_job_worker_connection_lifecycle.py. This test proves the claim
session is closed before client.send() ever runs.
"""

from __future__ import annotations

from typing import Any

from app.repositories.identity_outbox_repository import OutboxItem
from app.services.identity_sync import IdentitySyncError
from app.services.identity_sync_worker import IdentitySyncWorker


class _FakeSession:
    def __init__(self, log: list[str], name: str) -> None:
        self._log = log
        self._name = name

    def __enter__(self) -> "_FakeSession":
        self._log.append(f"{self._name}:open")
        return self

    def __exit__(self, *exc: object) -> bool:
        self._log.append(f"{self._name}:close")
        return False


class _FakeDatabase:
    def __init__(self, log: list[str]) -> None:
        self._log = log
        self._count = 0

    def session(self) -> _FakeSession:
        self._count += 1
        return _FakeSession(self._log, f"session-{self._count}")


def _item(employee_id: str = "EMP-1") -> OutboxItem:
    return OutboxItem(employee_id, {"id": employee_id}, 1, 0)


class _FakeOutboxRepository:
    instances: list["_FakeOutboxRepository"] = []

    def __init__(self, session: _FakeSession) -> None:
        self.session = session
        self.calls: list[str] = []
        _FakeOutboxRepository.instances.append(self)

    def claim_due(self, **kwargs: Any) -> list[OutboxItem]:
        self.calls.append("claim_due")
        return [_item()]

    def mark_delivered(self, item: OutboxItem) -> None:
        self.calls.append("mark_delivered")

    def mark_failed(self, item: OutboxItem, *, error: str, retry_in_seconds: int) -> None:
        self.calls.append("mark_failed")


def _make_worker(database: _FakeDatabase) -> IdentitySyncWorker:
    # Bypass __init__ (it builds a real IdentitySyncClient from Settings) -
    # only _database and _client matter for this test.
    worker = IdentitySyncWorker.__new__(IdentitySyncWorker)
    worker._database = database  # type: ignore[attr-defined]
    return worker


def test_the_claim_session_is_closed_before_send_runs(monkeypatch: Any) -> None:
    import app.services.identity_sync_worker as worker_module

    log: list[str] = []
    _FakeOutboxRepository.instances.clear()
    monkeypatch.setattr(worker_module, "IdentityOutboxRepository", _FakeOutboxRepository)

    database = _FakeDatabase(log)
    worker = _make_worker(database)

    def fake_send(payload: dict) -> None:
        log.append("send:run")
        assert "session-1:close" in log, "claim session was still open during client.send()"

    worker._client = type("C", (), {"send": staticmethod(fake_send)})()  # type: ignore[attr-defined]

    delivered = worker._deliver_due()

    assert delivered == 1
    assert log == [
        "session-1:open",
        "session-1:close",
        "send:run",
        "session-2:open",
        "session-2:close",
    ]
    assert [repo.calls for repo in _FakeOutboxRepository.instances] == [["claim_due"], ["mark_delivered"]]


def test_a_failed_send_still_reacquires_a_fresh_session_to_mark_failed(monkeypatch: Any) -> None:
    import app.services.identity_sync_worker as worker_module

    log: list[str] = []
    _FakeOutboxRepository.instances.clear()
    monkeypatch.setattr(worker_module, "IdentityOutboxRepository", _FakeOutboxRepository)

    database = _FakeDatabase(log)
    worker = _make_worker(database)

    def failing_send(payload: dict) -> None:
        assert "session-1:close" in log
        raise IdentitySyncError("boom")

    worker._client = type("C", (), {"send": staticmethod(failing_send)})()  # type: ignore[attr-defined]

    delivered = worker._deliver_due()

    assert delivered == 0
    assert log == [
        "session-1:open",
        "session-1:close",
        "session-2:open",
        "session-2:close",
    ]
    assert [repo.calls for repo in _FakeOutboxRepository.instances] == [["claim_due"], ["mark_failed"]]
