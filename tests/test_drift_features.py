"""II-7 — the shared ``drift_features`` projector + golden byte-parity of the drift decision.

This proves the REFACTOR-WITH-PARITY contract: the drift-feature math was extracted VERBATIM out of
:class:`~veridex.strategies.drift.CumulativeDriftStrategy` into ONE pure projector, and the strategy
now CONSUMES that projector. The load-bearing test is golden parity — the refactored strategy must
reproduce the exact pre-refactor decision sequence on ``wd2_momentum_replay.json`` (captured below as
committed literals derived from the pre-refactor run, NOT by importing the old code). The projector is
the stable contract II-8 (LLM-Drift) consumes, so its snapshot shape + signature are pinned here too.
"""

from __future__ import annotations

from veridex.ingest.marketstate import replay_marketstates
from veridex.strategies.drift import CumulativeDriftStrategy, cumulative_drift_agent
from veridex.strategies.drift_features import (
    DriftFeatureParams,
    DriftFeatureSnapshot,
    drift_features,
)
from veridex.ingest.marketstate import MarketState
from veridex.strategies.sharp_stats import logit

FIXTURE = "tests/fixtures/wd2_momentum_replay.json"

# Discriminating params so the 4-tick fixture actually exercises firing (a meaningful golden, not an
# all-abstain one). SAME kwargs were used to capture the pre-refactor golden below.
_GOLDEN_KW = dict(
    min_tick_count=3,
    min_horizon_s=0,
    cum_drift_logit_min=0.05,
    trend_strength_min=0.5,
    cooldown_ticks=0,
    close_quality_required=True,
)

# The pre-refactor decision sequence, captured by replaying the fixture through the CURRENT
# (pre-refactor) ``CumulativeDriftStrategy`` under ``_GOLDEN_KW``. Committed as literals — the byte
# target the refactored strategy must reproduce exactly (same firing drift values, same abstentions,
# same order, same untrusted UX metadata).
_GOLDEN: list[dict] = [
    {"type": "WAIT", "params": {}},
    {"type": "WAIT", "params": {}},
    {
        "type": "FOLLOW_MOMENTUM",
        "params": {
            "market_key": "M",
            "side": "home",
            "reason": "cumulative drift +0.200 logit, sustained trend",
            "claimed_edge_bps": 20,
        },
    },
    {
        "type": "FOLLOW_MOMENTUM",
        "params": {
            "market_key": "M",
            "side": "home",
            "reason": "cumulative drift +0.321 logit, sustained trend",
            "claimed_edge_bps": 32,
        },
    },
]

# Pinned pre-refactor sealed identity for the DEFAULT drift config (captured before the refactor).
# The refactor must not add/remove/reorder any behavioural param in the hash → this stays byte-equal.
_PINNED_DEFAULT_CONFIG_HASH = "127d3de6bfe9ae9a80d1d81bd1a1151d3c98d51806d37e00707d9b7d797f90d0"


def _params(alpha: float = 0.2) -> DriftFeatureParams:
    return DriftFeatureParams(ewma_slope_alpha=alpha)


def _series(bps: list[int]) -> list[float]:
    return [logit(b / 10000.0) for b in bps]


# ---------------------------------------------------------------------------------------------------
# 1. Determinism — same inputs ⇒ same snapshot incl. evidence_hash; changed window ⇒ different hash.
# ---------------------------------------------------------------------------------------------------


def test_projector_is_deterministic() -> None:
    series = _series([4800, 5000, 5300, 5600])
    a = drift_features(series, 1000, 4000, _params())
    b = drift_features(series, 1000, 4000, _params())
    assert a == b
    assert a.evidence_hash == b.evidence_hash


def test_changed_input_window_changes_evidence_hash() -> None:
    base = drift_features(_series([4800, 5000, 5300, 5600]), 1000, 4000, _params())
    shifted = drift_features(_series([4800, 5000, 5300, 5700]), 1000, 4000, _params())  # last tick moved
    assert shifted.evidence_hash != base.evidence_hash


# ---------------------------------------------------------------------------------------------------
# 2. Golden parity (load-bearing) — refactored strategy is byte-identical to the pre-refactor golden.
# ---------------------------------------------------------------------------------------------------


def test_golden_decision_sequence_is_byte_identical() -> None:
    strat = CumulativeDriftStrategy(**_GOLDEN_KW)
    got = [
        {"type": a.type.value, "params": dict(a.params)}
        for a in (strat.decide(ms) for ms in replay_marketstates(FIXTURE))
    ]
    assert got == _GOLDEN


# ---------------------------------------------------------------------------------------------------
# 3. Identity unchanged — the sealed config_hash for a representative state equals its pinned value.
# ---------------------------------------------------------------------------------------------------


def test_sealed_identity_is_unchanged() -> None:
    ms = MarketState(fixture_id=5, tick_seq=0, ts=0, phase=2, markets={}, scores={})
    assert cumulative_drift_agent().config_hash(ms) == _PINNED_DEFAULT_CONFIG_HASH


# ---------------------------------------------------------------------------------------------------
# 4. Purity — snapshot is a total function of its inputs; the call mutates none of its arguments.
# ---------------------------------------------------------------------------------------------------


def test_projector_is_pure_and_non_mutating() -> None:
    series = _series([4800, 5000, 5300, 5600])
    before = list(series)
    snap = drift_features(series, 1000, 4000, _params())
    assert isinstance(snap, DriftFeatureSnapshot)
    assert series == before  # argument list was not mutated
    again = drift_features(series, 1000, 4000, _params())
    assert snap == again  # no hidden decision state between calls


def test_snapshot_fields_match_verbatim_feature_math() -> None:
    # The 9-field contract, computed the SAME way the pre-refactor _score_side computed them.
    series = _series([4800, 5000, 5300, 5600])
    snap = drift_features(series, 1000, 4000, _params())
    assert snap.first == series[0]
    assert snap.current == series[-1]
    assert snap.cum_logit_drift == series[-1] - series[0]
    assert snap.tick_count == 4
    assert snap.horizon_s == 3000
    # trend_strength IS the EWMA-slope in the deterministic drift projector (rising series ⇒ +1).
    assert snap.trend_strength == snap.ewma_slope == 1.0
    assert isinstance(snap.evidence_hash, str) and len(snap.evidence_hash) == 64
