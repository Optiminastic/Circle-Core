"""Document links: only public-category files open without a login.

Needs CIRCLE_TEST_DATABASE_URL (a disposable PostgreSQL), like the outbox tests.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

TEST_DB_URL = os.environ.get("CIRCLE_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not TEST_DB_URL, reason="CIRCLE_TEST_DATABASE_URL not set")

SESSION_SECRET = "test-session-secret-for-documents"


class _FakeStorage:
    def get(self, key: str) -> tuple[bytes, str]:
        return b"%PDF-1.4 test", "application/pdf"

    def presigned_url(self, key: str, expires: int = 900, **_: Any) -> str:
        return f"https://bucket.invalid/{key}"

    def put(self, key: str, data: bytes, content_type: str) -> None:
        pass

    def delete(self, key: str) -> None:
        pass


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    from fastapi.testclient import TestClient

    from app.api.dependencies import get_storage
    from app.core.config import get_settings

    monkeypatch.setenv("DATABASE_URL", TEST_DB_URL)
    monkeypatch.setenv("SESSION_SECRET", SESSION_SECRET)
    monkeypatch.setenv("IDSYNC_PUSH_URL", "")
    get_settings.cache_clear()
    from app.main import create_app

    app = create_app()
    app.dependency_overrides[get_storage] = lambda: _FakeStorage()
    with TestClient(app) as c:
        db = app.state.database
        from app.repositories.document_repository import SqlAlchemyDocumentRepository

        with db.session() as session:
            repo = SqlAlchemyDocumentRepository(session)
            for doc_id, category in (("docresume001", "resume"), ("docaadhaar01", "Aadhaar card"),
                                     ("dochandover1", "handover"), ("docgeneral01", "document"),
                                     ("docbrief0001", "assignment-brief")):
                repo.upsert("documents", doc_id, {
                    "id": doc_id, "entityType": "employee", "entityId": "EMP-1",
                    "category": category, "fileName": f"{doc_id}.pdf",
                    "contentType": "application/pdf", "storageKey": f"k/{doc_id}",
                })
        yield c
    get_settings.cache_clear()


def _login(client: Any) -> None:
    from app.core.config import get_settings
    from app.services.sessions import COOKIE_NAME, issue_session

    token = issue_session(get_settings(), email="hr@corp.io", role="hr", name="HR")
    client.cookies.set(COOKIE_NAME, token)


def test_upload_needs_a_login(client: Any) -> None:
    resp = client.post(
        "/api/documents",
        data={"entityType": "employee", "entityId": "EMP-1", "category": "Payslip"},
        files={"file": ("x.pdf", b"%PDF", "application/pdf")},
    )
    assert resp.status_code == 401


@pytest.mark.parametrize("doc_id", ["docresume001", "dochandover1", "docbrief0001"])
@pytest.mark.parametrize("endpoint", ["preview", "url"])
def test_public_categories_open_without_login(client: Any, doc_id: str, endpoint: str) -> None:
    assert client.get(f"/api/documents/{doc_id}/{endpoint}").status_code == 200


@pytest.mark.parametrize("doc_id", ["docaadhaar01", "docgeneral01"])
@pytest.mark.parametrize("endpoint", ["preview", "url"])
def test_sensitive_documents_need_login(client: Any, doc_id: str, endpoint: str) -> None:
    assert client.get(f"/api/documents/{doc_id}/{endpoint}").status_code == 401


@pytest.mark.parametrize("endpoint", ["preview", "url"])
def test_signed_in_hr_can_open_sensitive_documents(client: Any, endpoint: str) -> None:
    _login(client)
    assert client.get(f"/api/documents/docaadhaar01/{endpoint}").status_code == 200
