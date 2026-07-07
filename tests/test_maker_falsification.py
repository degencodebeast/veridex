from veridex.maker.falsification import falsify


def test_clear_candidate_advantage_is_separated():
    naive = [-50]*40; cand = [80]*40
    r = falsify(naive, cand)
    assert r.verdict == "SEPARATED" and r.ci_low_bps > 0 and r.delta_bps > 0


def test_overlapping_distributions_are_inconclusive():
    naive = [10, -10, 5, -5]*10; cand = [8, -12, 6, -4]*10
    r = falsify(naive, cand)
    assert r.verdict == "INCONCLUSIVE"


def test_is_deterministic_under_fixed_seed():
    naive = [-50]*40; cand = [80]*40
    assert falsify(naive, cand).model_dump() == falsify(naive, cand).model_dump()
