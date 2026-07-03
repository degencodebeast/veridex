"""M5 (S4) — deterministic baselines: Task 15."""

from __future__ import annotations

from veridex.backtest.baselines import BASELINES


def test_four_named_baselines_all_flagged_baseline_not_alpha():
    assert set(BASELINES) == {"no_trade", "favorite", "threshold_move", "seeded_random"}
    for fn in BASELINES.values():
        assert getattr(fn, "is_baseline", False) is True


def test_no_trade_always_waits():
    action = BASELINES["no_trade"](prices=[0.5, 0.7], horizon_s=3600)
    assert action.type == "WAIT"


def test_seeded_random_is_deterministic_for_a_seed():
    a = BASELINES["seeded_random"](prices=[0.5, 0.6, 0.7], horizon_s=3600, seed=42)
    b = BASELINES["seeded_random"](prices=[0.5, 0.6, 0.7], horizon_s=3600, seed=42)
    assert a.type == b.type


def test_favorite_backs_the_highest_fair_prob_side():
    action = BASELINES["favorite"](fair_probs={"home": 0.4, "away": 0.6}, horizon_s=3600)
    assert action.type == "FOLLOW_MOMENTUM"
    assert action.params["side"] == "away"


def test_favorite_waits_when_no_sides_are_eligible():
    action = BASELINES["favorite"](fair_probs={}, horizon_s=3600)
    assert action.type == "WAIT"


def test_threshold_move_fires_when_move_clears_the_pct_threshold():
    action = BASELINES["threshold_move"](
        prices=[0.50, 0.50, 0.65], horizon_s=3600, move_threshold_pct=2.0
    )
    assert action.type == "FOLLOW_MOMENTUM"


def test_threshold_move_waits_when_move_is_below_threshold():
    action = BASELINES["threshold_move"](
        prices=[0.50, 0.505], horizon_s=3600, move_threshold_pct=50.0
    )
    assert action.type == "WAIT"
