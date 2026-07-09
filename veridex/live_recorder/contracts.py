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

from pydantic import BaseModel, ConfigDict


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
