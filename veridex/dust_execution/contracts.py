"""R4-A dust-execution lifecycle event contracts (Section 4.1).

Standalone pydantic v2 contract layer for the REAL-fill dust-execution lane. This mirrors
the ``live_recorder/contracts.py`` trust-boundary pattern but is a **copy, not an import**:
SEC-003 keeps ``veridex.dust_execution`` isolated from both the ranked maker/scoring lanes
AND the COUNTERFACTUAL-only ``veridex.live_recorder`` lane. This module imports ONLY
``veridex.runtime.evidence.serialize_payload`` (the shared canonical serializer used for
every evidence hash) and the standard library.

Trust boundaries enforced here:

* Every event/config model is ``frozen=True, extra="forbid"`` — an unknown/leaked field is
  rejected at construction.
* An ``_EventEnvelope`` carries ``sequence_no: int``, ``event_type: str`` (validated
  ``== type(self).__name__``), ``source_ts: int | None`` (venue seconds, may be ``None``),
  and ``recv_ts: int`` (recorder clock, integer **milliseconds**).
* Native ``[0,1]`` price guard (CON-004): every venue price / fill price / markout reference
  rejects decimal-odds-style values (e.g. ``1.4``) at construction.
* Unlike the live-recorder lane, R4-A DOES record realized fills — ``filled_size`` /
  ``fill_price`` / ``own_fill`` are first-class here (they are the whole point of the lane),
  yet ``extra="forbid"`` still rejects any UNMODELLED leaked field.
* ``config_hash()`` = ``sha256(serialize_payload(model_dump()))`` — the canonical evidence
  hash, deterministic across processes (AC-021).
"""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from veridex.runtime.evidence import serialize_payload

# --- Closed literal sets ------------------------------------------------------------------

# Evidence class for a defined-but-unvalidated strategy (EXPERIMENTAL_DUST) up through a
# promoted one. R4-A admits EXPERIMENTAL_DUST without a profitability flag (REQ-010/AC-024).
EvidenceClass = Literal["EXPERIMENTAL_DUST", "EVIDENCE_GATED", "PROMOTED"]

# Session execution mode: Mode A dry-run/fake adapter vs Mode B live-guarded real money.
ExecutionMode = Literal["dry_run", "live_guarded"]

# Time-in-force per REQ-016: FAK/FOK are the taker forms, GTC/GTD the post-only maker forms.
TimeInForce = Literal["FAK", "FOK", "GTC", "GTD"]

# The cancel-all trigger cause — never a single order id (SAF-003). ``loss_breach`` (SAF-002d)
# labels the realized-loss-cap breach sweep: mislabeling it as ``breaker``/``manual`` would destroy
# audit fidelity, so the loss path carries its OWN cause. (§4.1's one-line cause list omitted a loss
# cause; this ADDITIVE value completes it — Gate #1 spec review must ratify the deviation.)
# ``reconciliation_timeout`` (E4-T3) labels the AUTOMATED reconciliation-timeout sweep for the same
# reason: a bounded-poll timeout fallback is NOT an operator's manual choice, so mislabeling it
# ``manual`` destroys audit fidelity — it carries its OWN cause (ADDITIVE, mirrors ``loss_breach``;
# Gate #2 MINOR-1).
CancelAllCause = Literal[
    "breaker", "kill_switch", "shutdown", "manual", "loss_breach", "reconciliation_timeout"
]

# Honest venue order status (AC-013) — matched fill only, never fabricated.
OrderStatus = Literal["partial", "filled", "rejected", "expired", "unresolved"]

# The mutually-exclusive ACK-lost tri-state reconciled against complete venue truth (AC-011).
UncertainState = Literal["RESOLVED", "DEFINITIVELY-ABSENT", "AMBIGUOUS"]


def _reject_price_out_of_unit_interval(value: float) -> float:
    """Native ``[0,1]`` price guard: rejects decimal-odds-style values (e.g. ``1.4``)."""
    if not (0.0 <= value <= 1.0):
        raise ValueError(f"price must be a native probability in [0, 1], got {value!r}")
    return value


