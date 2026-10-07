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
from app.repositories.identity_outbox_repository import IdentityOutboxRepository
from app.services.identity_sync import IdentitySyncClient, IdentitySyncService

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
        with self._database.session() as session:
            service = IdentitySyncService(IdentityOutboxRepository(session), self._client)
            return service.deliver_due()
