"""PostgreSQL JSONB implementation of DocumentRepository.

Each resource is a table of (id, data JSONB). Storing the nested HR documents as
JSONB keeps the flexible shape of the frontend models while remaining queryable
and indexable in Postgres. All DB errors are normalized to RepositoryError so
callers never see raw driver exceptions (error tolerance).
"""

from __future__ import annotations

import json
from typing import Any, Callable, TypeVar

from sqlalchemy import text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

T = TypeVar("T")

from app.core.errors import RepositoryError
from app.core.logging import get_logger
from app.repositories.base import Document

logger = get_logger("curcle.repository")


def _as_dict(value: Any) -> Document:
    # psycopg returns JSONB as dict, but be defensive across drivers.
    if isinstance(value, str):
        return json.loads(value)
    return value


class SqlAlchemyDocumentRepository:
    """DocumentRepository backed by SQLAlchemy + PostgreSQL JSONB."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def _run(self, op: Callable[[], T]) -> T:
        """Execute, retrying once on a stale pooled connection.

        With pool_pre_ping disabled (latency), a connection killed by the
        server/network surfaces as OperationalError on first use. SQLAlchemy
        invalidates the dead connection, so a single retry gets a fresh one.
        """
        try:
            return op()
        except OperationalError:
            self._session.rollback()
            return op()

    def list(self, table: str, *, limit: int | None = None, offset: int = 0) -> list[Document]:
        try:
            sql = f'SELECT data FROM "{table}" ORDER BY created_at ASC'
            params: dict[str, Any] = {}
            if limit is not None:
                sql += " LIMIT :limit OFFSET :offset"
                params = {"limit": limit, "offset": max(0, offset)}
            rows = self._run(lambda: self._session.execute(text(sql), params).fetchall())
            return [_as_dict(row[0]) for row in rows]
        except SQLAlchemyError as exc:
            raise RepositoryError(f"Failed to list '{table}'") from exc

    def find(
        self, table: str, match: Document, *, limit: int | None = None, offset: int = 0
    ) -> list[Document]:
        """Filtered list via indexed JSONB containment (uses the GIN index)."""
        try:
            sql = (
                f'SELECT data FROM "{table}" WHERE data @> CAST(:match AS JSONB) '
                "ORDER BY created_at ASC"
            )
            params: dict[str, Any] = {"match": json.dumps(match)}
            if limit is not None:
                sql += " LIMIT :limit OFFSET :offset"
                params["limit"] = limit
                params["offset"] = max(0, offset)
            rows = self._run(lambda: self._session.execute(text(sql), params).fetchall())
            return [_as_dict(row[0]) for row in rows]
        except SQLAlchemyError as exc:
            raise RepositoryError(f"Failed to query '{table}'") from exc

    def find_text_ci(self, table: str, field: str, value: str) -> list[Document]:
        """Rows whose top-level text `field` equals `value` ignoring case and
        surrounding spaces (e.g. an email typed with capitals)."""
        try:
            sql = (
                f'SELECT data FROM "{table}" WHERE lower(trim(data->>:field)) = lower(trim(:value)) '
                "ORDER BY created_at ASC"
            )
            params = {"field": field, "value": value}
            rows = self._run(lambda: self._session.execute(text(sql), params).fetchall())
            return [_as_dict(row[0]) for row in rows]
        except SQLAlchemyError as exc:
            raise RepositoryError(f"Failed to query '{table}'") from exc

    def count(self, table: str, match: Document | None = None) -> int:
        try:
            if match:
                row = self._run(
                    lambda: self._session.execute(
                        text(f'SELECT count(*) FROM "{table}" WHERE data @> CAST(:match AS JSONB)'),
                        {"match": json.dumps(match)},
                    ).fetchone()
                )
            else:
                row = self._run(
                    lambda: self._session.execute(text(f'SELECT count(*) FROM "{table}"')).fetchone()
                )
            return int(row[0]) if row else 0
        except SQLAlchemyError as exc:
            raise RepositoryError(f"Failed to count '{table}'") from exc

    def get(self, table: str, item_id: str) -> Document | None:
        try:
            row = self._run(
                lambda: self._session.execute(
                    text(f'SELECT data FROM "{table}" WHERE id = :id'), {"id": item_id}
                ).fetchone()
            )
            return _as_dict(row[0]) if row else None
        except SQLAlchemyError as exc:
            raise RepositoryError(f"Failed to read '{table}/{item_id}'") from exc

    def upsert(self, table: str, item_id: str, data: Document) -> Document:
        try:
            self._run(
                lambda: self._session.execute(
                    text(
                        f'INSERT INTO "{table}" (id, data) VALUES (:id, CAST(:data AS JSONB)) '
                        "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data, updated_at = now()"
                    ),
                    {"id": item_id, "data": json.dumps(data)},
                )
            )
            self._session.commit()
            return data
        except SQLAlchemyError as exc:
            self._session.rollback()
            raise RepositoryError(f"Failed to save '{table}/{item_id}'") from exc

    def delete(self, table: str, item_id: str) -> bool:
        try:
            result = self._run(
                lambda: self._session.execute(
                    text(f'DELETE FROM "{table}" WHERE id = :id'), {"id": item_id}
                )
            )
            self._session.commit()
            return result.rowcount > 0
        except SQLAlchemyError as exc:
            self._session.rollback()
            raise RepositoryError(f"Failed to delete '{table}/{item_id}'") from exc
