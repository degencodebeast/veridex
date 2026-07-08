"""E4-T1/T2: extended MM-R1.5 trade-aware diagnostics under the no-fill boundary.

The extended :class:`AdverseSelectionReport` adds report-only, ``_diagnostic``-suffixed
metrics that abstain to ``None`` when there is no resolvable near-trade + fair value,
and an ``independent_reference_verdict`` tautology-breaker. ``real_executable_edge_bps``
stays literal ``None`` — trades are diagnostics, never fills, and ``.size`` is never read.
"""

import pytest

from veridex.maker.diagnostic import (
    AdverseSelectionReport,
    compute_trade_aware_diagnostic,
)
from veridex.maker.trades import AggressorSide, TradePrint


# --- E4-T1 --------------------------------------------------------------------
def test_extended_report_fields_none_by_default_and_no_edge():
    r = AdverseSelectionReport(trades_near_quote_count=0)
    assert r.signed_flow_pressure_bps_diagnostic is None and r.picked_off_pressure_diagnostic is None
    assert r.real_executable_edge_bps is None
    with pytest.raises(Exception):
        AdverseSelectionReport(real_executable_edge_bps=5)  # literal None
    with pytest.raises(Exception):
        AdverseSelectionReport(pnl=5)  # extra=forbid


# --- E4-T2 --------------------------------------------------------------------
def _tp(ts, price, side, size=1.0):
    return TradePrint(ts=ts, price=price, size=size, aggressor_side=side, condition_id="0xc", token_id="42")


def test_diagnostic_abstains_when_no_fv_after():
    # two near-quote trades but fv_at returns None (no resolvable future FV) -> metrics None, verdict INSUFFICIENT_DATA
    trades = [_tp(0, 0.51, AggressorSide.BUY), _tp(30, 0.49, AggressorSide.SELL)]
    rep = compute_trade_aware_diagnostic(trades=trades, fv_at=lambda ts: None, quote_price=0.50)
    assert rep.signed_flow_pressure_bps_diagnostic is None
    assert rep.independent_reference_verdict == "INSUFFICIENT_DATA"


def test_diagnostic_ignores_trade_size():
    # resolvable rising FV so metrics ARE computed; doubling every .size must leave every diagnostic unchanged
    T = [_tp(0, 0.51, AggressorSide.BUY), _tp(30, 0.52, AggressorSide.BUY)]
    F = lambda ts: {0: 0.50, 30: 0.55, 60: 0.60, 90: 0.62}.get(ts)
    base = compute_trade_aware_diagnostic(trades=T, fv_at=F, quote_price=0.50)
    doubled = compute_trade_aware_diagnostic(
        trades=[t.model_copy(update={"size": t.size * 2}) for t in T], fv_at=F, quote_price=0.50
    )
    assert base == doubled  # `.size` is never read


# --- E4-T2 Item-10 (Gate-#2 pre-empt): concrete SEPARATED / INCONCLUSIVE verdicts ----
def test_diagnostic_verdict_separated():
    # buys near the quote precede a rising FV -> positive post-trade markout separates the
    # candidate; AND the falsification agrees (SEPARATED) -> verdict SEPARATED.
    T = [_tp(0, 0.51, AggressorSide.BUY), _tp(30, 0.51, AggressorSide.BUY)]
    F = lambda ts: {0: 0.50, 30: 0.52, 60: 0.60, 90: 0.62}.get(ts)
    rep = compute_trade_aware_diagnostic(
        trades=T, fv_at=F, quote_price=0.50, window_s=30, falsification_verdict="SEPARATED"
    )
    assert rep.post_trade_fv_markout_bps_diagnostic is not None
    assert rep.post_trade_fv_markout_bps_diagnostic > 0  # real-trade markout separates the candidate
    assert rep.independent_reference_verdict == "SEPARATED"


def test_diagnostic_verdict_inconclusive():
    # SAME separating real-trade markout, but the falsification DISAGREES (INCONCLUSIVE):
    # the two references disagree -> the tautology-breaker returns INCONCLUSIVE, not SEPARATED.
    T = [_tp(0, 0.51, AggressorSide.BUY), _tp(30, 0.51, AggressorSide.BUY)]
    F = lambda ts: {0: 0.50, 30: 0.52, 60: 0.60, 90: 0.62}.get(ts)
    rep = compute_trade_aware_diagnostic(
        trades=T, fv_at=F, quote_price=0.50, window_s=30, falsification_verdict="INCONCLUSIVE"
    )
    assert rep.post_trade_fv_markout_bps_diagnostic > 0  # markout DOES separate
    assert rep.independent_reference_verdict == "INCONCLUSIVE"  # but references disagree


def test_candidate_vs_naive_toxicity_delta_and_size_agnostic_verdict():
    # the candidate-vs-naive delta is a pure subtraction; None if either operand absent.
    T = [_tp(0, 0.51, AggressorSide.BUY), _tp(30, 0.52, AggressorSide.BUY)]
    F = lambda ts: {0: 0.50, 30: 0.55, 60: 0.60, 90: 0.62}.get(ts)
    rep = compute_trade_aware_diagnostic(
        trades=T, fv_at=F, quote_price=0.50,
        candidate_toxicity_loss_bps=40, naive_toxicity_loss_bps=100,
    )
    assert rep.candidate_vs_naive_toxicity_delta_bps_diagnostic == -60
    # only one operand -> None (cannot compute a delta)
    rep2 = compute_trade_aware_diagnostic(
        trades=T, fv_at=F, quote_price=0.50, candidate_toxicity_loss_bps=40
    )
    assert rep2.candidate_vs_naive_toxicity_delta_bps_diagnostic is None
