"""E1-T3 tests for the agent-callable MM tool boundary (§4.3, AC-020, §6 group 10).

Trust boundaries proven here:

* ``MMExecutionToolRequest`` is frozen + ``extra="forbid"``; a REQUIRED pinned hash that is
  missing is rejected at construction (fail closed). The sanctioned admission constructor
  :meth:`MMExecutionToolRequest.build` cross-checks the strategy-declared
  ``manifest_hash`` / ``policy_hash`` / ``strategy_config_hash`` against the admitted pins and
  RAISES on any mismatch — a mismatch is a hard failure, never a soft flag (§4.3, group 12).
* ``MMExecutionToolResult`` is frozen + ``extra="forbid"`` and is STRUCTURALLY incapable of
  carrying a raw venue/signer/client handle: every field annotation bottoms out in a
  JSON-primitive leaf (``str``/``int``/``float``/``bool``/``None``) or a pinned ``Literal`` —
  never a rich object type. This is AC-020 / §6 group 10 (the result returns only a typed
  admission + an opaque ``lifecycle_receipt_ref``, never a live client/wallet/key).
* The honest labels reuse the pinned literals from ``contracts.py`` (``DUST_LIVE`` /
  ``UNCALIBRATED`` / ``NOT_PROVEN_EDGE`` / ``EXPERIMENTAL_DUST``); the result carries no
  profitability/edge claim field.
"""

from __future__ import annotations

import typing

import pytest
from pydantic import BaseModel, ValidationError

from veridex.dust_execution.facade import (
    MMExecutionToolRequest,
    MMExecutionToolResult,
    MMIntentParams,
)

# JSON-primitive leaf types a boundary-safe result field may bottom out in.
_ALLOWED_LEAF_TYPES = frozenset({str, int, float, bool, type(None)})


def _assert_only_safe_leaves(annotation: object) -> None:
    """Recursively assert an annotation contains ONLY primitive/Literal leaves.

    A raw venue/signer/client handle would surface as a rich object type (a bare class not in
    ``_ALLOWED_LEAF_TYPES`` and not a pydantic ``BaseModel`` primitive carrier); this walk fails
    on it, making the no-raw-handle guarantee STRUCTURAL rather than by-inspection.
    """
    origin = typing.get_origin(annotation)
    if origin is None:
        if isinstance(annotation, type):
            # A nested BaseModel would (recursively) also have to be handle-free; the result
            # contract intentionally uses NO nested model, so any bare class must be a leaf.
            assert not (
                isinstance(annotation, type) and issubclass(annotation, BaseModel)
            ), f"result field nests a model ({annotation!r}); keep result fields flat"
            assert annotation in _ALLOWED_LEAF_TYPES, (
                f"unsafe result field leaf type {annotation!r} — a raw handle could hide here"
            )
        return
    if origin is typing.Literal:
        for arg in typing.get_args(annotation):
            assert isinstance(arg, (str, int, bool)), f"non-primitive Literal member {arg!r}"
        return
    for arg in typing.get_args(annotation):
        if arg is Ellipsis:
            continue
        _assert_only_safe_leaves(arg)


def _params() -> MMIntentParams:
    return MMIntentParams(
        token_id="0xtokenYES",
        side="BUY",
        price=0.42,
        size=1.0,
        tif="FAK",
        client_order_id="coid-1",
    )


def _request_fields() -> dict[str, object]:
    return {
        "intent_kind": "take",
        "intent_params": _params(),
        "strategy_id": "dust-maker-v0",
        "strategy_config_hash": "cfg" * 4,
        "policy_hash": "pol-hash",
        "session_id": "sess-1",
        "manifest_hash": "MANIFEST_GOOD",
        "evidence_class": "EXPERIMENTAL_DUST",
        "mode": "dry_run",
    }


def test_facade_result_never_carries_raw_handles_and_hash_mismatch_denies() -> None:
    # (A) The sanctioned admission constructor builds when the declared pins MATCH the admitted
    # pins, and the untrusted reason/confidence are accepted but distinct from the pins.
    fields = _request_fields()
    ok = MMExecutionToolRequest.build(
        admitted_manifest_hash="MANIFEST_GOOD",
        admitted_policy_hash="pol-hash",
        admitted_strategy_config_hash="cfg" * 4,
        reason="agent thinks this is a good quote",  # untrusted metadata
        confidence=0.99,  # untrusted metadata
        **fields,
    )
    assert ok.manifest_hash == "MANIFEST_GOOD"
    assert ok.intent_kind == "take"

    # (B) A MISMATCHED manifest_hash fails closed at build/validation — a hard raise, not a flag.
    with pytest.raises(ValueError):
        MMExecutionToolRequest.build(
            admitted_manifest_hash="MANIFEST_OTHER",  # != declared "MANIFEST_GOOD"
            admitted_policy_hash="pol-hash",
            admitted_strategy_config_hash="cfg" * 4,
            **fields,
        )
    # A mismatched policy_hash and a mismatched strategy_config_hash also fail closed.
    with pytest.raises(ValueError):
        MMExecutionToolRequest.build(
            admitted_manifest_hash="MANIFEST_GOOD",
            admitted_policy_hash="WRONG_POLICY",
            admitted_strategy_config_hash="cfg" * 4,
            **fields,
        )
    with pytest.raises(ValueError):
        MMExecutionToolRequest.build(
            admitted_manifest_hash="MANIFEST_GOOD",
            admitted_policy_hash="pol-hash",
            admitted_strategy_config_hash="WRONG_CFG",
            **fields,
        )

    # (C) A MISSING required pinned hash is rejected at construction (extra="forbid" + required).
    missing = {k: v for k, v in fields.items() if k != "manifest_hash"}
    with pytest.raises(ValidationError):
        MMExecutionToolRequest(**missing)

    # (D) The request is frozen + extra="forbid": an unmodelled/leaked field is rejected.
    with pytest.raises(ValidationError):
        MMExecutionToolRequest(venue_client="leaked-handle", **fields)

    # (E) STRUCTURAL no-raw-handle guarantee: every MMExecutionToolResult field bottoms out in a
    # JSON-primitive/Literal leaf — no field of venue/signer/client type can exist (AC-020).
    result = MMExecutionToolResult(
        admission="APPROVED",
        reason_codes=("admitted",),
        lifecycle_receipt_ref="receipt:0xabc",
        run_label="DUST_LIVE",
        calibration_label="UNCALIBRATED",
        edge_label="NOT_PROVEN_EDGE",
        evidence_class="EXPERIMENTAL_DUST",
        policy_hash="pol-hash",
    )
    assert result.lifecycle_receipt_ref == "receipt:0xabc"
    assert result.admission == "APPROVED"
    for name, field in MMExecutionToolResult.model_fields.items():
        _assert_only_safe_leaves(field.annotation)

    # (F) The result is frozen + extra="forbid": a raw handle field cannot be smuggled in.
    with pytest.raises(ValidationError):
        MMExecutionToolResult(
            admission="APPROVED",
            reason_codes=(),
            lifecycle_receipt_ref="receipt:0xabc",
            run_label="DUST_LIVE",
            calibration_label="UNCALIBRATED",
            edge_label="NOT_PROVEN_EDGE",
            evidence_class="EXPERIMENTAL_DUST",
            policy_hash="pol-hash",
            venue_client="leaked-handle",  # extra="forbid" rejects a raw handle field
        )
