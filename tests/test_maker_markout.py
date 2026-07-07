import pytest
from veridex.maker.contracts import Side
from veridex.maker.markout import forward_markout_bps, assert_native_prob, MarkoutError

def test_bid_markout_positive_when_ref_rises_above_quote():
    # bid at 0.58, future fair 0.62 → good placement, positive markout
    assert forward_markout_bps(side=Side.BID, quote_price=0.58, ref_now=0.60, ref_future=0.62) > 0

def test_ask_markout_positive_when_ref_falls_below_quote():
    assert forward_markout_bps(side=Side.ASK, quote_price=0.62, ref_now=0.60, ref_future=0.58) > 0

def test_decimal_odds_operand_is_rejected_not_mis_scaled():
    # a decimal-odds price (e.g. 1.667) must RAISE, never silently compute (Run-002 bug class)
    with pytest.raises(MarkoutError):
        forward_markout_bps(side=Side.BID, quote_price=1.667, ref_now=0.60, ref_future=0.62)
    with pytest.raises(MarkoutError):
        forward_markout_bps(side=Side.BID, quote_price=0.58, ref_now=0.60, ref_future=1.05)

def test_assert_native_prob_bounds():
    assert assert_native_prob(0.0, "x") == 0.0 and assert_native_prob(1.0, "x") == 1.0
    with pytest.raises(MarkoutError):
        assert_native_prob(-0.01, "x")
