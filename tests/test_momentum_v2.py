"""T18 — momentum v2 sharp-move decision layer (REQ-2D-502, AC-2D-502).

The v2 layer sits on top of :mod:`veridex.strategies.sharp_stats`: logit-space movement,
EWMA-smoothed levels, robust (median/MAD, scale-floored) z-score, a DIRECTIONAL Page-Hinkley
change-point, and a persistence run — gated behind warmup, min-samples, and a per-market cooldown.
It is a PROPOSER ONLY: it emits ``FOLLOW_MOMENTUM``; the deterministic law scores edge/CLV. Any
``reason``/``claimed_edge_bps`` is untrusted UX metadata.

Fixtures are the OPERATING-CURVE tapes — deterministic, committed seeded integer-bps arrays (home
side; away = 10000 - home), authored once with a fixed RNG then frozen here. NO runtime randomness.
The demo claim they make provable: **v2 reduces naive-momentum false positives while still catching
sustained TxLINE repricing** — NOT "v2 proves the line move is sharp money".
"""

from __future__ import annotations

from veridex.ingest.marketstate import MarketState
from veridex.runtime.orchestrator import Agent
from veridex.runtime.schemas import AgentAction, SportsActionType
from veridex.strategies.momentum import (
    MOMENTUM_V1_LABEL,
    SHARP_MOMENTUM_V2_LABEL,
    STRATEGY_LABELS,
    MomentumStrategy,
    SharpMomentumStrategy,
    is_clean_family,
    sharp_momentum_agent,
)

MARKET = "1X2_HOME"  # a clean family (1X2)
FALSE_POSITIVE_BUDGET = 0  # v2's allowed false positives on a pure-noise tape

# --- Operating-curve tapes (committed, seeded, deterministic) --------------------------------
# (a) NULL / NOISE: mean 5000, ~35 bps sd, no drift — the false-positive-budget tape.
_TAPE_NOISE = [4963, 4978, 5053, 4977, 4998, 4990, 4944, 5005, 5013, 4999, 5015, 5060, 5015, 5030, 4958, 4981, 4983, 4998, 5015, 4956, 4977, 5025, 4960, 5014, 5007, 5025, 4999, 4945, 4971, 5035, 5065, 4994, 5048, 5043, 4983, 5037, 4994, 4975, 5005, 4957]  # noqa: E501
# (b) INJECTED SHARP move: flat warmup then a strong sustained rise (home).
_TAPE_SHARP = [5000, 5009, 4992, 4973, 4986, 4970, 5002, 5040, 4985, 4981, 5015, 5011, 5003, 4972, 5132, 5306, 5425, 5603, 5734, 5906, 6055, 6247, 6387, 6578, 6582, 6572, 6502, 6561, 6576, 6581, 6532, 6563]  # noqa: E501
# (c) SLOW DRIFT: gentle +11 bps/tick drift with noise — v1 false-fires; v2 should be quieter.
_TAPE_DRIFT = [5001, 5052, 5059, 5018, 5035, 5039, 5083, 5075, 5110, 5044, 5157, 5118, 5152, 5139, 5143, 5179, 5201, 5181, 5193, 5230, 5194, 5186, 5254, 5233, 5206, 5251, 5272, 5261, 5263, 5320, 5357, 5334, 5330, 5375, 5396, 5376, 5412, 5438, 5412, 5405]  # noqa: E501
# (d) SINGLE OUTLIER: noise around 5000 with one +550 bps spike at idx 20 that reverts.
_TAPE_OUTLIER = [5002, 4986, 5002, 5021, 4947, 5051, 4986, 4982, 4969, 5028, 5020, 5037, 5027, 5008, 5010, 5028, 4974, 4999, 5011, 4986, 5572, 4989, 5020, 5004, 5014, 4954, 4974, 5040, 5005, 4998, 5029, 5023, 4999, 4981, 5059, 5021, 4953, 5025, 5023, 5024]  # noqa: E501
# (e) SUSTAINED REPRICING: flat warmup then a ramp to a new level that holds (the catch case).
_TAPE_REPRICE = [5037, 4954, 5008, 4990, 4992, 4996, 4964, 4996, 4984, 5060, 5004, 4994, 4995, 4988, 4981, 4993, 5150, 5289, 5457, 5590, 5743, 5916, 6051, 6185, 6340, 6501, 6540, 6496, 6496, 6521, 6483, 6495, 6519, 6513, 6503, 6514]  # noqa: E501
# (f) DOWN then small UP bounce (home): a sustained DOWN move then a small up bounce on home.
_TAPE_DOWN_UP = [5008, 4974, 5019, 5024, 4951, 4967, 5003, 4992, 5000, 4979, 5022, 5019, 5002, 5028, 4887, 4713, 4585, 4411, 4294, 4127, 3975, 3816, 3878, 3864, 3862, 3862]  # noqa: E501
# Short volatility-noise tape that nets > 50 bps inside v1's lookback (v1 false-fires, v2 quiet).
_TAPE_V1_TRAP = [5000, 5030, 4995, 5040, 5010, 5050, 5015, 5055]


