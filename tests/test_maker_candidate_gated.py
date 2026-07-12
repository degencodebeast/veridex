from veridex.maker.contracts import Side
from veridex.maker.agents import TxLineFairMarketMakerAgent

def test_stale_feed_produces_empty_quotes_regardless_of_as_shaping():
    a = TxLineFairMarketMakerAgent(base_half_spread=0.02)
    qs = a.propose(reference_fv={"fv": 0.50, "staleness_s": 999},
                   venue_view={"mid": 0.60}, inventory={"net": 0.0},
                   params={"freshness_s": 120, "as_shaping": {"reservation_shift": 0.05}}, clock=1000)
    assert qs.quotes == [] and qs.regime == "NO_QUOTE"

def test_widen_multiplies_half_spread():
    a = TxLineFairMarketMakerAgent(base_half_spread=0.02)
    qs = a.propose(reference_fv={"fv": 0.50, "fair_vol_bps": 500},
                   venue_view={"mid": 0.60}, inventory={"net": 0.0},
                   params={"fair_vol_widen_bps": 300, "widen_multiplier": 3.0}, clock=1000)
    prices = sorted(q.price for q in qs.quotes)
    assert prices == [0.44, 0.56]   # half-spread 0.02*3 = 0.06 around FV 0.50

def test_inventory_extreme_quotes_one_side_only():
    a = TxLineFairMarketMakerAgent(base_half_spread=0.02)
    qs = a.propose(reference_fv={"fv": 0.50}, venue_view={"mid": 0.60},
                   inventory={"net": 0.9},
                   params={"inventory_extreme": 0.5}, clock=1000)
    assert len(qs.quotes) == 1 and qs.quotes[0].side == Side.ASK   # long → reduce by asking only
