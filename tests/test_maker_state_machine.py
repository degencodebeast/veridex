from veridex.maker.state_machine import MakerState, GateContext, classify_state

def _ctx(**kw):
    base = dict(suspended=False, staleness_s=0, freshness_s=120, event_cooldown_active=False,
                fair_vol_bps=0, fair_vol_widen_bps=300, inventory=0.0, inventory_extreme=0.5)
    base.update(kw); return GateContext(**base)

def test_suspension_forces_no_quote():
    assert classify_state(_ctx(suspended=True, fair_vol_bps=0)) == MakerState.NO_QUOTE

def test_stale_feed_forces_no_quote_over_widen():
    assert classify_state(_ctx(staleness_s=999, fair_vol_bps=9999)) == MakerState.NO_QUOTE

def test_event_cooldown_forces_no_quote():
    assert classify_state(_ctx(event_cooldown_active=True)) == MakerState.NO_QUOTE

def test_inventory_extreme_is_one_sided_reduce():
    assert classify_state(_ctx(inventory=0.6)) == MakerState.ONE_SIDED_REDUCE

def test_high_fair_vol_widens():
    assert classify_state(_ctx(fair_vol_bps=400)) == MakerState.WIDEN

def test_calm_is_maker_safe():
    assert classify_state(_ctx()) == MakerState.MAKER_SAFE
