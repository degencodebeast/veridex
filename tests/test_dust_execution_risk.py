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

from veridex.dust_execution.contracts import CancelAllAck, CancelAllTriggeredEvent
from veridex.dust_execution.emergency import DustSafetySession, SafetyController
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
_FROZEN_CLOCK_MS = 1_700_000_000_500
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


# --- SAF-002d: a loss-cap crossing ATOMICALLY blocks + fires the SAME cancel-all sweep ----


class _RecordingSweepAdapter:
    """Venue cancel-all double: ``calls`` moves ONLY when the sweep wire is actually awaited.

    ``canceled_count`` models the resting GTC orders the venue reports swept — a mere
    submit-block/deny flag flip inside the controller can NEVER move ``calls``, so the test
    proves the venue SWEEP fired (the resting orders were proactively cancelled), not merely
    that a deny flag was set (SAF-002d).
    """

    def __init__(self, *, canceled_count: int) -> None:
        self.calls = 0
        self._canceled_count = canceled_count

    async def cancel_all_orders(self) -> int:
        self.calls += 1
        return self._canceled_count


async def test_cap_crossing_while_gtc_rest_blocks_and_sweeps() -> None:
    """A realized fill crossing ``max_session_loss`` ATOMICALLY blocks submits AND fires the SAME
    cancel-all sweep — it does NOT merely deny the next quote while resting GTC orders keep filling.

    Two resting GTC orders are modelled by the recording-fake's ``canceled_count=2``. Feeding a
    real fill whose fee-inclusive loss (0.61) crosses ``max_session_loss=0.50`` must, in the SAME
    transition: (a) fire the venue cancel-all WIRE (recording-fake ``calls == 1`` — a deny-flag
    flip can NEVER move it), (b) block new submits, (c) emit CancelAllTriggeredEvent/CancelAllAck
    carrying ``trigger_cause == "loss_breach"`` and the swept count, never a single order id.
    """
    controller = SafetyController(clock_ms=lambda: _FROZEN_CLOCK_MS)
    session = DustSafetySession(session_id=_SESSION)
    acc = RiskAccumulator(session_id=_SESSION)
    rec = _RecordingSweepAdapter(canceled_count=2)  # two resting GTC orders at the venue

    # Precondition: submits are admitted BEFORE the breach fill lands.
    assert controller.check_can_submit(session) is True

    ack = await controller.on_realized_fill(
        _real_fill(-0.60, 0.01),  # fee-inclusive loss 0.61 > cap 0.50
        adapter=rec,
        session=session,
        risk=acc,
        envelope=_env(max_session_loss=0.50),
    )

    # (a) SWEEP wire ACTUALLY fired — the two resting GTC orders were swept (recording-fake rule),
    #     NOT a deny-only flag flip.
    assert rec.calls == 1
    # (b) new submits blocked in the SAME transition (no resting order can fill through the breach).
    assert controller.check_can_submit(session) is False
    assert session.submit_blocked is True
    # (c) cause-labelled "loss_breach" on the ack + swept count, never a single order id.
    assert isinstance(ack, CancelAllAck)
    assert ack.trigger_cause == "loss_breach"
    assert ack.canceled_count == 2
    assert "venue_order_id" not in ack.model_dump()
    # The CancelAllTriggeredEvent carries the loss_breach cause (SAF-002d audit fidelity, §4.1).
    triggered = [e for e in session.events if isinstance(e, CancelAllTriggeredEvent)]
    assert len(triggered) == 1
    assert triggered[0].trigger_cause == "loss_breach"
    assert "venue_order_id" not in triggered[0].model_dump()


async def test_daily_cap_crossing_also_blocks_and_sweeps() -> None:
    """The DAILY cap has its OWN atomic breach-sweep path (independent of the session cap)."""
    controller = SafetyController(clock_ms=lambda: _FROZEN_CLOCK_MS)
    session = DustSafetySession(session_id=_SESSION)
    acc = RiskAccumulator(session_id=_SESSION)
    rec = _RecordingSweepAdapter(canceled_count=3)

    ack = await controller.on_realized_fill(
        _real_fill(-0.60, 0.01),  # fee-inclusive daily loss 0.61 > cap 0.50
        adapter=rec,
        session=session,
        risk=acc,
        envelope=_env(max_daily_loss=0.50),  # session cap disabled
    )

    assert rec.calls == 1
    assert controller.check_can_submit(session) is False
    assert isinstance(ack, CancelAllAck)
    assert ack.trigger_cause == "loss_breach"
    assert ack.canceled_count == 3


async def test_realized_fill_below_cap_does_not_block_or_sweep() -> None:
    """A fill that does NOT cross either cap fires NO sweep and leaves submits open (no false stop).

    Distinguishes the atomic breach-sweep from a broad "every fill sweeps" bug: the wire is
    fired ONLY on a crossing.
    """
    controller = SafetyController(clock_ms=lambda: _FROZEN_CLOCK_MS)
    session = DustSafetySession(session_id=_SESSION)
    acc = RiskAccumulator(session_id=_SESSION)
    rec = _RecordingSweepAdapter(canceled_count=2)

    result = await controller.on_realized_fill(
        _real_fill(-0.10, 0.00),  # loss 0.10 < cap 0.50 — no breach
        adapter=rec,
        session=session,
        risk=acc,
        envelope=_env(max_session_loss=0.50),
    )

    assert result is None
    assert rec.calls == 0  # NO sweep on a non-crossing fill
    assert controller.check_can_submit(session) is True
    assert session.submit_blocked is False
    # The fill WAS folded into the accumulator (the accumulator is still fed on non-breach fills).
    assert acc.realized_loss_session == pytest.approx(0.10)


async def test_paper_receipt_cannot_drive_the_breach_sweep() -> None:
    """The anti-inert control holds on the coordinating method: a paper source is rejected AT
    SOURCE (``ValueError``) before any breach check — a simulated fill can never fire the sweep."""
    controller = SafetyController(clock_ms=lambda: _FROZEN_CLOCK_MS)
    session = DustSafetySession(session_id=_SESSION)
    acc = RiskAccumulator(session_id=_SESSION)
    rec = _RecordingSweepAdapter(canceled_count=2)

    with pytest.raises(ValueError):
        await controller.on_realized_fill(
            PaperReceipt(simulated_pnl=-99.0, session_id=_SESSION),  # type: ignore[arg-type]
            adapter=rec,
            session=session,
            risk=acc,
            envelope=_env(max_session_loss=0.50),
        )
    assert rec.calls == 0
    assert controller.check_can_submit(session) is True
