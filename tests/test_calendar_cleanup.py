"""cleanup_calendar_events: batched DB lookups/deletes instead of one query per
event — see docs/PERFORMANCE_AUDIT.md P1-5. Stubs only, no database/network.
"""

from __future__ import annotations

from typing import Any

from app.api.routes.calendar import cleanup_calendar_events
from app.core.config import Settings


class _Repo:
    def __init__(self, oauth: dict[str, Any] | None, links: dict[str, dict[str, Any]]) -> None:
        self._oauth = oauth
        self._links = dict(links)
        self.get_many_calls: list[list[str]] = []
        self.delete_many_calls: list[list[str]] = []

    def get(self, table: str, item_id: str) -> dict[str, Any] | None:
        assert table == "google_oauth", "only the shared OAuth row should use a single get()"
        return self._oauth

    def get_many(self, table: str, item_ids: list[str]) -> dict[str, dict[str, Any]]:
        self.get_many_calls.append(list(item_ids))
        return {k: v for k, v in self._links.items() if k in item_ids}

    def delete_many(self, table: str, item_ids: list[str]) -> int:
        self.delete_many_calls.append(list(item_ids))
        for item_id in item_ids:
            self._links.pop(item_id, None)
        return len(item_ids)


class _Service:
    def __init__(self, fail_for: set[str] | None = None) -> None:
        self.deleted: list[str] = []
        self._fail_for = fail_for or set()

    def delete_event(self, refresh_token: str, calendar_id: str, google_event_id: str) -> None:
        if google_event_id in self._fail_for:
            raise RuntimeError("Google is unavailable")
        self.deleted.append(google_event_id)


def _settings() -> Settings:
    return Settings()


def test_empty_input_makes_no_calls_at_all() -> None:
    repo = _Repo({"refreshToken": "tok"}, {})
    service = _Service()
    removed = cleanup_calendar_events(repo, service, _settings(), [])
    assert removed == 0
    assert repo.get_many_calls == []
    assert repo.delete_many_calls == []


def test_looks_up_every_event_in_one_batched_call_not_one_per_event() -> None:
    repo = _Repo(
        {"refreshToken": "tok"},
        {
            "ev1": {"googleEventId": "g1"},
            "ev2": {"googleEventId": "g2"},
            "ev3": {"googleEventId": "g3"},
        },
    )
    service = _Service()
    cleanup_calendar_events(repo, service, _settings(), ["ev1", "ev2", "ev3"])
    assert len(repo.get_many_calls) == 1
    assert sorted(repo.get_many_calls[0]) == ["ev1", "ev2", "ev3"]


def test_links_removed_in_one_batched_delete_not_one_per_event() -> None:
    repo = _Repo(
        {"refreshToken": "tok"},
        {"ev1": {"googleEventId": "g1"}, "ev2": {"googleEventId": "g2"}},
    )
    service = _Service()
    removed = cleanup_calendar_events(repo, service, _settings(), ["ev1", "ev2"])
    assert removed == 2
    assert len(repo.delete_many_calls) == 1
    assert sorted(repo.delete_many_calls[0]) == ["ev1", "ev2"]


def test_an_event_with_no_link_is_skipped_not_deleted() -> None:
    repo = _Repo({"refreshToken": "tok"}, {"ev1": {"googleEventId": "g1"}})
    service = _Service()
    removed = cleanup_calendar_events(repo, service, _settings(), ["ev1", "ev-missing"])
    assert removed == 1
    assert repo.delete_many_calls == [["ev1"]]


def test_a_link_with_no_google_event_id_is_skipped() -> None:
    repo = _Repo({"refreshToken": "tok"}, {"ev1": {}})
    service = _Service()
    removed = cleanup_calendar_events(repo, service, _settings(), ["ev1"])
    assert removed == 0
    assert service.deleted == []


def test_no_refresh_token_still_removes_the_link_rows() -> None:
    # Not connected to Google at all - the link rows are still local
    # bookkeeping that should be cleaned up regardless.
    repo = _Repo(None, {"ev1": {"googleEventId": "g1"}})
    service = _Service()
    removed = cleanup_calendar_events(repo, service, _settings(), ["ev1"])
    assert removed == 1
    assert service.deleted == []  # never called Google with no refresh token


def test_a_failed_google_delete_does_not_block_the_others_best_effort() -> None:
    repo = _Repo(
        {"refreshToken": "tok"},
        {
            "ev1": {"googleEventId": "g1"},
            "ev2": {"googleEventId": "g2-will-fail"},
            "ev3": {"googleEventId": "g3"},
        },
    )
    service = _Service(fail_for={"g2-will-fail"})
    removed = cleanup_calendar_events(repo, service, _settings(), ["ev1", "ev2", "ev3"])
    # All three links are still removed - a Google-side failure is logged,
    # never raised, and never stops the rest of the cascade.
    assert removed == 3
    assert sorted(service.deleted) == ["g1", "g3"]
