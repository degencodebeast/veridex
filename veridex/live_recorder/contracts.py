"""E1 event + config contracts for the live-recorder lane (MM-R3).

Standalone pydantic v2 contract layer. Trust boundaries enforced here:

* Every event/config model is ``frozen=True, extra="forbid"``.
* ``recv_ts``/``decision_ts``/``book_obs_ts``/``book_ts``/``trade_ts``/``proof_ts``
  are integer **milliseconds**; ``source_ts`` stays integer **seconds** and may be
  ``None``.
* Proof statuses serialize **lowercase** as a closed :data:`ProofStatus` set.
* Native ``[0,1]`` prices — decimal-odds-style values (e.g. ``1.4``) are rejected.
* NO fill / PnL / edge / post-decision fields are ever stored on the immutable events;
  ``extra="forbid"`` rejects any such leaked field at construction.

This module imports nothing from ``veridex.chain.merkle``, ``veridex.scoring``,
``veridex.maker``, or any live-recorder guard surface.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

# Closed, lowercase proof-status set. Mirrors veridex/ingest/odds_proof.py constants
# (PROVEN="proven", BOUNDARY="boundary", ABSENT="absent", ERROR="error") plus the
# recorder-only "unavailable_no_message_id" honesty label used when no message_id exists
# to anchor a proof reference. Statuses are NEVER uppercased and NEVER fabricated.
ProofStatus = Literal[
    "proven",
    "boundary",
    "absent",
    "error",
    "unavailable_no_message_id",
]


def _reject_price_out_of_unit_interval(value: float) -> float:
    """Native ``[0,1]`` price guard: rejects decimal-odds-style values (e.g. ``1.4``)."""
    if not (0.0 <= value <= 1.0):
        raise ValueError(f"price must be a native probability in [0, 1], got {value!r}")
    return value


class _FrozenModel(BaseModel):
    """Shared base: immutable, no extra fields tolerated."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class _EventEnvelope(_FrozenModel):
    """Common envelope mixed into every recorder event.

    ``source_ts`` is integer seconds (venue/source clock) and may be ``None`` when the
    source carries no timestamp; ``recv_ts`` is integer milliseconds (recorder clock).
    """

    sequence_no: int
    event_type: str
    source_ts: int | None
    recv_ts: int  # integer milliseconds


class RecorderHeartbeatEvent(_EventEnvelope):
    """Periodic liveness beat: what the poll loop saw this cycle."""

    poll_index: int
    venue_mids_seen: int
    fv_points_recv: int
    fv_aligned: bool


class RecorderGapEvent(_EventEnvelope):
    """A recorded gap in the source stream (dropped window), honestly labeled."""

    from_ts: int
    to_ts: int
    source: str
    reason: str


class ReplayCheckpointEvent(_EventEnvelope):
    """Rolling content-hash checkpoint so a crash-partial replay is verifiable."""

    partial_content_hash: str


class LiveRecorderSessionMeta(_FrozenModel):
    """Session-level provenance. All post-start fields are Optional so a crash-partial
    meta (written before the session cleanly ends) still parses."""

    session_ts: int
    endpoints: dict[str, str]
    tool_version: str
    config_hash: str
    source_provenance: dict[str, str]
    fixture_ids: tuple[int, ...]
    mapping_hash: str | None = None
    event_count: int | None = None
    content_hash: str | None = None
    ended_ts: int | None = None


class FairValueEvent(_EventEnvelope):
    """A fair-value observation carrying an HONEST proof reference.

    A proof status may never be fabricated when there is no ``message_id`` to anchor it:
    if ``message_id is None`` the status MUST be ``"unavailable_no_message_id"``.
    """

    fixture_id: int
    market_ref: str
    side: str
    fv: float
    phase: int
    suspended: bool
    message_id: str | None
    proof_ts: int | None
    proof_status: ProofStatus

    @field_validator("fv")
    @classmethod
    def _fv_in_unit_interval(cls, value: float) -> float:
        return _reject_price_out_of_unit_interval(value)

    @model_validator(mode="after")
    def _proof_reference_is_honest(self) -> FairValueEvent:
        if self.message_id is None and self.proof_status != "unavailable_no_message_id":
            raise ValueError(
                "proof_status must be 'unavailable_no_message_id' when message_id is None; "
                f"a proof status may never be fabricated (got {self.proof_status!r})"
            )
        return self


class BookLevel(_FrozenModel):
    """One depth level: native ``[0,1]`` price and a non-negative size."""

    price: float
    size: float

    @field_validator("price")
    @classmethod
    def _price_in_unit_interval(cls, value: float) -> float:
        return _reject_price_out_of_unit_interval(value)

    @field_validator("size")
    @classmethod
    def _size_non_negative(cls, value: float) -> float:
        if value < 0.0:
            raise ValueError(f"size must be >= 0, got {value!r}")
        return value


