"""Avora's server-to-server reads: secret handling, mapping, and that one
employee's request can never reach another employee's files."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

from app.services.avora_export import compensation_for, is_shareable_document, parse_annual_ctc, review_of

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


def test_offer_letter_fills_what_the_record_lacks() -> None:
    out = compensation_for(
        {"id": "EMP-1", "annualCtc": "TBD"},
        {"ctcAnnual": 410000, "pfEnabled": False, "joiningDate": "2026-08-21"},
    )
    assert out["annual_ctc_inr"] == 410_000
    assert out["annual_ctc_text"] == "410000 (from offer letter)"
    assert out["pf_enabled"] is False
    assert out["joining_date"] == "2026-08-21"


def test_employee_record_wins_over_the_offer_letter() -> None:
    out = compensation_for(
        {"id": "EMP-1", "annualCtc": "6 LPA", "ctcBreakdown": {"employerPf": 1800}},
        {"ctcAnnual": 410000, "pfEnabled": False},
    )
    assert out["annual_ctc_inr"] == 600_000 and out["pf_enabled"] is True


def test_old_offer_letter_without_pf_flag_says_nothing_about_pf() -> None:
    assert compensation_for({"id": "EMP-1"}, {"ctcAnnual": 410000})["pf_enabled"] is None


_REFS = {("candidate", "c1")}


def _doc(doc_id: str, category: str) -> dict[str, Any]:
    return {"id": doc_id, "entityType": "candidate", "entityId": "c1", "category": category, "storageKey": "k"}


def test_latest_upload_per_type_wins_across_requests() -> None:
    review = review_of([
        {"submissions": [{"docType": "PAN card", "documentId": "old", "status": "Verified", "uploadedAt": "1"}]},
        {"submissions": [{"docType": "PAN card", "documentId": "new", "status": "Verified", "uploadedAt": "2"}]},
    ])
    assert is_shareable_document(_doc("new", "PAN card"), _REFS, review)
    assert not is_shareable_document(_doc("old", "PAN card"), _REFS, review)


@pytest.mark.parametrize(("status", "shared"), [("Submitted", True), ("Verified", True), ("Rejected", False)])
def test_signed_letters_count_unless_rejected(status: str, shared: bool) -> None:
    review = review_of([{"submissions": [
        {"docType": "Signed Offer Letter", "documentId": "s1", "status": status, "uploadedAt": "1"}]}])
    assert is_shareable_document(_doc("s1", "Signed Offer Letter"), _REFS, review) is shared


def test_requested_but_not_yet_uploaded_type_hides_stray_files() -> None:
    review = review_of([{"requiredDocs": ["Aadhaar card"], "submissions": []}])
    assert not is_shareable_document(_doc("x", "Aadhaar card"), _REFS, review)


def test_hr_attached_files_are_shared() -> None:
    assert is_shareable_document(_doc("hr1", "document"), _REFS, review_of([]))


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
            from sqlalchemy import text

            # Disposable test database only (CIRCLE_TEST_DATABASE_URL): start clean.
            session.execute(text("TRUNCATE employees, documents, doc_requests, onboarding"))
            repo = SqlAlchemyDocumentRepository(session)
            for code, email, cand in (("EMP-701", "Asha@Corp.io", "cand-asha"),
                                      ("EMP-702", "ravi@corp.io", "cand-ravi")):
                repo.upsert("employees", code, {"id": code, "email": email, "annualCtc": "TBD",
                                                "candidateId": cand})
            repo.upsert("onboarding", "cand-asha", {"candidateId": "cand-asha",
                                                    "offerLetter": {"ctcAnnual": 410000, "pfEnabled": False}})
            repo.upsert("doc_requests", "req-asha", {"id": "req-asha", "candidateId": "cand-asha", "submissions": [
                {"docType": "PAN card", "documentId": "avdoc000006", "status": "Verified", "uploadedAt": "2026-08-02"},
                {"docType": "Address proof", "documentId": "avdoc000008", "status": "Rejected", "uploadedAt": "2026-08-02"},
                {"docType": "Education certificates", "documentId": "avdoc000009", "status": "Submitted",
                 "uploadedAt": "2026-08-02"},
            ]})
            for doc_id, entity_type, owner, category in (
                ("avdoc000001", "employee", "EMP-701", "Aadhaar card"),
                ("avdoc000002", "employee", "EMP-702", "PAN card"),
                ("avdoc000003", "employee", "EMP-701", "avatar"),
                # Uploaded through the onboarding link and never moved.
                ("avdoc000004", "candidate", "cand-asha", "Signed Offer Letter"),
                # Another person's candidate file: must never be reachable.
                ("avdoc000005", "candidate", "cand-ravi", "Aadhaar card"),
                ("avdoc000006", "candidate", "cand-asha", "PAN card"),  # verified
                ("avdoc000007", "candidate", "cand-asha", "PAN card"),  # replaced by 06
                ("avdoc000008", "candidate", "cand-asha", "Address proof"),  # rejected
                ("avdoc000009", "candidate", "cand-asha", "Education certificates"),  # not reviewed
            ):
                repo.upsert("documents", doc_id, {
                    "id": doc_id, "entityType": entity_type, "entityId": owner, "category": category,
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
    # The record's CTC is unreadable ("TBD"), so the offer letter's figure is used.
    assert body["employee_code"] == "EMP-701" and body["annual_ctc_inr"] == 410_000
    assert body["pf_enabled"] is False


@needs_db
def test_non_ascii_secret_is_403_not_500(client: Any) -> None:
    resp = client.get("/api/internal/avora/compensation?email=asha@corp.io",
                      headers={"X-Avora-Secret": "\u00e9".encode("latin-1")})
    assert resp.status_code == 403


@needs_db
def test_shared_email_prefers_the_current_record(client: Any) -> None:
    from app.repositories.document_repository import SqlAlchemyDocumentRepository

    with client.app.state.database.session() as session:
        SqlAlchemyDocumentRepository(session).upsert("employees", "EMP-700", {
            "id": "EMP-700", "email": "asha@corp.io", "status": "Offboarded", "annualCtc": "1 LPA"})
    assert _get(client, "/compensation?email=asha@corp.io").json()["employee_code"] == "EMP-701"


@needs_db
def test_two_current_records_with_one_email_is_a_conflict(client: Any) -> None:
    from app.repositories.document_repository import SqlAlchemyDocumentRepository

    with client.app.state.database.session() as session:
        SqlAlchemyDocumentRepository(session).upsert("employees", "EMP-703", {
            "id": "EMP-703", "email": "asha@corp.io", "status": "Active"})
    assert _get(client, "/compensation?email=asha@corp.io").status_code == 409


@needs_db
def test_unknown_employee_is_404(client: Any) -> None:
    assert _get(client, "/compensation?email=nobody@corp.io").status_code == 404


@needs_db
def test_documents_lists_only_own_paperwork(client: Any) -> None:
    body = _get(client, "/documents?email=asha@corp.io").json()
    # Employee-record files plus files still on her own candidate record; no
    # avatar, and nothing of Ravi's.
    assert sorted(d["id"] for d in body["documents"]) == ["avdoc000001", "avdoc000004", "avdoc000006"]


@needs_db
def test_candidate_record_files_can_be_downloaded_by_their_owner(client: Any) -> None:
    resp = _get(client, "/documents/avdoc000004/content?email=asha@corp.io")
    assert resp.status_code == 200


@needs_db
@pytest.mark.parametrize("doc_id", ["avdoc000007", "avdoc000008", "avdoc000009"])
def test_replaced_rejected_or_unreviewed_uploads_cannot_be_fetched(client: Any, doc_id: str) -> None:
    assert _get(client, f"/documents/{doc_id}/content?email=asha@corp.io").status_code == 404


@needs_db
def test_cannot_reach_another_persons_candidate_files(client: Any) -> None:
    assert _get(client, "/documents/avdoc000005/content?email=asha@corp.io").status_code == 404


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


def test_profile_mapping() -> None:
    from app.services.avora_export import profile_for

    out = profile_for({
        "id": "EMP-1",
        "avatarUrl": "https://api.circle.example/api/documents/abc123def456/preview",
        "personalDetails": {"dateOfBirth": "1998-04-12", "gender": "Female", "panNumber": "X"},
    })
    assert out == {"employee_code": "EMP-1", "date_of_birth": "1998-04-12", "gender": "Female",
                   "avatar_document_id": "abc123def456"}
    assert profile_for({"id": "EMP-2", "personalDetails": {"dateOfBirth": "12/04/1998"}})["date_of_birth"] is None


@needs_db
def test_avatar_only_served_when_it_is_the_persons_own(client: Any) -> None:
    from app.repositories.document_repository import SqlAlchemyDocumentRepository

    with client.app.state.database.session() as session:
        repo = SqlAlchemyDocumentRepository(session)
        repo.upsert("employees", "EMP-701", {"id": "EMP-701", "email": "asha@corp.io", "candidateId": "cand-asha",
                                            "avatarUrl": "/api/documents/avdoc000003/preview"})
        repo.upsert("employees", "EMP-702", {"id": "EMP-702", "email": "ravi@corp.io",
                                            "avatarUrl": "/api/documents/avdoc000003/preview"})  # someone else's photo
    assert _get(client, "/avatar?email=asha@corp.io").status_code == 200
    assert _get(client, "/avatar?email=ravi@corp.io").status_code == 404
    assert _get(client, "/profile?email=asha@corp.io").json()["avatar_document_id"] == "avdoc000003"
