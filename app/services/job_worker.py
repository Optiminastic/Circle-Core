"""In-process background job worker — no new infrastructure.

Runs inside each API worker process, same as `identity_sync_worker.py`. That
is safe here for the identical reason: claiming is `FOR UPDATE SKIP LOCKED`
(see `app/repositories/job_repository.py`), so the two uvicorn workers never
process the same job twice.

A per-job wall-clock timeout is enforced via a small thread pool
(`future.result(timeout=...)`) rather than an `asyncio` timeout, because job
handlers are themselves synchronous (they call synchronous SQLAlchemy /
urllib / googleapiclient code, like the rest of this codebase). Known,
accepted limitation of that approach: Python cannot forcibly kill a thread,
so a handler that ignores the timeout (e.g. a blocking socket call with no
timeout of its own) keeps running in its executor thread after this worker
has already marked the job failed/retrying and moved on — its eventual
return value is simply discarded. This is why every external call a job
handler makes must have its OWN timeout (see app/services/ongrid.py /
google_calendar.py's resilience work, Phase 3 C4) — the job-level timeout is
a second line of defense, not the only one.
"""

from __future__ import annotations

import asyncio
import random
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import suppress
from typing import Any, Callable

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.database import Database
from app.repositories.job_repository import JobRecord, JobRepository

logger = get_logger("curcle.jobs")

JobHandler = Callable[[dict[str, Any]], dict[str, Any] | None]

#: Default max wall-clock time for one job handler call. Override per job
#: type at register() if a job type is known to need longer (or shorter).
_DEFAULT_JOB_TIMEOUT_SECONDS = 120
#: How long a claimed job is hidden from other claims — must exceed the
#: longest job timeout + a margin, or a slow-but-still-running job could be
#: claimed a second time before its own lease expires.
_DEFAULT_LEASE_SECONDS = 300


def backoff_seconds(attempts: int) -> int:
    """Exponential backoff with jitter: 5s, 10s, 20s, 40s... capped at 5min,
    plus up to 50% random jitter so many simultaneously-failing jobs don't
    all retry in the same instant (thundering herd)."""
    base = min(300, 5 * (2**attempts))
    return base + random.randint(0, base // 2)


class JobWorker:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        poll_interval: int = 5,
        batch_size: int = 5,
        lease_seconds: int = _DEFAULT_LEASE_SECONDS,
    ) -> None:
        self._database = database
        self._poll_interval = max(1, poll_interval)
        self._batch_size = max(1, batch_size)
        self._lease_seconds = lease_seconds
        self._handlers: dict[str, JobHandler] = {}
        self._timeouts: dict[str, int] = {}
        self._task: asyncio.Task[None] | None = None
        self._executor = ThreadPoolExecutor(
            max_workers=self._batch_size, thread_name_prefix="curcle-job-worker"
        )

    def register(self, job_type: str, handler: JobHandler, *, timeout_seconds: int | None = None) -> None:
        self._handlers[job_type] = handler
        self._timeouts[job_type] = timeout_seconds or _DEFAULT_JOB_TIMEOUT_SECONDS

    def start(self) -> None:
        if not self._handlers:
            logger.info("Job worker has no registered handlers; not starting.")
            return
        self._task = asyncio.create_task(self._run(), name="job-worker")
        logger.info("Job worker started for types: %s", sorted(self._handlers))

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        # wait=False: don't block shutdown on in-flight jobs that ignored
        # their timeout (see module docstring) — they'll be reclaimed by
        # whichever worker process is up once their lease expires.
        self._executor.shutdown(wait=False, cancel_futures=True)

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self._process_batch)
            except Exception:  # the poll loop must outlive any one failure
                logger.exception("Job worker pass failed; retrying next tick.")
            await asyncio.sleep(self._poll_interval)

    def _process_batch(self) -> int:
        # Claim with a short-lived session, then release it BEFORE processing
        # any job. A job's `future.result(timeout=...)` can block for up to
        # that job type's full timeout (default 120s) waiting on an external
        # call (OnGrid, Google Calendar) - holding a pooled DB connection for
        # that whole span, times up to `batch_size` jobs processed
        # sequentially, starved the connection pool in production (observed:
        # a single `db_connection_held` of 125.9s). Same fix shape as
        # app/api/routes/bgv_ongrid.py's sync_ongrid_status: release before
        # the slow external work, reacquire only to persist the result.
        with self._database.session() as session:
            jobs = JobRepository(session).claim_due(
                job_types=list(self._handlers), limit=self._batch_size, lease_seconds=self._lease_seconds
            )
        for job in jobs:
            self._process_one(job)
        return len(jobs)

    def _process_one(self, job: JobRecord) -> None:
        handler = self._handlers[job.job_type]
        timeout = self._timeouts[job.job_type]
        future = self._executor.submit(handler, job.payload)
        try:
            result = future.result(timeout=timeout)
            with self._database.session() as session:
                JobRepository(session).mark_completed(job.id, result)
            logger.info("Job %s (%s) completed.", job.id, job.job_type)
        except FutureTimeoutError:
            self._fail_or_retry(job, error=f"job timed out after {timeout}s")
        except Exception as exc:  # noqa: BLE001 - a job's own failure must never kill the worker loop
            logger.exception("Job %s (%s) failed", job.id, job.job_type)
            self._fail_or_retry(job, error=str(exc))

    def _fail_or_retry(self, job: JobRecord, *, error: str) -> None:
        with self._database.session() as session:
            repo = JobRepository(session)
            if job.attempts + 1 >= job.max_attempts:
                repo.mark_failed_permanently(job.id, error=error)
                logger.warning("Job %s (%s) permanently failed: %s", job.id, job.job_type, error)
            else:
                delay = backoff_seconds(job.attempts)
                repo.mark_retrying(job.id, error=error, retry_in_seconds=delay)
                logger.info("Job %s (%s) retrying in %ss: %s", job.id, job.job_type, delay, error)
