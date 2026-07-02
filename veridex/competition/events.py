"""Canonical competition event log — Phase 2A Task 2 (the load-bearing keystone).

This module projects the FROZEN Phase-1 sealed proof record (a
:class:`~veridex.runtime.orchestrator.RunResult`) into a single canonical competition event
log. It is **never a second source of truth**: :func:`build_event_log` is a PURE, SYNC,
DETERMINISTIC function — identical inputs yield a byte-identical log (no wall-clock, no
randomness, no mutation of its inputs).

Trust invariant (CON-203):
    * Every ``evidence=True`` event is hash-bound to a sealed Phase-1 ``RunEvent`` via
      ``payload_hash`` — the hash covers the FULL sealed ``RunEvent`` dict, not the (smaller,
      secret-free) UI ``payload`` projection. Tampering any sealed field changes exactly one
      evidence hash.
    * Every ``evidence=False`` event is a deterministic derivation carrying non-empty
      ``derived_from`` references; it is NEVER a scoring input.

The single canonical serializer is :func:`veridex.runtime.evidence.serialize_payload`
(sorted keys, compact separators) — reused here so cross-process hashes match Phase-1.

TRUST PATH: this module MUST NOT import any LLM SDK (enforced by
``veridex.verifier.import_audit``).
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from veridex.runtime.evidence import serialize_payload
from veridex.runtime.orchestrator import (
    EVENT_DECISION,
    EVENT_ERROR,
    EVENT_TICK,
    RunResult,
)
from veridex.runtime.window import CLV_FIELD_WINDOW


class EventType(str, Enum):
    """Closed set of competition event types.

    Phase 2A emits: ``COMPETITION_STARTED``, ``MARKET_TICK``, ``AGENT_ACTION``,
    ``LAW_RESULT``, ``SCORE_UPDATE``, ``PROOF_ANCHOR``, ``COMPETITION_FINALIZED``.

    Phase 2B (executor lane) emits: ``POLICY_RESULT``, ``EXECUTION_SUBMITTED``,
    ``EXECUTION_RECEIPT`` — these are DEFINED here for forward wire-compatibility but MUST
    NEVER be emitted by :func:`build_event_log`.

    Phase 2D (reserved): ``PAYOUT_STATUS`` — defined here for wire-compatibility only; not
    emitted by any current lane.
    """

    COMPETITION_STARTED = "competition_started"
    MARKET_TICK = "market_tick"
    AGENT_ACTION = "agent_action"
    LAW_RESULT = "law_result"
    POLICY_RESULT = "policy_result"  # Phase 2B (executor lane) — never emitted by build_event_log
    EXECUTION_SUBMITTED = "execution_submitted"  # Phase 2B (executor lane) — never emitted by build_event_log
    EXECUTION_RECEIPT = "execution_receipt"  # Phase 2B (executor lane) — never emitted by build_event_log
    APPROVAL_AUDIT = "approval_audit"  # Phase 2B (human-approval resolution) — never emitted by build_event_log
    SCORE_UPDATE = "score_update"
    PROOF_ANCHOR = "proof_anchor"
    PAYOUT_STATUS = "payout_status"  # reserved (Phase 2D) — not emitted by any current lane
    COMPETITION_FINALIZED = "competition_finalized"


def event_payload_hash(payload: dict[str, Any]) -> str:
    """Return the SHA-256 of the canonically-serialized ``payload``.

    Reuses the Phase-1 canonical serializer so the digest is reproducible across processes.

    Args:
        payload: Any JSON-serializable mapping (a sealed ``RunEvent`` dict for evidence
            events, or a derived payload for derived events).

    Returns:
        The 64-character hex SHA-256 digest.
    """
    return hashlib.sha256(serialize_payload(payload).encode("utf-8")).hexdigest()


# The ten canonical fields that define event identity (hashing / equality / determinism).
_CANONICAL_FIELDS: tuple[str, ...] = (
    "competition_id",
    "run_id",
    "seq",
    "event_type",
    "event_ts",
    "evidence",
    "source_sequence_no",
    "derived_from",
    "payload",
    "payload_hash",
)


class CompetitionEvent(BaseModel):
    """One canonical event in the competition log.

    The ten canonical fields define the event's identity (see :meth:`canonical_dict`). The two
    operational fields (``persisted_at``, ``broadcasted_at``) are pipeline bookkeeping and are
    EXCLUDED from hashing, determinism, and equality.

    Attributes:
        competition_id: Owning competition identifier.
        run_id: Sealed Phase-1 run identifier this event derives from.
        seq: Monotonic competition sequence number (0-based, contiguous).
        event_type: One of :class:`EventType` (never a reserved 2B/2D type in Phase 2A).
        event_ts: Deterministic event timestamp (tick ts or meta base ts — never wall-clock).
        evidence: ``True`` iff hash-bound to a sealed ``RunEvent``; ``False`` for derivations.
        source_sequence_no: Sealed ``RunEvent.sequence_no`` for evidence events, else ``None``.
        derived_from: Non-empty reference list for derived events, ``[]`` for evidence events.
        payload: Small, secret-free projection for display (NOT the hashed object for
            evidence events).
        payload_hash: For evidence events, the hash over the FULL sealed ``RunEvent`` dict;
            for derived events, the hash over ``payload``.
        persisted_at: Operational; set by the store layer. Excluded from identity.
        broadcasted_at: Operational; set by the broadcast layer. Excluded from identity.
    """

    competition_id: str
    run_id: str
    seq: int
    event_type: EventType
    event_ts: int
    evidence: bool
    source_sequence_no: int | None
    derived_from: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)
    payload_hash: str
    persisted_at: str | None = None
    broadcasted_at: str | None = None

    def canonical_dict(self) -> dict[str, Any]:
        """Return only the ten canonical fields (excludes operational bookkeeping).

        This is the deterministic basis for equality and hashing — it MUST NOT contain
        ``persisted_at`` or ``broadcasted_at``.

        Returns:
            A dict of the canonical fields, with ``derived_from`` / ``payload`` copied.
        """
        return {
            "competition_id": self.competition_id,
            "run_id": self.run_id,
            "seq": self.seq,
            "event_type": self.event_type,
            "event_ts": self.event_ts,
            "evidence": self.evidence,
            "source_sequence_no": self.source_sequence_no,
            "derived_from": list(self.derived_from),
            "payload": dict(self.payload),
            "payload_hash": self.payload_hash,
        }

    def __eq__(self, other: object) -> bool:
        """Equality over canonical fields only (operational fields are ignored)."""
        if not isinstance(other, CompetitionEvent):
            return NotImplemented
        return self.canonical_dict() == other.canonical_dict()

    __hash__ = None  # type: ignore[assignment]  # canonical_dict carries unhashable dicts


def _tick_ts(state_snapshot_json: str | None) -> int | None:
    """Parse a tick's ``state_snapshot_json`` and return its integer ``ts`` (or ``None``)."""
    if not state_snapshot_json:
        return None
    snapshot = json.loads(state_snapshot_json)
    ts = snapshot.get("ts")
    return int(ts) if ts is not None else None


