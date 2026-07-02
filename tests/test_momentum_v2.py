"""T18 — momentum v2 sharp-move decision layer (REQ-2D-502, AC-2D-502).

The v2 layer sits on top of :mod:`veridex.strategies.sharp_stats`: logit-space movement,
EWMA-smoothed levels, robust (median/MAD) z-score, and a Page-Hinkley change-point confirmation,
gated behind a per-market cooldown. It is a PROPOSER ONLY — it emits ``FOLLOW_MOMENTUM`` actions;
the deterministic law scores edge/CLV. Any ``reason``/``claimed_edge_bps`` is untrusted UX metadata.

All fixtures are committed integer bps arrays — NO runtime randomness anywhere.
"""

from __future__ import annotations

from veridex.ingest.marketstate import MarketState
from veridex.runtime.orchestrator import Agent
from veridex.runtime.schemas import AgentAction, SportsActionType
from veridex.strategies.momentum import (
    MomentumStrategy,
    SharpMomentumStrategy,
    is_clean_family,
    sharp_momentum_agent,
)

MARKET = "1X2_HOME"  # a clean family (1X2)


def _state(tick_seq: int, home_bps: int, away_bps: int, *, market_key: str = MARKET) -> MarketState:
    return MarketState(
        fixture_id=1,
        tick_seq=tick_seq,
        ts=1000 + tick_seq,
        phase=2,
        markets={
            market_key: {
                "stable_prob_bps": {"home": home_bps, "away": away_bps},
                "stable_price": {"home": 2.0, "away": 2.0},
                "suspended": False,
            }
        },
        scores={},
    )


def _run(strategy: object, series_home: list[int]) -> list[tuple[str, str | None]]:
    """Feed a home-bps series to ``strategy``; return per-tick (action_type, side)."""
    actions: list[tuple[str, str | None]] = []
    for i, home in enumerate(series_home):
        action = strategy.decide(_state(i, home, 10000 - home))  # type: ignore[attr-defined]
        actions.append((action.type.value, action.params.get("side")))
    return actions


# Committed integer-bps fixtures ---------------------------------------------------------------

# A genuine SHARP move: micro-noise prefix (so MAD > 0) then a strong sustained ramp up.
_SHARP = [5000, 5020, 4990, 5010, 4995, 5015, 5300, 5600, 5900, 6200, 6500]
# Ordinary VOLATILITY NOISE: a gentle, oscillating drift that nets > 50 bps within v1's 8-tick
# lookback (so v1's raw last-first delta false-fires) but contains no sharp, sustained,
# statistically-significant single move — so v2's robust-z + Page-Hinkley never confirm.
_NOISE = [5000, 5030, 4995, 5040, 5010, 5050, 5015, 5055]
# A long sustained ramp — the raw v2 condition holds for many consecutive ticks (cooldown test).
_SUSTAINED = [5000, 5020, 4990, 5010, 4995, 5015, 5300, 5600, 5900, 6200, 6500, 6800, 7100, 7400]


# ------------------------------------------------------------------------------------------
# Core signal
# ------------------------------------------------------------------------------------------


def test_v2_fires_follow_momentum_on_a_genuine_sharp_move() -> None:
    actions = _run(SharpMomentumStrategy(), _SHARP)
    fires = [(i, side) for i, (t, side) in enumerate(actions) if t == SportsActionType.FOLLOW_MOMENTUM.value]
    assert fires, "v2 must flag a genuine sharp, sustained rise"
    assert all(side == "home" for _, side in fires)  # it follows the RISING side


def test_v2_stays_quiet_where_v1_false_positives() -> None:
    # AC-2D-502: v1's raw last-first delta CROSSES its threshold on ordinary noise (false
    # positive); v2 (robust-z + Page-Hinkley) stays quiet on the SAME ticks. Proves v2 != v1.
    v1 = MomentumStrategy(min_momentum_bps=50)
    v1_actions = _run(v1, _NOISE)
    assert any(t == SportsActionType.FOLLOW_MOMENTUM.value for t, _ in v1_actions), "v1 should false-fire here"

    v2_actions = _run(SharpMomentumStrategy(), _NOISE)
    assert all(t == SportsActionType.WAIT.value for t, _ in v2_actions), "v2 must stay quiet on noise"


# ------------------------------------------------------------------------------------------
# Backtest-integrity properties
# ------------------------------------------------------------------------------------------


def test_v2_is_deterministic_same_ticks_same_actions() -> None:
    # AC-2D-502: identical tick sequence → identical actions, on two independent instances.
    assert _run(SharpMomentumStrategy(), _SHARP) == _run(SharpMomentumStrategy(), _SHARP)


