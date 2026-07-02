"""Pure circuit-breaker state machine (REQ-2D-404).

Deterministic, I/O-free: same event sequence -> same state. Any time input is INJECTED so the
breaker is reproducible (no wall clock, no randomness). This is the state the policy PRE-quote
gate consults; the breaker itself never decides -- the gate reads :meth:`CircuitBreaker.allows`.
"""

from __future__ import annotations

from pathlib import Path

from veridex.policy.circuit_breaker import CircuitBreaker, CircuitState
from veridex.verifier.import_audit import assert_no_llm_imports

_THRESHOLD = 3
_COOLDOWN_S = 60.0


def _open_breaker(*, opened_at: float = 0.0) -> CircuitBreaker:
    """Drive a fresh breaker to OPEN via ``_THRESHOLD`` consecutive failures."""
    cb = CircuitBreaker()
    for _ in range(_THRESHOLD):
        cb = cb.record_failure(threshold=_THRESHOLD, now=opened_at)
    return cb


def test_starts_closed_and_allows() -> None:
    cb = CircuitBreaker()
    assert cb.state is CircuitState.CLOSED
    assert cb.allows() is True


def test_closed_stays_closed_below_threshold() -> None:
    cb = CircuitBreaker()
    for _ in range(_THRESHOLD - 1):
        cb = cb.record_failure(threshold=_THRESHOLD, now=0.0)
    assert cb.state is CircuitState.CLOSED
    assert cb.allows() is True


def test_opens_after_threshold_consecutive_failures() -> None:
    cb = _open_breaker()
    assert cb.state is CircuitState.OPEN
    assert cb.allows() is False


def test_success_resets_consecutive_failures() -> None:
    cb = CircuitBreaker()
    cb = cb.record_failure(threshold=_THRESHOLD, now=0.0)
    cb = cb.record_failure(threshold=_THRESHOLD, now=0.0)
    cb = cb.record_success()  # streak cleared -> further failures start from zero
    cb = cb.record_failure(threshold=_THRESHOLD, now=0.0)
    cb = cb.record_failure(threshold=_THRESHOLD, now=0.0)
    assert cb.state is CircuitState.CLOSED  # only 2 in a row since the success


def test_threshold_zero_disables_breaker() -> None:
    cb = CircuitBreaker()
    for _ in range(10):
        cb = cb.record_failure(threshold=0, now=0.0)
    assert cb.state is CircuitState.CLOSED
    assert cb.allows() is True


def test_open_holds_before_cooldown_elapses() -> None:
    cb = _open_breaker(opened_at=0.0)
    resolved = cb.resolve(now=_COOLDOWN_S - 1, cooldown_s=_COOLDOWN_S)
    assert resolved.state is CircuitState.OPEN
    assert resolved.allows() is False


def test_open_transitions_to_half_open_after_cooldown() -> None:
    cb = _open_breaker(opened_at=0.0)
    resolved = cb.resolve(now=_COOLDOWN_S, cooldown_s=_COOLDOWN_S)
    assert resolved.state is CircuitState.HALF_OPEN
    assert resolved.allows() is True  # exactly one probe is admitted


def test_half_open_admits_exactly_one_probe() -> None:
    half = _open_breaker(opened_at=0.0).resolve(now=_COOLDOWN_S, cooldown_s=_COOLDOWN_S)
    assert half.allows() is True
    after = half.start_probe()  # dispatch the single probe
    assert after.allows() is False  # no second probe until the probe resolves


def test_half_open_probe_success_closes() -> None:
    half = _open_breaker(opened_at=0.0).resolve(now=_COOLDOWN_S, cooldown_s=_COOLDOWN_S)
    closed = half.start_probe().record_success()
    assert closed.state is CircuitState.CLOSED
    assert closed.allows() is True


def test_half_open_probe_failure_reopens() -> None:
    half = _open_breaker(opened_at=0.0).resolve(now=_COOLDOWN_S, cooldown_s=_COOLDOWN_S)
    reopened = half.start_probe().record_failure(threshold=_THRESHOLD, now=_COOLDOWN_S)
    assert reopened.state is CircuitState.OPEN
    assert reopened.allows() is False


def test_deterministic_same_events_same_state() -> None:
    def _drive() -> CircuitBreaker:
        cb = CircuitBreaker()
        cb = cb.record_failure(threshold=_THRESHOLD, now=0.0)
        cb = cb.record_failure(threshold=_THRESHOLD, now=0.0)
        cb = cb.record_failure(threshold=_THRESHOLD, now=0.0)  # -> OPEN at t=0
        cb = cb.resolve(now=_COOLDOWN_S, cooldown_s=_COOLDOWN_S)  # -> HALF_OPEN
        return cb.start_probe()

    assert _drive() == _drive()  # pure: identical event sequence -> identical state


def test_circuit_breaker_is_llm_free() -> None:
    assert_no_llm_imports(Path("veridex/policy"))
