from veridex.maker.scorer import aggregate_agent_metrics, QuoteMarkout, QuoteAccounting
from veridex.maker.contracts import Side


def test_avg_markout_is_none_when_no_scored_quotes():
    acc = QuoteAccounting(scored=0, abstained=3, excluded={})
    m = aggregate_agent_metrics("naive-mm", [], acc)
    assert m["avg_markout_bps"] is None and m["real_executable_edge_bps"] is None


def test_avg_markout_recomputed_from_marks():
    marks = [QuoteMarkout(fixture_id=1, tick_seq=0, side=Side.BID, market_key="k", horizon_s=60, markout_bps=100),
             QuoteMarkout(fixture_id=1, tick_seq=1, side=Side.BID, market_key="k", horizon_s=60, markout_bps=200)]
    acc = QuoteAccounting(scored=2, abstained=0, excluded={})
    m = aggregate_agent_metrics("txline-fair-mm", marks, acc)
    assert m["avg_markout_bps"] == 150 and m["real_executable_edge_bps"] is None