def test_v2_has_no_lookahead() -> None:
    # The decision at tick k is UNCHANGED by future ticks: computing on the prefix [:k+1] yields
    # the same tick-k action as computing on the full sequence. The critical backtest property.
    full = _run(SharpMomentumStrategy(), _SHARP)
    for k in range(len(_SHARP)):
        prefix = _run(SharpMomentumStrategy(), _SHARP[: k + 1])
        assert prefix[k] == full[k], f"tick {k} decision changed when future ticks were appended"


def test_v2_cooldown_suppresses_refire() -> None:
    cooldown = 3
    actions = _run(SharpMomentumStrategy(cooldown_ticks=cooldown), _SUSTAINED)
    fire_idx = [i for i, (t, _) in enumerate(actions) if t == SportsActionType.FOLLOW_MOMENTUM.value]
    assert fire_idx, "the sustained ramp must fire at least once"
    first = fire_idx[0]
    # The next `cooldown` ticks after a fire suppress a refire even though the raw signal holds.
    for i in range(first + 1, min(first + 1 + cooldown, len(actions))):
        assert actions[i][0] == SportsActionType.WAIT.value, f"tick {i} should be suppressed by cooldown"
    # Consecutive fires are never closer together than the cooldown window.
    for a, b in zip(fire_idx, fire_idx[1:], strict=False):
        assert b - a > cooldown


# ------------------------------------------------------------------------------------------
# Clean-family gate
# ------------------------------------------------------------------------------------------


def test_is_clean_family_accepts_1x2_and_totals_defers_props() -> None:
    assert is_clean_family("1X2_HOME")
    assert is_clean_family("1X2_PARTICIPANT_RESULT")
    assert is_clean_family("OU_2_5")
    assert not is_clean_family("PLAYER_PROP_ASSISTS")
    assert not is_clean_family("ANYTIME_SCORER")


def test_v2_ignores_prop_markets() -> None:
    # A prop-family market never produces a signal, even on an identical sharp ramp.
    strat = SharpMomentumStrategy()
    out = [
        strat.decide(_state(i, home, 10000 - home, market_key="PLAYER_PROP_POINTS"))
        for i, home in enumerate(_SHARP)
    ]
    assert all(a.type == SportsActionType.WAIT for a in out)


# ------------------------------------------------------------------------------------------
# Proposer-only + config_hash reproducibility
# ------------------------------------------------------------------------------------------


def test_sharp_agent_is_reproducible_proof_and_proposer_only() -> None:
    agent = sharp_momentum_agent("mom-sharp")
    assert agent.agent_id == "mom-sharp"
    assert agent.proof_mode == "reproducible"
    assert agent.config_hash is not None


def test_v2_action_carries_only_untrusted_metadata() -> None:
    actions = _run(SharpMomentumStrategy(), _SHARP)
    fire = next(a for a in (_run_actions_objs(_SHARP)) if a.type == SportsActionType.FOLLOW_MOMENTUM)
    # The proposal names a target (market_key/side) and only UNTRUSTED UX metadata — never a
    # scored/self-certified edge. The law computes edge; the strategy must not.
    assert set(fire.params) <= {"market_key", "side", "reason", "claimed_edge_bps"}
    assert fire.params["market_key"] == MARKET
    assert fire.params["side"] == "home"
    assert any(t == SportsActionType.FOLLOW_MOMENTUM.value for t, _ in actions)


def _run_actions_objs(series_home: list[int]) -> list[AgentAction]:
    strat = SharpMomentumStrategy()
    return [strat.decide(_state(i, home, 10000 - home)) for i, home in enumerate(series_home)]


def _config_hash(agent: Agent, snapshot: MarketState) -> str:
    assert agent.config_hash is not None
    return agent.config_hash(snapshot)


def test_all_five_params_enter_config_hash() -> None:
    # A backtest is reproducible-by-config only if EVERY param changes the sealed config hash.
    snapshot = MarketState(fixture_id=1, tick_seq=0, ts=0, phase=2, markets={}, scores={})
    base_hash = _config_hash(sharp_momentum_agent("x"), snapshot)
    variants = [
        sharp_momentum_agent("x", alpha=0.9),
        sharp_momentum_agent("x", z_threshold=9.0),
        sharp_momentum_agent("x", ph_delta=0.999),
        sharp_momentum_agent("x", ph_lambda=9.0),
        sharp_momentum_agent("x", cooldown_ticks=99),
    ]
    for variant in variants:
        assert _config_hash(variant, snapshot) != base_hash
