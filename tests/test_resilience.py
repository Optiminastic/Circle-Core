"""CircuitBreaker and retry_with_backoff — the primitives
app/services/ongrid.py and app/services/google_calendar.py build on for
Phase 3 C4. Pure logic, no database/network; time-sensitive bits use a tiny
monkeypatched clock instead of real sleeps.
"""

from __future__ import annotations

import pytest

from app.core.resilience import CircuitBreaker, CircuitOpenError, retry_with_backoff


class _FailNTimes:
    """Fails its first N calls, then succeeds forever after."""

    def __init__(self, n: int) -> None:
        self.n = n
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        if self.calls <= self.n:
            raise RuntimeError(f"transient failure #{self.calls}")
        return "ok"


# --------------------------------------------------------------------------
# CircuitBreaker
# --------------------------------------------------------------------------


def test_stays_closed_below_the_failure_threshold() -> None:
    breaker = CircuitBreaker(name="t", failure_threshold=3, reset_timeout_seconds=60)
    failing = _FailNTimes(n=100)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            breaker.call(failing)
    # Still closed - the real error propagates, not CircuitOpenError.
    with pytest.raises(RuntimeError):
        breaker.call(failing)
    assert failing.calls == 3


def test_opens_after_the_failure_threshold_and_fails_fast() -> None:
    breaker = CircuitBreaker(name="t", failure_threshold=3, reset_timeout_seconds=60)
    failing = _FailNTimes(n=100)
    for _ in range(3):
        with pytest.raises(RuntimeError):
            breaker.call(failing)
    # Now open: no further attempt reaches the function at all.
    with pytest.raises(CircuitOpenError):
        breaker.call(failing)
    assert failing.calls == 3  # the 4th call to breaker.call() never invoked it


def test_a_success_resets_the_failure_count() -> None:
    breaker = CircuitBreaker(name="t", failure_threshold=3, reset_timeout_seconds=60)
    calls = iter([RuntimeError("1"), RuntimeError("2"), "ok", RuntimeError("3"), RuntimeError("4")])

    def flaky() -> str:
        item = next(calls)
        if isinstance(item, Exception):
            raise item
        return item

    with pytest.raises(RuntimeError):
        breaker.call(flaky)
    with pytest.raises(RuntimeError):
        breaker.call(flaky)
    assert breaker.call(flaky) == "ok"  # success - failure count resets to 0
    with pytest.raises(RuntimeError):
        breaker.call(flaky)
    with pytest.raises(RuntimeError):
        breaker.call(flaky)
    # Only 2 consecutive failures since the reset - still below threshold 3.
    assert breaker._state() == "closed"


def _fake_clock(monkeypatch: pytest.MonkeyPatch, start: float = 1_000.0) -> list[float]:
    """A controllable fake clock: `clock[0]` is the current time, advanced
    explicitly by the test rather than derived from the breaker's own state
    (which the breaker itself mutates — tying the fake clock to it creates a
    moving target instead of a fixed point in time)."""
    import app.core.resilience as resilience_module

    clock = [start]
    monkeypatch.setattr(resilience_module.time, "monotonic", lambda: clock[0])
    return clock


def test_half_open_probe_success_closes_the_circuit(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _fake_clock(monkeypatch)
    breaker = CircuitBreaker(name="t", failure_threshold=1, reset_timeout_seconds=10)
    with pytest.raises(RuntimeError):
        breaker.call(_FailNTimes(n=100))
    assert breaker._state() == "open"

    clock[0] += 11  # past the reset timeout
    assert breaker._state() == "half-open"
    assert breaker.call(lambda: "recovered") == "recovered"
    assert breaker._state() == "closed"


def test_half_open_probe_failure_reopens_for_another_full_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _fake_clock(monkeypatch)
    breaker = CircuitBreaker(name="t", failure_threshold=1, reset_timeout_seconds=10)
    with pytest.raises(RuntimeError):
        breaker.call(_FailNTimes(n=100))

    clock[0] += 11  # past the reset timeout
    assert breaker._state() == "half-open"
    with pytest.raises(RuntimeError):
        breaker.call(_FailNTimes(n=100))
    assert breaker._state() == "open"

    # Immediately after re-opening (no further time advanced), it must NOT
    # already look like the timeout elapsed again.
    assert breaker._state() == "open"
    clock[0] += 11  # past the SECOND reset timeout
    assert breaker._state() == "half-open"


# --------------------------------------------------------------------------
# retry_with_backoff
# --------------------------------------------------------------------------


def test_succeeds_without_retrying_when_the_first_attempt_works() -> None:
    fn = _FailNTimes(n=0)
    assert retry_with_backoff(fn, max_attempts=3, base_delay=0, max_delay=0) == "ok"
    assert fn.calls == 1


def test_retries_up_to_max_attempts_then_gives_up(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.core.resilience as resilience_module

    monkeypatch.setattr(resilience_module.time, "sleep", lambda _: None)  # no real waiting in tests
    fn = _FailNTimes(n=100)
    with pytest.raises(RuntimeError, match="transient failure #3"):
        retry_with_backoff(fn, max_attempts=3, base_delay=0.01, max_delay=0.01)
    assert fn.calls == 3


def test_succeeds_on_a_later_attempt_within_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.core.resilience as resilience_module

    monkeypatch.setattr(resilience_module.time, "sleep", lambda _: None)
    fn = _FailNTimes(n=2)
    assert retry_with_backoff(fn, max_attempts=3, base_delay=0.01, max_delay=0.01) == "ok"
    assert fn.calls == 3


def test_only_retries_the_specified_exception_types(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.core.resilience as resilience_module

    monkeypatch.setattr(resilience_module.time, "sleep", lambda _: None)

    def raises_value_error() -> None:
        raise ValueError("not retryable here")

    with pytest.raises(ValueError):
        retry_with_backoff(raises_value_error, max_attempts=3, retry_on=(RuntimeError,))
