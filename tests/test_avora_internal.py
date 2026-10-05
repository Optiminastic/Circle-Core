"""Avora's server-to-server reads: secret handling, mapping, and that one
employee's request can never reach another employee's files."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

from app.services.avora_export import compensation_for, parse_annual_ctc

TEST_DB_URL = os.environ.get("CIRCLE_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(not TEST_DB_URL, reason="CIRCLE_TEST_DATABASE_URL not set")
AVORA_SECRET = "test-avora-secret"


@pytest.mark.parametrize(
    ("text", "expected"),
    [("12 LPA", 1_200_000), ("12.5 lakh", 1_250_000), ("1,80,000", 180_000), ("180000", 180_000),
     ("", None), (None, None), ("TBD", None), ("0", None)],
)
def test_parse_annual_ctc(text: Any, expected: int | None) -> None:
    assert parse_annual_ctc(text) == expected


def test_compensation_mapping() -> None:
    out = compensation_for({
        "id": "EMP-1", "annualCtc": "6 LPA", "joiningDate": "2026-08-21",
        "ctcBreakdown": {"basic": 15000, "employerPf": 1800, "employeePf": 1800},
        "personalDetails": {"bankName": "HDFC", "accountNumber": " 123456789 ", "ifsc": "HDFC0001234",
                            "panNumber": "ABCDE1234F", "aadhaarNumber": "1234"},
    })
    assert out == {
        "employee_code": "EMP-1", "annual_ctc_text": "6 LPA", "annual_ctc_inr": 600_000,
        "pf_enabled": True, "joining_date": "2026-08-21",
        "bank": {"bank_name": "HDFC", "account_number": "123456789", "ifsc_code": "HDFC0001234"},
    }


def test_pf_unknown_without_a_breakdown() -> None:
    assert compensation_for({"id": "EMP-1"})["pf_enabled"] is None


class _FakeStorage:
    def get(self, key: str) -> tuple[bytes, str]:
        return f"bytes of {key}".encode(), "application/pdf"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    from fastapi.testclient import TestClient

    from app.api.dependencies import get_storage
    from app.core.config import get_settings
    from app.repositories.document_repository import SqlAlchemyDocumentRepository

    monkeypatch.setenv("DATABASE_URL", TEST_DB_URL)
    monkeypatch.setenv("AVORA_API_SECRET", AVORA_SECRET)
    monkeypatch.setenv("IDSYNC_PUSH_URL", "")
    get_settings.cache_clear()
    from app.main import create_app

    app = create_app()
    app.dependency_overrides[get_storage] = lambda: _FakeStorage()
    with TestClient(app) as c:
        with app.state.database.session() as session:
            repo = SqlAlchemyDocumentRepository(session)
            for code, email in (("EMP-701", "Asha@Corp.io"), ("EMP-702", "ravi@corp.io")):
                repo.upsert("employees", code, {"id": code, "email": email, "annualCtc": "6 LPA"})
            for doc_id, owner, category in (("avdoc000001", "EMP-701", "Aadhaar card"),
                                            ("avdoc000002", "EMP-702", "PAN card"),
                                            ("avdoc000003", "EMP-701", "avatar")):
                repo.upsert("documents", doc_id, {
                    "id": doc_id, "entityType": "employee", "entityId": owner, "category": category,
                    "fileName": f"{doc_id}.pdf", "contentType": "application/pdf", "storageKey": f"k/{doc_id}",
                })
        yield c
    get_settings.cache_clear()


def _get(client: Any, path: str, secret: str | None = AVORA_SECRET) -> Any:
    headers = {"X-Avora-Secret": secret} if secret else {}
    return client.get(f"/api/internal/avora{path}", headers=headers)


@needs_db
def test_requires_the_avora_secret(client: Any) -> None:
    assert _get(client, "/compensation?email=asha@corp.io", secret=None).status_code == 403
    assert _get(client, "/compensation?email=asha@corp.io", secret="wrong").status_code == 403


@needs_db
def test_directory_secret_does_not_open_it(client: Any) -> None:
    resp = client.get("/api/internal/avora/compensation?email=asha@corp.io",
                      headers={"X-Internal-Secret": AVORA_SECRET})
    assert resp.status_code == 403


@needs_db
def test_compensation_by_email_ignores_case(client: Any) -> None:
    body = _get(client, "/compensation?email=ASHA@corp.io").json()
    assert body["employee_code"] == "EMP-701" and body["annual_ctc_inr"] == 600_000


@needs_db
def test_unknown_employee_is_404(client: Any) -> None:
    assert _get(client, "/compensation?email=nobody@corp.io").status_code == 404


@needs_db
def test_documents_lists_only_own_paperwork(client: Any) -> None:
    body = _get(client, "/documents?email=asha@corp.io").json()
    assert [d["id"] for d in body["documents"]] == ["avdoc000001"]


@needs_db
def test_document_content_for_owner(client: Any) -> None:
    resp = _get(client, "/documents/avdoc000001/content?email=asha@corp.io")
    assert resp.status_code == 200 and resp.content == b"bytes of k/avdoc000001"


@needs_db
def test_cannot_fetch_another_employees_document(client: Any) -> None:
    assert _get(client, "/documents/avdoc000002/content?email=asha@corp.io").status_code == 404


@needs_db
def test_endpoints_off_without_a_secret(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("AVORA_API_SECRET", "")
    get_settings.cache_clear()
    assert _get(client, "/compensation?email=asha@corp.io").status_code == 503
