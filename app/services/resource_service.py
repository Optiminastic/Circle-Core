"""Resource service — business rules for the generic CRUD lifecycle.

Depends on the DocumentRepository abstraction (DIP). Owns id generation and
not-found semantics so routers stay thin (SRP).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.core.errors import NotFoundError, ValidationError
from app.domain import resource_search
from app.domain.registry import ResourceDef
from app.repositories.base import Document, DocumentRepository

#: Defaults/cap for the paginated list endpoint — see docs/PERFORMANCE_AUDIT.md
#: P0-2 and docs/PERFORMANCE_ROADMAP.md Phase B1.
PAGE_SIZE_DEFAULT = 50
PAGE_SIZE_MAX = 200


@dataclass(frozen=True)
class Page:
    """Matches the `{data, pagination}` contract every paginated list
    endpoint returns — computed once here so the route stays a thin
    pass-through and every consumer gets identical pagination math."""

    data: list[Document]
    page: int
    limit: int
    total: int

    @property
    def total_pages(self) -> int:
        return max(1, -(-self.total // self.limit)) if self.total else 0

    @property
    def has_next_page(self) -> bool:
        return self.page < self.total_pages

    @property
    def has_previous_page(self) -> bool:
        return self.page > 1

    def to_dict(self) -> dict[str, object]:
        return {
            "data": self.data,
            "pagination": {
                "page": self.page,
                "limit": self.limit,
                "total": self.total,
                "totalPages": self.total_pages,
                "hasNextPage": self.has_next_page,
                "hasPreviousPage": self.has_previous_page,
            },
        }


@dataclass(frozen=True)
class CursorPage:
    """The `{data, pagination}` contract for keyset (cursor) pagination —
    a sibling of `Page`'s OFFSET contract, not a replacement. No `total`:
    a keyset page never counts the whole table (that's the point)."""

    data: list[Document]
    next_cursor: str | None
    limit: int

    def to_dict(self) -> dict[str, object]:
        return {
            "data": self.data,
            "pagination": {
                "limit": self.limit,
                "nextCursor": self.next_cursor,
                "hasNextPage": self.next_cursor is not None,
            },
        }


class ResourceService:
    def __init__(self, repository: DocumentRepository) -> None:
        self._repo = repository

    def list(
        self, resource: ResourceDef, *, limit: int | None = None, offset: int = 0
    ) -> list[Document]:
        return self._repo.list(resource.table, limit=limit, offset=offset)

    def list_paginated(
        self,
        resource: ResourceDef,
        *,
        page: int = 1,
        limit: int = PAGE_SIZE_DEFAULT,
        search: str | None = None,
        sort_by: str | None = None,
        sort_order: str = "asc",
        filters: Document | None = None,
    ) -> Page:
        """Database-side pagination/search/sort — see
        docs/PERFORMANCE_AUDIT.md P0-2. `sort_by` is checked against this
        resource's own allowlist (`app.domain.resource_search`) and silently
        falls back to the default `createdAt` sort on anything not on it —
        never executed unvalidated, and never a 400 for a frontend that
        passes a slightly stale sort key after a rename."""
        page = max(1, page)
        limit = max(1, min(limit, PAGE_SIZE_MAX))
        search_fields = resource_search.search_fields_for(resource.slug)
        allowed_sorts = resource_search.sortable_fields_for(resource.slug)
        sort_field = sort_by if sort_by in allowed_sorts else resource_search.DEFAULT_SORT_FIELD
        sort_desc = str(sort_order).lower() == "desc"
        data, total = self._repo.list_paginated(
            resource.table,
            page=page,
            limit=limit,
            filters=filters,
            search=search if (search and search_fields) else None,
            search_fields=search_fields,
            sort_field=sort_field,
            sort_desc=sort_desc,
        )
        return Page(data=data, page=page, limit=limit, total=total)

    def list_cursor_paginated(
        self,
        resource: ResourceDef,
        *,
        limit: int = PAGE_SIZE_DEFAULT,
        cursor: str | None = None,
        search: str | None = None,
    ) -> CursorPage:
        """Keyset pagination — see docs/PERFORMANCE_IMPLEMENTATION_LOG.md
        Phase 3. Always newest-first (`sort_desc=True`); unlike
        `list_paginated` there's no arbitrary sort field to validate."""
        limit = max(1, min(limit, PAGE_SIZE_MAX))
        search_fields = resource_search.search_fields_for(resource.slug)
        try:
            data, next_cursor = self._repo.list_cursor_paginated(
                resource.table,
                limit=limit,
                cursor=cursor,
                search=search if (search and search_fields) else None,
                search_fields=search_fields,
                sort_desc=True,
            )
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        return CursorPage(data=data, next_cursor=next_cursor, limit=limit)

    def find(
        self, resource: ResourceDef, match: Document, *, limit: int | None = None, offset: int = 0
    ) -> list[Document]:
        """Filtered list via indexed JSONB containment — see
        DocumentRepository.find. Prefer this over `list()` + a Python filter
        whenever the match is a simple field equality (see
        docs/PERFORMANCE_AUDIT.md P1-4)."""
        return self._repo.find(resource.table, match, limit=limit, offset=offset)

    def get(self, resource: ResourceDef, item_id: str) -> Document:
        item = self._repo.get(resource.table, item_id)
        if item is None:
            raise NotFoundError(f"{resource.slug} '{item_id}' not found")
        return item

    def create(self, resource: ResourceDef, payload: Document) -> Document:
        item_id = str(payload.get(resource.id_field) or uuid.uuid4().hex[:8])
        payload[resource.id_field] = item_id
        return self._repo.upsert(resource.table, item_id, payload)

    def replace(self, resource: ResourceDef, item_id: str, payload: Document) -> Document:
        payload[resource.id_field] = item_id
        return self._repo.upsert(resource.table, item_id, payload)

    def patch(self, resource: ResourceDef, item_id: str, changes: Document) -> Document:
        existing = self.get(resource, item_id)
        existing.update(changes)
        return self._repo.upsert(resource.table, item_id, existing)

    def delete(self, resource: ResourceDef, item_id: str) -> None:
        if not self._repo.delete(resource.table, item_id):
            raise NotFoundError(f"{resource.slug} '{item_id}' not found")
