from veridex.maker.leaderboard import window_clv_analog


def test_analog_mirrors_window_clv_shape_labeled_not_ranked():
    a = window_clv_analog(60, 10)
    assert a["window_markout_bps"] == 60 and a["window_action_count"] == 10
    assert "NOT a CLV rank axis" in a["note"]


def test_none_markout_passes_through():
    assert window_clv_analog(None, 0)["window_markout_bps"] is None
