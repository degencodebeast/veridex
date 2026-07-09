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