def _tick_payload(state_snapshot_json: str | None) -> dict[str, Any]:
    """Build a minimal, secret-free MARKET_TICK display payload from a tick snapshot."""
    if not state_snapshot_json:
        return {}
    snapshot = json.loads(state_snapshot_json)
    markets = snapshot.get("markets", {}) or {}
    return {
        "tick_seq": snapshot.get("tick_seq"),
        "ts": snapshot.get("ts"),
        "phase": snapshot.get("phase"),
        "markets": sorted(markets.keys()),
    }


def _action_payload(run_event: dict[str, Any]) -> dict[str, Any]:
    """Build a small AGENT_ACTION display payload from a sealed decision/error ``RunEvent``.

    For error events the payload is marked ``{"error": True, ...}`` while the event type stays
    ``AGENT_ACTION`` (an error variant). The payload is for display only — the binding
    ``payload_hash`` is still taken over the full sealed ``RunEvent``.
    """
    result = json.loads(run_event["result_payload_json"]) if run_event.get("result_payload_json") else {}
    agent_id = result.get("agent_id")

    if run_event["event_type"] == EVENT_ERROR:
        return {
            "error": True,
            "agent_id": agent_id,
            "message": result.get("message"),
        }

    action = json.loads(run_event["action_payload_json"]) if run_event.get("action_payload_json") else {}
    params = action.get("params", {}) or {}
    return {
        "agent_id": agent_id,
        "action": action.get("type"),
        "market_key": params.get("market_key"),
        "side": params.get("side"),
    }


def _is_number(value: Any) -> bool:
    """True for real numeric scores (excludes bools and the ``"pending"`` sentinel)."""
    return isinstance(value, int | float) and not isinstance(value, bool)


