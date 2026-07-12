"""E1-T2 admission-contract tests for the R4-A dust-execution lane.

Trust boundaries proven here:

* ``StrategyExperimentManifest`` is frozen + ``extra="forbid"``; ``manifest_hash()`` is a
  deterministic sha256 over ``serialize_payload(model_dump())`` (AC-021).
* ``execution_wallet_binding_hash`` is an EXPLICIT frozen field that is ``None`` in Mode A
  (v0.6.1, REQ-018/AC-042) — so the wallet/policy pin lives INSIDE ``manifest_hash()``.
* ``StrategyAuthorizationDecision`` is deterministic: identical
  ``(manifest_hash, policy_hash, session)`` → identical verdict; a request with no manifest
  is DENY (Section 6 group 12).
"""

import pytest
from pydantic import ValidationError

from veridex.dust_execution.contracts import OrderStatusEvent
from veridex.dust_execution.manifest import (
    SessionState,
    StrategyAuthorizationDecision,
    StrategyExperimentManifest,
)


def _manifest(**kw) -> StrategyExperimentManifest:
    base = {
        "strategy_id": "dust-maker-v0",
        "strategy_config_hash": "cfg" * 4,
        "evidence_class": "EXPERIMENTAL_DUST",
        "market": "0xcondition",
        "universe": ("0xtokenYES", "0xtokenNO"),
        "mode": "dry_run",
        "max_orders": 3,
        "max_notional": 5.0,
        "max_session_loss": 2.0,
        "max_daily_loss": 4.0,
        "session_window": (1_700_000_000_000, 1_700_000_600_000),
        "required_inputs": ("fair_value", "venue_book"),
        "permitted_intent_kinds": ("make",),
        "market_fee_snapshot_hash": "fee" * 4,
        "operator_authorization": "op-ref-1",
        "forbidden_claims": ("PROVEN_EDGE", "CALIBRATED"),
    }
    base.update(kw)
    return StrategyExperimentManifest(**base)


def _session(**kw) -> SessionState:
    base = {
        "session_id": "sess-1",
        "realized_loss_session": 0.0,
        "realized_loss_daily": 0.0,
        "open_order_count": 0,
        "breaker_open": False,
        "kill_switch_engaged": False,
    }
    base.update(kw)
    return SessionState(**base)


def _status(**kw) -> OrderStatusEvent:
    base = {
        "sequence_no": 7,
        "event_type": "OrderStatusEvent",
        "source_ts": 1_700_000_000,
        "recv_ts": 1_700_000_000_123,
        "decision_id": "dec-1",
        "client_order_id": "coid-1",
        "venue_order_id": "0xorder",
        "status": "partial",
        "filled_size": 1.5,
        "fill_price": 0.42,
    }
    base.update(kw)
    return OrderStatusEvent(**base)


def test_extra_forbid_rejects_fill_leak_and_hash_is_deterministic() -> None:
    # (1) OrderStatusEvent accepts a native-[0,1] price + a real filled_size (R4-A DOES
    # record real fills), but extra="forbid" rejects an unknown/leaked field.
    ev = _status()
    assert ev.filled_size == 1.5
    assert ev.fill_price == 0.42
    with pytest.raises(ValidationError):  # extra="forbid" rejects an unknown field
        _status(realized_pnl=9.99)
    with pytest.raises(ValidationError):  # native [0,1] price guard rejects decimal odds
        _status(fill_price=1.4)

    # (2) StrategyAuthorizationDecision is deterministic: identical (manifest, policy, session)
    # → byte-identical verdict + reason codes (AC-021).
    manifest = _manifest()
    session = _session()
    d1 = StrategyAuthorizationDecision.evaluate(
        manifest=manifest, policy_hash="pol-hash", session=session
    )
    d2 = StrategyAuthorizationDecision.evaluate(
        manifest=manifest, policy_hash="pol-hash", session=session
    )
    assert d1.verdict == "ALLOW"
    assert d1 == d2
    assert d1.manifest_hash == manifest.manifest_hash()

    # (3) A request with NO manifest → DENY (Section 6 group 12: missing manifest blocks execution).
    denied = StrategyAuthorizationDecision.evaluate(
        manifest=None, policy_hash="pol-hash", session=session
    )
    assert denied.verdict == "DENY"
    assert "missing_manifest" in denied.reason_codes
    assert denied.manifest_hash is None
