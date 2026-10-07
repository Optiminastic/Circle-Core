"""Repository abstraction (Dependency Inversion + Interface Segregation).

Services depend on this Protocol, not on SQLAlchemy. Swapping the storage engine
(e.g. to Mongo, or an in-memory fake for tests) means providing another
implementation — no service/route changes.
"""

from __future__ import annotations

from typing import Any, Protocol

Document = dict[str, Any]


class DocumentRepository(Protocol):
    def list(self, table: str, *, limit: int | None = None, offset: int = 0) -> list[Document]: ...

    def find(
        self, table: str, match: Document, *, limit: int | None = None, offset: int = 0
    ) -> list[Document]: ...

    def find_text_ci(self, table: str, field: str, value: str) -> list[Document]: ...

    def count(self, table: str, match: Document | None = None) -> int: ...

    def get(self, table: str, item_id: str) -> Document | None: ...

    def upsert(self, table: str, item_id: str, data: Document) -> Document: ...

    def delete(self, table: str, item_id: str) -> bool: ...