# ---------------------------------------------------------------------------
# Shared event constructors (single source of truth for live ≡ projection parity)
# ---------------------------------------------------------------------------
#
# CON-203 corollary: the live spectator stream MUST be a byte-faithful projection of the sealed
# record. To guarantee that without a second source of truth, the seq=0 ``COMPETITION_STARTED``
# event and each per-``RunEvent`` evidence event are built HERE by pure constructors that BOTH
# ``build_event_log`` (the offline projection) and ``veridex.competition.service`` (the live
# sink) call. Any drift would change both at once — so they cannot diverge.


def build_competition_started_event(
    *,
    competition_id: str,
    run_id: str,
    source_mode: str,
    agent_ids: list[str],
    base_ts: int,
) -> CompetitionEvent:
    """Build the canonical seq=0 ``COMPETITION_STARTED`` event (derived, deterministic).

    Args:
        competition_id: Owning competition identifier.
        run_id: Sealed Phase-1 run identifier.
        source_mode: ``"replay"`` or ``"live"`` (carried into the display payload).
        agent_ids: Participating agent identifiers, in run order.
        base_ts: Deterministic base timestamp for synthetic/derived events.

    Returns:
        The seq=0 :class:`CompetitionEvent` (``evidence=False``, ``derived_from=["competition_meta"]``).
    """
    started_payload: dict[str, Any] = {
        "competition_id": competition_id,
        "run_id": run_id,
        "source_mode": source_mode,
        "agent_ids": list(agent_ids),
    }
    return CompetitionEvent(
        competition_id=competition_id,
        run_id=run_id,
        seq=0,
        event_type=EventType.COMPETITION_STARTED,
        event_ts=base_ts,
        evidence=False,
        source_sequence_no=None,
        derived_from=["competition_meta"],
        payload=started_payload,
        payload_hash=event_payload_hash(started_payload),
    )


def build_policy_result_event(
    *,
    competition_id: str,
    run_id: str,
    seq: int,
    event_ts: int,
    agent_id: str,
    source_sequence_no_ref: int,
    policy_result_payload: dict[str, Any],
    execution_id: str | None = None,
) -> CompetitionEvent:
    """Build a Phase-2B ``POLICY_RESULT`` derived event (executor lane only).

    Called exclusively by the Phase-2B executor lane (Task 6). MUST NOT be called by
    :func:`build_event_log` — the 2A canonical log MUST stay byte-identical to Phase 2A.

    Args:
        competition_id: Owning competition identifier.
        run_id: Sealed Phase-1 run identifier.
        seq: Monotonic competition sequence number for this event.
        event_ts: Deterministic event timestamp.
        agent_id: The agent whose score row triggered this policy check.
        source_sequence_no_ref: The sealed ``RunEvent.sequence_no`` that the score row
            references (used to build the ``derived_from`` reference, NOT stored as
            ``source_sequence_no``).
        policy_result_payload: The :class:`~veridex.competition.policy.PolicyResult` as a
            plain dict (``decision``, ``reason_codes``, ``policy_hash``). Must be
            secret-free.
        execution_id: The execution-record id this decision governs. Threaded into the
            payload (additively) so POLICY_OBEYED can correlate a ``denied`` decision with a
            submit for the same execution (Plan-A Task 4). ``None`` (e.g. a pre-quote deny
            with no record yet) leaves the payload untouched — a missing id simply cannot be
            a bypass, so no false positive. This stays a DERIVED, ``evidence=False`` event:
            the field changes only this event's own ``payload_hash``, never the sealed prefix.

    Returns:
        A :class:`CompetitionEvent` with ``evidence=False``, ``source_sequence_no=None``,
        and ``derived_from=["score_row:{agent_id}:seq-{source_sequence_no_ref}"]``.
    """
    payload = policy_result_payload if execution_id is None else {**policy_result_payload, "execution_id": execution_id}
    return CompetitionEvent(
        competition_id=competition_id,
        run_id=run_id,
        seq=seq,
        event_type=EventType.POLICY_RESULT,
        event_ts=event_ts,
        evidence=False,
        source_sequence_no=None,
        derived_from=[f"score_row:{agent_id}:seq-{source_sequence_no_ref}"],
        payload=payload,
        payload_hash=event_payload_hash(payload),
    )


