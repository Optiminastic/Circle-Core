"""`_pick_doc_request` / `_document_for` / `_portal_field` now query via the
indexed `find()` (JSONB containment) instead of `list()` + a Python filter
over the whole table — see docs/PERFORMANCE_AUDIT.md P1-4. This pins down
that the swap is behavior-preserving: same selection among duplicates, same
handling of an empty result, same candidate isolation.

Stub repo only — no database, no network.
"""

from __future__ import annotations

from typing import Any

from app.api.routes.bgv_ongrid import (
    DOC_REQUESTS,
    DOCUMENTS,
    _document_for,
    _pick_doc_request,
    _portal_field,
)

CANDIDATE_A = "cand-a"
CANDIDATE_B = "cand-b"


class _Repo:
    """Mirrors the real DocumentRepository's indexed containment semantics —
    find() filters by exact field equality, the same thing `data @>` does."""

    def __init__(
        self,
        doc_requests: list[dict[str, Any]],
        documents: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._doc_requests = doc_requests
        self._documents = documents or {}

    def list(self, table: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._doc_requests] if table == DOC_REQUESTS else []

    def find(self, table: str, match: dict[str, Any]) -> list[dict[str, Any]]:
        return [row for row in self.list(table) if all(row.get(k) == v for k, v in match.items())]

    def get(self, table: str, key: str) -> dict[str, Any] | None:
        return self._documents.get(key) if table == DOCUMENTS else None


class _Storage:
    def __init__(self, blob: bytes = b"%PDF-1.4") -> None:
        self._blob = blob

    def get(self, key: str) -> tuple[bytes, str]:
        return self._blob, "application/pdf"


def test_pick_doc_request_returns_none_when_the_candidate_has_no_requests() -> None:
    repo = _Repo([{"id": "r1", "candidateId": CANDIDATE_B, "submissions": []}])
    assert _pick_doc_request(repo, CANDIDATE_A) is None


def test_pick_doc_request_never_crosses_into_another_candidates_requests() -> None:
    # A request with far more content, but for a different candidate - must
    # never win, and must never even be considered.
    repo = _Repo(
        [
            {"id": "mine", "candidateId": CANDIDATE_A, "submissions": [{"docType": "PAN"}]},
            {
                "id": "theirs",
                "candidateId": CANDIDATE_B,
                "submissions": [{"docType": "PAN"}, {"docType": "Aadhaar"}, {"docType": "Photo"}],
            },
        ]
    )
    picked = _pick_doc_request(repo, CANDIDATE_A)
    assert picked is not None
    assert picked["id"] == "mine"


def test_pick_doc_request_excludes_signed_letter_links() -> None:
    # A re-issued link for the signed appointment letter is not a "joining
    # docs" submission and must never be picked as one, even if it is the
    # only request on file.
    repo = _Repo(
        [{"id": "letter", "candidateId": CANDIDATE_A, "kind": "signed-offer", "submissions": [{"x": 1}]}]
    )
    assert _pick_doc_request(repo, CANDIDATE_A) is None


def test_pick_doc_request_prefers_the_fullest_duplicate() -> None:
    # Resends mint new links, so several requests can exist for one
    # candidate; the one with the most content (submissions + bank details)
    # is the real submission and must win.
    repo = _Repo(
        [
            {"id": "old", "candidateId": CANDIDATE_A, "submissions": [{"docType": "PAN"}]},
            {
                "id": "latest",
                "candidateId": CANDIDATE_A,
                "submissions": [{"docType": "PAN"}, {"docType": "Aadhaar"}],
                "bankDetails": {"status": "Verified"},
            },
        ]
    )
    picked = _pick_doc_request(repo, CANDIDATE_A)
    assert picked is not None
    assert picked["id"] == "latest"


def test_document_for_returns_none_when_nothing_was_uploaded() -> None:
    repo = _Repo([{"id": "r1", "candidateId": CANDIDATE_A, "submissions": []}])
    assert _document_for(repo, _Storage(), CANDIDATE_A, "PAN Card") is None


def test_document_for_only_searches_this_candidates_requests() -> None:
    repo = _Repo(
        [
            {"id": "mine", "candidateId": CANDIDATE_A, "submissions": []},
            {
                "id": "theirs",
                "candidateId": CANDIDATE_B,
                "submissions": [{"docType": "PAN Card", "documentId": "doc-b", "status": "Verified"}],
            },
        ],
        documents={"doc-b": {"storageKey": "k/doc-b", "fileName": "pan.pdf"}},
    )
    assert _document_for(repo, _Storage(), CANDIDATE_A, "PAN Card") is None


def test_document_for_prefers_the_verified_submission_across_duplicate_requests() -> None:
    # Links get re-issued, so a candidate's submissions for the same doc type
    # can be spread across several requests — the verified one must win even
    # if an unverified, more-recent one exists on a different request.
    repo = _Repo(
        [
            {
                "id": "r1",
                "candidateId": CANDIDATE_A,
                "submissions": [
                    {
                        "docType": "PAN Card",
                        "documentId": "doc-verified",
                        "status": "Verified",
                        "uploadedAt": "2026-01-01T00:00:00Z",
                    }
                ],
            },
            {
                "id": "r2",
                "candidateId": CANDIDATE_A,
                "submissions": [
                    {
                        "docType": "PAN Card",
                        "documentId": "doc-newer",
                        "status": "Submitted",
                        "uploadedAt": "2026-06-01T00:00:00Z",
                    }
                ],
            },
        ],
        documents={
            "doc-verified": {"storageKey": "k/verified", "fileName": "pan-verified.pdf"},
            "doc-newer": {"storageKey": "k/newer", "fileName": "pan-newer.pdf"},
        },
    )
    result = _document_for(repo, _Storage(), CANDIDATE_A, "PAN Card")
    assert result is not None
    _, _, filename = result
    assert filename == "pan-verified.pdf"


def test_portal_field_returns_none_when_no_request_has_it() -> None:
    repo = _Repo([{"id": "r1", "candidateId": CANDIDATE_A, "submissions": []}])
    assert _portal_field(repo, CANDIDATE_A, "isFresher") is None


def test_portal_field_finds_the_value_on_whichever_duplicate_request_has_it() -> None:
    # A candidate's answers end up spread across requests from resent links;
    # only one of them carries any given field.
    repo = _Repo(
        [
            {"id": "r1", "candidateId": CANDIDATE_A, "submissions": []},
            {"id": "r2", "candidateId": CANDIDATE_A, "submissions": [], "isFresher": True},
        ]
    )
    assert _portal_field(repo, CANDIDATE_A, "isFresher") is True


def test_portal_field_never_crosses_into_another_candidates_request() -> None:
    repo = _Repo([{"id": "theirs", "candidateId": CANDIDATE_B, "submissions": [], "isFresher": True}])
    assert _portal_field(repo, CANDIDATE_A, "isFresher") is None
