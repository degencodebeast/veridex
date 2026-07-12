"""R4-A agent-callable MM tool boundary contracts (Section 4.3, AC-020, §6 group 10).

The two frozen models here are the ONLY typed surface between the R4-B strategy/agent layer and
the policy-gated dust-execution runner:

* :class:`MMExecutionToolRequest` — a strategy PROPOSES a typed intent (``make_quote`` / ``take``
  / ``cancel_replace`` / ``cancel_all`` / ``no_quote``) together with the hashes it DECLARES it
  was admitted under. The sanctioned admission constructor :meth:`MMExecutionToolRequest.build`
  cross-checks those declared hashes against the ADMITTED pins and **fails closed** (raises) on
  any mismatch, so an approved intent can never be silently re-bound to a different
  manifest/policy/strategy config (§4.3). A missing pinned hash is rejected at construction
  (``extra="forbid"`` + required field).
* :class:`MMExecutionToolResult` — the boundary returns ONLY a typed ``admission`` verdict, ordered
  ``reason_codes``, an OPAQUE ``lifecycle_receipt_ref`` string, the honest labels, and the
  ``policy_hash``. It NEVER carries a raw venue client, signer, wallet, or private-key handle
  (AC-020): every field bottoms out in a JSON-primitive or a pinned ``Literal``.

This module imports ONLY ``.contracts`` (same isolated package — SEC-003 permits the intra-lane
import of the frozen base + pinned labels) and the standard library. It does NOT import
``veridex.live_recorder`` and has NO ranked-lane dependency. The proposer/adapter WIRING that
turns a request into a real submit lands in E7-T1 (needs the runner); this module is the
CONTRACTS ONLY — no runner, adapter, agent tool registry, or venue I/O is defined here.
"""

from __future__ import annotations

from typing import Literal

from pydantic import field_validator

from veridex.dust_execution.contracts import (
    EvidenceClass,
    ExecutionMode,
    TimeInForce,
    _FrozenModel,
    _reject_price_out_of_unit_interval,
)

# The closed set of agent-proposable intent kinds (§4.3). ``no_quote`` is an explicit abstention.
IntentKind = Literal["make_quote", "take", "cancel_replace", "cancel_all", "no_quote"]

# The typed admission verdict returned to the agent (§4.3): approved, denied, or human-gated.
Admission = Literal["APPROVED", "DENIED", "REQUIRES_HUMAN"]


