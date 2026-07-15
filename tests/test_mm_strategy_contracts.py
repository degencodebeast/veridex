"""Frozen neutral contract guards (MM-R4-B — REQ-020/021/025/060/063 + construction invariants).

These are behavioral, not import, tests: each one pins one load-bearing invariant of the
pure-tier neutral contracts so a later "natural mistake" (a size field on an intent, an FV
element leaked to the top level, a stray/omitted reason code, a `WIDEN` decision kind, a
non-sentinel market-status leg, a future-dated timestamp) is caught by an assertion — never by
a missing-symbol collection error.
"""

from __future__ import annotations

from typing import get_args, get_origin

import pytest
from pydantic import BaseModel, ValidationError

from veridex.dust_execution.analysis import (
    ScopedNegativeRelabelError,
    reject_scoped_negative_relabel,
)
from veridex.mm_strategy import contracts as _contracts_module
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
    RestingOrderView,
    StrategyDecision,
    StrategyObservation,
    StrategyState,
)
from veridex.runtime.evidence import serialize_payload

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


def test_neutral_intent_leg_role_is_closed_literal() -> None:
    # REQ-095 closes leg_role to {bid, ask, reduce} — an open `str` lets a typo'd role slip into
    # `client_order_id` identity unnoticed (Gate #1 Stage-1 finding). cancel_all/abstain legs
    # still carry `leg_role=None`, so the closed set stays `Literal[...] | None`, never bare `str`.
    with pytest.raises(ValidationError):
        NeutralIntent(kind="place_quote", leg_role="bogus")

    annotation = NeutralIntent.model_fields["leg_role"].annotation
    non_none_args = [arg for arg in get_args(annotation) if arg is not type(None)]
    assert len(non_none_args) == 1, f"expected exactly one non-None arm, got {non_none_args}"
    assert frozenset(get_args(non_none_args[0])) == {"bid", "ask", "reduce"}


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


# --- Finite / positive native-unit construction guard (Gate #2 MAJOR-5 — REQ-041/070) ------


def test_tick_size_zero_rejected_at_construction() -> None:
    # Gate #2 MAJOR-5 / REQ-070 totality: a zero (or non-positive / non-finite) tick_size makes the
    # directional tick rounding (`floor_to_tick`/`ceil_to_tick`: `price / tick`) crash the
    # supposedly-total core with ZeroDivisionError. Reject it AT THE BOUNDARY so `decide` stays total
    # for every CONSTRUCTIBLE observation — the crash surface is removed where the value enters, not
    # patched with a defensive branch deep in the QUOTE-math terminal.
    for bad in (0.0, -0.01, float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValidationError):
            _make_observation(tick_size=bad)

    # A valid positive finite tick still constructs — the guard REJECTS bad input, never narrows the
    # valid set (so every existing valid-tick observation is byte-identical to before).
    ok = _make_observation(tick_size=0.01)
    assert ok.tick_size == 0.01


def test_non_finite_native_inputs_rejected() -> None:
    # NaN/Inf in a native price/size field silently DISABLE comparisons (every `<` / `<=` on NaN is
    # False), so a poisoned book could pass a zone / anchor / depth gate it should fail. Reject
    # non-finite native units at construction (the Gate #2 MAJOR-5 audit): best_bid / best_ask /
    # top-of-book depths on the observation, plus the shared inventory value objects.
    for field in ("bid", "ask", "bid_size", "ask_size"):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(ValidationError):
                _make_observation(**{field: bad})

    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValidationError):
            InventoryProjection(net_position=bad, resting=(), projection_as_of_ts=1_000, fresh=True)
        with pytest.raises(ValidationError):
            RestingOrderView(client_order_id="c1", side="YES", price=0.5, size=bad)

    # None is still allowed on the optional top-of-book legs (a degraded book) — the guard rejects a
    # PRESENT non-finite value only, it never forces presence.
    degraded = _make_observation(
        book_status="gap", bid=None, ask=None, bid_size=None, ask_size=None
    )
    assert degraded.bid is None and degraded.ask is None
    assert degraded.bid_size is None and degraded.ask_size is None


# --- StrategyState EWMA accumulator pair invariant (REQ-031/035) ---------------------------


