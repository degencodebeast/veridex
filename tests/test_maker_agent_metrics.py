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


def test_avg_toxicity_loss_is_mean_of_picked_off_losses():
    # Toxicity loss = mean of max(0, -markout): a FAVORABLE mark (positive markout)
    # contributes 0; a PICKED-OFF mark (negative markout) contributes its magnitude.
    marks = [QuoteMarkout(fixture_id=1, tick_seq=0, side=Side.BID, market_key="k", horizon_s=60, markout_bps=100),  # favorable -> 0
             QuoteMarkout(fixture_id=1, tick_seq=1, side=Side.BID, market_key="k", horizon_s=60, markout_bps=-60),  # picked off -> 60
             QuoteMarkout(fixture_id=1, tick_seq=2, side=Side.BID, market_key="k", horizon_s=60, markout_bps=-30)]  # picked off -> 30
    acc = QuoteAccounting(scored=3, abstained=0, excluded={})
    m = aggregate_agent_metrics("naive-mm", marks, acc)
    # (0 + 60 + 30) / 3 = 30
    assert m["avg_toxicity_loss_bps"] == 30
    # markout retained as a diagnostic, unchanged
    assert m["avg_markout_bps"] == round((100 - 60 - 30) / 3)


def test_avg_toxicity_loss_is_zero_when_never_picked_off():
    # All-favorable agent (never adversely selected) has zero toxicity loss.
    marks = [QuoteMarkout(fixture_id=1, tick_seq=0, side=Side.BID, market_key="k", horizon_s=60, markout_bps=100),
             QuoteMarkout(fixture_id=1, tick_seq=1, side=Side.BID, market_key="k", horizon_s=60, markout_bps=200)]
    acc = QuoteAccounting(scored=2, abstained=0, excluded={})
    m = aggregate_agent_metrics("txline-fair-mm", marks, acc)
    assert m["avg_toxicity_loss_bps"] == 0


def test_avg_toxicity_loss_is_none_when_no_scored_quotes():
    acc = QuoteAccounting(scored=0, abstained=3, excluded={})
    m = aggregate_agent_metrics("naive-mm", [], acc)
    assert m["avg_toxicity_loss_bps"] is None
