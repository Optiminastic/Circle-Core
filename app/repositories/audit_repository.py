"""Read/write access for the append-only audit log.

Kept separate from DocumentRepository because the audit feed needs real queries
- filter by actor / action / date range, a case-insensitive text search, and
GROUP BY aggregation for the per-HR summary - none of which the generic JSONB
document store exposes. Writes are INSERT-only: there is deliberately no update
or delete method, matching the append-only requirement in curcle-be CLAUDE.md
section 5.6.

The audit record lives in the `data` JSONB column of the shared table shape, so
the table is created by the normal `ensure_tables` path and gets the same
created_at btree + GIN indexes as every other resource.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.errors import RepositoryError
from app.core.logging import get_logger

logger = get_logger("curcle.audit")

TABLE = "audit_events"

# Shared WHERE fragment for the feed + count. Every clause is null-guarded so an
# absent filter is a no-op. Dates are cast from text so ISO 8601 strings from the
# API bind directly; `q` matches the human summary, the entity label or the
# actor's name (case-insensitive).
_FILTER = """
  (:actor IS NULL OR data->>'actor_email' = :actor)
  AND (:action IS NULL OR data->>'action' = :action)
  AND (CAST(:date_from AS timestamptz) IS NULL OR created_at >= CAST(:date_from AS timestamptz))
  AND (CAST(:date_to   AS timestamptz) IS NULL OR created_at <= CAST(:date_to   AS timestamptz))
  AND (:q IS NULL OR (
        data->>'summary' ILIKE :q
     OR data->>'entity_label' ILIKE :q
     OR data->>'actor_name' ILIKE :q))
"""


class AuditRepository:
    """SQLAlchemy + PostgreSQL access to the `audit_events` table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # --- write (INSERT only) --------------------------------------------------

    def insert(self, event_id: str, data: dict[str, Any]) -> None:
        """Append one event. Raises on failure; AuditService swallows it so an
        audit write can never break the HR action that triggered it."""
        self._session.execute(
            text(f'INSERT INTO "{TABLE}" (id, data) VALUES (:id, CAST(:data AS JSONB))'),
            {"id": event_id, "data": json.dumps(data)},
        )
        self._session.commit()

    # --- read -----------------------------------------------------------------

    def _filters(
        self,
        *,
        actor: str | None,
        action: str | None,
        date_from: str | None,
        date_to: str | None,
        q: str | None,
    ) -> dict[str, Any]:
        return {
            "actor": actor or None,
            "action": action or None,
            "date_from": date_from or None,
            "date_to": date_to or None,
            "q": f"%{q}%" if q else None,
        }

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        data = row[1] if isinstance(row[1], dict) else json.loads(row[1])
        created = row[2]
        return {**data, "id": row[0], "at": created.isoformat() if created else None}

    def query(
        self,
        *,
        actor: str | None = None,
        action: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        params = self._filters(actor=actor, action=action, date_from=date_from, date_to=date_to, q=q)
        params["limit"] = max(1, min(limit, 200))
        params["offset"] = max(0, offset)
        try:
            rows = self._session.execute(
                text(
                    f'SELECT id, data, created_at FROM "{TABLE}" WHERE {_FILTER} '
                    "ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
                ),
                params,
            ).fetchall()
            return [self._row(r) for r in rows]
        except SQLAlchemyError as exc:
            raise RepositoryError("Failed to read the audit log.") from exc

    def count(
        self,
        *,
        actor: str | None = None,
        action: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        q: str | None = None,
    ) -> int:
        params = self._filters(actor=actor, action=action, date_from=date_from, date_to=date_to, q=q)
        try:
            row = self._session.execute(
                text(f'SELECT count(*) FROM "{TABLE}" WHERE {_FILTER}'), params
            ).fetchone()
            return int(row[0]) if row else 0
        except SQLAlchemyError as exc:
            raise RepositoryError("Failed to count audit events.") from exc

    def summary(self, *, date_from: str | None = None, date_to: str | None = None) -> dict[str, Any]:
        """Per-HR and per-action counts over the given window (both nullable)."""
        params = self._filters(actor=None, action=None, date_from=date_from, date_to=date_to, q=None)
        try:
            by_actor = self._session.execute(
                text(
                    "SELECT data->>'actor_email' AS email, max(data->>'actor_name') AS name, "
                    f'count(*) AS c FROM "{TABLE}" WHERE {_FILTER} '
                    "GROUP BY data->>'actor_email' ORDER BY c DESC"
                ),
                params,
            ).fetchall()
            by_action = self._session.execute(
                text(
                    f"SELECT data->>'action' AS action, count(*) AS c FROM \"{TABLE}\" WHERE {_FILTER} "
                    "GROUP BY data->>'action' ORDER BY c DESC"
                ),
                params,
            ).fetchall()
        except SQLAlchemyError as exc:
            raise RepositoryError("Failed to summarize the audit log.") from exc
        return {
            "total": sum(int(r[2]) for r in by_actor),
            "byActor": [
                {"email": r[0], "name": r[1] or r[0], "count": int(r[2])} for r in by_actor if r[0]
            ],
            "byAction": [{"action": r[0], "count": int(r[1])} for r in by_action if r[0]],
        }
