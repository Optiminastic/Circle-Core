"""Regression test for a production finding (2026-11-27, found while auditing
every live endpoint's response time): GET /api/documents/{doc_id}/preview
took 800ms-1.7s per call (the S3 get_object + full-body download), and
depended on `repo: DocumentRepository = Depends(get_repository)` - which
holds one pooled DB connection open for FastAPI's whole request-dependency
lifecycle, i.e. across that entire slow S3 fetch, even though the DB is
only touched once, to look the document up, before the fetch starts.

Fixed with `database.repository()` (app/db/database.py), the same
short-lived-session helper already used by bgv_ongrid.sync_ongrid_status
for an identical reason (see docs/PERFORMANCE_AUDIT.md P1-1): open a
session, do the one lookup, close it, THEN do the slow external work.

This test calls `preview_document` directly (bypassing FastAPI's DI, same
approach as the worker/scheduler connection-lifecycle tests) with a fake
Database that logs session open/close, and a fake FileStorage that logs
when it's invoked - proving the session is closed before storage.get()
runs.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from app.api.routes.documents import preview_document


class _FakeRepo:
    def __init__(self, log: list[str], doc: dict[str, Any]) -> None:
        self._log = log
        self._doc = doc

    def get(self, table: str, doc_id: str) -> dict[str, Any] | None:
        self._log.append("repo:get")
        return self._doc


class _FakeDatabase:
    def __init__(self, log: list[str], doc: dict[str, Any]) -> None:
        self._log = log
        self._doc = doc

    @contextmanager
    def repository(self) -> Iterator[_FakeRepo]:
        self._log.append("session:open")
        try:
            yield _FakeRepo(self._log, self._doc)
        finally:
            self._log.append("session:close")


class _FakeStorage:
    def __init__(self, log: list[str]) -> None:
        self._log = log

    def get(self, key: str) -> tuple[bytes, str]:
        self._log.append("storage:get")
        return b"file-bytes", "application/pdf"


class _FakeRequest:
    cookies: dict[str, str] = {}


def test_the_db_session_closes_before_the_slow_storage_fetch_runs() -> None:
    log: list[str] = []
    doc = {"id": "doc1", "category": "resume", "storageKey": "k1", "fileName": "r.pdf"}
    database = _FakeDatabase(log, doc)
    storage = _FakeStorage(log)

    response = preview_document(
        "doc1",
        _FakeRequest(),
        settings=Any,  # type: ignore[arg-type]
        database=database,  # type: ignore[arg-type]
        storage=storage,  # type: ignore[arg-type]
    )

    assert response.status_code == 200
    # The lookup's session opens and closes BEFORE the slow S3 fetch runs -
    # never held open across it.
    assert log == ["session:open", "repo:get", "session:close", "storage:get"]