class MMIntentParams(_FrozenModel):
    """Typed parameters for a proposed MM intent (§4.3 ``intent_params``).

    Deliberately typed (never ``dict[str, Any]``): every field is a primitive with a native
    ``[0,1]`` price guard (CON-004), so a malformed/odds-style intent is rejected at construction.
    All fields are optional because their applicability depends on ``intent_kind`` (e.g.
    ``cancel_all`` / ``no_quote`` carry none, ``cancel_replace`` names the order it replaces via
    ``replaces_client_order_id``). ``extra="forbid"`` (inherited) still rejects any leaked field.
    """

    token_id: str | None = None
    side: str | None = None
    price: float | None = None
    size: float | None = None
    tif: TimeInForce | None = None
    client_order_id: str | None = None
    replaces_client_order_id: str | None = None

    @field_validator("price")
    @classmethod
    def _price_in_unit_interval(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _reject_price_out_of_unit_interval(value)


class MMExecutionToolRequest(_FrozenModel):
    """A typed, frozen agent-callable MM execution request (§4.3).

    Carries the pinned admission hashes the strategy DECLARES it is operating under
    (``strategy_config_hash`` / ``policy_hash`` / ``manifest_hash``) plus a typed intent. Every
    pinned hash is a REQUIRED field, so a missing one is rejected at construction
    (``extra="forbid"``). ``reason`` / ``confidence`` are OPTIONAL untrusted agent metadata with
    NO gate effect (AC-022) — they never move admission.

    Use :meth:`build` as the admission entry point: it fails closed on a hash mismatch. Direct
    construction is a plain data carrier of the strategy's declaration and does NOT (cannot) know
    the admitted pins — the cross-check lives in :meth:`build`.
    """

    intent_kind: IntentKind
    intent_params: MMIntentParams
    strategy_id: str
    strategy_config_hash: str
    policy_hash: str
    session_id: str
    manifest_hash: str
    evidence_class: EvidenceClass
    mode: ExecutionMode
    reason: str | None = None  # untrusted agent metadata; no gate effect (AC-022)
    confidence: float | None = None  # untrusted agent metadata; no gate effect (AC-022)

    @classmethod
    def build(
        cls,
        *,
        intent_kind: IntentKind,
        intent_params: MMIntentParams,
        strategy_id: str,
        strategy_config_hash: str,
        policy_hash: str,
        session_id: str,
        manifest_hash: str,
        evidence_class: EvidenceClass,
        mode: ExecutionMode,
        admitted_manifest_hash: str,
        admitted_policy_hash: str,
        admitted_strategy_config_hash: str,
        reason: str | None = None,
        confidence: float | None = None,
    ) -> MMExecutionToolRequest:
        """Construct a request only if the declared pins MATCH the admitted pins (fail closed).

        The strategy declares ``manifest_hash`` / ``policy_hash`` / ``strategy_config_hash``; this
        constructor compares each against the corresponding ADMITTED pin and RAISES
        :class:`ValueError` on any mismatch, so an approved intent can never be silently rerouted
        to a different manifest/policy/strategy config (§4.3, group 12). Mismatches are reported in
        a fixed order for a deterministic message.
        """
        mismatches: list[str] = []
        if manifest_hash != admitted_manifest_hash:
            mismatches.append("manifest_hash")
        if policy_hash != admitted_policy_hash:
            mismatches.append("policy_hash")
        if strategy_config_hash != admitted_strategy_config_hash:
            mismatches.append("strategy_config_hash")
        if mismatches:
            raise ValueError(
                "MM execution request fails closed: declared hashes do not match the admitted "
                f"pins for {', '.join(mismatches)}"
            )
        return cls(
            intent_kind=intent_kind,
            intent_params=intent_params,
            strategy_id=strategy_id,
            strategy_config_hash=strategy_config_hash,
            policy_hash=policy_hash,
            session_id=session_id,
            manifest_hash=manifest_hash,
            evidence_class=evidence_class,
            mode=mode,
            reason=reason,
            confidence=confidence,
        )


class MMExecutionToolResult(_FrozenModel):
    """The typed, frozen result returned across the agent boundary (§4.3, AC-020).

    Carries ONLY: a typed ``admission`` verdict, ordered ``reason_codes``, an OPAQUE
    ``lifecycle_receipt_ref`` (a string reference into the lifecycle evidence, never a live
    object), the honest labels, and ``policy_hash``. It NEVER carries a raw venue client, signer,
    wallet, or private-key handle — every field is a JSON-primitive or a pinned ``Literal``, which
    makes the no-raw-handle guarantee STRUCTURAL (§6 group 10).

    The honest labels reuse the pinned literals from ``contracts.DustRunLabelEvent`` so a dust run
    can never be relabeled as validated/promoted (AC-025); there is deliberately NO
    ``expected_pnl`` / ``edge_bps`` field — the result implies no profitability/edge claim.
    """

    admission: Admission
    reason_codes: tuple[str, ...]
    lifecycle_receipt_ref: str
    run_label: Literal["DUST_LIVE"]  # pinned, mirrors contracts.DustRunLabelEvent.run_label
    calibration_label: Literal["UNCALIBRATED"]  # mirrors DustRunLabelEvent.calibration_label
    edge_label: Literal["NOT_PROVEN_EDGE"]  # mirrors DustRunLabelEvent.edge_label
    evidence_class: EvidenceClass
    policy_hash: str
