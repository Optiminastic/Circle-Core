"""In-memory test doubles for the ports services depend on."""

from __future__ import annotations

import copy
from typing import Any

Document = dict[str, Any]


class InMemoryDocumentRepository:
    """Implements app.repositories.base.DocumentRepository with plain dicts.

    Mirrors the SQL repository's semantics: upsert replaces the whole document,
    find matches top-level keys (JSONB containment for flat values) and keeps
    insertion order (the SQL one orders by created_at ascending).
    """

    def __init__(self) -> None:
        self._tables: dict[str, dict[str, Document]] = {}

    def _table(self, table: str) -> dict[str, Document]:
        return self._tables.setdefault(table, {})

    def list(self, table: str, *, limit: int | None = None, offset: int = 0) -> list[Document]:
        rows = [copy.deepcopy(d) for d in self._table(table).values()]
        return rows[offset : offset + limit] if limit is not None else rows[offset:]

    def find(
        self, table: str, match: Document, *, limit: int | None = None, offset: int = 0
    ) -> list[Document]:
        rows = [
            copy.deepcopy(d)
            for d in self._table(table).values()
            if all(d.get(k) == v for k, v in match.items())
        ]
        return rows[offset : offset + limit] if limit is not None else rows[offset:]

    def count(self, table: str, match: Document | None = None) -> int:
        return len(self.find(table, match or {}))

    def get(self, table: str, item_id: str) -> Document | None:
        doc = self._table(table).get(item_id)
        return copy.deepcopy(doc) if doc is not None else None

    def upsert(self, table: str, item_id: str, data: Document) -> Document:
        self._table(table)[item_id] = copy.deepcopy(data)
        return data

    def delete(self, table: str, item_id: str) -> bool:
        return self._table(table).pop(item_id, None) is not None
