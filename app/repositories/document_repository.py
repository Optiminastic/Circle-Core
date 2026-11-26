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

    def list_paginated(
        self,
        table: str,
        *,
        page: int,
        limit: int,
        filters: Document | None = None,
        search: str | None = None,
        search_fields: tuple[str, ...] = (),
        sort_field: str = "createdAt",
        sort_desc: bool = False,
    ) -> tuple[list[Document], int]:
        """Database-side paginated list: filtering, free-text search, and
        sorting all happen in SQL — never fetch-everything-then-slice-in-Python.
        See docs/PERFORMANCE_AUDIT.md P0-2.

        Every identifier that varies per call (the search field names, the
        sort field) is passed as a genuine bind parameter, never spliced into
        the SQL text, even though `sort_field`/`search_fields` are validated
        against a per-resource allowlist one layer up (ResourceService) —
        `data ->> :key` accepts a bound parameter for the JSONB key just as
        readily as a literal, so there is no reason to trust the allowlist
        alone. `page`/`limit` are already-validated plain ints, not strings.
        `sort_desc`/the ASC-vs-DESC choice is a Python bool, not a spliced
        string, since direction can't be parameterized in SQL. Only the ONE
        truly unparameterizable piece is the real `created_at` column name
        for the one magic sort field — a hardcoded literal, never user input.
        """
        try:
            where_clauses: list[str] = []
            params: dict[str, Any] = {}

            if filters:
                where_clauses.append("data @> CAST(:filters AS JSONB)")
                params["filters"] = json.dumps(filters)

            if search and search_fields:
                ors = []
                for i, field in enumerate(search_fields):
                    key = f"search_field_{i}"
                    params[key] = field
                    ors.append(f"(data ->> :{key}) ILIKE :search_term")
                where_clauses.append("(" + " OR ".join(ors) + ")")
                params["search_term"] = f"%{search}%"

            where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

            if sort_field == "createdAt":
                order_expr = "created_at"
            else:
                order_expr = "(data ->> :sort_field)"
                params["sort_field"] = sort_field
            direction = "DESC" if sort_desc else "ASC"
            # `id` is a stable tie-breaker so pagination never skips/repeats a
            # row when the sort field has duplicate values across pages.
            # NULLS LAST regardless of direction: a row missing the sort
            # field entirely must never masquerade as the "newest"/"first"
            # one just because Postgres's default null-ordering for DESC is
            # NULLS FIRST.
            order_sql = f"ORDER BY {order_expr} {direction} NULLS LAST, id ASC"

            count_row = self._run(
                lambda: self._session.execute(
                    text(f'SELECT count(*) FROM "{table}" {where_sql}'), dict(params)
                ).fetchone()
            )
            total = int(count_row[0]) if count_row else 0

            list_params = dict(params)
            list_params["limit"] = limit
            list_params["offset"] = max(0, (page - 1) * limit)
            rows = self._run(
                lambda: self._session.execute(
                    text(f'SELECT data FROM "{table}" {where_sql} {order_sql} LIMIT :limit OFFSET :offset'),
                    list_params,
                ).fetchall()
            )
            return [_as_dict(row[0]) for row in rows], total
        except SQLAlchemyError as exc:
            raise RepositoryError(f"Failed to paginate '{table}'") from exc

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

    def get_many(self, table: str, item_ids: list[str]) -> dict[str, Document]:
        """Batched PK lookup — one query instead of one `get()` per id.

        See docs/PERFORMANCE_AUDIT.md P1-5: calendar.py's cleanup cascade
        used to call `get()` once per event in a loop."""
        if not item_ids:
            return {}
        try:
            rows = self._run(
                lambda: self._session.execute(
                    text(f'SELECT id, data FROM "{table}" WHERE id = ANY(:ids)'),
                    {"ids": item_ids},
                ).fetchall()
            )
            return {row[0]: _as_dict(row[1]) for row in rows}
        except SQLAlchemyError as exc:
            raise RepositoryError(f"Failed to batch-read '{table}'") from exc

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

    def delete_many(self, table: str, item_ids: list[str]) -> int:
        """Batched delete — one query instead of one `delete()` per id. See
        docs/PERFORMANCE_AUDIT.md P1-5 (calendar.py's cleanup cascade)."""
        if not item_ids:
            return 0
        try:
            result = self._run(
                lambda: self._session.execute(
                    text(f'DELETE FROM "{table}" WHERE id = ANY(:ids)'), {"ids": item_ids}
                )
            )
            self._session.commit()
            return result.rowcount
        except SQLAlchemyError as exc:
            self._session.rollback()
            raise RepositoryError(f"Failed to batch-delete '{table}'") from exc
