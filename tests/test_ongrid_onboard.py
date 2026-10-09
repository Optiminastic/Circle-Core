"""Onboarding the same candidate twice must not create two people in OnGrid.

A second create does not update the first, it forks it - and the fork takes the
stored id with it, hiding any check already running on the original while OnGrid
carries on billing for it. HR retries the onboard whenever a document upload
failed, so the retry has to land on the individual that already exists.

Stubs only, so these need no database and no network.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.api.routes import bgv_ongrid

CANDIDATE_ID = "cand-1"
INDIVIDUAL_ID = "ind-9001"

CANDIDATE = {
    "id": CANDIDATE_ID,
    "fullName": "Test Candidate",
    "gender": "Male",
    "location": "Mumbai",
    "phone": "9876543210",
    "appliedRole": "Engineer",
}

DOC_REQUEST = {
    "id": "tok",
    "candidateId": CANDIDATE_ID,
    "consent": {"agreed": True, "text": "I agree.", "at": "2026-01-01T00:00:00Z"},
    "submissions": [],
}


class _Repo:
    def __init__(self, bgv: dict[str, Any] | None) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        if bgv is not None:
            self.rows[bgv_ongrid.BGVS] = bgv
        self.writes: list[dict[str, Any]] = []

    def get(self, table: str, key: str) -> dict[str, Any] | None:
        if table == bgv_ongrid.CANDIDATES:
            return dict(CANDIDATE)
        return self.rows.get(table)

    def list(self, table: str) -> list[dict[str, Any]]:
        return [dict(DOC_REQUEST)] if table == bgv_ongrid.DOC_REQUESTS else []

    def upsert(self, table: str, key: str, data: dict[str, Any]) -> dict[str, Any]:
        self.rows[table] = data
        self.writes.append(data)
        return data


class _Storage:
    def get(self, key: str) -> tuple[bytes, str]:
        return b"%PDF-1.4", "application/pdf"


class _FakeClient:
    """Records what the route asked OnGrid to do."""

    created: list[dict[str, Any]] = []

    def __init__(self, settings: Any) -> None:
        pass

    def create_individual(self, payload: dict[str, Any]) -> dict[str, Any]:
        _FakeClient.created.append(payload)
        return {"individual": {"id": "ind-fresh", "name": payload.get("name")}}

    def upload_document(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    _FakeClient.created = []
    monkeypatch.setattr(bgv_ongrid, "OnGridClient", _FakeClient)
    return _FakeClient


def _settings() -> Any:
    from app.core.config import Settings

    return Settings(
        ongrid_username="user",
        ongrid_password="secret",
        ongrid_community_id="187363",
    )


def _onboard(repo: _Repo) -> Any:
    return bgv_ongrid.ongrid_onboard(
        candidate_id=CANDIDATE_ID,
        settings=_settings(),
        repo=repo,  # type: ignore[arg-type]
        storage=_Storage(),  # type: ignore[arg-type]
    )


def test_a_first_onboard_creates_the_individual(client: Any) -> None:
    repo = _Repo(None)
    result = _onboard(repo)

    assert result.ok is True
    assert result.reused is False
    assert result.individualId == "ind-fresh"
    assert len(client.created) == 1


def test_a_second_onboard_reuses_the_one_the_candidate_has(client: Any) -> None:
    repo = _Repo({"id": CANDIDATE_ID, "ongridIndividualId": INDIVIDUAL_ID})
    result = _onboard(repo)

    assert result.ok is True
    assert result.reused is True
    assert result.individualId == INDIVIDUAL_ID
    assert client.created == [], "a retry must not create a second person in OnGrid"


def test_the_stored_individual_id_survives_a_retry(client: Any) -> None:
    # The real damage of a fork is here: the record stops pointing at the
    # individual the running checks belong to.
    repo = _Repo({"id": CANDIDATE_ID, "ongridIndividualId": INDIVIDUAL_ID})
    _onboard(repo)

    assert repo.rows[bgv_ongrid.BGVS]["ongridIndividualId"] == INDIVIDUAL_ID


def test_the_timeline_says_which_of_the_two_happened(client: Any) -> None:
    fresh = _Repo(None)
    _onboard(fresh)
    assert "Onboarded to OnGrid" in fresh.rows[bgv_ongrid.BGVS]["verificationTimeline"][-1]["action"]

    again = _Repo({"id": CANDIDATE_ID, "ongridIndividualId": INDIVIDUAL_ID})
    _onboard(again)
    assert "existing individual" in again.rows[bgv_ongrid.BGVS]["verificationTimeline"][-1]["action"]


def test_an_unconfigured_server_does_not_reach_ongrid(client: Any) -> None:
    from app.core.config import Settings

    result = bgv_ongrid.ongrid_onboard(
        candidate_id=CANDIDATE_ID,
        settings=Settings(ongrid_username="", ongrid_password="", ongrid_community_id=""),
        repo=_Repo(None),  # type: ignore[arg-type]
        storage=_Storage(),  # type: ignore[arg-type]
    )

    assert result.ok is False
    assert result.reason == "not_configured"
    assert client.created == []


# --------------------------------------------------------------------------
# An individual id means something only inside the community it was made in.
# The same number is a different person in staging and in production, so
# reading one across the boundary does not fail - it answers about a stranger.
# --------------------------------------------------------------------------


def _settings_for(community: str) -> Any:
    from app.core.config import Settings

    return Settings(
        ongrid_username="user", ongrid_password="secret", ongrid_community_id=community
    )


@pytest.mark.parametrize(
    "stored,configured,expected",
    [
        ("187363", "187363", True),
        ("79355", "187363", False),  # staging id, production credentials
        ("", "187363", False),  # written before the community was stamped
    ],
)
def test_which_records_belong_here(stored: str, configured: str, expected: bool) -> None:
    assert (
        bgv_ongrid.belongs_here({"ongridCommunityId": stored}, _settings_for(configured))
        is expected
    )


def test_status_refuses_to_read_a_foreign_individual(client: Any) -> None:
    repo = _Repo({"id": CANDIDATE_ID, "ongridIndividualId": "191569", "ongridCommunityId": "79355"})
    result = bgv_ongrid.ongrid_status(
        candidate_id=CANDIDATE_ID,
        settings=_settings_for("187363"),
        repo=repo,  # type: ignore[arg-type]
    )
    assert result.ok is False
    assert result.reason == bgv_ongrid.FOREIGN_COMMUNITY
    assert result.individualId == "191569"


def test_verify_refuses_rather_than_billing_checks_on_a_stranger(client: Any) -> None:
    repo = _Repo({"id": CANDIDATE_ID, "ongridIndividualId": "191569", "ongridCommunityId": "79355"})
    result = bgv_ongrid.ongrid_verify(
        candidate_id=CANDIDATE_ID,
        body=bgv_ongrid.VerifyRequest(services=["PANV"]),
        settings=_settings_for("187363"),
        repo=repo,  # type: ignore[arg-type]
        storage=_Storage(),  # type: ignore[arg-type]
    )
    assert result.ok is False
    assert result.reason == bgv_ongrid.FOREIGN_COMMUNITY


def test_an_onboard_stamps_the_community_it_created_the_individual_in(client: Any) -> None:
    repo = _Repo(None)
    _onboard(repo)
    stored = repo.rows[bgv_ongrid.BGVS]
    assert stored["ongridCommunityId"] == "187363"
    assert bgv_ongrid.belongs_here(stored, _settings_for("187363")) is True
