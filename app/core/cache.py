"""A small in-process TTL cache. No Redis, no new schema — see Phase 3 in
docs/PERFORMANCE_IMPLEMENTATION_LOG.md.

Process-local: each of the app's 2 uvicorn workers holds its own copy, and
`invalidate()` only clears the copy IN THE WORKER THAT HANDLED THE WRITE —
the other worker's entry just sits there until its own TTL expires. This is
a deliberate, accepted limitation of "no new infra": with no shared cache
store, the TTL — not invalidation — is the real cross-worker staleness
bound. Invalidation only shortens the common case (the same worker serves
the write and the next read); it is a latency optimization, not a
consistency guarantee.

Fails open: any internal error here must never become a 500 for the
caller — on any failure, get_or_set() simply skips the cache and calls
`compute()` directly, same as a cold cache miss.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, TypeVar

T = TypeVar("T")


class TTLCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._store: dict[str, tuple[float, Any]] = {}

    def get_or_set(self, key: str, ttl_seconds: float, compute: Callable[[], T]) -> T:
        try:
            with self._lock:
                entry = self._store.get(key)
            if entry is not None:
                expires_at, value = entry
                if expires_at > time.monotonic():
                    return value
        except Exception:  # noqa: BLE001 - a cache bug must never cause an outage
            pass

        value = compute()

        try:
            with self._lock:
                self._store[key] = (time.monotonic() + ttl_seconds, value)
        except Exception:  # noqa: BLE001 - caller still got a correct value either way
            pass
        return value

    def invalidate(self, *prefixes: str) -> None:
        """Drop every cached entry whose key starts with one of `prefixes`."""
        try:
            with self._lock:
                self._store = {
                    k: v
                    for k, v in self._store.items()
                    if not any(k.startswith(p) for p in prefixes)
                }
        except Exception:  # noqa: BLE001
            pass


# One process-wide instance - see the module docstring for why a per-request
# or per-session cache wouldn't make sense here.
dashboard_cache = TTLCache()
