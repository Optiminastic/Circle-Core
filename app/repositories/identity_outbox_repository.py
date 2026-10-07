"""Durable queue of employee changes waiting to be pushed to id-sync.

One row per employee, holding the LATEST payload: a second change before the
first is delivered replaces it (id-sync's push is an idempotent upsert, so only
the newest state matters). `version` bumps on every enqueue so a delivery that
raced a newer change never deletes the newer row, and `in_flight` keeps a newer
change from being sent while an older one is still on the wire (which could
land second and overwrite it).

The engine runs in AUTOCOMMIT, so every method is a single statement; claiming
uses `FOR UPDATE SKIP LOCKED` inside one UPDATE so two API workers never send
the same row at once.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

TABLE = "identity_outbox"

TABLE_DDL = f"""
CREATE TABLE IF NOT EXISTS "{TABLE}" (
    employee_id     TEXT PRIMARY KEY,
    payload         JSONB NOT NULL,
    version         BIGINT NOT NULL DEFAULT 1,
    attempts        INTEGER NOT NULL DEFAULT 0,
    in_flight       BOOLEAN NOT NULL DEFAULT false,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error      TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""
INDEX_DDL = f'CREATE INDEX IF NOT EXISTS "ix_{TABLE}_due" ON "{TABLE}" (next_attempt_at)'

# Errors are stored for debugging only; keep a bounded slice.
_MAX_ERROR_CHARS = 500


@dataclass(frozen=True)
class OutboxItem:
    employee_id: str
    payload: dict[str, Any]
    version: int
    attempts: int


class IdentityOutboxRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def enqueue(self, employee_id: str, payload: dict[str, Any]) -> None:
        self._session.execute(
            text(
                f"""
                INSERT INTO "{TABLE}" (employee_id, payload)
                VALUES (:employee_id, CAST(:payload AS JSONB))
                ON CONFLICT (employee_id) DO UPDATE SET
                    payload = EXCLUDED.payload,
                    version = "{TABLE}".version + 1,
                    attempts = 0,
                    -- While an older version is being sent, keep its lease:
                    -- the newer one goes out right after (mark_delivered).
                    next_attempt_at = CASE WHEN "{TABLE}".in_flight
                                           THEN "{TABLE}".next_attempt_at ELSE now() END,
                    last_error = NULL,
                    updated_at = now()
                """
            ),
            {"employee_id": employee_id, "payload": json.dumps(payload)},
        )

    def claim_due(self, *, limit: int, lease_seconds: int) -> list[OutboxItem]:
        """Take up to `limit` due rows and hide them for `lease_seconds`, so a
        crash mid-delivery only delays the row instead of losing it."""
        rows = self._session.execute(
            text(
                f"""
                UPDATE "{TABLE}"
                   SET next_attempt_at = now() + make_interval(secs => :lease),
                       in_flight = true
                 WHERE employee_id IN (
                        SELECT employee_id FROM "{TABLE}"
                         WHERE next_attempt_at <= now()
                         ORDER BY next_attempt_at
                         LIMIT :limit
                         FOR UPDATE SKIP LOCKED)
                RETURNING employee_id, payload, version, attempts
                """
            ),
            {"limit": limit, "lease": lease_seconds},
        ).fetchall()
        return [OutboxItem(r.employee_id, r.payload, r.version, r.attempts) for r in rows]

    def mark_delivered(self, item: OutboxItem) -> None:
        deleted = self._session.execute(
            text(f'DELETE FROM "{TABLE}" WHERE employee_id = :id AND version = :version'),
            {"id": item.employee_id, "version": item.version},
        )
        if deleted.rowcount == 0:
            # A newer change arrived while this one was on the wire: send it now.
            self._session.execute(
                text(
                    f'UPDATE "{TABLE}" SET in_flight = false, next_attempt_at = now() '
                    "WHERE employee_id = :id"
                ),
                {"id": item.employee_id},
            )

    def mark_failed(self, item: OutboxItem, *, error: str, retry_in_seconds: int) -> None:
        self._session.execute(
            text(
                f"""
                UPDATE "{TABLE}"
                   SET attempts = attempts + 1,
                       in_flight = false,
                       next_attempt_at = now() + make_interval(secs => :delay),
                       last_error = :error,
                       updated_at = now()
                 WHERE employee_id = :id AND version = :version
                """
            ),
            {
                "id": item.employee_id,
                "version": item.version,
                "delay": retry_in_seconds,
                "error": error[:_MAX_ERROR_CHARS],
            },
        )
        # If a newer version replaced this one meanwhile, release it to go now.
        self._session.execute(
            text(
                f'UPDATE "{TABLE}" SET in_flight = false, next_attempt_at = now() '
                "WHERE employee_id = :id AND version <> :version AND in_flight"
            ),
            {"id": item.employee_id, "version": item.version},
        )
