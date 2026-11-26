"""External-call resilience primitives: a circuit breaker and a bounded,
jittered retry helper. Used by app/services/ongrid.py and
app/services/google_calendar.py — see docs/PERFORMANCE_IMPLEMENTATION_LOG.md
Phase 3 C4.

No new infrastructure: state lives in process memory. Each of the 2 uvicorn
workers has its own breaker state, which is a known, accepted limitation
(one worker can be "open" while the other is still trying) rather than a
requirement for a shared store this app doesn't otherwise need — the goal is
"stop hammering a downed service from THIS process," not perfectly
synchronized cluster-wide circuit state.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, TypeVar

from app.core.logging import get_logger

logger = get_logger("curcle.resilience")

T = TypeVar("T")


class CircuitOpenError(RuntimeError):
    """Raised instead of even attempting a call while the breaker is open."""


@dataclass
class CircuitBreaker:
    """Classic closed → open → half-open breaker.

    - closed: calls go through normally; a run of `failure_threshold`
      consecutive failures opens the breaker.
    - open: calls fail immediately (CircuitOpenError), no network attempt at
      all, for `reset_timeout_seconds`.
    - half-open: after the timeout, the NEXT call is allowed through as a
      probe. Success closes the breaker again; failure re-opens it for
      another full timeout.
    """

    name: str
    failure_threshold: int = 5
    reset_timeout_seconds: float = 60.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _consecutive_failures: int = field(default=0, repr=False)
    _opened_at: float | None = field(default=None, repr=False)
    _half_open_probe_in_flight: bool = field(default=False, repr=False)

    def _state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if time.monotonic() - self._opened_at >= self.reset_timeout_seconds:
            return "half-open"
        return "open"

    def call(self, fn: Callable[[], T]) -> T:
        with self._lock:
            state = self._state()
            if state == "open":
                raise CircuitOpenError(
                    f"{self.name}: circuit open ({self._consecutive_failures} consecutive failures); "
                    f"not attempting a call for {self._retry_after():.0f} more seconds."
                )
            if state == "half-open":
                if self._half_open_probe_in_flight:
                    raise CircuitOpenError(f"{self.name}: a recovery probe is already in flight.")
                self._half_open_probe_in_flight = True

        try:
            result = fn()
        except Exception:
            with self._lock:
                self._consecutive_failures += 1
                self._half_open_probe_in_flight = False
                if self._consecutive_failures >= self.failure_threshold and self._opened_at is None:
                    self._opened_at = time.monotonic()
                    logger.warning(
                        "%s: circuit opened after %d consecutive failures.",
                        self.name,
                        self._consecutive_failures,
                    )
                elif state == "half-open":
                    # Probe failed - stay open for another full timeout.
                    self._opened_at = time.monotonic()
                    logger.warning("%s: recovery probe failed, circuit re-opened.", self.name)
            raise
        else:
            with self._lock:
                if self._consecutive_failures or self._opened_at is not None:
                    logger.info("%s: circuit closed (recovered).", self.name)
                self._consecutive_failures = 0
                self._opened_at = None
                self._half_open_probe_in_flight = False
            return result

    def _retry_after(self) -> float:
        if self._opened_at is None:
            return 0.0
        return max(0.0, self.reset_timeout_seconds - (time.monotonic() - self._opened_at))


def retry_with_backoff(
    fn: Callable[[], T],
    *,
    max_attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    retry_on: tuple[type[Exception], ...] = (Exception,),
) -> T:
    """Retries an IDEMPOTENT callable with exponential backoff + full jitter.

    Only ever use this around a call that is safe to run more than once for
    the same logical request (a read, or a write with its own idempotency
    key) — see docs/PERFORMANCE_IMPLEMENTATION_LOG.md Phase 3 C4: "never
    retry a non-idempotent external operation without an idempotency or
    reconciliation strategy." Callers decide what's idempotent; this helper
    has no opinion.
    """
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except retry_on as exc:  # noqa: BLE001 - caller-specified exception types
            last_exc = exc
            if attempt == max_attempts - 1:
                break
            delay = min(max_delay, base_delay * (2**attempt))
            delay = random.uniform(0, delay)  # full jitter
            logger.info(
                "Retrying after %.2fs (attempt %d/%d) following: %s",
                delay, attempt + 2, max_attempts, exc,
            )
            time.sleep(delay)
    assert last_exc is not None  # max_attempts >= 1 guarantees at least one iteration ran
    raise last_exc
