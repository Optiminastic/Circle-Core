"""Background job handlers — registered onto app/services/job_worker.py's
JobWorker in app/main.py's lifespan.

Each `make_*_handler` factory closes over `database`/`settings` (bound once
at registration time, same as IdentitySyncWorker's own construction) and
returns a plain `payload: dict -> result: dict | None` callable — the shape
JobWorker expects. A handler raises on failure; JobWorker turns that into a
retry (with backoff) or a permanent failure once max_attempts is exhausted.
"""

from __future__ import annotations

from typing import Any

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.database import Database
from app.services.google_calendar import GoogleCalendarService

logger = get_logger("curcle.jobs")

_OAUTH_TABLE = "google_oauth"
_OAUTH_ROW_ID = "shared"
_LINKS_TABLE = "calendar_links"
_BGVS_TABLE = "bgvs"


def make_calendar_cleanup_retry_handler(database: Database, settings: Settings):
    """Retries a single Google Calendar event delete that failed inline
    during a candidate-delete cascade. See
    app/api/routes/calendar.py:cleanup_calendar_events and Phase 3 C4."""

    def handler(payload: dict[str, Any]) -> dict[str, Any]:
        app_event_id = payload["appEventId"]
        calendar_id = payload["calendarId"]
        google_event_id = payload["googleEventId"]

        with database.repository() as repo:
            conn = repo.get(_OAUTH_TABLE, _OAUTH_ROW_ID)
        refresh_token = (conn or {}).get("refreshToken")
        if not refresh_token:
            # Nothing to retry against - Calendar was disconnected since the
            # original failure. Not a transient error, so don't keep retrying.
            return {"skipped": "not_connected"}

        service = GoogleCalendarService(settings)
        service.delete_event(refresh_token, calendar_id, google_event_id)
        logger.info("Retried calendar cleanup succeeded for %s", app_event_id)
        return {"appEventId": app_event_id, "deleted": True}

    return handler


def make_ongrid_status_sync_handler(database: Database, settings: Settings):
    """Refreshes one candidate's cached OnGrid status in the background —
    the same logic `GET .../ongrid-status` runs on demand
    (app/api/routes/bgv_ongrid.py:sync_ongrid_status), just triggered by the
    scheduler (app/services/ongrid_scheduler.py) instead of an HR page view.
    Keeps the cache warm so a later on-demand read is fast, and so a cache
    fallback during an OnGrid outage (Phase 3 C4) is recent rather than
    stale from whenever HR last happened to look."""

    def handler(payload: dict[str, Any]) -> dict[str, Any]:
        # Local import: avoids a module-level import cycle (bgv_ongrid.py
        # imports from doc_requests.py; this module is imported by main.py
        # before either route module is guaranteed loaded).
        from app.api.routes.bgv_ongrid import sync_ongrid_status

        candidate_id = payload["candidateId"]
        result = sync_ongrid_status(database, settings, candidate_id)
        return result.model_dump()

    return handler
