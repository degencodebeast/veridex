"""WD-2 — deterministic trend-continuation strategy (proposes a side; law scores CLV)."""

from __future__ import annotations

from veridex.ingest.marketstate import MarketState
from veridex.runtime.schemas import SportsActionType
from veridex.strategies.momentum import (
    MomentumStrategy,
    momentum_agent,
    prob_momentum,
    select_momentum_action,
)


def _state(tick_seq: int, home_bps: int, away_bps: int) -> MarketState:
    return MarketState(
        fixture_id=1,
        tick_seq=tick_seq,
        ts=tick_seq,
        phase=1,
        markets={
            "M": {
                "stable_prob_bps": {"home": home_bps, "away": away_bps},
                "stable_price": {"home": 2.0, "away": 2.0},
                "suspended": False,
            }
        },
        scores={},
    )


def test_prob_momentum_is_last_minus_first() -> None:
    history = [{"home": 4800}, {"home": 5000}, {"home": 5300}]
    assert prob_momentum(history, "home") == 500
    assert prob_momentum([{"home": 5000}], "home") == 0  # single observation → no momentum


def test_select_flags_strongest_riser_above_threshold() -> None:
    history_by_market = {"M": [{"home": 4800, "away": 5200}, {"home": 5300, "away": 4700}]}
    action = select_momentum_action(history_by_market, min_momentum_bps=50)
    assert action.type == SportsActionType.FOLLOW_MOMENTUM
    assert action.params["market_key"] == "M"
    assert action.params["side"] == "home"  # +500 bps riser, away is falling


def test_select_waits_when_no_riser_clears_threshold() -> None:
    history_by_market = {"M": [{"home": 5000, "away": 5000}, {"home": 5010, "away": 4990}]}
    action = select_momentum_action(history_by_market, min_momentum_bps=50)
    assert action.type == SportsActionType.WAIT


def test_strategy_is_stateful_and_deterministic() -> None:
    strat = MomentumStrategy(min_momentum_bps=50)
    a0 = strat.decide(_state(0, 4800, 5200))  # only 1 obs → WAIT
    a1 = strat.decide(_state(1, 5300, 4700))  # home rose +500 → FOLLOW_MOMENTUM home
    assert a0.type == SportsActionType.WAIT
    assert a1.type == SportsActionType.FOLLOW_MOMENTUM
    assert a1.params["side"] == "home"
    # Re-running the same sequence on a fresh strategy reproduces the actions exactly.
    strat2 = MomentumStrategy(min_momentum_bps=50)
    assert strat2.decide(_state(0, 4800, 5200)).type == SportsActionType.WAIT
    assert strat2.decide(_state(1, 5300, 4700)).params["side"] == "home"


def test_momentum_agent_factory_is_reproducible_proof_mode() -> None:
    agent = momentum_agent("mom")
    assert agent.agent_id == "mom"
    assert agent.proof_mode == "reproducible"
    assert agent.config_hash is not None