def test_partial_ewma_accumulator_rejected() -> None:
    # Codex Gate#1-R3 MAJOR-1: basis_ewma_value/basis_ewma_ts are a semantic pair — a partial
    # snapshot (one set, one None) must never construct. Left unconstrained it survives Pydantic
    # construction, canonical hashing, and model_validate_json(), then silently loses EWMA
    # history on the next admitted sample instead of failing closed (REQ-031/035 state-integrity).
    with pytest.raises(ValidationError):
        StrategyState(basis_ewma_value=0.25, basis_ewma_ts=None)
    with pytest.raises(ValidationError):
        StrategyState(basis_ewma_value=None, basis_ewma_ts=1_000)

    # A valid pair constructs AND survives a canonical JSON round trip unchanged.
    state = StrategyState(basis_ewma_value=0.25, basis_ewma_ts=1_000)
    restored = StrategyState.model_validate_json(state.model_dump_json())
    assert restored == state

    # Both-None is the default, fresh-state seed — stays backward-compatible with old snapshots.
    fresh = StrategyState()
    assert fresh.basis_ewma_value is None
    assert fresh.basis_ewma_ts is None


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


# --- Immutable-nested + hash-stability defense (RED-32) ------------------------------------


def _frozen_contract_classes() -> tuple[type[BaseModel], ...]:
    """Every frozen contract model declared in `contracts.py` (scanned, not hand-maintained, so a
    new frozen model added later is picked up automatically instead of silently going unchecked)."""
    found: list[type[BaseModel]] = []
    for name in dir(_contracts_module):
        obj = getattr(_contracts_module, name)
        if (
            isinstance(obj, type)
            and issubclass(obj, BaseModel)
            and obj.model_config.get("frozen") is True
        ):
            found.append(obj)
    return tuple(found)


def _contains_bare_list(annotation: object) -> bool:
    """True if a bare `list[...]` appears anywhere in the annotation's generic tree (incl. nested
    inside `X | None`), so an `Optional[list[...]]` escape hatch is caught too, not just a top-level
    `list[...]` field."""
    if get_origin(annotation) is list:
        return True
    return any(_contains_bare_list(arg) for arg in get_args(annotation))


def test_all_sequence_fields_are_tuples() -> None:
    # Every sequence field on every frozen contract must be `tuple[...]`, never bare `list[...]` —
    # a `list` field is a nested-mutable escape hatch: two hashes taken over "equal" content could
    # diverge after an in-place `.append()` the frozen top-level model_config can't see or block.
    offenders = [
        f"{model_cls.__name__}.{field_name}"
        for model_cls in _frozen_contract_classes()
        for field_name, field_info in model_cls.model_fields.items()
        if _contains_bare_list(field_info.annotation)
    ]
    assert not offenders, f"bare list[...] sequence field(s) (must be tuple[...]): {offenders}"

    # Explicit pin on the two decision-identity-bearing sequences called out by REQ-025/063.
    assert get_origin(StrategyDecision.model_fields["intent_plan"].annotation) is tuple
    assert get_origin(StrategyDecision.model_fields["reason_codes"].annotation) is tuple


def test_hash_stable_under_unicode_and_float() -> None:
    # `serialize_payload` (evidence.py) is the SOLE byte authority every observation/decision hash
    # is taken over (REQ-040). It must canonicalize identically every time — not "usually equal" —
    # across a non-ASCII provenance string and a repeated-arithmetic float edge (`0.1 + 0.2`, the
    # classic base-2 rounding artifact), or two honest replays of the same content would silently
    # diverge into two different hashes.
    float_edge = 0.1 + 0.2  # 0.30000000000000004, not 0.3
    obs = _make_observation(token_id="café-üñíçødé-mañana", bid=float_edge)

    assert serialize_payload(obs.model_dump()) == serialize_payload(obs.model_dump())
    assert obs.observation_hash() == obs.observation_hash()

    decision = StrategyDecision(
        decision_id="café-decision",
        kind="QUOTE_ONE_SIDED",
        reason_codes=("boundary_zone",),
        intent_plan=(
            NeutralIntent(
                kind="place_quote",
                leg_role="bid",
                price=float_edge - 0.29,  # another repeated-arithmetic edge, still in [0, 1]
                # leg_role is now a closed Literal (REQ-095), so the unicode probe moves onto
                # this genuinely free-text field instead.
                client_order_id="café-léğ",
            ),
        ),
        observation_hash=obs.observation_hash(),
    )
    assert serialize_payload(decision.model_dump()) == serialize_payload(decision.model_dump())
