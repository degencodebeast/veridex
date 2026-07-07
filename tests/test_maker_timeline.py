from veridex.maker.contracts import TargetQuote, TargetQuoteSet, Side
from veridex.maker.timeline import build_event_gate_timeline


def _qs(ts, regime, has_quotes):
    q = [TargetQuote(side=Side.BID, market_key="k", price=0.5, size=1.0)] if has_quotes else []
    return TargetQuoteSet(fixture_id=1, tick_seq=ts, ts=ts, quotes=q, regime=regime)


def test_suspend_after_quoting_is_suspend_cancel():
    tl = build_event_gate_timeline([_qs(0, "MAKER_SAFE", True), _qs(60, "NO_QUOTE", False)])
    assert tl.entries[1]["action"] == "suspend_cancel"


def test_no_quote_from_start_is_wait():
    tl = build_event_gate_timeline([_qs(0, "NO_QUOTE", False)])
    assert tl.entries[0]["action"] == "wait"
