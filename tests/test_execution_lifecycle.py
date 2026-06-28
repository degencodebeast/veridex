"""Tests for veridex.execution lifecycle state machine + normalized receipt.

Covers:
- Happy-path forward transitions through the full execution lifecycle.
- AWAITING_HUMAN branch (law_approved → awaiting_human → policy_approved).
- AWAITING_HUMAN rejection (awaiting_human → rejected).
- Illegal skip transitions raise ExecutionTransitionError.
- Terminal states are dead ends.
- Receipt round-trips via Pydantic without exposing secrets.
- Additional edge cases: accepted→partial→settled, law_approved→rejected.
"""

import pytest

from veridex.execution.models import (
    ExecutionReceipt,
    ExecutionRecord,
    ExecutionStatus,
    ExecutionTransitionError,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rec(status: ExecutionStatus = ExecutionStatus.PROPOSED) -> ExecutionRecord:
    """Build a minimal ExecutionRecord for testing."""
    return ExecutionRecord(
        execution_id="e1",
        competition_id="c1",
        run_id="r1",
        agent_id="a",
        source_sequence_no=3,
        status=status,
        policy_hash="ph",
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_advances() -> None:
    """Full mainline: PROPOSED → ... → SETTLED."""
    r = _rec()
    for nxt in [
        ExecutionStatus.LAW_APPROVED,
        ExecutionStatus.POLICY_APPROVED,
        ExecutionStatus.SUBMITTED,
        ExecutionStatus.ACCEPTED,
        ExecutionStatus.FILLED,
        ExecutionStatus.SETTLED,
    ]:
        r.advance(nxt)
    assert r.status is ExecutionStatus.SETTLED


# ---------------------------------------------------------------------------
# AWAITING_HUMAN branch
# ---------------------------------------------------------------------------


def test_awaiting_human_path() -> None:
    """law_approved → awaiting_human → policy_approved (approval re-check passed)."""
    r = _rec(ExecutionStatus.LAW_APPROVED)
    r.advance(ExecutionStatus.AWAITING_HUMAN)
    r.advance(ExecutionStatus.POLICY_APPROVED)
    assert r.status is ExecutionStatus.POLICY_APPROVED


def test_awaiting_human_can_reject() -> None:
    """awaiting_human → rejected (approval re-check failed / human said no)."""
    r = _rec(ExecutionStatus.AWAITING_HUMAN)
    r.advance(ExecutionStatus.REJECTED)
    assert r.status is ExecutionStatus.REJECTED


# ---------------------------------------------------------------------------
# Illegal transitions
# ---------------------------------------------------------------------------


def test_illegal_skip_raises() -> None:
    """proposed → submitted skips law + policy gates — must be blocked."""
    with pytest.raises(ExecutionTransitionError):
        _rec().advance(ExecutionStatus.SUBMITTED)


def test_terminal_is_dead_end() -> None:
    """No transition is valid from a terminal status."""
    with pytest.raises(ExecutionTransitionError):
        _rec(ExecutionStatus.REJECTED).advance(ExecutionStatus.SUBMITTED)


def test_same_status_raises() -> None:
    """Self-loop transition is illegal (no _NEXT_EXEC entry maps a status to itself)."""
    with pytest.raises(ExecutionTransitionError):
        _rec(ExecutionStatus.PROPOSED).advance(ExecutionStatus.PROPOSED)


def test_backward_transition_raises() -> None:
    """Back-stepping from SUBMITTED to PROPOSED must be blocked."""
    r = _rec(ExecutionStatus.LAW_APPROVED)
    r.advance(ExecutionStatus.POLICY_APPROVED)
    r.advance(ExecutionStatus.SUBMITTED)
    with pytest.raises(ExecutionTransitionError):
        r.advance(ExecutionStatus.PROPOSED)


# ---------------------------------------------------------------------------
# Edge-case paths
# ---------------------------------------------------------------------------


def test_accepted_partial_settled() -> None:
    """accepted → partial → settled is a valid partial-fill path."""
    r = _rec(ExecutionStatus.ACCEPTED)
    r.advance(ExecutionStatus.PARTIAL)
    r.advance(ExecutionStatus.SETTLED)
    assert r.status is ExecutionStatus.SETTLED


def test_law_approved_can_reject_directly() -> None:
    """law_approved → rejected (immediate rejection without policy stage)."""
    r = _rec(ExecutionStatus.LAW_APPROVED)
    r.advance(ExecutionStatus.REJECTED)
    assert r.status is ExecutionStatus.REJECTED


def test_filled_can_void() -> None:
    """filled → voided is valid (e.g. venue rollback)."""
    r = _rec(ExecutionStatus.FILLED)
    r.advance(ExecutionStatus.VOIDED)
    assert r.status is ExecutionStatus.VOIDED


def test_filled_can_unresolved() -> None:
    """filled → unresolved is valid (e.g. settlement dispute)."""
    r = _rec(ExecutionStatus.FILLED)
    r.advance(ExecutionStatus.UNRESOLVED)
    assert r.status is ExecutionStatus.UNRESOLVED


def test_accepted_can_expire() -> None:
    """accepted → expired is valid (order timed out at venue)."""
    r = _rec(ExecutionStatus.ACCEPTED)
    r.advance(ExecutionStatus.EXPIRED)
    assert r.status is ExecutionStatus.EXPIRED


def test_submitted_can_expire() -> None:
    """submitted → expired is valid (venue never acknowledged)."""
    r = _rec(ExecutionStatus.SUBMITTED)
    r.advance(ExecutionStatus.EXPIRED)
    assert r.status is ExecutionStatus.EXPIRED


def test_voided_is_terminal() -> None:
    """voided is a terminal state — nothing may follow."""
    with pytest.raises(ExecutionTransitionError):
        _rec(ExecutionStatus.VOIDED).advance(ExecutionStatus.FILLED)


def test_cancelled_is_terminal() -> None:
    """cancelled is a terminal state — nothing may follow."""
    with pytest.raises(ExecutionTransitionError):
        _rec(ExecutionStatus.CANCELLED).advance(ExecutionStatus.FILLED)


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------


def test_receipt_round_trips_no_secrets() -> None:
    """Receipt serialises to dict without exposing token/auth/secret fields."""
    rcpt = ExecutionReceipt(
        execution_id="e1",
        venue="sx_bet",
        market_ref="m",
        side="over",
        requested_size=100.0,
        filled_size=100.0,
        price=2.05,
        status=ExecutionStatus.FILLED,
        venue_order_id="o1",
        mode="dry_run",
    )
    dumped = rcpt.model_dump()
    assert dumped["status"] == "filled"
    assert "token" not in dumped
    assert "auth" not in dumped
    assert "secret" not in dumped
    assert "key" not in dumped


def test_receipt_optional_timestamps_default_none() -> None:
    """submitted_at and settled_at default to None when omitted."""
    rcpt = ExecutionReceipt(
        execution_id="e2",
        venue="betfair",
        market_ref="mr1",
        side="under",
        requested_size=50.0,
        filled_size=0.0,
        price=1.85,
        status=ExecutionStatus.SUBMITTED,
        venue_order_id=None,
        mode="paper",
    )
    assert rcpt.submitted_at is None
    assert rcpt.settled_at is None


def test_receipt_with_timestamps() -> None:
    """submitted_at and settled_at are stored when provided."""
    rcpt = ExecutionReceipt(
        execution_id="e3",
        venue="pinnacle",
        market_ref="mr2",
        side="over",
        requested_size=200.0,
        filled_size=200.0,
        price=1.91,
        status=ExecutionStatus.SETTLED,
        venue_order_id="ord-999",
        mode="live_guarded",
        submitted_at="2026-01-01T10:00:00Z",
        settled_at="2026-01-01T11:00:00Z",
    )
    assert rcpt.submitted_at == "2026-01-01T10:00:00Z"
    assert rcpt.settled_at == "2026-01-01T11:00:00Z"


def test_record_with_receipt_attached() -> None:
    """ExecutionRecord can carry an attached receipt after settlement."""
    rcpt = ExecutionReceipt(
        execution_id="e1",
        venue="sx_bet",
        market_ref="m",
        side="over",
        requested_size=100.0,
        filled_size=100.0,
        price=2.05,
        status=ExecutionStatus.SETTLED,
        venue_order_id="o1",
        mode="dry_run",
        settled_at="2026-01-01T12:00:00Z",
    )
    r = _rec(ExecutionStatus.FILLED)
    r.advance(ExecutionStatus.SETTLED)
    r.receipt = rcpt
    assert r.receipt.status is ExecutionStatus.SETTLED
