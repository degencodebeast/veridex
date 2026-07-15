"""Deterministic decision / client-order identity (REQ-025 / REQ-095 / AC-022).

Pins the pure identity helpers ``decision_id`` and ``client_order_id`` in
``veridex.mm_strategy.contracts``. Both are ``sha256`` hexdigests over the SHARED canonical
serializer ``veridex.runtime.evidence.serialize_payload`` of an ordered mapping of their inputs
— pure functions of those inputs ONLY: NO module-level counter, NO wall clock, NO randomness.

Because identity is a pure function of its causes (strategy identity + config + session +
observation + prior state), an authorized retry with identical inputs reproduces a byte-identical
id, while a distinct observation yields a distinct id (REQ-095). These tests recompute the digest
INDEPENDENTLY (never by calling the function under test) so they pin the exact byte contract, not a
tautology. (This file is shared with E5-T1, which later wires these helpers into StrategyDecision.)
"""

from __future__ import annotations

import hashlib

from veridex.mm_strategy.config import StrategyConfig
from veridex.mm_strategy.contracts import (
    STRATEGY_REVISION,
    InventoryProjection,
    StrategyObservation,
    StrategyState,
    client_order_id,
    decision_id,
)
from veridex.mm_strategy.core import decide
from veridex.runtime.evidence import serialize_payload

# A fixed, fully-specified id-input tuple reused across the cases below.
_STRATEGY_ID = "venue-anchored-txline-guarded-maker"
_STRATEGY_REVISION = "r4b.0"
_CONFIG_HASH = "c" * 64
_SESSION_ID = "session-0001"
_OBSERVATION_HASH = "a" * 64
_PRIOR_STATE_HASH = "b" * 64