def build_execution_submitted_event(
    *,
    competition_id: str,
    run_id: str,
    seq: int,
    event_ts: int,
    execution_id: str,
    payload: dict[str, Any],
) -> CompetitionEvent:
    """Build a Phase-2B ``EXECUTION_SUBMITTED`` derived event (executor lane only).

    Called exclusively by the Phase-2B executor lane (Task 6). MUST NOT be called by
    :func:`build_event_log` — the 2A canonical log MUST stay byte-identical to Phase 2A.

    Args:
        competition_id: Owning competition identifier.
        run_id: Sealed Phase-1 run identifier.
        seq: Monotonic competition sequence number for this event.
        event_ts: Deterministic event timestamp.
        execution_id: Unique execution record identifier.
        payload: The secret-free submission payload (``venue``, ``market_ref``, ``side``,
            ``size``, etc.).

    Returns:
        A :class:`CompetitionEvent` with ``evidence=False``, ``source_sequence_no=None``,
        and ``derived_from=["execution_record:{execution_id}"]``.
    """
    return CompetitionEvent(
        competition_id=competition_id,
        run_id=run_id,
        seq=seq,
        event_type=EventType.EXECUTION_SUBMITTED,
        event_ts=event_ts,
        evidence=False,
        source_sequence_no=None,
        derived_from=[f"execution_record:{execution_id}"],
        payload=payload,
        payload_hash=event_payload_hash(payload),
    )


def build_execution_receipt_event(
    *,
    competition_id: str,
    run_id: str,
    seq: int,
    event_ts: int,
    execution_id: str,
    receipt_payload: dict[str, Any],
) -> CompetitionEvent:
    """Build a Phase-2B ``EXECUTION_RECEIPT`` derived event (executor lane only).

    Called exclusively by the Phase-2B executor lane (Task 6). MUST NOT be called by
    :func:`build_event_log` — the 2A canonical log MUST stay byte-identical to Phase 2A.

    Args:
        competition_id: Owning competition identifier.
        run_id: Sealed Phase-1 run identifier.
        seq: Monotonic competition sequence number for this event.
        event_ts: Deterministic event timestamp.
        execution_id: Unique execution record identifier (must match the submitted record).
        receipt_payload: The normalized, secret-free receipt dict (``execution_id``,
            ``venue``, ``status``, ``filled_size``, ``mode``, etc.).

    Returns:
        A :class:`CompetitionEvent` with ``evidence=False``, ``source_sequence_no=None``,
        and ``derived_from=["execution_record:{execution_id}"]``.
    """
    return CompetitionEvent(
        competition_id=competition_id,
        run_id=run_id,
        seq=seq,
        event_type=EventType.EXECUTION_RECEIPT,
        event_ts=event_ts,
        evidence=False,
        source_sequence_no=None,
        derived_from=[f"execution_record:{execution_id}"],
        payload=receipt_payload,
        payload_hash=event_payload_hash(receipt_payload),
    )


def build_approval_audit_event(
    *,
    competition_id: str,
    run_id: str,
    seq: int,
    event_ts: int,
    execution_id: str,
    audit_payload: dict[str, Any],
) -> CompetitionEvent:
    """Build a Phase-2B ``APPROVAL_AUDIT`` derived event (human-approval resolution only).

    Emitted by the Task-7 control-plane approve endpoint to record an operator's NON-SCORING
    decision on an ``awaiting_human`` execution. MUST NOT be called by :func:`build_event_log` —
    the 2A canonical log stays byte-identical to Phase 2A.

    Args:
        competition_id: Owning competition identifier.
        run_id: Sealed Phase-1 run identifier.
        seq: Monotonic competition sequence number for this event.
        event_ts: Deterministic event timestamp.
        execution_id: The execution record this decision resolves.
        audit_payload: Secret-free audit dict (``approver_id``, ``execution_id``, ``policy_hash``,
            ``decision``, ``note``, ``ts``).

    Returns:
        A :class:`CompetitionEvent` with ``evidence=False``, ``source_sequence_no=None``, and
        ``derived_from=["execution_record:{execution_id}"]``.
    """
    return CompetitionEvent(
        competition_id=competition_id,
        run_id=run_id,
        seq=seq,
        event_type=EventType.APPROVAL_AUDIT,
        event_ts=event_ts,
        evidence=False,
        source_sequence_no=None,
        derived_from=[f"execution_record:{execution_id}"],
        payload=audit_payload,
        payload_hash=event_payload_hash(audit_payload),
    )


