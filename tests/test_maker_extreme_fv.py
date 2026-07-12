from veridex.maker.agents import NaiveMarketMakerAgent, TxLineFairMarketMakerAgent

def test_candidate_abstains_when_fv_near_zero_boundary():
    a = TxLineFairMarketMakerAgent(base_half_spread=0.02)
    qs = a.propose(reference_fv={"fv": 0.01}, venue_view={"mid": 0.5}, inventory={}, params={}, clock=1000)
    assert qs.quotes == []   # bid would be -0.01 → abstain (settlement zone)

def test_candidate_abstains_when_fv_near_one_boundary():
    a = TxLineFairMarketMakerAgent(base_half_spread=0.02)
    qs = a.propose(reference_fv={"fv": 0.99}, venue_view={"mid": 0.5}, inventory={}, params={}, clock=1000)
    assert qs.quotes == []   # ask would be 1.01 → abstain

def test_naive_abstains_when_mid_near_boundary():
    a = NaiveMarketMakerAgent(fixed_half_spread=0.02)
    qs = a.propose(reference_fv={}, venue_view={"mid": 0.005}, inventory={}, params={}, clock=1000)
    assert qs.quotes == []

def test_candidate_still_quotes_interior_fv():
    a = TxLineFairMarketMakerAgent(base_half_spread=0.02)
    qs = a.propose(reference_fv={"fv": 0.50}, venue_view={"mid": 0.5}, inventory={}, params={}, clock=1000)
    assert len(qs.quotes) == 2   # interior → normal two-sided
