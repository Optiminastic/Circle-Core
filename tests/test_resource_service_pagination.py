"""ResourceService.list_paginated: the validation that happens BEFORE any SQL
is built — page/limit clamping and the sort-field allowlist fallback. The
actual SQL (document_repository.list_paginated) needs a real Postgres and is
covered by the DB-backed suite (CIRCLE_TEST_DATABASE_URL); this is the part
that's pure logic and needs no database at all.
"""

from __future__ import annotations

from typing import Any

from app.domain.registry import ResourceDef
from app.services.resource_service import PAGE_SIZE_DEFAULT, PAGE_SIZE_MAX, ResourceService

CANDIDATES = ResourceDef("candidates", "candidates", "id")


class _Repo:
    """Records exactly what list_paginated was called with, so the test can
    assert on the validated/clamped values without needing a database."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def list_paginated(
        self,
        table: str,
        *,
        page: int,
        limit: int,
        filters: dict[str, Any] | None = None,
        search: str | None = None,
        search_fields: tuple[str, ...] = (),
        sort_field: str = "createdAt",
        sort_desc: bool = False,
    ) -> tuple[list[dict[str, Any]], int]:
        self.calls.append(
            dict(
                table=table,
                page=page,
                limit=limit,
                filters=filters,
                search=search,
                search_fields=search_fields,
                sort_field=sort_field,
                sort_desc=sort_desc,
            )
        )
        return [], 0


def test_page_below_one_is_clamped_to_one() -> None:
    repo = _Repo()
    ResourceService(repo).list_paginated(CANDIDATES, page=0)
    assert repo.calls[0]["page"] == 1

    repo2 = _Repo()
    ResourceService(repo2).list_paginated(CANDIDATES, page=-5)
    assert repo2.calls[0]["page"] == 1


def test_limit_defaults_when_not_given() -> None:
    repo = _Repo()
    ResourceService(repo).list_paginated(CANDIDATES)
    assert repo.calls[0]["limit"] == PAGE_SIZE_DEFAULT


def test_limit_is_capped_at_the_maximum() -> None:
    repo = _Repo()
    ResourceService(repo).list_paginated(CANDIDATES, limit=100_000)
    assert repo.calls[0]["limit"] == PAGE_SIZE_MAX


def test_limit_below_one_is_clamped_to_one_not_zero_or_negative() -> None:
    repo = _Repo()
    ResourceService(repo).list_paginated(CANDIDATES, limit=0)
    assert repo.calls[0]["limit"] == 1

    repo2 = _Repo()
    ResourceService(repo2).list_paginated(CANDIDATES, limit=-10)
    assert repo2.calls[0]["limit"] == 1


def test_an_allowed_sort_field_is_passed_through() -> None:
    repo = _Repo()
    ResourceService(repo).list_paginated(CANDIDATES, sort_by="fullName")
    assert repo.calls[0]["sort_field"] == "fullName"


def test_an_unknown_sort_field_falls_back_to_created_at_never_executed_raw() -> None:
    # A stale/typo'd sortBy from the frontend must never reach SQL unchecked -
    # see docs/PERFORMANCE_AUDIT.md "Avoid arbitrary SQL construction".
    repo = _Repo()
    ResourceService(repo).list_paginated(CANDIDATES, sort_by="'; DROP TABLE candidates; --")
    assert repo.calls[0]["sort_field"] == "createdAt"


def test_sort_order_desc_is_case_insensitive() -> None:
    repo = _Repo()
    ResourceService(repo).list_paginated(CANDIDATES, sort_order="DESC")
    assert repo.calls[0]["sort_desc"] is True


def test_sort_order_anything_else_is_ascending() -> None:
    repo = _Repo()
    ResourceService(repo).list_paginated(CANDIDATES, sort_order="sideways")
    assert repo.calls[0]["sort_desc"] is False


def test_search_is_dropped_when_the_resource_has_no_search_fields_configured() -> None:
    no_search_resource = ResourceDef("assets", "assets", "id")
    repo = _Repo()
    ResourceService(repo).list_paginated(no_search_resource, search="laptop")
    assert repo.calls[0]["search"] is None
    assert repo.calls[0]["search_fields"] == ()


def test_search_is_kept_when_the_resource_has_search_fields_configured() -> None:
    repo = _Repo()
    ResourceService(repo).list_paginated(CANDIDATES, search="jane")
    assert repo.calls[0]["search"] == "jane"
    assert "fullName" in repo.calls[0]["search_fields"]


def test_result_pagination_math() -> None:
    class _RepoWithData:
        def list_paginated(self, table: str, *, page: int, limit: int, **kwargs: Any) -> tuple[list[dict], int]:
            return [{"id": "1"}, {"id": "2"}], 125

    result = ResourceService(_RepoWithData()).list_paginated(CANDIDATES, page=2, limit=50)
    assert result.total == 125
    assert result.total_pages == 3
    assert result.has_next_page is True
    assert result.has_previous_page is True

    as_dict = result.to_dict()
    assert as_dict["pagination"] == {
        "page": 2,
        "limit": 50,
        "total": 125,
        "totalPages": 3,
        "hasNextPage": True,
        "hasPreviousPage": True,
    }


def test_empty_result_has_zero_total_pages_and_no_next_page() -> None:
    repo = _Repo()
    result = ResourceService(repo).list_paginated(CANDIDATES)
    assert result.total == 0
    assert result.total_pages == 0
    assert result.has_next_page is False
    assert result.has_previous_page is False
