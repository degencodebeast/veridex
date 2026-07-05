"""Permanent regression gate for M8 (StaleLine decision gate) — Task 20, Part A.

DEFERRAL: StaleLineAgent is intentionally NOT built — S1b sub-minute cadence is unproven (no live
recorder, no captured quote session). This gate makes the block permanent; the agent is deferred
until a real recorder demonstrates cadence_sufficient=True on a captured fixture (CON-S1B-004).
"""

from veridex.venues.quote_recorder import VenueQuoteFrame, cadence_report


def _f(ts):
    return VenueQuoteFrame(ts=ts, fixture_id=1, market_ref="m", condition_id="0x",
        token_id="t", best_bid_decimal=1.9, best_ask_decimal=2.1, bid_size=1, ask_size=1, quote_status="live")


def test_stale_line_is_blocked_without_sufficient_cadence():
    sparse = cadence_report([_f(0), _f(300)])          # 5-min gap -> insufficient cadence
    assert sparse["cadence_sufficient"] is False
    # the S6 evaluation must refuse to include stale-line under this cadence:
    from veridex.backtest.evaluation import EvalProtocol, run_multi_fixture_evaluation
    proto = EvalProtocol(protocol_id="p", fixture_ids=[1], strategy_configs=["stale-line"],
        window="pre_match", close_semantics="con-040_last_pre_inrunning",
        baselines=[], committed_at="2026-07-05T00:00:00Z")
    out = run_multi_fixture_evaluation(proto, results_by_fixture={}, cadence_ok=sparse["cadence_sufficient"])
    assert out["stale_line_included"] is False
