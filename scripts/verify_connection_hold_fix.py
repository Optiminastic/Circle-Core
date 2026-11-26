"""Phase 4 verification: empirically prove the IdentitySyncWorker fix
eliminates connection-pool blocking of concurrent requests, using a real
(small) connection pool against the dev database - not a mock.

Simulates the OLD pattern (hold one session across a slow "external call")
and the NEW pattern (release before, reacquire only to persist) side by
side, under a pool_size=2 engine, and measures how long 5 concurrent
"normal" queries take to complete while each pattern's "worker" is running.

Run: .venv/Scripts/python.exe scripts/verify_connection_hold_fix.py
"""

from __future__ import annotations

import os
import threading
import time

from sqlalchemy import create_engine, text

DATABASE_URL = os.environ["DATABASE_URL"]
# Deliberately tiny, so a single held connection is a meaningful fraction of
# the pool - this is what makes the effect measurable in a short test,
# exactly analogous to production's pool_size=6 vs CLAIM_BATCH_SIZE=10 items
# each able to hold a connection if the old bug were present.
POOL_SIZE = 2
SIMULATED_EXTERNAL_CALL_SECONDS = 8
NUM_CONCURRENT_REQUESTS = 5


def make_engine():
    engine = create_engine(DATABASE_URL, pool_size=POOL_SIZE, max_overflow=0, pool_timeout=20, pool_pre_ping=True)
    # Pre-warm both pool slots so the dev environment's Neon TLS-handshake
    # cost (not representative of production's local, same-host Postgres)
    # isn't counted as part of what we're actually measuring here.
    conns = [engine.connect() for _ in range(POOL_SIZE)]
    for c in conns:
        c.execute(text("SELECT 1"))
        c.close()
    return engine


def old_pattern_worker(engine) -> None:
    """Reproduces the pre-fix bug: one connection held across a slow
    'external call'."""
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))  # the "claim" work
        time.sleep(SIMULATED_EXTERNAL_CALL_SECONDS)  # the "external HTTP call"
        conn.execute(text("SELECT 1"))  # the "mark result" work


def new_pattern_worker(engine) -> None:
    """The fix: release before the slow call, reacquire only to persist."""
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))  # the "claim" work
    time.sleep(SIMULATED_EXTERNAL_CALL_SECONDS)  # no connection held here
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))  # the "mark result" work


def run_concurrent_requests(engine, label: str, n: int) -> list[float]:
    durations: list[float] = []
    lock = threading.Lock()

    def one_request() -> None:
        start = time.monotonic()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        elapsed = time.monotonic() - start
        with lock:
            durations.append(elapsed)

    threads = [threading.Thread(target=one_request) for _ in range(n)]
    wall_start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall_elapsed = time.monotonic() - wall_start
    print(f"  [{label}] {n} concurrent plain queries: wall-clock {wall_elapsed:.2f}s, "
          f"per-request: {[round(d, 2) for d in sorted(durations)]}")
    return durations


def scenario(label: str, worker_fn) -> None:
    print(f"\n=== {label} (pool_size={POOL_SIZE}) ===")
    engine = make_engine()
    worker_thread = threading.Thread(target=worker_fn, args=(engine,))
    worker_thread.start()
    time.sleep(0.3)  # let the worker claim its connection first
    durations = run_concurrent_requests(engine, label, NUM_CONCURRENT_REQUESTS)
    worker_thread.join()
    engine.dispose()
    print(f"  [{label}] max concurrent-request latency: {max(durations):.2f}s")


if __name__ == "__main__":
    scenario("OLD (pre-fix) pattern", old_pattern_worker)
    scenario("NEW (fixed) pattern", new_pattern_worker)
