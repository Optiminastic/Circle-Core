"""Regression test for a production incident (2026-11-26, found while
investigating the Dashboard's route-priority fetching architecture):
OngridSyncScheduler._enqueue_due() held one DB connection open across the
find-due query AND every due candidate's enqueue() call (each 1-2 round
trips: an idempotency-key check, then an INSERT). With production's real
scale (2,468 candidates, most newly due at once right after this phase's
own deploy) that's up to ~400 sequential round trips in one session.
Observed live: db_connection_held warnings of 18.9s, 50.5s, and 214.1s.
Fixed by finding-due with a short session, then opening a fresh short
session per candidate for its own enqueue() call - same shape as
tests/test_job_worker_connection_lifecycle.py and
tests/test_identity_sync_worker_connection_lifecycle.py. This test proves
the find-due session is closed before any enqueue() call runs, and that
each candidate gets its own session rather than sharing one across the
whole batch.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.services.ongrid_scheduler import OngridSyncScheduler


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


class _FakeJobRepository:
    instances: list["_FakeJobRepository"] = []

    def __init__(self, session: _FakeSession) -> None:
        self.session = session
        self.calls: list[tuple[str, ...]] = []
        _FakeJobRepository.instances.append(self)

    def enqueue(self, job_type: str, payload: dict, **kwargs: Any) -> None:
        self.calls.append((job_type, payload["candidateId"]))


def _make_scheduler(database: _FakeDatabase) -> OngridSyncScheduler:
    scheduler = OngridSyncScheduler.__new__(OngridSyncScheduler)
    scheduler._database = database  # type: ignore[attr-defined]
    scheduler._settings = SimpleNamespace(ongrid_community_id="community-1")  # type: ignore[attr-defined]
    scheduler._stale_after = 900  # type: ignore[attr-defined]
    return scheduler


def test_the_find_due_session_closes_before_any_enqueue_call(monkeypatch: Any) -> None:
    import app.services.ongrid_scheduler as scheduler_module

    log: list[str] = []
    _FakeJobRepository.instances.clear()
    monkeypatch.setattr(scheduler_module, "JobRepository", _FakeJobRepository)
    monkeypatch.setattr(
        scheduler_module, "_find_due_candidate_ids", lambda session, community_id, stale: ["C1", "C2", "C3"]
    )

    database = _FakeDatabase(log)
    scheduler = _make_scheduler(database)

    count = scheduler._enqueue_due()

    assert count == 3
    # One session for find-due, closed, THEN one fresh session PER
    # candidate - never one session shared across the whole batch.
    assert log == [
        "session-1:open",
        "session-1:close",
        "session-2:open",
        "session-2:close",
        "session-3:open",
        "session-3:close",
        "session-4:open",
        "session-4:close",
    ]
    assert [repo.calls for repo in _FakeJobRepository.instances] == [
        [("ongrid_status_sync", "C1")],
        [("ongrid_status_sync", "C2")],
        [("ongrid_status_sync", "C3")],
    ]


def test_no_due_candidates_means_no_enqueue_session_at_all(monkeypatch: Any) -> None:
    import app.services.ongrid_scheduler as scheduler_module

    log: list[str] = []
    _FakeJobRepository.instances.clear()
    monkeypatch.setattr(scheduler_module, "JobRepository", _FakeJobRepository)
    monkeypatch.setattr(scheduler_module, "_find_due_candidate_ids", lambda session, community_id, stale: [])

    database = _FakeDatabase(log)
    scheduler = _make_scheduler(database)

    count = scheduler._enqueue_due()

    assert count == 0
    assert log == ["session-1:open", "session-1:close"]
    assert _FakeJobRepository.instances == []
