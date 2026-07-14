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

from veridex.mm_strategy.contracts import client_order_id, decision_id
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