def _expected_decision_id(observation_hash: str) -> str:
    """Recompute the decision-id digest INDEPENDENTLY from the exact ordered mapping."""
    canonical = serialize_payload(
        {
            "strategy_id": _STRATEGY_ID,
            "strategy_revision": _STRATEGY_REVISION,
            "config_hash": _CONFIG_HASH,
            "session_id": _SESSION_ID,
            "observation_hash": observation_hash,
            "prior_state_hash": _PRIOR_STATE_HASH,
        }
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_decision_id_is_serialize_payload_of_ordered_fields() -> None:
    # The decision id is EXACTLY sha256 over serialize_payload of the six ordered id fields.
    result = decision_id(
        _STRATEGY_ID,
        _STRATEGY_REVISION,
        _CONFIG_HASH,
        _SESSION_ID,
        _OBSERVATION_HASH,
        _PRIOR_STATE_HASH,
    )
    assert result == _expected_decision_id(_OBSERVATION_HASH)
    # A hex sha256 digest is 64 lowercase hex chars.
    assert len(result) == 64
    assert result == result.lower()


def test_same_inputs_same_id_no_counter() -> None:
    # Purity / no-counter teeth: two calls with IDENTICAL inputs yield IDENTICAL ids. A hidden
    # module-level counter (or wall clock) folded into the digest would make the second call drift.
    first = decision_id(
        _STRATEGY_ID,
        _STRATEGY_REVISION,
        _CONFIG_HASH,
        _SESSION_ID,
        _OBSERVATION_HASH,
        _PRIOR_STATE_HASH,
    )
    second = decision_id(
        _STRATEGY_ID,
        _STRATEGY_REVISION,
        _CONFIG_HASH,
        _SESSION_ID,
        _OBSERVATION_HASH,
        _PRIOR_STATE_HASH,
    )
    assert first == second


def test_distinct_observation_distinct_id() -> None:
    # A different observation_hash MUST change the decision id (the observation is bound in).
    base = decision_id(
        _STRATEGY_ID,
        _STRATEGY_REVISION,
        _CONFIG_HASH,
        _SESSION_ID,
        _OBSERVATION_HASH,
        _PRIOR_STATE_HASH,
    )
    other = decision_id(
        _STRATEGY_ID,
        _STRATEGY_REVISION,
        _CONFIG_HASH,
        _SESSION_ID,
        "d" * 64,  # distinct observation_hash
        _PRIOR_STATE_HASH,
    )
    assert base != other
    assert other == _expected_decision_id("d" * 64)


def test_client_order_id_is_serialize_payload_of_decision_and_leg() -> None:
    # The client-order id is EXACTLY sha256 over serialize_payload of {decision_id, leg_role}.
    parent = _expected_decision_id(_OBSERVATION_HASH)
    result = client_order_id(parent, "bid")
    expected = hashlib.sha256(
        serialize_payload({"decision_id": parent, "leg_role": "bid"}).encode("utf-8")
    ).hexdigest()
    assert result == expected
    assert len(result) == 64


def test_client_order_id_distinct_leg_distinct_id() -> None:
    # Same decision, different leg_role → different client-order id (per-leg identity), and two
    # calls with the same (decision_id, leg_role) stay identical (no counter).
    parent = _expected_decision_id(_OBSERVATION_HASH)
    bid = client_order_id(parent, "bid")
    ask = client_order_id(parent, "ask")
    assert bid != ask
    assert bid == client_order_id(parent, "bid")


# --- E5-T1: decide() WIRES the ids into every decision (REQ-095 / AC-022 / RED-18) ------------
# The cases above pin the pure byte contract of the two helpers; the cases below pin that the REAL
# reducer stamps them onto every StrategyDecision — a decision_id over the six provenance inputs, a
# per-leg client_order_id = H(decision_id, leg_role), and FULL determinism across repeated decide()
# calls on identical inputs (no wall clock / call counter leaks into any id).


def _quoting_config() -> StrategyConfig:
    """A valid guard-off config (guard_enabled is REQUIRED)."""
    return StrategyConfig(guard_enabled=False)


def _warm_state() -> StrategyState:
    """A mid-stream WARM state (smoother seeded + both rolling refs past ``ref_min_samples``) so a
    healthy in-window frame reaches row H and materialises a two-legged QUOTE_TWO_SIDED — the per-leg
    ``client_order_id`` wiring only has teeth on a decision whose ``intent_plan`` is non-empty."""
    return StrategyState(
        last_observation_sequence=1,
        last_book_source_epoch=1,
        last_as_of_ts=99_000,
        last_market_status_epoch=1,
        last_market_status_recv_ts=1,
        smoother_mid=0.50,
        smoother_mid_ts=99_000,
        spread_ref_samples=tuple(0.19 for _ in range(25)),
        depth_ref_samples=tuple(100.0 for _ in range(25)),
    )


def _quoting_obs(*, bid: float = 0.406, ask: float = 0.594) -> StrategyObservation:
    """A healthy, guard-off row-H observation (mirrors the quote-math fixture): a WIDE book whose mid
    is 0.50, so it quotes two-sided. ``bid`` / ``ask`` are exposed so a DISTINCT observation (distinct
    ``observation_hash``) is constructible."""
    as_of_ts = 100_000
    recv = as_of_ts - 10
    return StrategyObservation(
        fixture_id=1,
        market_ref="TEAM-A/YES",
        side="YES",
        token_id="TOKEN-YES",
        venue_market_ref="0xmarket",
        tick_size=0.01,
        observation_sequence=2,
        book_source_epoch=1,
        bid=bid,
        ask=ask,
        bid_size=100.0,
        ask_size=120.0,
        book_status="ok",
        status_reason=None,
        book_recv_ts=recv,
        level_count_in_band=5,
        tick_regime_changed=False,
        phase=1,
        suspended=False,
        match_state_recv_ts=recv,
        guard_fv=None,
        market_status="ACTIVE",
        market_status_recv_ts=recv,
        market_status_epoch=5,
        order_stream_ok=True,
        projection_fresh=True,
        inventory=InventoryProjection(
            net_position=0.0, resting=(), projection_as_of_ts=as_of_ts, fresh=True
        ),
        as_of_ts=as_of_ts,
    )


def test_same_authorized_decision_same_ids() -> None:
    # Two decide() calls on the SAME (observation, state, config) → IDENTICAL decision_id AND
    # identical per-leg client_order_ids. The ids are pure functions of the decision's causes.
    config = _quoting_config()
    state = _warm_state()
    obs = _quoting_obs()

    first, first_next = decide(obs, state, config)
    second, _ = decide(obs, state, config)

    # non-vacuous: a real two-legged quoting decision, so per-leg ids are actually exercised.
    assert first.kind == "QUOTE_TWO_SIDED"
    assert len(first.intent_plan) == 2

    # the decision_id is a real 64-hex digest (not the "" default) over the EXACT six provenance
    # inputs: config.strategy_id + the pinned STRATEGY_REVISION + config/observation/prior-state
    # hashes + the (default-empty) run session — recomputed independently to pin the wiring.
    expected_did = decision_id(
        config.strategy_id,
        STRATEGY_REVISION,
        config.config_hash(),
        "",
        obs.observation_hash(),
        state.state_hash(),
    )
    assert first.decision_id == expected_did
    assert len(first.decision_id) == 64

    # SAME authorized inputs → identical decision_id and identical per-leg client_order_ids.
    assert first.decision_id == second.decision_id
    assert [leg.client_order_id for leg in first.intent_plan] == [
        leg.client_order_id for leg in second.intent_plan
    ]

    # each leg carries a per-leg client_order_id = H(decision_id, leg_role), distinct per role.
    ids = {leg.leg_role: leg.client_order_id for leg in first.intent_plan}
    assert ids["bid"] == client_order_id(first.decision_id, "bid")
    assert ids["ask"] == client_order_id(first.decision_id, "ask")
    assert ids["bid"] != ids["ask"]

    # the four causal hashes are stamped from the exact provenance inputs (observation / config /
    # prior state / next state).
    assert first.observation_hash == obs.observation_hash()
    assert first.config_hash == config.config_hash()
    assert first.prior_state_hash == state.state_hash()
    assert first.next_state_hash == first_next.state_hash()


def test_distinct_observation_distinct_ids() -> None:
    # A DIFFERENT observation (different observation_hash) → a DIFFERENT decision_id: the observation
    # is bound into the id, so it cannot collide across distinct market views.
    config = _quoting_config()
    state = _warm_state()

    base, _ = decide(_quoting_obs(bid=0.406, ask=0.594), state, config)
    other, _ = decide(_quoting_obs(bid=0.404, ask=0.596), state, config)

    assert base.observation_hash != other.observation_hash
    assert base.decision_id != other.decision_id


def test_ids_independent_of_call_count() -> None:  # RED-18
    # Call decide() repeatedly on IDENTICAL inputs; every id is invariant to HOW MANY times it ran —
    # no attempt / call counter (and no wall clock) leaks into decision_id or any client_order_id.
    config = _quoting_config()
    state = _warm_state()
    obs = _quoting_obs()

    runs = [decide(obs, state, config)[0] for _ in range(3)]

    assert len(runs[0].intent_plan) == 2  # non-vacuous: the legs really exist to be id'd.
    # exactly ONE unique decision_id across all calls — a call-count drift would split this set.
    assert {d.decision_id for d in runs} == {runs[0].decision_id}
    # and exactly ONE unique per-leg client_order_id tuple (the mutation seeds a counter here).
    assert len({tuple(leg.client_order_id for leg in d.intent_plan) for d in runs}) == 1