def build_evidence_event(
    *,
    competition_id: str,
    run_id: str,
    run_event: dict[str, Any],
    current_tick_ts: int,
) -> tuple[CompetitionEvent, int]:
    """Build ONE evidence event from a sealed ``RunEvent``, carrying the tick ts forward.

    The mapping mirrors :func:`build_event_log`: ``tick→MARKET_TICK``, ``decision→AGENT_ACTION``,
    ``error→AGENT_ACTION`` (error variant). ``payload_hash`` binds the FULL sealed ``RunEvent``
    (not the UI ``payload`` projection). ``event_ts`` is the current tick ts; a ``tick`` event
    updates it and the new value is returned so callers can thread it across decisions.

    Args:
        competition_id: Owning competition identifier.
        run_id: Sealed Phase-1 run identifier.
        run_event: A single sealed, ``RunEvent``-validated event dict.
        current_tick_ts: The ts carried forward from the most recent ``tick`` event.

    Returns:
        ``(event, updated_current_tick_ts)``.

    Raises:
        ValueError: If ``run_event`` carries an unknown sealed event type.
    """
    event_type = run_event["event_type"]
    if event_type == EVENT_TICK:
        parsed_ts = _tick_ts(run_event.get("state_snapshot_json"))
        if parsed_ts is not None:
            current_tick_ts = parsed_ts
        comp_event_type = EventType.MARKET_TICK
        payload = _tick_payload(run_event.get("state_snapshot_json"))
    elif event_type in (EVENT_DECISION, EVENT_ERROR):
        comp_event_type = EventType.AGENT_ACTION
        payload = _action_payload(run_event)
    else:  # defensive: orchestrator emits only the three known types
        raise ValueError(f"unknown sealed run_event type: {event_type!r}")

    event = CompetitionEvent(
        competition_id=competition_id,
        run_id=run_id,
        seq=run_event["sequence_no"] + 1,
        event_type=comp_event_type,
        event_ts=current_tick_ts,
        evidence=True,
        source_sequence_no=run_event["sequence_no"],
        derived_from=[],
        payload=payload,
        # KEYSTONE: hash binds the FULL sealed run_event, not the UI payload.
        payload_hash=event_payload_hash(run_event),
    )
    return event, current_tick_ts


