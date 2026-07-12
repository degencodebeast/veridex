"""Tests for the R4-A realized-loss risk accumulator + Mode B fail-closed cap validation.

Covers SAF-002/002a (§4.4): fee-inclusive realized-PnL accounting fed ONLY from real,
venue-reconciled fills (never paper/simulated), session + UTC-day identity, the two new
``PolicyEnvelope`` loss caps enforced in ``evaluate_pre_quote``, and Mode B admission that
fails closed on a non-finite / non-positive / disabled cap.

The anti-inert control is by TYPE: ``RiskAccumulator.apply_realized_fill`` accepts ONLY a
real ``RealizedFillRecord`` and REJECTS a ``PaperReceipt`` at the source (a paper/simulated
fill can never move a real-money loss cap).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from veridex.dust_execution.risk import (
    FailClosed,
    PaperReceipt,
    RealizedFillRecord,
    RiskAccumulator,
    authorize_mode_b,
)
from veridex.policy.engine import PolicyDecision
from veridex.policy.envelope import PolicyEnvelope
from veridex.policy.gate import PreQuoteContext, evaluate_pre_quote

_SESSION = "sess-001"
# A fixed UTC-day timestamp (2026-07-06T12:00:00Z) in integer milliseconds.
_TS_DAY1 = int(datetime(2026, 7, 6, 12, 0, 0, tzinfo=UTC).timestamp() * 1000)
_TS_DAY2 = int(datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC).timestamp() * 1000)


def _env(**kw: object) -> PolicyEnvelope:
    base: dict[str, object] = {
        "max_stake": 100.0,
        "max_orders_per_run": 5,
        "max_orders_per_session": 20,
        "max_orders_per_day": 50,
        "venue_allowlist": ["sx_bet"],
        "market_allowlist": ["OU|2.5|full"],
        "min_edge_bps": 50,
        "max_slippage_bps": 100,
        "max_price": 3.0,
        "max_quote_age_s": 10,
        "cooldown_s": 0,
        "human_approval_threshold": 1000.0,
        "kill_switch": False,
    }
    base.update(kw)
    return PolicyEnvelope(**base)  # type: ignore[arg-type]


def _pre(**kw: object) -> PreQuoteContext:
    base: dict[str, object] = {
        "recomputed_edge_bps": 120,
        "stake": 50.0,
        "venue": "sx_bet",
        "market_key": "OU|2.5|full",
        "orders_this_run": 0,
        "seconds_since_last_order": None,
        "agent_eligible": True,
    }
    base.update(kw)
    return PreQuoteContext(**base)  # type: ignore[arg-type]


def _real_fill(realized_pnl: float, fee: float, *, ts_ms: int = _TS_DAY1) -> RealizedFillRecord:
    return RealizedFillRecord(
        realized_pnl=realized_pnl,
        fee=fee,
        session_id=_SESSION,
        fill_ts_ms=ts_ms,
    )


# --- SAF-002(b): fee-inclusive realized accounting fed ONLY from real fills --------------


def test_realized_fill_pnl_trips_session_loss_cap_but_paper_source_cannot() -> None:
    """A REAL losing fill trips the session loss cap; a PaperReceipt is rejected AT SOURCE.

    max_session_loss=0.50; a real fill with realized_pnl=-0.60, fee=0.01 -> fee-inclusive
    session loss 0.61 > 0.50 -> DENIED + ``session_loss_over_max``. A PaperReceipt can never
    feed the accumulator (ValueError), so a simulated source can NEVER move a real-money cap.
    """
    acc = RiskAccumulator(session_id=_SESSION)
    acc.apply_realized_fill(_real_fill(-0.60, 0.01))
    assert acc.realized_loss_session == pytest.approx(0.61)

    r = evaluate_pre_quote(
        _pre(realized_loss_session=acc.realized_loss_session),
        _env(max_session_loss=0.50),
    )
    assert r.decision is PolicyDecision.DENIED
    assert "session_loss_over_max" in r.reason_codes

    # Anti-inert control: a paper/simulated receipt is rejected by TYPE at the source.
    with pytest.raises(ValueError):
        acc.apply_realized_fill(PaperReceipt(simulated_pnl=-99.0, session_id=_SESSION))  # type: ignore[arg-type]


def test_realized_fill_pnl_trips_DAILY_loss_cap() -> None:
    """The daily cap has its OWN deny path (independent of the session cap)."""
    acc = RiskAccumulator(session_id=_SESSION)
    acc.apply_realized_fill(_real_fill(-0.60, 0.01))
    assert acc.realized_loss_day == pytest.approx(0.61)

    r = evaluate_pre_quote(
        _pre(realized_loss_day=acc.realized_loss_day),
        _env(max_daily_loss=0.50),  # session cap disabled
    )
    assert r.decision is PolicyDecision.DENIED
    assert "daily_loss_over_max" in r.reason_codes
    assert "session_loss_over_max" not in r.reason_codes


def test_accumulator_is_fee_inclusive_and_offsets_gains() -> None:
    """Loss magnitude is fee-inclusive; a later gain reduces the standing loss (>= 0)."""
    acc = RiskAccumulator(session_id=_SESSION)
    acc.apply_realized_fill(_real_fill(-0.60, 0.01))  # net -0.61
    assert acc.realized_loss_session == pytest.approx(0.61)
    acc.apply_realized_fill(_real_fill(0.20, 0.00))  # net -0.41
    assert acc.realized_loss_session == pytest.approx(0.41)
    acc.apply_realized_fill(_real_fill(1.00, 0.00))  # net +0.59 -> not at a loss
    assert acc.realized_loss_session == pytest.approx(0.0)


def test_utc_day_boundary_resets_daily_not_session() -> None:
    """Daily loss resets on the UTC-day boundary; session loss keeps accumulating."""
    acc = RiskAccumulator(session_id=_SESSION)
    acc.apply_realized_fill(_real_fill(-0.30, 0.00, ts_ms=_TS_DAY1))
    acc.apply_realized_fill(_real_fill(-0.40, 0.00, ts_ms=_TS_DAY2))
    assert acc.realized_loss_day == pytest.approx(0.40)  # only day-2 loss
    assert acc.realized_loss_session == pytest.approx(0.70)  # both days


def test_apply_realized_fill_rejects_wrong_session_id() -> None:
    """A fill from a different session identity fails closed (immutable session identity)."""
    acc = RiskAccumulator(session_id=_SESSION)
    stray = RealizedFillRecord(realized_pnl=-1.0, fee=0.0, session_id="other", fill_ts_ms=_TS_DAY1)
    with pytest.raises(FailClosed):
        acc.apply_realized_fill(stray)


def test_realized_fill_record_rejects_non_finite_pnl_or_negative_fee() -> None:
    """The real-fill carrier itself fails closed on a non-finite pnl/fee or a negative fee."""
    for bad in (math.inf, -math.inf, math.nan):
        with pytest.raises(ValueError):
            RealizedFillRecord(realized_pnl=bad, fee=0.0, session_id=_SESSION, fill_ts_ms=_TS_DAY1)
        with pytest.raises(ValueError):
            RealizedFillRecord(realized_pnl=0.0, fee=bad, session_id=_SESSION, fill_ts_ms=_TS_DAY1)
    with pytest.raises(ValueError):
        RealizedFillRecord(realized_pnl=0.0, fee=-0.01, session_id=_SESSION, fill_ts_ms=_TS_DAY1)


# --- SAF-002(a): Mode B admission fails closed on a bad/disabled cap ---------------------


def test_mode_b_rejects_disabled_or_nonfinite_cap() -> None:
    """Mode B REQUIRES finite positive caps; disabled/non-finite/non-positive fails closed."""
    for bad in (0.0, -1.0, math.inf, math.nan):
        with pytest.raises(FailClosed):
            authorize_mode_b(_env(max_session_loss=bad, max_daily_loss=0.50))
        with pytest.raises(FailClosed):
            authorize_mode_b(_env(max_session_loss=0.50, max_daily_loss=bad))


def test_mode_b_authorizes_finite_positive_caps() -> None:
    """Both caps finite + positive -> Mode B admission passes (no raise)."""
    authorize_mode_b(_env(max_session_loss=0.50, max_daily_loss=1.00))
