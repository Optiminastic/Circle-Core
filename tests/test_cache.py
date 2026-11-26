"""TTLCache — the in-process dashboard-overview cache (Phase 3). Pure logic,
no database; time-sensitive bits use a monkeypatched clock."""

from __future__ import annotations

import pytest

from app.core.cache import TTLCache


def test_get_or_set_computes_once_within_the_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.core.cache as cache_module

    clock = [0.0]
    monkeypatch.setattr(cache_module.time, "monotonic", lambda: clock[0])
    cache = TTLCache()
    calls = []

    def compute() -> str:
        calls.append(1)
        return "value"

    assert cache.get_or_set("k", 10, compute) == "value"
    assert cache.get_or_set("k", 10, compute) == "value"
    assert len(calls) == 1  # second call served from cache, compute() not re-run


def test_recomputes_after_the_ttl_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.core.cache as cache_module

    clock = [0.0]
    monkeypatch.setattr(cache_module.time, "monotonic", lambda: clock[0])
    cache = TTLCache()
    calls = []

    def compute() -> str:
        calls.append(1)
        return f"value-{len(calls)}"

    assert cache.get_or_set("k", 10, compute) == "value-1"
    clock[0] += 11
    assert cache.get_or_set("k", 10, compute) == "value-2"
    assert len(calls) == 2


def test_invalidate_removes_matching_prefixes_only() -> None:
    cache = TTLCache()
    cache.get_or_set("stats:overview", 60, lambda: "a")
    cache.get_or_set("stats:other", 60, lambda: "b")
    cache.get_or_set("unrelated:key", 60, lambda: "c")

    cache.invalidate("stats:")

    assert cache.get_or_set("stats:overview", 60, lambda: "recomputed-a") == "recomputed-a"
    assert cache.get_or_set("stats:other", 60, lambda: "recomputed-b") == "recomputed-b"
    assert cache.get_or_set("unrelated:key", 60, lambda: "recomputed-c") == "c"  # untouched


def test_a_failing_compute_propagates_and_is_not_cached() -> None:
    cache = TTLCache()

    def boom() -> str:
        raise RuntimeError("downstream failed")

    with pytest.raises(RuntimeError):
        cache.get_or_set("k", 60, boom)

    # Nothing cached from the failed attempt - the next call tries again.
    assert cache.get_or_set("k", 60, lambda: "recovered") == "recovered"
