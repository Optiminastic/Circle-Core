"""Periodic scheduler that keeps cached OnGrid statuses warm.

Mirrors app/services/identity_sync_worker.py's exact shape (asyncio loop,
started/stopped from main.py's lifespan, safe to run in both uvicorn workers).
Unlike that worker, this one doesn't do the work itself — it finds candidates
whose cached status is missing or stale and ENQUEUES an `ongrid_status_sync`
background job per candidate (app/services/job_handlers.py), so the actual
OnGrid calls go through the same job queue, retry/backoff, and per-job
timeout as any other background job — see
docs/PERFORMANCE_IMPLEMENTATION_LOG.md Phase 3 C1/C2/C4.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

from sqlalchemy import text

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.database import Database
from app.repositories.job_repository import JobRepository

logger = get_logger("curcle.ongrid_sync_scheduler")

_BGVS_TABLE = "bgvs"


def _find_due_candidate_ids(session: Any, community_id: str, stale_after_seconds: int) -> list[str]:
    """Candidates onboarded into OUR OnGrid community whose cached status is
    missing or older than `stale_after_seconds`. Matches `belongs_here()`'s
    own check in bgv_ongrid.py — never schedules a sync for an individual id
    that belongs to a different community."""
    rows = session.execute(
        text(
            f"""
            SELECT id FROM "{_BGVS_TABLE}"
             WHERE data->>'ongridIndividualId' IS NOT NULL
               AND data->>'ongridIndividualId' != ''
               AND data->>'ongridCommunityId' = :community_id
               AND (
                     data->>'ongridStatusAt' IS NULL
                     OR (data->>'ongridStatusAt') !~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}'
                     OR (data->>'ongridStatusAt')::timestamptz < now() - make_interval(secs => :stale)
                   )
             LIMIT 200
            """
        ),
        {"community_id": community_id, "stale": stale_after_seconds},
    ).fetchall()
    return [row[0] for row in rows]


class OngridSyncScheduler:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        interval_seconds: int = 300,
        stale_after_seconds: int = 900,
    ) -> None:
        self._database = database
        self._settings = settings
        self._interval = max(30, interval_seconds)
        self._stale_after = stale_after_seconds
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="ongrid-sync-scheduler")
        logger.info("OnGrid sync scheduler started (every %ss, stale after %ss).", self._interval, self._stale_after)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self._enqueue_due)
            except Exception:  # the loop must outlive any one failure
                logger.exception("OnGrid sync scheduler pass failed; retrying next tick.")
            await asyncio.sleep(self._interval)

    def _enqueue_due(self) -> int:
        with self._database.session() as session:
            candidate_ids = _find_due_candidate_ids(
                session, str(self._settings.ongrid_community_id or ""), self._stale_after
            )
            if not candidate_ids:
                return 0
            job_repo = JobRepository(session)
            for candidate_id in candidate_ids:
                # Idempotency key has no time component: while a sync for
                # this candidate is queued/processing, a second scheduler
                # tick finds the SAME existing job instead of piling up
                # duplicates. Once it completes, ongridStatusAt is fresh
                # again, so this candidate drops out of _find_due_candidate_ids
                # until it goes stale once more - no need to roll the key.
                job_repo.enqueue(
                    "ongrid_status_sync",
                    {"candidateId": candidate_id},
                    idempotency_key=f"ongrid-sync:{candidate_id}",
                    max_attempts=3,
                )
            return len(candidate_ids)
