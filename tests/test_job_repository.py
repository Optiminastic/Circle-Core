"""JobRepository against a real Postgres — the SKIP LOCKED claim, the
partial-unique idempotency behavior, and status transitions all depend on
real Postgres semantics that can't be faked. Needs CIRCUIT_TEST_DATABASE_URL
(a disposable Postgres), like the other DB-backed suites in this repo.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.repositories.job_repository import TABLE, INDEX_DDL, TABLE_DDL, JobRepository

TEST_DB_URL = os.environ.get("CIRCLE_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not TEST_DB_URL, reason="CIRCLE_TEST_DATABASE_URL not set")


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine(TEST_DB_URL, isolation_level="AUTOCOMMIT")
    with engine.begin() as conn:
        conn.execute(text(TABLE_DDL))
        for ddl in INDEX_DDL:
            conn.execute(text(ddl))
    factory = sessionmaker(bind=engine, future=True)
    s = factory()
    try:
        s.execute(text(f'TRUNCATE TABLE "{TABLE}"'))
        yield s
    finally:
        s.close()
        engine.dispose()


def test_enqueue_and_get(session: Session) -> None:
    repo = JobRepository(session)
    job = repo.enqueue("test_job", {"x": 1})
    fetched = repo.get(job.id)
    assert fetched is not None
    assert fetched.job_type == "test_job"
    assert fetched.payload == {"x": 1}
    assert fetched.status == "queued"
    assert fetched.attempts == 0


def test_get_missing_job_returns_none(session: Session) -> None:
    repo = JobRepository(session)
    assert repo.get("does-not-exist") is None


def test_duplicate_idempotency_key_returns_the_same_pending_job(session: Session) -> None:
    repo = JobRepository(session)
    first = repo.enqueue("ocr", {"a": 1}, idempotency_key="k1")
    second = repo.enqueue("ocr", {"a": 2}, idempotency_key="k1")
    assert second.id == first.id
    assert second.payload == {"a": 1}  # the ORIGINAL payload, not the second submission's


def test_idempotency_key_is_free_again_once_the_job_completes(session: Session) -> None:
    repo = JobRepository(session)
    first = repo.enqueue("ongrid_status_sync", {"candidateId": "c1"}, idempotency_key="k2")
    repo.mark_completed(first.id, {"ok": True})

    second = repo.enqueue("ongrid_status_sync", {"candidateId": "c1"}, idempotency_key="k2")
    assert second.id != first.id  # a FRESH job, not the stale completed one
    assert repo.get(first.id).status == "completed"
    assert repo.get(second.id).status == "queued"


def test_claim_due_only_claims_jobs_at_or_past_their_schedule(session: Session) -> None:
    repo = JobRepository(session)
    repo.enqueue("test_job", {}, delay_seconds=3600)  # due in an hour
    due_now = repo.enqueue("test_job", {})

    claimed = repo.claim_due(job_types=["test_job"], limit=10, lease_seconds=60)
    assert [j.id for j in claimed] == [due_now.id]


def test_claim_due_respects_job_type_filter(session: Session) -> None:
    repo = JobRepository(session)
    repo.enqueue("type_a", {})
    job_b = repo.enqueue("type_b", {})

    claimed = repo.claim_due(job_types=["type_b"], limit=10, lease_seconds=60)
    assert [j.id for j in claimed] == [job_b.id]


def test_claim_due_does_not_reclaim_a_job_still_within_its_lease(session: Session) -> None:
    repo = JobRepository(session)
    repo.enqueue("test_job", {})
    first_claim = repo.claim_due(job_types=["test_job"], limit=10, lease_seconds=300)
    assert len(first_claim) == 1

    second_claim = repo.claim_due(job_types=["test_job"], limit=10, lease_seconds=300)
    assert second_claim == []  # still leased - not claimable again yet


def test_claim_due_reclaims_a_job_whose_lease_expired(session: Session) -> None:
    repo = JobRepository(session)
    repo.enqueue("test_job", {})
    repo.claim_due(job_types=["test_job"], limit=10, lease_seconds=0)  # immediately-expiring lease

    reclaimed = repo.claim_due(job_types=["test_job"], limit=10, lease_seconds=60)
    assert len(reclaimed) == 1  # a crashed worker's job is picked up again


def test_mark_completed_sets_terminal_state_and_result(session: Session) -> None:
    repo = JobRepository(session)
    job = repo.enqueue("test_job", {})
    repo.mark_completed(job.id, {"found": 3})
    fetched = repo.get(job.id)
    assert fetched.status == "completed"
    assert fetched.result == {"found": 3}
    assert fetched.completed_at is not None


def test_mark_retrying_increments_attempts_and_reschedules(session: Session) -> None:
    repo = JobRepository(session)
    job = repo.enqueue("test_job", {})
    repo.mark_retrying(job.id, error="boom", retry_in_seconds=3600)
    fetched = repo.get(job.id)
    assert fetched.status == "retrying"
    assert fetched.attempts == 1
    assert fetched.error == "boom"
    # Not claimable again immediately - it's scheduled an hour out.
    assert repo.claim_due(job_types=["test_job"], limit=10, lease_seconds=60) == []


def test_mark_failed_permanently_is_terminal(session: Session) -> None:
    repo = JobRepository(session)
    job = repo.enqueue("test_job", {}, max_attempts=1)
    repo.mark_failed_permanently(job.id, error="dead")
    fetched = repo.get(job.id)
    assert fetched.status == "failed"
    assert fetched.error == "dead"
    assert repo.claim_due(job_types=["test_job"], limit=10, lease_seconds=60) == []


def test_cancel_only_works_on_queued_or_retrying_jobs(session: Session) -> None:
    repo = JobRepository(session)
    queued = repo.enqueue("test_job", {})
    assert repo.cancel(queued.id) is True
    assert repo.get(queued.id).status == "cancelled"

    completed = repo.enqueue("test_job", {})
    repo.mark_completed(completed.id)
    assert repo.cancel(completed.id) is False  # already terminal - nothing to cancel


def test_retry_now_only_works_on_failed_or_cancelled_jobs(session: Session) -> None:
    repo = JobRepository(session)
    failed = repo.enqueue("test_job", {}, max_attempts=1)
    repo.mark_failed_permanently(failed.id, error="dead")

    assert repo.retry_now(failed.id) is True
    fetched = repo.get(failed.id)
    assert fetched.status == "queued"
    assert fetched.error is None

    queued = repo.enqueue("test_job", {})
    assert repo.retry_now(queued.id) is False  # not failed/cancelled - nothing to do