def build_event_log(run_result: RunResult, competition_meta: dict[str, Any]) -> list[CompetitionEvent]:
    """Project a sealed ``RunResult`` into the full canonical competition event log.

    Pure / sync / deterministic and non-mutating. Layout:

    * ``seq=0`` — ``COMPETITION_STARTED`` (derived; ``derived_from=["competition_meta"]``).
    * ``seq=1..N`` — one evidence event per sealed ``RunEvent`` in ``sequence_no`` order, with
      ``seq = sequence_no + 1``. ``tick→MARKET_TICK``, ``decision→AGENT_ACTION``,
      ``error→AGENT_ACTION`` (error variant). ``payload_hash`` binds the FULL sealed
      ``RunEvent``. ``event_ts`` is the current tick ts (carried forward across decisions).
    * ``seq>N`` — derived tail (all derived): ``LAW_RESULT`` per score row, ``SCORE_UPDATE``
      per agent, ``PROOF_ANCHOR``, then ``COMPETITION_FINALIZED`` last.

    Args:
        run_result: The frozen Phase-1 sealed run. Not mutated.
        competition_meta: Metadata dict. Read keys: ``competition_id`` (required),
            ``anchor_status`` (default ``"not_anchored"``), ``event_ts`` (deterministic base
            timestamp for synthetic/derived events, default ``0``).

    Returns:
        The full, ordered list of :class:`CompetitionEvent`.
    """
    competition_id = str(competition_meta["competition_id"])
    run_id = run_result.run_id
    anchor_status = str(competition_meta.get("anchor_status", "not_anchored"))
    base_ts = int(competition_meta.get("event_ts", 0))

    events: list[CompetitionEvent] = []

    # --- seq 0: COMPETITION_STARTED (derived) ----------------------------------------------
    events.append(
        build_competition_started_event(
            competition_id=competition_id,
            run_id=run_id,
            source_mode=run_result.source_mode,
            agent_ids=list(run_result.agent_ids),
            base_ts=base_ts,
        )
    )

    # --- seq 1..N: one evidence event per sealed RunEvent ----------------------------------
    # Shared with the live sink (veridex.competition.service) so live ≡ projection by construction.
    sorted_run_events = sorted(run_result.run_events, key=lambda r: r["sequence_no"])
    current_tick_ts = base_ts
    for run_event in sorted_run_events:
        event, current_tick_ts = build_evidence_event(
            competition_id=competition_id,
            run_id=run_id,
            run_event=run_event,
            current_tick_ts=current_tick_ts,
        )
        events.append(event)

    # --- seq > N: derived tail -------------------------------------------------------------
    derived_seq = len(run_result.run_events) + 1

    def _emit_derived(event_type: EventType, payload: dict[str, Any], derived_from: list[str]) -> None:
        nonlocal derived_seq
        events.append(
            CompetitionEvent(
                competition_id=competition_id,
                run_id=run_id,
                seq=derived_seq,
                event_type=event_type,
                event_ts=base_ts,
                evidence=False,
                source_sequence_no=None,
                derived_from=derived_from,
                payload=payload,
                payload_hash=event_payload_hash(payload),
            )
        )
        derived_seq += 1

    # (1) LAW_RESULT — one per scored decision, ordered by (agent_id, tick_seq).
    # DEC-2D-1 honesty: a fixed_duration/manual_stop window's finalize renamed the numeric CLV out of
    # clv_bps into window_clv_bps (row.pop("clv_bps")), so a window row carries NO clv_bps. Emit the
    # value under whichever field the row actually holds — window_clv_bps for a window row, clv_bps for
    # a true-CLV / WAIT / pending_horizon row — so downstream can never mistake window CLV for the true
    # closing-line value, and so this projection never KeyErrors on a windowed run (mirrors T8b).
    for row in sorted(run_result.score_rows, key=lambda r: (r["agent_id"], r["tick_seq"])):
        clv_field = CLV_FIELD_WINDOW if CLV_FIELD_WINDOW in row else "clv_bps"
        _emit_derived(
            EventType.LAW_RESULT,
            {
                "agent_id": row["agent_id"],
                "tick_seq": row["tick_seq"],
                clv_field: row[clv_field],
                "valid": row["valid"],
                "reason": row["reason"],
                "recomputed_edge_bps": row["recomputed_edge_bps"],
            },
            [f"score_row:{row['agent_id']}:tick-{row['tick_seq']}"],
        )

    # (2) SCORE_UPDATE — one per agent (ordered by agent_id), aggregating that agent's rows.
    rows_by_agent: dict[str, list[dict[str, Any]]] = {}
    for row in run_result.score_rows:
        rows_by_agent.setdefault(row["agent_id"], []).append(row)

    for agent_id in sorted(rows_by_agent):
        agent_rows = sorted(rows_by_agent[agent_id], key=lambda r: r["tick_seq"])
        # true-CLV aggregation only: a window row carries its value under window_clv_bps (no clv_bps),
        # and WAIT/pending_horizon carry the "pending" sentinel. r.get("clv_bps") is None for a window
        # row, so _is_number excludes it — window CLV is NEVER blended into the true-CLV mean/total,
        # exactly like scoring.is_scored / score_run (which never scores a window row).
        numeric_clvs = [r["clv_bps"] for r in agent_rows if _is_number(r.get("clv_bps"))]
        valid_count = sum(1 for r in agent_rows if r["valid"])
        total_clv = sum(numeric_clvs)
        mean_clv = (total_clv / len(numeric_clvs)) if numeric_clvs else None
        proof_mode = run_result.proof_mode_map.get(agent_id)
        _emit_derived(
            EventType.SCORE_UPDATE,
            {
                "agent_id": agent_id,
                "proof_mode": proof_mode,
                "scored_count": len(agent_rows),
                "valid_count": valid_count,
                "total_clv_bps": total_clv,
                "mean_clv_bps": mean_clv,
            },
            [f"score_row:{agent_id}:tick-{r['tick_seq']}" for r in agent_rows],
        )

    # (3) PROOF_ANCHOR — from meta anchor status + sealed evidence hash.
    _emit_derived(
        EventType.PROOF_ANCHOR,
        {"anchor_status": anchor_status, "evidence_hash": run_result.evidence_hash},
        ["evidence_hash"],
    )

    # (4) COMPETITION_FINALIZED — always last.
    _emit_derived(
        EventType.COMPETITION_FINALIZED,
        {
            "competition_id": competition_id,
            "run_id": run_id,
            "agent_count": len(run_result.agent_ids),
            "scored_count": len(run_result.score_rows),
        },
        [f"run:{run_result.run_id}"],
    )

    return events


def replay_from(events: list[CompetitionEvent], since_seq: int) -> list[CompetitionEvent]:
    """Return events with ``seq > since_seq``, in ascending ``seq`` order.

    Args:
        events: The competition event log (any order).
        since_seq: Exclusive lower bound; only events with a strictly greater ``seq`` return.

    Returns:
        The matching events, sorted by ``seq``.
    """
    return sorted((e for e in events if e.seq > since_seq), key=lambda e: e.seq)