class BookChange(_FrozenModel):
    """One incremental book change: level price/size plus the side it applies to."""

    price: float
    size: float
    side: str

    @field_validator("price")
    @classmethod
    def _price_in_unit_interval(cls, value: float) -> float:
        return _reject_price_out_of_unit_interval(value)

    @field_validator("size")
    @classmethod
    def _size_non_negative(cls, value: float) -> float:
        if value < 0.0:
            raise ValueError(f"size must be >= 0, got {value!r}")
        return value


class ExecutabilityMeasurement(_FrozenModel):
    """A COUNTERFACTUAL clearing measurement — what it WOULD cost to clear, never a fill.

    ``label`` is pinned to the literal ``"COUNTERFACTUAL"``: this is an honesty guard so
    the measurement can never be mistaken for a realized fill. There are deliberately NO
    ``fill_price``/``filled_size``/``realized_pnl``/``real_executable_edge_bps`` fields;
    ``extra="forbid"`` rejects any such leaked field at construction.
    """

    candidate_price: float
    available_size_at_price: float
    cumulative_size_to_clear: float
    spread: float
    half_spread: float
    cost_clearing_threshold: float
    taker_fee_bps: float
    fee_stress_multiplier: float
    stale_window_s: int
    clears: bool
    label: Literal["COUNTERFACTUAL"]

    @field_validator("candidate_price")
    @classmethod
    def _candidate_price_in_unit_interval(cls, value: float) -> float:
        return _reject_price_out_of_unit_interval(value)


class FillAssumptionConfig(_FrozenModel):
    """Pinned counterfactual fill-cost assumptions. The Rose 4x stress variant is simply
    ``FillAssumptionConfig(fee_stress_multiplier=4, ...)``."""

    taker_fee_bps: float
    fee_stress_multiplier: float
    spread_assumption: float
    slippage_assumption: float

    def config_hash(self) -> str:
        """sha256 hexdigest of the canonical JSON dump (stable, sorted keys)."""
        canonical = json.dumps(
            self.model_dump(),
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class DecisionEvent(_EventEnvelope):
    """ONE logged decision line: the full decision-time context for an intent.

    ``source_ts`` is ``None`` for a decision (it is recorder-internal, not sourced from a
    venue clock); ``recv_ts`` is the decision-time recorder clock in integer ms.
    """

    source_ts: int | None = None
    decision_id: str
    fixture_id: int
    market_ref: str
    side: str
    intent_kind: Literal["make", "take", "no_quote"]
    fv_event_id: str
    book_snapshot_id: str
    reason_code: str
    config_hash: str
    policy_hash: str
    model_inputs_hash: str


class QuoteIntentEvent(_EventEnvelope):
    """A decision-time quote intent. DECISION-TIME FIELDS ONLY: post-decision outcomes
    (e.g. ``outbid_within_ms``, ``stepped_ahead_count``) are derived later and are NEVER
    stored on this immutable intent — ``extra="forbid"`` rejects them at construction."""

    decision_id: str
    native_price: float
    desired_size: float
    side: str
    ladder_rung: int
    quote_intent_type: Literal["join", "improve_one_tick"]
    queue_ahead_size: float | None

    @field_validator("native_price")
    @classmethod
    def _native_price_in_unit_interval(cls, value: float) -> float:
        return _reject_price_out_of_unit_interval(value)


class TakeIntentEvent(_EventEnvelope):
    """A decision-time take intent, carrying its COUNTERFACTUAL executability measurement."""

    decision_id: str
    native_price: float
    desired_size: float
    side: str
    executability: ExecutabilityMeasurement

    @field_validator("native_price")
    @classmethod
    def _native_price_in_unit_interval(cls, value: float) -> float:
        return _reject_price_out_of_unit_interval(value)


class VenueBookSnapshotEvent(_EventEnvelope):
    """Full-depth venue book snapshot — stores ``(price, size)`` levels, never a mid.

    An empty side is a legitimate empty tuple and is NEVER imputed. Spread is computed
    on read, never stored.
    """

    token_id: str
    venue_market_ref: str
    book_ts: int  # integer milliseconds
    tick_size: float
    min_price_increment: float
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    is_snapshot: bool


class VenueBookDeltaEvent(_EventEnvelope):
    """Incremental venue book update as an ordered tuple of level changes."""

    token_id: str
    book_ts: int  # integer milliseconds
    changes: tuple[BookChange, ...]


class VenueTradeEvent(_EventEnvelope):
    """An observed venue trade print (with optional on-chain provenance)."""

    token_id: str
    trade_ts: int  # integer milliseconds
    price: float
    size: float
    aggressor_side: str
    block_number: int | None
    tx_hash: str | None
    log_index: int | None

    @field_validator("price")
    @classmethod
    def _price_in_unit_interval(cls, value: float) -> float:
        return _reject_price_out_of_unit_interval(value)

    @field_validator("size")
    @classmethod
    def _size_non_negative(cls, value: float) -> float:
        if value < 0.0:
            raise ValueError(f"size must be >= 0, got {value!r}")
        return value