def _state(tick_seq: int, home_bps: int, *, market_key: str = MARKET) -> MarketState:
    return MarketState(
        fixture_id=1,
        tick_seq=tick_seq,
        ts=1000 + tick_seq,
        phase=2,
        markets={
            market_key: {
                "stable_prob_bps": {"home": home_bps, "away": 10000 - home_bps},
                "stable_price": {"home": 2.0, "away": 2.0},
                "suspended": False,
            }
        },
        scores={},
    )


def _run_v2(series: list[int], *, market_key: str = MARKET, **kwargs: float) -> list[AgentAction]:
    strat = SharpMomentumStrategy(**kwargs)  # type: ignore[arg-type]
    return [strat.decide(_state(i, h, market_key=market_key)) for i, h in enumerate(series)]


def _fire_indices(actions: list[AgentAction]) -> list[int]:
    return [i for i, a in enumerate(actions) if a.type == SportsActionType.FOLLOW_MOMENTUM]


def _v1_fire_count(series: list[int]) -> int:
    strat = MomentumStrategy(min_momentum_bps=50)
    return sum(strat.decide(_state(i, h)).type == SportsActionType.FOLLOW_MOMENTUM for i, h in enumerate(series))


# ------------------------------------------------------------------------------------------
# Operating curve — the false-positive-budget suite
# ------------------------------------------------------------------------------------------


def test_curve_a_null_noise_under_false_positive_budget() -> None:
    v2 = _run_v2(_TAPE_NOISE)
    assert len(_fire_indices(v2)) <= FALSE_POSITIVE_BUDGET  # v2 stays quiet on pure noise
    assert _v1_fire_count(_TAPE_NOISE) > 5  # ...while v1's raw delta false-fires repeatedly


def test_curve_b_injected_sharp_fires_correct_direction() -> None:
    v2 = _run_v2(_TAPE_SHARP)
    fires = _fire_indices(v2)
    assert fires, "v2 must catch a genuine sharp, sustained rise"
    assert all(v2[i].params["side"] == "home" for i in fires)  # follows the RISING side


def test_curve_c_slow_drift_quieter_than_v1() -> None:
    v2_fires = len(_fire_indices(_run_v2(_TAPE_DRIFT)))
    v1_fires = _v1_fire_count(_TAPE_DRIFT)
    assert v2_fires < v1_fires  # v2 is strictly quieter on an ordinary slow drift
    assert v2_fires == 0


def test_curve_d_single_outlier_does_not_fire() -> None:
    assert _fire_indices(_run_v2(_TAPE_OUTLIER)) == []  # a lone spike-and-revert never fires


