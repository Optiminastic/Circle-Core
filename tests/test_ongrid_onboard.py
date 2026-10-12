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
    def __init__(
        self,
        bgv: dict[str, Any] | None,
        *,
        doc_request: dict[str, Any] | None = None,
        documents: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        if bgv is not None:
            self.rows[bgv_ongrid.BGVS] = bgv
        self.doc_request = doc_request or DOC_REQUEST
        self.documents = documents or {}
        self.writes: list[dict[str, Any]] = []

    def get(self, table: str, key: str) -> dict[str, Any] | None:
        if table == bgv_ongrid.CANDIDATES:
            return dict(CANDIDATE)
        if table == bgv_ongrid.DOCUMENTS:
            return self.documents.get(key)
        return self.rows.get(table)

    def list(self, table: str) -> list[dict[str, Any]]:
        return [dict(self.doc_request)] if table == bgv_ongrid.DOC_REQUESTS else []

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
    #: Which individual each document was attached to. The id is the whole
    #: point: an upload aimed at the wrong one lands on another person.
    uploaded_to: list[str] = []

    def __init__(self, settings: Any) -> None:
        pass

    def create_individual(self, payload: dict[str, Any]) -> dict[str, Any]:
        _FakeClient.created.append(payload)
        return {"individual": {"id": "ind-fresh", "name": payload.get("name")}}

    def upload_document(self, individual_id: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        _FakeClient.uploaded_to.append(individual_id)
        return {}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    _FakeClient.created = []
    _FakeClient.uploaded_to = []
    monkeypatch.setattr(bgv_ongrid, "OnGridClient", _FakeClient)
    return _FakeClient


def _settings() -> Any:
    from app.core.config import Settings

    return Settings(
        ongrid_base_url="https://api.ongrid.in/app",
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


def _ours(**extra: Any) -> dict[str, Any]:
    """A BGV already onboarded into the community we are configured for."""
    return {
        "id": CANDIDATE_ID,
        "ongridIndividualId": INDIVIDUAL_ID,
        "ongridCommunityId": "187363",
        **extra,
    }


def test_a_second_onboard_reuses_the_one_the_candidate_has(client: Any) -> None:
    repo = _Repo(_ours())
    result = _onboard(repo)

    assert result.ok is True
    assert result.reused is True
    assert result.individualId == INDIVIDUAL_ID
    assert client.created == [], "a retry must not create a second person in OnGrid"


def test_the_stored_individual_id_survives_a_retry(client: Any) -> None:
    # The real damage of a fork is here: the record stops pointing at the
    # individual the running checks belong to.
    repo = _Repo(_ours())
    _onboard(repo)

    assert repo.rows[bgv_ongrid.BGVS]["ongridIndividualId"] == INDIVIDUAL_ID


def test_the_timeline_says_which_of_the_two_happened(client: Any) -> None:
    fresh = _Repo(None)
    _onboard(fresh)
    assert "Onboarded to OnGrid" in fresh.rows[bgv_ongrid.BGVS]["verificationTimeline"][-1]["action"]

    again = _Repo(_ours())
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
        ongrid_base_url="https://api.ongrid.in/app",
        ongrid_username="user",
        ongrid_password="secret",
        ongrid_community_id=community,
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


# -- an id from another community names a different person --------------------
#
# Individual ids are minted per community, so 193781 in staging and 193781 in
# production are two unrelated people. Reusing a stored id across that boundary
# does not fail loudly: it uploads this candidate's Aadhaar and PAN onto
# whoever happens to hold that number in the community we are now pointed at.


STAGING = {"id": CANDIDATE_ID, "ongridIndividualId": "193781", "ongridCommunityId": "79355"}

SUBMITTED_DOC_REQUEST = {
    **DOC_REQUEST,
    "submissions": [{"docType": "aadhaar", "status": "Verified", "documentId": "doc-1"}],
}
DOC_META = {"doc-1": {"storageKey": "k", "fileName": "aadhaar.pdf", "contentType": "application/pdf"}}


def test_an_individual_from_another_community_is_never_reused(client: Any) -> None:
    repo = _Repo(dict(STAGING))
    result = _onboard(repo)

    assert result.reused is False
    assert result.individualId == "ind-fresh"
    assert len(client.created) == 1, "a foreign id means there is nobody here yet"


def test_documents_go_to_the_new_individual_not_the_foreign_one(client: Any) -> None:
    # The whole point of the guard. Before it, these uploads were addressed to
    # 193781 in the live community - a stranger.
    repo = _Repo(dict(STAGING), doc_request=SUBMITTED_DOC_REQUEST, documents=DOC_META)
    _onboard(repo)

    assert client.uploaded_to == ["ind-fresh"]
    assert "193781" not in client.uploaded_to


def test_an_unstamped_individual_is_not_reused_either(client: Any) -> None:
    # Every record predating the stamp was created while Circle pointed at
    # staging, so "unknown community" is in practice "not this one".
    repo = _Repo({"id": CANDIDATE_ID, "ongridIndividualId": "193781"})
    result = _onboard(repo)

    assert result.reused is False
    assert len(client.created) == 1


def test_the_abandoned_individual_is_kept_for_the_erasure_request(client: Any) -> None:
    # Dropping it would erase the only record that this candidate was run in
    # another community, and that community still has to be asked to delete it.
    repo = _Repo(dict(STAGING))
    _onboard(repo)
    stored = repo.rows[bgv_ongrid.BGVS]

    assert stored["ongridPreviousIndividualId"] == "193781"
    assert stored["ongridPreviousCommunityId"] == "79355"
    assert stored["ongridIndividualId"] == "ind-fresh"
    assert stored["ongridCommunityId"] == "187363"


def test_the_foreign_individuals_cached_status_is_not_shown_against_the_new_one(
    client: Any,
) -> None:
    repo = _Repo(dict(STAGING, ongridStatus={"overall": "Clear"}, ongridStatusAt="2026-10-01T00:00:00Z"))
    _onboard(repo)
    stored = repo.rows[bgv_ongrid.BGVS]

    assert "ongridStatus" not in stored, "a verdict from staging must not read as this one's"
    assert "ongridStatusAt" not in stored


def test_the_timeline_explains_why_a_new_number_appeared(client: Any) -> None:
    repo = _Repo(dict(STAGING))
    _onboard(repo)
    action = repo.rows[bgv_ongrid.BGVS]["verificationTimeline"][-1]["action"]

    assert "193781" in action and "79355" in action
    assert "not reused" in action


# -- there is no staging to fall back to -------------------------------------
#
# A default of staging meant a missing ONGRID_BASE_URL did not fail. It sent
# real candidates to the staging community instead, and nothing in the app
# said so. Unset must mean off.


def _settings_env(**over: Any) -> Any:
    from app.core.config import Settings

    base = {
        "ongrid_base_url": "https://api.ongrid.in/app",
        "ongrid_username": "user",
        "ongrid_password": "secret",
        "ongrid_community_id": "187363",
    }
    base.update(over)
    return Settings(**base)


def test_no_ongrid_url_is_configured_by_default() -> None:
    # `_env_file=None` reads the code default, not whatever this machine's
    # .env happens to say - the point is that the fallback baked into the
    # source is nothing at all.
    from app.core.config import Settings

    default = Settings(_env_file=None).ongrid_base_url
    assert default == "", "a default URL is a default community"
    assert "staging" not in default


def test_ongrid_is_off_until_the_url_is_set() -> None:
    # Credentials alone are not enough: they say who we are, not which OnGrid.
    assert _settings_env(ongrid_base_url="").has_ongrid is False
    assert _settings_env().has_ongrid is True


@pytest.mark.parametrize(
    "missing", ["ongrid_base_url", "ongrid_username", "ongrid_password", "ongrid_community_id"]
)
def test_every_one_of_the_four_is_required(missing: str) -> None:
    assert _settings_env(**{missing: ""}).has_ongrid is False


def test_an_unconfigured_server_does_not_quietly_pick_a_community(client: Any) -> None:
    # The route's own guard, with the URL missing rather than the credentials.
    repo = _Repo(None)
    result = bgv_ongrid.ongrid_onboard(
        candidate_id=CANDIDATE_ID,
        settings=_settings_env(ongrid_base_url=""),
        repo=repo,  # type: ignore[arg-type]
        storage=_Storage(),  # type: ignore[arg-type]
    )
    assert result.ok is False
    assert client.created == [], "nothing may be created when no OnGrid is configured"
