from veridex.maker.contracts import Side
from veridex.maker.agents import NaiveMarketMakerAgent, TxLineFairMarketMakerAgent


def test_naive_quotes_symmetric_around_venue_mid():
    a = NaiveMarketMakerAgent(fixed_half_spread=0.02)
    qs = a.propose(reference_fv={}, venue_view={"mid": 0.60}, inventory={}, params={}, clock=1000)
    prices = {q.side: q.price for q in qs.quotes}
    assert prices[Side.BID] == 0.58 and prices[Side.ASK] == 0.62


def test_candidate_quotes_around_txline_fv_not_venue_mid():
    a = TxLineFairMarketMakerAgent(base_half_spread=0.02)
    qs = a.propose(reference_fv={"fv": 0.50}, venue_view={"mid": 0.60}, inventory={}, params={}, clock=1000)
    prices = {q.side: q.price for q in qs.quotes}
    assert prices[Side.BID] == 0.48 and prices[Side.ASK] == 0.52   # anchored to FV=0.50, ignores venue mid


def test_candidate_no_quotes_when_suspended():
    a = TxLineFairMarketMakerAgent(base_half_spread=0.02)
    qs = a.propose(reference_fv={"fv": 0.50, "suspended": True}, venue_view={"mid": 0.60}, inventory={}, params={}, clock=1000)
    assert qs.quotes == []   # NO_QUOTE