def test_curve_e_sustained_repricing_fires_after_warmup() -> None:
    warmup = 10
    fires = _fire_indices(_run_v2(_TAPE_REPRICE, warmup_ticks=warmup))
    assert fires, "a flat market that genuinely reprices must eventually fire"
    assert min(fires) >= warmup  # never before warmup completes
    for a, b in zip(fires, fires[1:], strict=False):
        assert b - a > 3  # consecutive fires respect the cooldown window


# ------------------------------------------------------------------------------------------
# Directional correctness (Codex #1) + v1-vs-v2 (AC-2D-502)
# ------------------------------------------------------------------------------------------


def test_directional_down_then_up_bounce_never_fires_the_up_side() -> None:
    # A sustained DOWN move on home then a small UP bounce on home must NOT emit a home
    # FOLLOW_MOMENTUM: a downward Page-Hinkley change-point can never confirm an up move.
    actions = _run_v2(_TAPE_DOWN_UP)
    home_fires = [
        i
        for i, a in enumerate(actions)
        if a.type == SportsActionType.FOLLOW_MOMENTUM and a.params["side"] == "home"
    ]
    assert home_fires == []
    # (The genuinely-rising AWAY side may fire during home's fall — that is correct, not a bug.)


def test_v2_stays_quiet_where_v1_false_positives() -> None:
    # AC-2D-502: v1's raw last-first delta CROSSES its threshold on ordinary noise; v2 stays quiet.
    v1 = MomentumStrategy(min_momentum_bps=50)
    v1_actions = [v1.decide(_state(i, h)) for i, h in enumerate(_TAPE_V1_TRAP)]
    assert any(a.type == SportsActionType.FOLLOW_MOMENTUM for a in v1_actions), "v1 should false-fire here"
    # v2 with no warmup barrier still refuses (robust-z + directional-PH + persistence disagree).
    v2_actions = _run_v2(_TAPE_V1_TRAP, warmup_ticks=0, min_movements=2)
    assert all(a.type == SportsActionType.WAIT for a in v2_actions)


# ------------------------------------------------------------------------------------------
# Gates: warmup, min-samples, cooldown
# ------------------------------------------------------------------------------------------


def test_warmup_gate_suppresses_all_early_action() -> None:
    # Default warmup: no fire before tick 10. A huge warmup gates the whole tape.
    default_fires = _fire_indices(_run_v2(_TAPE_SHARP))
    assert default_fires and min(default_fires) >= 10
    assert _fire_indices(_run_v2(_TAPE_SHARP, warmup_ticks=100)) == []


def test_min_movements_gate_requires_enough_samples() -> None:
    # With warmup disabled, a fire still cannot occur before `min_movements` samples accumulate.
    min_movements = 12
    fires = _fire_indices(_run_v2(_TAPE_SHARP, warmup_ticks=0, min_movements=min_movements))
    assert all(i >= min_movements for i in fires)


def test_cooldown_suppresses_otherwise_firing_ticks() -> None:
    cooldown = 3
    with_cd = _fire_indices(_run_v2(_TAPE_SHARP, cooldown_ticks=cooldown))
    without_cd = _fire_indices(_run_v2(_TAPE_SHARP, cooldown_ticks=0))
    assert with_cd, "the sharp move must fire at least once"
    # Without cooldown the raw signal holds on CONSECUTIVE ticks; cooldown thins them out.
    assert any(b - a == 1 for a, b in zip(without_cd, without_cd[1:], strict=False))
    assert len(with_cd) < len(without_cd)
    first = with_cd[0]
    default = _run_v2(_TAPE_SHARP, cooldown_ticks=cooldown)
    for i in range(first + 1, min(first + 1 + cooldown, len(_TAPE_SHARP))):
        assert default[i].type == SportsActionType.WAIT
    for a, b in zip(with_cd, with_cd[1:], strict=False):
        assert b - a > cooldown


# ------------------------------------------------------------------------------------------
# Backtest-integrity: determinism + no lookahead
# ------------------------------------------------------------------------------------------


