"""Regression test for a production incident (2026-11-26): JobWorker held a
pooled DB connection open for the ENTIRE duration of a job's external call
(observed live: one `db_connection_held` of 125.9s), because
`_process_batch` wrapped the whole claim-and-process loop in one
`with database.session()` block. Fixed by releasing the session
immediately after claiming, and reacquiring a fresh one only to persist
the result - same shape as app/api/routes/bgv_ongrid.py's
sync_ongrid_status. This test proves the session used to claim a job is
closed BEFORE the (potentially slow) handler ever runs.
"""

from __future__ import annotations

from typing import Any

import app.services.job_worker as job_worker_module
from app.repositories.job_repository import JobRecord
from app.services.job_worker import JobWorker


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


def _job(job_id: str = "job-1") -> JobRecord:
    return JobRecord(
        id=job_id,
        job_type="slow_job",
        payload={},
        status="processing",
        attempts=0,
        max_attempts=3,
        idempotency_key=None,
        result=None,
        error=None,
        created_at=None,
        updated_at=None,
        started_at=None,
        completed_at=None,
    )


class _FakeJobRepository:
    """Records which fake session it was built with, and which claim/mark
    calls happened, without touching a real database."""

    instances: list["_FakeJobRepository"] = []

    def __init__(self, session: _FakeSession) -> None:
        self.session = session
        self.calls: list[str] = []
        _FakeJobRepository.instances.append(self)

    def claim_due(self, **kwargs: Any) -> list[JobRecord]:
        self.calls.append("claim_due")
        return [_job()]

    def mark_completed(self, job_id: str, result: Any = None) -> None:
        self.calls.append("mark_completed")

    def mark_retrying(self, job_id: str, *, error: str, retry_in_seconds: int) -> None:
        self.calls.append("mark_retrying")

    def mark_failed_permanently(self, job_id: str, *, error: str) -> None:
        self.calls.append("mark_failed_permanently")


def test_the_claim_session_is_closed_before_the_handler_runs(monkeypatch: Any) -> None:
    log: list[str] = []
    _FakeJobRepository.instances.clear()
    monkeypatch.setattr(job_worker_module, "JobRepository", _FakeJobRepository)

    database = _FakeDatabase(log)
    worker = JobWorker(database, settings=object(), batch_size=1)

    def handler(payload: dict) -> dict:
        # The claim session must already be closed by the time the handler
        # (standing in for a slow external call) runs.
        log.append("handler:run")
        assert "session-1:close" in log, "claim session was still open during job processing"
        return {"ok": True}

    worker.register("slow_job", handler)
    claimed_count = worker._process_batch()

    assert claimed_count == 1
    # Exactly: claim session opens+closes, THEN the handler runs, THEN a
    # fresh session opens+closes to persist the result.
    assert log == [
        "session-1:open",
        "session-1:close",
        "handler:run",
        "session-2:open",
        "session-2:close",
    ]
    assert [repo.calls for repo in _FakeJobRepository.instances] == [["claim_due"], ["mark_completed"]]


def test_a_failing_job_also_reacquires_a_fresh_session_to_record_the_failure(monkeypatch: Any) -> None:
    log: list[str] = []
    _FakeJobRepository.instances.clear()
    monkeypatch.setattr(job_worker_module, "JobRepository", _FakeJobRepository)

    database = _FakeDatabase(log)
    worker = JobWorker(database, settings=object(), batch_size=1)

    def failing_handler(payload: dict) -> dict:
        assert "session-1:close" in log
        raise RuntimeError("boom")

    worker.register("slow_job", failing_handler)
    worker._process_batch()

    assert log == [
        "session-1:open",
        "session-1:close",
        "session-2:open",
        "session-2:close",
    ]
    assert [repo.calls for repo in _FakeJobRepository.instances] == [["claim_due"], ["mark_retrying"]]
