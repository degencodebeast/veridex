"""Frozen neutral contract guards (MM-R4-B — REQ-020/021/025/060/063 + construction invariants).

These are behavioral, not import, tests: each one pins one load-bearing invariant of the
pure-tier neutral contracts so a later "natural mistake" (a size field on an intent, an FV
element leaked to the top level, a stray/omitted reason code, a `WIDEN` decision kind, a
non-sentinel market-status leg, a future-dated timestamp) is caught by an assertion — never by
a missing-symbol collection error.
"""

from __future__ import annotations

from typing import get_args

import pytest
from pydantic import ValidationError

from veridex.dust_execution.analysis import (
    ScopedNegativeRelabelError,
    reject_scoped_negative_relabel,
)
from veridex.mm_strategy.config import StrategyConfig
from veridex.mm_strategy.contracts import (
    CALIBRATION_LABEL,
    EDGE_LABEL,
    EVIDENCE_CLASS,
    RUN_LABEL,
    STRATEGY_ID,
    STRATEGY_REVISION,
    DecisionKind,
    GuardFairValue,
    InventoryProjection,
    MarketStatus,
    NeutralIntent,
    ReasonCode,
    StrategyObservation,
)

# The §4.4 closed reason-code vocabulary, VERBATIM and in declared order (35 codes). Pinned here
# as the independent authority so any add/remove/rename in the contract fails the exact-image
# test — the vocabulary enters decisions, hashes, telemetry, denylists, and replay, so a change
# is behavior-bearing (REQ-063 / Codex-plan-review MAJOR-5 attack 3).
_SPEC_4_4_REASON_CODES = frozenset(
    {
        "stale_observation",
        "clock_regression",
        "epoch_regression",
        "token_mapping_missing",
        "book_gap",
        "book_excluded",
        "book_stale",
        "book_thin",
        "level_count_low",
        "leg_skew",
        "boundary_zone",
        "leg_out_of_zone",
        "two_sided_zone_exit",
        "tick_regime_changed",
        "phase_transition",
        "market_status_unknown",
        "market_halted",
        "market_closed",
        "stream_degraded",
        "projection_stale",
        "event_cooldown",
        "event_ref_warmup",
        "txline_missing",
        "txline_stale",
        "txline_suspended",
        "basis_warmup",
        "prematch_basis_exceeds_spread",
        "residual_pull_ask",
        "residual_pull_bid",
        "residual_extreme",
        "inventory_reduce",
        "reduce_conflict",
        "hold_unchanged",
        "plan_frozen_pending_reconcile",
        "cancel_exposure_first",
    }
)

# The closed decision taxonomy (REQ-060). `WIDEN` and `TAKE` are CUT from v0.
_SPEC_DECISION_KINDS = frozenset(
    {
        "QUOTE_TWO_SIDED",
        "QUOTE_ONE_SIDED",
        "ONE_SIDED_REDUCE",
        "NO_QUOTE",
        "HOLD",
    }
)


def _healthy_inventory() -> InventoryProjection:
    return InventoryProjection(
        net_position=0.0,
        resting=(),
        projection_as_of_ts=1_000,
        fresh=True,
    )


def _make_observation(**overrides: object) -> StrategyObservation:
    """A healthy, guard-off baseline observation; `overrides` perturb single fields per test."""
    base: dict[str, object] = {
        "fixture_id": 42,
        "market_ref": "TEAM-A/YES",
        "side": "YES",
        "token_id": "tok-1",
        "venue_market_ref": "0xmarket",
        "tick_size": 0.01,
        "observation_sequence": 10,
        "book_source_epoch": 1,
        "bid": 0.49,
        "ask": 0.51,
        "bid_size": 100.0,
        "ask_size": 120.0,
        "book_status": "ok",
        "status_reason": None,
        "book_recv_ts": 1_000,
        "level_count_in_band": 5,
        "tick_regime_changed": False,
        "phase": 1,
        "suspended": False,
        "match_state_recv_ts": 990,
        "guard_fv": None,
        "market_status": "ACTIVE",
        "market_status_recv_ts": 995,
        "market_status_epoch": 3,
        "order_stream_ok": True,
        "projection_fresh": True,
        "inventory": _healthy_inventory(),
        "as_of_ts": 1_000,
    }
    base.update(overrides)
    return StrategyObservation(**base)


def _guard_leg(**overrides: object) -> GuardFairValue:
    base: dict[str, object] = {
        "fv": 0.50,
        "fv_source_ts": 1,
        "fv_recv_ts": 999,
        "fv_source_epoch": 2,
        "message_id": "msg-1",
        "proof_status": "proven",
    }
    base.update(overrides)
    return GuardFairValue(**base)


def test_reason_code_is_exact_closed_image() -> None:
    # The Literal image must equal the pinned §4.4 vocabulary EXACTLY — no extra member, none
    # missing. Adding a stray `microprice_pull` or dropping any code fails here.
    assert frozenset(get_args(ReasonCode)) == _SPEC_4_4_REASON_CODES


def test_decision_kind_is_exact_closed_image() -> None:
    # No `WIDEN`, no `TAKE`: the v0 taxonomy is exactly these five kinds.
    assert frozenset(get_args(DecisionKind)) == _SPEC_DECISION_KINDS


def test_neutral_intent_has_no_size_field() -> None:
    # R4-B proposes NO size of any kind (REQ-057): the wire size authority stays downstream, so a
    # size field on the neutral intent would be identity/churn-bearing yet wire-inert.
    assert "size" not in NeutralIntent.model_fields


