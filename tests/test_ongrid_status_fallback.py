"""sync_ongrid_status falls back to the last-known cached status (not just the
report link) when the live OnGrid call itself fails - Phase 3 C4 ("if OnGrid
is unavailable, preserve last known verification status, indicate when it
was last synced"). Stubs only, no database/network.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest

from app.api.routes import bgv_ongrid
from app.services.ongrid import OnGridError

CANDIDATE_ID = "cand-1"


class _Repo:
    def __init__(self, bgv: dict[str, Any]) -> None:
        self._bgv = bgv
        self.upserts: list[dict[str, Any]] = []

    def get(self, table: str, key: str) -> dict[str, Any] | None:
        return dict(self._bgv) if table == bgv_ongrid.BGVS else None

    def upsert(self, table: str, key: str, data: dict[str, Any]) -> dict[str, Any]:
        self.upserts.append(data)
        return data


class _FakeDatabase:
    def __init__(self, repo: _Repo) -> None:
        self._repo = repo

    @contextmanager
    def repository(self) -> Any:
        yield self._repo


class _FailingClient:
    """Every call raises - simulates OnGrid being unreachable."""

    def __init__(self, settings: Any) -> None:
        pass

    def verification_status(self, individual_id: str) -> dict[str, Any]:
        raise OnGridError("OnGrid timed out")


def _settings() -> Any:
    from app.core.config import Settings

    return Settings(
        ongrid_base_url="https://api.ongrid.in/app",
        ongrid_username="user",
        ongrid_password="secret",
        ongrid_community_id="187363",
    )


@pytest.fixture
def failing_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bgv_ongrid, "OnGridClient", _FailingClient)


def test_falls_back_to_cached_status_when_ongrid_is_unreachable(failing_client: None) -> None:
    bgv = {
        "id": CANDIDATE_ID,
        "ongridIndividualId": "ind-1",
        "ongridCommunityId": "187363",
        "ongridStatus": {
            "overall": "In Progress",
            "checks": [{"name": "PANV", "status": "Completed"}],
            "reportUrl": None,
            "reportComplete": None,
        },
        "ongridStatusAt": "2026-11-20T10:00:00+00:00",
    }
    repo = _Repo(bgv)
    database = _FakeDatabase(repo)

    result = bgv_ongrid.sync_ongrid_status(database, _settings(), CANDIDATE_ID)

    assert result.ok is True
    assert result.stale is True
    assert result.overallStatus == "In Progress"
    assert result.checks == [{"name": "PANV", "status": "Completed"}]
    assert result.syncedAt == "2026-11-20T10:00:00+00:00"
    assert result.reason == "OnGrid timed out"
    # A failed live call must never overwrite the cache it's falling back to.
    assert repo.upserts == []


def test_no_cache_and_ongrid_unreachable_is_a_clean_failure(failing_client: None) -> None:
    bgv = {"id": CANDIDATE_ID, "ongridIndividualId": "ind-1", "ongridCommunityId": "187363"}
    repo = _Repo(bgv)
    database = _FakeDatabase(repo)

    result = bgv_ongrid.sync_ongrid_status(database, _settings(), CANDIDATE_ID)

    assert result.ok is False
    assert result.stale is False
    assert result.reason == "OnGrid timed out"
