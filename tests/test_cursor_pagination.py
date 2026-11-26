"""SqlAlchemyDocumentRepository.list_cursor_paginated — keyset pagination
(Phase 3). Needs a real Postgres (CIRCLE_TEST_DATABASE_URL), like the other
DB-backed suites; the SQL depends on real tuple comparisons and timestamp
casting that can't be faked. Manually verified live against the dev
database (zero overlap/missing rows across pages, malformed-cursor
rejection) before this file was written — see
docs/PERFORMANCE_IMPLEMENTATION_LOG.md Phase 3.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.repositories.document_repository import SqlAlchemyDocumentRepository

TEST_DB_URL = os.environ.get("CIRCLE_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not TEST_DB_URL, reason="CIRCLE_TEST_DATABASE_URL not set")

_TABLE = "test_cursor_items"


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine(TEST_DB_URL, isolation_level="AUTOCOMMIT")
    with engine.begin() as conn:
        conn.execute(
            text(
                f"""
                CREATE TABLE IF NOT EXISTS "{_TABLE}" (
                    id TEXT PRIMARY KEY,
                    data JSONB NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        )
    factory = sessionmaker(bind=engine, future=True)
    s = factory()
    try:
        s.execute(text(f'TRUNCATE TABLE "{_TABLE}"'))
        yield s
    finally:
        s.close()
        engine.dispose()


def _seed(session: Session, n: int) -> list[str]:
    """Inserts `n` rows with strictly increasing created_at so ordering is
    deterministic (a tiny sleep per row - fine for a handful of test rows,
    never done in production code)."""
    ids = []
    for i in range(n):
        item_id = f"item-{i:03d}"
        session.execute(
            text(f'INSERT INTO "{_TABLE}" (id, data) VALUES (:id, CAST(:data AS JSONB))'),
            {"id": item_id, "data": f'{{"name": "Item {i}"}}'},
        )
        session.commit()
        ids.append(item_id)
        time.sleep(0.005)
    return ids


def test_first_page_with_no_cursor_returns_the_newest_rows(session: Session) -> None:
    ids = _seed(session, 7)
    repo = SqlAlchemyDocumentRepository(session)
    data, next_cursor = repo.list_cursor_paginated(_TABLE, limit=3)
    assert [d["name"] for d in data] == ["Item 6", "Item 5", "Item 4"]  # newest-first
    assert next_cursor is not None


def test_walking_every_page_via_cursor_covers_all_rows_with_no_overlap(session: Session) -> None:
    _seed(session, 10)
    repo = SqlAlchemyDocumentRepository(session)

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):  # safety bound - must terminate well before this
        data, cursor = repo.list_cursor_paginated(_TABLE, limit=4, cursor=cursor)
        seen.extend(d["name"] for d in data)
        if cursor is None:
            break

    assert len(seen) == 10
    assert len(set(seen)) == 10  # no duplicates across pages
    assert cursor is None  # the walk terminated


def test_last_page_has_no_next_cursor(session: Session) -> None:
    _seed(session, 3)
    repo = SqlAlchemyDocumentRepository(session)
    data, next_cursor = repo.list_cursor_paginated(_TABLE, limit=10)
    assert len(data) == 3
    assert next_cursor is None


def test_a_new_row_inserted_between_page_fetches_does_not_shift_already_seen_rows(
    session: Session,
) -> None:
    """The exact case OFFSET pagination gets wrong: a new row arrives at the
    front of the (newest-first) ordering between two page fetches. A keyset
    cursor is anchored to a specific row, not a row COUNT, so it's immune."""
    _seed(session, 4)
    repo = SqlAlchemyDocumentRepository(session)
    page1, cursor = repo.list_cursor_paginated(_TABLE, limit=2)
    assert [d["name"] for d in page1] == ["Item 3", "Item 2"]

    # A new row is inserted, shifting what WOULD be at OFFSET 2 if this were
    # OFFSET pagination.
    session.execute(
        text(f'INSERT INTO "{_TABLE}" (id, data) VALUES (:id, CAST(:data AS JSONB))'),
        {"id": "item-new", "data": '{"name": "Item New"}'},
    )
    session.commit()

    page2, _ = repo.list_cursor_paginated(_TABLE, limit=2, cursor=cursor)
    # Still exactly Item 1, Item 0 - the new row (newer than the cursor) never
    # appears on a page anchored strictly before it.
    assert [d["name"] for d in page2] == ["Item 1", "Item 0"]


def test_malformed_cursor_raises_value_error(session: Session) -> None:
    repo = SqlAlchemyDocumentRepository(session)
    with pytest.raises(ValueError):
        repo.list_cursor_paginated(_TABLE, limit=5, cursor="not-a-real-cursor")


def test_search_filters_within_cursor_pagination(session: Session) -> None:
    repo = SqlAlchemyDocumentRepository(session)
    session.execute(
        text(f'INSERT INTO "{_TABLE}" (id, data) VALUES (:id, CAST(:data AS JSONB))'),
        {"id": "a", "data": '{"name": "Alpha"}'},
    )
    session.execute(
        text(f'INSERT INTO "{_TABLE}" (id, data) VALUES (:id, CAST(:data AS JSONB))'),
        {"id": "b", "data": '{"name": "Beta"}'},
    )
    session.commit()

    data, _ = repo.list_cursor_paginated(_TABLE, limit=10, search="alph", search_fields=("name",))
    assert [d["name"] for d in data] == ["Alpha"]