def test_guard_off_observation_has_no_fv_element() -> None:
    # Codex-R5 MAJOR-1: the whole FV leg is one optional nested field, so a guard-off observation
    # carries NO fv / fv_source_ts / fv_source_epoch (nor fv_recv_ts) anywhere at the top level —
    # baseline hashes are byte-identical across healthy / stale / absent / reconnecting FV.
    top_level = set(StrategyObservation.model_fields)
    for leaked in ("fv", "fv_source_ts", "fv_source_epoch", "fv_recv_ts"):
        assert leaked not in top_level, (
            f"{leaked!r} must live inside the optional guard leg, never as a top-level "
            "StrategyObservation field"
        )

    obs = _make_observation(guard_fv=None)
    assert obs.guard_fv is None
    assert obs.model_dump()["guard_fv"] is None


def test_guard_on_observation_nests_the_fv_leg() -> None:
    # Symmetric positive: with the guard enabled the FV element exists ONLY inside the nested leg.
    obs = _make_observation(guard_fv=_guard_leg())
    assert obs.guard_fv is not None
    assert obs.guard_fv.fv == 0.50
    assert obs.guard_fv.fv_source_epoch == 2


def test_market_status_sentinel_iff_unknown() -> None:
    # recv_ts / epoch are None IFF status == UNKNOWN; anything else is a construction error.
    unknown = _make_observation(
        market_status="UNKNOWN",
        market_status_recv_ts=None,
        market_status_epoch=None,
    )
    assert unknown.market_status_recv_ts is None
    assert unknown.market_status_epoch is None

    active = _make_observation(market_status="ACTIVE")
    assert active.market_status_recv_ts is not None
    assert active.market_status_epoch is not None

    # ACTIVE with a None sentinel violates the iff.
    with pytest.raises(ValidationError):
        _make_observation(market_status="ACTIVE", market_status_recv_ts=None)
    with pytest.raises(ValidationError):
        _make_observation(market_status="ACTIVE", market_status_epoch=None)
    # UNKNOWN carrying a populated leg violates the iff.
    with pytest.raises(ValidationError):
        _make_observation(market_status="UNKNOWN", market_status_recv_ts=995)
    with pytest.raises(ValidationError):
        _make_observation(
            market_status="UNKNOWN", market_status_recv_ts=None, market_status_epoch=3
        )


def test_future_timestamp_is_construction_error() -> None:
    # REQ-022 fail-closed: any recv_ts-bearing field ahead of as_of_ts is unconstructible.
    with pytest.raises(ValidationError):
        _make_observation(book_recv_ts=2_000)  # > as_of_ts (1_000)
    with pytest.raises(ValidationError):
        _make_observation(match_state_recv_ts=2_000)
    with pytest.raises(ValidationError):
        _make_observation(
            market_status="ACTIVE", market_status_recv_ts=2_000, market_status_epoch=3
        )
    with pytest.raises(ValidationError):
        _make_observation(guard_fv=_guard_leg(fv_recv_ts=2_000))


def test_market_status_image_is_closed() -> None:
    # Defensive: the status alphabet is exactly the four spec values.
    assert frozenset(get_args(MarketStatus)) == {"ACTIVE", "HALTED", "CLOSED", "UNKNOWN"}


# --- Honest labels + pinned strategy identity (HON-001 / HON-003 / REQ-044) ----------------


def test_labels_are_pinned_literals() -> None:
    # HON-001/003: the four honesty-label VALUES are pinned VERBATIM to the R4-A dust surface, so
    # a softened label ("CALIBRATED", "PROVEN_EDGE", "DUST_BACKTEST", ...) is a spec revision — it
    # can never slip in as an incidental code edit. These are the values narrated onto every run.
    assert EVIDENCE_CLASS == "EXPERIMENTAL_DUST"
    assert RUN_LABEL == "DUST_LIVE"
    assert CALIBRATION_LABEL == "UNCALIBRATED"
    assert EDGE_LABEL == "NOT_PROVEN_EDGE"


def test_no_promotion_path() -> None:
    # R4-B proves SAFETY, not alpha: the pinned evidence class is EXPERIMENTAL_DUST and is NEVER
    # one of the promoted classes, and the shared relabel guard fails closed on any promotion
    # attempt (a bare token OR a metadata blob). Promotion is a Gate B concern, out of R4-B scope.
    assert EVIDENCE_CLASS not in {"EVIDENCE_GATED", "PROMOTED"}
    reject_scoped_negative_relabel(None)  # a non-promotion input is a NO-OP
    for promoted in ("EVIDENCE_GATED", "PROMOTED"):
        with pytest.raises(ScopedNegativeRelabelError):
            reject_scoped_negative_relabel(promoted)
        with pytest.raises(ScopedNegativeRelabelError):
            reject_scoped_negative_relabel({"evidence_class": promoted})


def test_strategy_id_pinned_and_distinct_from_historical() -> None:
    # REQ-044: the R4-B strategy identity is pinned AND distinct from the historical raw-FV agent
    # id, so the two strategies never collide in the decision-identity namespace. Reusing the
    # historical `TxLineFairMarketMakerAgent` id would fail this assertion.
    assert STRATEGY_ID == "venue-anchored-txline-guarded-maker"
    assert STRATEGY_ID != "TxLineFairMarketMakerAgent"
    assert STRATEGY_REVISION  # a non-empty pinned revision string feeds decision_id provenance


def test_strategy_id_matches_config_default() -> None:
    # config <-> contracts consistency: the two pure modules cannot cross-import (config is a leaf,
    # contracts is the base), so this test is the SOLE guard that the config `strategy_id` DEFAULT
    # and the pinned STRATEGY_ID never drift apart.
    config_default = StrategyConfig.model_fields["strategy_id"].default
    assert config_default == STRATEGY_ID
