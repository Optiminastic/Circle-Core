"""In-process loop that drains the id-sync push outbox.

Runs inside each API worker process. That is safe: claiming is
`SKIP LOCKED`, so two processes never send the same row. There is no full
re-queue here - id-sync pulls the whole roster on its own schedule.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.database import Database
from app.repositories.identity_outbox_repository import IdentityOutboxRepository, OutboxItem
from app.services.identity_sync import IdentitySyncClient, IdentitySyncError, retry_delay_seconds

logger = get_logger("curcle.identity_sync")


class IdentitySyncWorker:
    def __init__(self, database: Database, settings: Settings) -> None:
        self._database = database
        self._client = IdentitySyncClient(settings.idsync_push_url, settings.internal_api_secret)
        self._interval = max(1, settings.identity_sync_interval_seconds)
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="identity-sync")
        logger.info("id-sync push worker started.")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self._deliver_due)
            except Exception:  # the loop must outlive any one failure
                logger.exception("id-sync push pass failed; retrying next tick.")
            await asyncio.sleep(self._interval)

    def _deliver_due(self) -> int:
        # Claim with a short-lived session, then release it BEFORE sending
        # anything. Production incident (2026-11-26): this used to run the
        # whole batch - claim AND every item's client.send() (external HTTP,
        # up to 10s each, CLAIM_BATCH_SIZE=10 items) - inside one
        # `with database.session()` block, so a single pass could hold a
        # pooled connection for minutes (observed live: a single
        # db_connection_held of 371.9s), starving every other request on
        # that uvicorn worker. Same fix shape as app/services/job_worker.py's
        # Phase 3 fix and app/api/routes/bgv_ongrid.py's sync_ongrid_status:
        # release before the slow external work, reacquire only to persist
        # each result. IdentitySyncService.deliver_due() (still correct,
        # still covered by tests/test_identity_sync.py's single-session
        # scenarios) is intentionally NOT reused here for that reason - this
        # loop needs per-item session boundaries, not one for the batch.
        with self._database.session() as session:
            items = IdentityOutboxRepository(session).claim_due(
                limit=10, lease_seconds=600  # CLAIM_BATCH_SIZE / CLAIM_LEASE_SECONDS, see identity_sync.py
            )
        delivered = 0
        for item in items:
            if self._deliver_one(item):
                delivered += 1
        return delivered

    def _deliver_one(self, item: OutboxItem) -> bool:
        try:
            self._client.send(item.payload)
        except IdentitySyncError as exc:
            delay = retry_delay_seconds(item.attempts)
            logger.warning(
                "id-sync push for %s failed (attempt %d), retrying in %ds: %s",
                item.employee_id, item.attempts + 1, delay, exc,
            )
            with self._database.session() as session:
                IdentityOutboxRepository(session).mark_failed(item, error=str(exc), retry_in_seconds=delay)
            return False
        with self._database.session() as session:
            IdentityOutboxRepository(session).mark_delivered(item)
        return True