def _typed(actions: list[AgentAction]) -> list[tuple[str, str | None]]:
    return [(a.type.value, a.params.get("side")) for a in actions]


def test_v2_is_deterministic_same_ticks_same_actions() -> None:
    assert _typed(_run_v2(_TAPE_SHARP)) == _typed(_run_v2(_TAPE_SHARP))


def test_v2_has_no_lookahead() -> None:
    # Tick-k action on the prefix [:k+1] equals the tick-k action on the full sequence, for all k.
    full = _typed(_run_v2(_TAPE_SHARP))
    for k in range(len(_TAPE_SHARP)):
        prefix = _typed(_run_v2(_TAPE_SHARP[: k + 1]))
        assert prefix[k] == full[k], f"tick {k} decision changed when future ticks were appended"


# ------------------------------------------------------------------------------------------
# Clean-family gate, proposer-only, config_hash, labels
# ------------------------------------------------------------------------------------------


def test_is_clean_family_accepts_1x2_and_totals_defers_props() -> None:
    assert is_clean_family("1X2_HOME")
    assert is_clean_family("1X2_PARTICIPANT_RESULT")
    assert is_clean_family("OU_2_5")
    assert not is_clean_family("PLAYER_PROP_ASSISTS")
    assert not is_clean_family("ANYTIME_SCORER")


def test_v2_ignores_prop_markets() -> None:
    # The same sharp ramp on a prop-family market never produces a signal.
    assert _fire_indices(_run_v2(_TAPE_SHARP, market_key="PLAYER_PROP_POINTS")) == []


def test_v2_action_carries_only_untrusted_metadata() -> None:
    fire = next(a for a in _run_v2(_TAPE_SHARP) if a.type == SportsActionType.FOLLOW_MOMENTUM)
    # Names a target (market_key/side) and only UNTRUSTED UX metadata — never a scored edge.
    assert set(fire.params) <= {"market_key", "side", "reason", "claimed_edge_bps"}
    assert fire.params["market_key"] == MARKET
    assert fire.params["side"] == "home"


def test_sharp_agent_is_reproducible_proof_and_proposer_only() -> None:
    agent = sharp_momentum_agent("mom-sharp")
    assert agent.agent_id == "mom-sharp"
    assert agent.proof_mode == "reproducible"
    assert agent.config_hash is not None


def _config_hash(agent: Agent, snapshot: MarketState) -> str:
    assert agent.config_hash is not None
    return agent.config_hash(snapshot)


def test_all_behavioural_params_enter_config_hash() -> None:
    # A backtest is reproducible-by-config only if EVERY behavioural param changes the sealed hash.
    snapshot = MarketState(fixture_id=1, tick_seq=0, ts=0, phase=2, markets={}, scores={})
    base_hash = _config_hash(sharp_momentum_agent("x"), snapshot)
    variants = [
        sharp_momentum_agent("x", alpha=0.9),
        sharp_momentum_agent("x", z_threshold=9.0),
        sharp_momentum_agent("x", ph_delta=0.999),
        sharp_momentum_agent("x", ph_lambda=9.0),
        sharp_momentum_agent("x", cooldown_ticks=99),
        sharp_momentum_agent("x", warmup_ticks=99),
        sharp_momentum_agent("x", min_movements=99),
        sharp_momentum_agent("x", lookback=999),
        sharp_momentum_agent("x", scale_floor=0.9),
        sharp_momentum_agent("x", persistence_logit=0.9),
    ]
    for variant in variants:
        assert _config_hash(variant, snapshot) != base_hash


def test_strategy_labels_name_the_two_variants() -> None:
    assert STRATEGY_LABELS["momentum"] == MOMENTUM_V1_LABEL
    assert STRATEGY_LABELS["momentum-sharp"] == SHARP_MOMENTUM_V2_LABEL
    assert sharp_momentum_agent().agent_id == "momentum-sharp"  # default id maps to the v2 label
