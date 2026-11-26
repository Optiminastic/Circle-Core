"""Durable, Postgres-backed background job queue.

No new infrastructure (no Redis, no separate worker process/container) — a
single generic table, claimed via `FOR UPDATE SKIP LOCKED` exactly like
`identity_outbox_repository.py` already does for the id-sync push queue, so
this app's existing 2-uvicorn-worker deployment can run the in-process worker
loop (see `app/services/job_worker.py`) in BOTH workers safely: Postgres's row
locking guarantees a job is only ever claimed by one of them at a time.

`next_attempt_at` is both the schedule AND the visibility-timeout lease (same
trick as identity_outbox): claiming pushes it `lease_seconds` into the future,
so a worker that crashes mid-job doesn't lose the job — it just becomes
claimable again once the lease expires, same as the queued/retrying case. One
column, three meanings (initial delay, retry backoff, crash recovery) — no
separate "stuck job sweeper" needed.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

TABLE = "background_jobs"

TABLE_DDL = f"""
CREATE TABLE IF NOT EXISTS "{TABLE}" (
    id              TEXT PRIMARY KEY,
    job_type        TEXT NOT NULL,
    payload         JSONB NOT NULL,
    idempotency_key TEXT,
    status          TEXT NOT NULL DEFAULT 'queued',
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 3,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    result          JSONB,
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ
)
"""
# Partial unique index: NULL idempotency_key (most jobs) never collides;
# a duplicate submission of the SAME real idempotency_key does — but ONLY
# while that job is still non-terminal (see enqueue()). Once a job
# completes/fails/is cancelled, the SAME key is free to be reused by a NEW
# job occurrence — this is what makes a RECURRING idempotency key (e.g.
# "ongrid-sync:{candidate_id}", re-enqueued every scheduler tick) dedupe
# while pending without permanently blocking every future occurrence after
# the first one ever completes.
INDEX_DDL = (
    f'CREATE INDEX IF NOT EXISTS "ix_{TABLE}_due" ON "{TABLE}" (next_attempt_at)',
    f'CREATE UNIQUE INDEX IF NOT EXISTS "ix_{TABLE}_idempotency" ON "{TABLE}" (idempotency_key) '
    "WHERE idempotency_key IS NOT NULL AND status NOT IN ('completed', 'failed', 'cancelled')",
    f'CREATE INDEX IF NOT EXISTS "ix_{TABLE}_type_status" ON "{TABLE}" (job_type, status)',
)

#: Terminal states a job never leaves on its own.
TERMINAL_STATUSES = ("completed", "failed", "cancelled")
#: States `claim_due` will pick up — "processing" is included so a job
#: abandoned by a crashed worker (lease expired) gets picked up again.
_CLAIMABLE_STATUSES = ("queued", "retrying", "processing")

_MAX_ERROR_CHARS = 2000


@dataclass(frozen=True)
class JobRecord:
    id: str
    job_type: str
    payload: dict[str, Any]
    status: str
    attempts: int
    max_attempts: int
    idempotency_key: str | None
    result: dict[str, Any] | None
    error: str | None
    created_at: datetime | None
    updated_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None


def _row_to_record(row: Any) -> JobRecord:
    return JobRecord(
        id=row.id,
        job_type=row.job_type,
        payload=row.payload,
        status=row.status,
        attempts=row.attempts,
        max_attempts=row.max_attempts,
        idempotency_key=row.idempotency_key,
        result=row.result,
        error=row.error,
        created_at=row.created_at,
        updated_at=row.updated_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


class JobRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def enqueue(
        self,
        job_type: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        max_attempts: int = 3,
        delay_seconds: int = 0,
    ) -> JobRecord:
        """Create a job, or — if `idempotency_key` matches an existing job —
        return that existing job unchanged. This is how "prevent duplicate
        OCR processing and duplicate external submissions" is enforced: the
        caller passes a key derived from what makes the operation unique
        (e.g. f"ocr:{request_id}:{doc_type}"), and a second submission for
        the same key is a no-op that just hands back the original job."""
        if idempotency_key:
            # Only a NON-TERMINAL existing job blocks a duplicate submission —
            # see the INDEX_DDL comment above for why a completed/failed/
            # cancelled job must not permanently claim this key.
            existing = self._session.execute(
                text(
                    f"""
                    SELECT * FROM "{TABLE}"
                     WHERE idempotency_key = :key AND status NOT IN ('completed', 'failed', 'cancelled')
                    """
                ),
                {"key": idempotency_key},
            ).fetchone()
            if existing:
                return _row_to_record(existing)

        job_id = uuid.uuid4().hex
        row = self._session.execute(
            text(
                f"""
                INSERT INTO "{TABLE}"
                    (id, job_type, payload, idempotency_key, max_attempts, next_attempt_at)
                VALUES
                    (:id, :job_type, CAST(:payload AS JSONB), :idempotency_key, :max_attempts,
                     now() + make_interval(secs => :delay))
                RETURNING *
                """
            ),
            {
                "id": job_id,
                "job_type": job_type,
                "payload": json.dumps(payload),
                "idempotency_key": idempotency_key,
                "max_attempts": max_attempts,
                "delay": delay_seconds,
            },
        ).fetchone()
        return _row_to_record(row)

    def get(self, job_id: str) -> JobRecord | None:
        row = self._session.execute(
            text(f'SELECT * FROM "{TABLE}" WHERE id = :id'), {"id": job_id}
        ).fetchone()
        return _row_to_record(row) if row else None

    def claim_due(self, *, job_types: list[str], limit: int, lease_seconds: int) -> list[JobRecord]:
        """Atomically claim up to `limit` due jobs of the given types and hide
        them for `lease_seconds` (the processing lease). Safe to call from
        both uvicorn workers concurrently — `FOR UPDATE SKIP LOCKED` means a
        row already being claimed by the other worker is simply skipped, not
        waited on or double-claimed."""
        if not job_types:
            return []
        rows = self._session.execute(
            text(
                f"""
                UPDATE "{TABLE}"
                   SET status = 'processing',
                       started_at = COALESCE(started_at, now()),
                       next_attempt_at = now() + make_interval(secs => :lease),
                       updated_at = now()
                 WHERE id IN (
                        SELECT id FROM "{TABLE}"
                         WHERE status = ANY(:claimable)
                           AND job_type = ANY(:job_types)
                           AND next_attempt_at <= now()
                         ORDER BY next_attempt_at
                         LIMIT :limit
                         FOR UPDATE SKIP LOCKED)
                RETURNING *
                """
            ),
            {
                "claimable": list(_CLAIMABLE_STATUSES),
                "job_types": job_types,
                "limit": limit,
                "lease": lease_seconds,
            },
        ).fetchall()
        return [_row_to_record(row) for row in rows]

    def mark_completed(self, job_id: str, result: dict[str, Any] | None = None) -> None:
        self._session.execute(
            text(
                f"""
                UPDATE "{TABLE}"
                   SET status = 'completed',
                       result = CAST(:result AS JSONB),
                       error = NULL,
                       completed_at = now(),
                       updated_at = now()
                 WHERE id = :id
                """
            ),
            {"id": job_id, "result": json.dumps(result) if result is not None else None},
        )

    def mark_retrying(self, job_id: str, *, error: str, retry_in_seconds: int) -> None:
        self._session.execute(
            text(
                f"""
                UPDATE "{TABLE}"
                   SET status = 'retrying',
                       attempts = attempts + 1,
                       error = :error,
                       next_attempt_at = now() + make_interval(secs => :delay),
                       updated_at = now()
                 WHERE id = :id
                """
            ),
            {"id": job_id, "error": error[:_MAX_ERROR_CHARS], "delay": retry_in_seconds},
        )

    def mark_failed_permanently(self, job_id: str, *, error: str) -> None:
        """Dead-letter: attempts exhausted. The row stays (for visibility via
        the job-status endpoint / `GET /api/jobs/{id}`) rather than being
        deleted — a permanently-failed job is itself diagnostic information."""
        self._session.execute(
            text(
                f"""
                UPDATE "{TABLE}"
                   SET status = 'failed',
                       attempts = attempts + 1,
                       error = :error,
                       completed_at = now(),
                       updated_at = now()
                 WHERE id = :id
                """
            ),
            {"id": job_id, "error": error[:_MAX_ERROR_CHARS]},
        )

    def cancel(self, job_id: str) -> bool:
        """Cancel a job that hasn't started yet. Returns False if it was
        already processing/terminal — cancellation is not a hard interrupt
        of in-flight work, only a way to stop a job that hasn't run yet."""
        result = self._session.execute(
            text(
                f"""
                UPDATE "{TABLE}" SET status = 'cancelled', updated_at = now()
                 WHERE id = :id AND status IN ('queued', 'retrying')
                """
            ),
            {"id": job_id},
        )
        return result.rowcount > 0

    def retry_now(self, job_id: str) -> bool:
        """Operator/HR-triggered retry of a failed job — resets it to
        `queued` and due immediately, for `POST /api/jobs/{id}/retry`."""
        result = self._session.execute(
            text(
                f"""
                UPDATE "{TABLE}"
                   SET status = 'queued', next_attempt_at = now(), error = NULL, updated_at = now()
                 WHERE id = :id AND status IN ('failed', 'cancelled')
                """
            ),
            {"id": job_id},
        )
        return result.rowcount > 0