class _FrozenModel(BaseModel):
    """Shared base: immutable, no extra fields tolerated, canonical evidence hash.

    ``config_hash()`` hashes the canonical serialization of ``model_dump()`` via the shared
    ``veridex.runtime.evidence.serialize_payload`` (sorted keys, compact separators) so the
    same content yields the same hash in every process (AC-021 determinism).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    def config_hash(self) -> str:
        """``sha256`` hexdigest over ``serialize_payload(model_dump())`` (canonical)."""
        canonical = serialize_payload(self.model_dump())
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class _EventEnvelope(_FrozenModel):
    """Common envelope mixed into every dust-session lifecycle event.

    ``source_ts`` is integer seconds (venue/source clock) and may be ``None`` when the source
    carries no timestamp; ``recv_ts`` is integer milliseconds (recorder clock). ``event_type``
    is validated against the concrete class name so a mislabeled event cannot enter the stream.
    """

    sequence_no: int
    event_type: str
    source_ts: int | None
    recv_ts: int  # integer milliseconds

    @model_validator(mode="after")
    def _event_type_matches_class_name(self) -> _EventEnvelope:
        expected = type(self).__name__
        if self.event_type != expected:
            raise ValueError(
                f"event_type must match the concrete class name {expected!r}, "
                f"got {self.event_type!r}"
            )
        return self


# --- Session identity ---------------------------------------------------------------------


class DustExecutionSessionMeta(_FrozenModel):
    """Session identity/provenance for one dust-execution session.

    Carries only NON-SECRET references (``wallet_ref``, not a wallet secret). ``manifest_hash``
    and ``policy_hash`` pin the admitted manifest + policy; ``market_fee_snapshot_hash`` binds
    the hashed per-market fee snapshot from ``get_market``/``getClobMarketInfo`` (SAF-010 §4.6 /
    DAT-004) and fails closed upstream if it is unavailable. ``partial_content_hash`` is the
    rolling checkpoint; ``content_hash`` is the sealed digest written when the session ends.
    """

    session_id: str
    mode: ExecutionMode
    wallet_ref: str
    manifest_hash: str
    policy_hash: str
    caps_snapshot: dict[str, float]
    market_fee_snapshot_hash: str
    operator_authorization_ref: str
    partial_content_hash: str | None = None
    content_hash: str | None = None


# --- Per-decision / lifecycle events (envelope-based) -------------------------------------


class SessionRiskSnapshot(_EventEnvelope):
    """Realized-loss accumulator + breaker/kill-switch state captured at each decision.

    ``realized_loss_session`` / ``realized_loss_daily`` are fee-inclusive loss magnitudes
    (>= 0). ``decision_id`` may be ``None`` for a session-level snapshot not tied to a
    single decision.
    """

    decision_id: str | None
    realized_loss_session: float
    realized_loss_daily: float
    open_order_count: int
    breaker_open: bool
    kill_switch_engaged: bool


class OrderSubmitIntent(_EventEnvelope):
    """The typed intent under evaluation (pre-submit). Native ``[0,1]`` price.

    ``decision_id`` is the stable Veridex-local join key derived from the pinned decision —
    the immutable key ``PostTradeMarkoutEvent`` is later derived against; ``client_order_id``
    is Veridex-local (dedupe key, AC-012). ``tif`` distinguishes taker (FAK/FOK) from
    post-only maker (GTC/GTD) per REQ-016.
    """

    token_id: str
    side: str
    price: float
    size: float
    tif: TimeInForce
    client_order_id: str
    decision_id: str
    decision_ts: int  # integer milliseconds

    @field_validator("price")
    @classmethod
    def _price_in_unit_interval(cls, value: float) -> float:
        return _reject_price_out_of_unit_interval(value)


class PreSubmitRecord(_FrozenModel):
    """The durable pre-submit record written BEFORE the POST (IDM-005).

    Makes the order identifiable in complete venue truth even if the ACK is lost or the order
    never opens. ``integrity_commitment_hash`` is Veridex's private one-way digest over the
    intended POST body; ``venue_order_key`` is the venue-recognized V2 order hash/id that
    order/trade/fill responses are keyed by (v0.6.3, Codex-M2 — reconciliation matches on this,
    NOT on the private integrity digest); ``captured_id`` is a captured venue order id if the
    POST returned one.
    """

    integrity_commitment_hash: str
    venue_order_key: str
    captured_id: str | None = None


class OrderSubmitAttempt(_EventEnvelope):
    """The wire submit attempt. ``request_payload_ref`` is a SCRUBBED reference, never the raw
    payload (secrets/creds are never persisted). ``presubmit_record`` is the durable IDM-005
    record written before the POST."""

    decision_id: str
    client_order_id: str
    request_payload_ref: str
    attempt_ts: int  # integer milliseconds
    presubmit_record: PreSubmitRecord


class OrderAckEvent(_EventEnvelope):
    """Venue acknowledgement (terminal-or-not). ``venue_order_id`` is ``None`` if the ack
    carried none."""

    decision_id: str
    client_order_id: str
    venue_order_id: str | None
    ack_status: str


class OrderRejectEvent(_EventEnvelope):
    """Venue rejection. ``reject_reason`` is a scrubbed, controlled-taxonomy string — never a
    raw stack trace or credential-bearing payload (SEC-005)."""

    decision_id: str
    client_order_id: str
    reject_reason: str


class OrderStatusEvent(_EventEnvelope):
    """Honest venue status; matched fill only (AC-013). Native ``[0,1]`` fill price.

    R4-A records REAL fills: ``filled_size`` is the matched size and ``fill_price`` (when a
    fill exists) is a native probability. Fill size is never fabricated; ``extra="forbid"``
    still rejects any UNMODELLED post-hoc field (e.g. a leaked ``realized_pnl``).
    """

    decision_id: str
    client_order_id: str
    venue_order_id: str | None
    status: OrderStatus
    filled_size: float
    fill_price: float | None

    @field_validator("fill_price")
    @classmethod
    def _fill_price_in_unit_interval(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _reject_price_out_of_unit_interval(value)


class OrderCancelEvent(_EventEnvelope):
    """Single-order cancel result (the authenticated ``DELETE /order`` must-build). ``canceled``
    mirrors the venue ``canceled``/``not_canceled`` response. ``decision_id`` may be ``None``
    for a cancel not tied to a single decision."""

    decision_id: str | None
    client_order_id: str
    venue_order_id: str | None
    canceled: bool


class CancelAllTriggeredEvent(_EventEnvelope):
    """The cancel-all primitive was triggered. NEVER echoes a single order id — it carries only
    the trigger cause (SAF-003)."""

    trigger_cause: CancelAllCause


class CancelAllAck(_EventEnvelope):
    """The cancel-all primitive result. Carries the count swept, never a single order id."""

    trigger_cause: CancelAllCause
    canceled_count: int


class OwnFillEvent(_EventEnvelope):
    """A realized OWN fill. Native ``[0,1]`` fill price; ``fill_size`` >= 0."""

    decision_id: str
    client_order_id: str
    venue_order_id: str | None
    side: str
    fill_price: float
    fill_size: float
    fill_ts: int  # integer milliseconds

    @field_validator("fill_price")
    @classmethod
    def _fill_price_in_unit_interval(cls, value: float) -> float:
        return _reject_price_out_of_unit_interval(value)

    @field_validator("fill_size")
    @classmethod
    def _fill_size_non_negative(cls, value: float) -> float:
        if value < 0.0:
            raise ValueError(f"fill_size must be >= 0, got {value!r}")
        return value


class RealFillReconciliation(_EventEnvelope):
    """Reconciliation of realized own fills against complete venue truth (IDM-005/AC-011).

    ``venue_order_key`` is the durable join key; ``reconciled_state`` is the tri-state verdict.
    """

    decision_id: str
    venue_order_key: str
    reconciled_state: UncertainState
    reconciled_fill_size: float


class InventoryEvent(_EventEnvelope):
    """Current net inventory for a token — DIAGNOSTIC only, denylisted from rank (AC-015)."""

    token_id: str
    net_inventory: float


class PostTradeMarkoutEvent(_EventEnvelope):
    """Diagnostic markout keyed by ``decision_id``, derived at analysis time; it NEVER mutates
    a sealed order/decision event and is denylisted from rank (AC-014). ``reference_price`` is
    a native ``[0,1]`` price (CON-004)."""

    decision_id: str
    horizon_ms: int
    reference_price: float
    markout_bps: float

    @field_validator("reference_price")
    @classmethod
    def _reference_price_in_unit_interval(cls, value: float) -> float:
        return _reject_price_out_of_unit_interval(value)


class DustRunLabelEvent(_EventEnvelope):
    """Mandatory honesty labels for a dust run. Each label is pinned to a literal so the run can
    never be relabeled as validated/promoted by metadata (AC-025)."""

    run_label: Literal["DUST_LIVE"]
    evidence_class: EvidenceClass
    calibration_label: Literal["UNCALIBRATED"]
    edge_label: Literal["NOT_PROVEN_EDGE"]


class OperatorInterlockEvent(_EventEnvelope):
    """Records one human precondition being satisfied + explicit first-order authorization
    (SAF human gate). ``operator_authorization_ref`` is a non-secret reference."""

    precondition: str
    satisfied: bool
    operator_authorization_ref: str | None
    first_order_authorized: bool


class UncertainSubmitEvent(_EventEnvelope):
    """A named uncertain-submit state (AC-011): the tri-state verdict, which complete-venue-truth
    surfaces were queried (``open`` / ``status-by-id`` / ``trade-fill-history``), and the
    reconciliation path taken."""

    decision_id: str
    uncertain_state: UncertainState
    surfaces_queried: tuple[str, ...]
    reconciliation_path: str
