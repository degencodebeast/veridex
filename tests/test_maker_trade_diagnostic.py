from veridex.maker.trades import TradePrint, AggressorSide
from veridex.maker.diagnostic import compute_trade_aware_diagnostic

def _tp(ts, price, side):
    return TradePrint(ts=ts, price=price, size=1.0, aggressor_side=side, condition_id="0xA", token_id="1")

def test_toxic_flow_when_buys_precede_fv_rising():
    # buys near the quote, then fair value rises → buyers were informed (toxic to a maker who sold)
    trades = [_tp(1000, 0.58, AggressorSide.BUY), _tp(1020, 0.58, AggressorSide.BUY)]
    fv = {1000: 0.60, 1020: 0.60, 1140: 0.64}
    rep = compute_trade_aware_diagnostic(trades, lambda ts: fv.get(ts), quote_price=0.59, window_s=120)
    assert rep.trades_near_quote_count == 2
    assert rep.trade_flow_preceding_fv_move_bps_diagnostic is not None
    assert rep.real_executable_edge_bps is None   # still no edge

def test_no_trades_near_quote_yields_zero_count_and_none_diagnostics():
    rep = compute_trade_aware_diagnostic([], lambda ts: 0.60, quote_price=0.59)
    assert rep.trades_near_quote_count == 0 and rep.trade_flow_preceding_fv_move_bps_diagnostic is None

def test_ratio_abstains_when_no_resolvable_fv_after():
    # one near trade but its fv-after horizon is missing → both diagnostics abstain (None), not 0.0
    trades = [_tp(1000, 0.58, AggressorSide.BUY)]
    rep = compute_trade_aware_diagnostic(trades, lambda ts: (0.60 if ts == 1000 else None),
                                         quote_price=0.59, window_s=120)
    assert rep.trades_near_quote_count == 1
    assert rep.trade_flow_preceding_fv_move_bps_diagnostic is None
    assert rep.toxic_vs_benign_flow_ratio_diagnostic is None

def test_toxic_flow_when_sells_precede_fv_falling():
    trades = [_tp(1020, 0.58, AggressorSide.SELL)]
    fv = {1020: 0.60, 1140: 0.56}   # fv falls after the sell
    rep = compute_trade_aware_diagnostic(trades, lambda ts: fv.get(ts), quote_price=0.59, window_s=120)
    assert rep.trades_near_quote_count == 1
    assert rep.trade_flow_preceding_fv_move_bps_diagnostic is not None
    assert rep.trade_flow_preceding_fv_move_bps_diagnostic > 0   # sign(-1)*(0.56-0.60) = +400 → toxic
    assert rep.real_executable_edge_bps is None
