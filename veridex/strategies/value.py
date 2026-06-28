"""+EV taker proposal selection — Phase 2B Task 6 (pure / sync / LLM-free).

A "+EV taker" turns the SEALED competition result into a deterministic set of execution
proposals. It is the *first* link of the receipt≠skill trust boundary:

  * It reads ONLY the sealed ``score_rows`` — specifically the deterministic-law outputs
    ``recomputed_edge_bps`` and ``valid``. It NEVER reads the LLM-claimed edge, the action
    ``confidence``, or a venue quote as fair value. The law already computed the edge; this
    module merely selects which law-approved decisions clear the operator threshold.
  * ``market_key`` / ``side`` are pulled from the SEALED action
    (``score_row.raw_prescore.raw_action.params``) — the same bytes that entered the evidence
    hash — not from any live/untrusted source.
  * ``source_sequence_no`` is recovered by correlating each ``(agent_id, tick_seq)`` score row
    to its source decision ``RunEvent`` using ONLY sealed evidence (see
    :func:`_decision_seq_index`).

The module is import-light: it pulls the two ``event_type`` constants and the ``RunResult``
type from the orchestrator (offline-safe) and does no I/O, no async, and no LLM work.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from veridex.runtime.orchestrator import EVENT_DECISION, EVENT_TICK, RunResult


class Proposal(BaseModel):
    """One law-approved, threshold-clearing execution candidate.

    Attributes:
        agent_id: The agent whose sealed decision produced this proposal.
        source_sequence_no: The sealed decision ``RunEvent.sequence_no`` this row derives from.
        tick_seq: The tick the decision was made on (correlation key with the score row).
        market_key: Sealed target market key (from the sealed action params).
        side: Sealed market side (from the sealed action params).
        recomputed_edge_bps: The deterministic-law edge in basis points (the SEALED value the
            policy gate evaluates — never the LLM claim).
        kelly_fraction: The deterministic-law advisory Kelly fraction (the SEALED value used to
            size the order). Read straight from the score row — the lane NEVER re-derives Kelly
            from a venue price (the law owns that math). ``0.0`` when the law advised no sizing.
    """

    agent_id: str
    source_sequence_no: int
    tick_seq: int
    market_key: str
    side: str
    recomputed_edge_bps: int
    kelly_fraction: float


def _decision_seq_index(run_events: list[dict[str, Any]]) -> dict[tuple[str, int], int]:
    """Map each ``(agent_id, tick_seq)`` to its source decision ``RunEvent.sequence_no``.

    Built from sealed evidence only. Walking ``run_events`` in ``sequence_no`` order, each
    ``tick`` event carries ``tick_seq`` inside its ``state_snapshot_json`` and every following
    ``decision`` event carries ``agent_id`` inside its ``result_payload_json`` — so the pairing
    of a score row ``(agent_id, tick_seq)`` to its decision sequence number is unambiguous and
    deterministic.

    Args:
        run_events: The sealed, ``RunEvent``-validated event dicts of the run.

    Returns:
        A mapping ``(agent_id, tick_seq) -> sequence_no`` for every decision event.
    """
    index: dict[tuple[str, int], int] = {}
    current_tick_seq: int | None = None
    for event in sorted(run_events, key=lambda e: e["sequence_no"]):
        event_type = event["event_type"]
        if event_type == EVENT_TICK:
            snapshot = json.loads(event["state_snapshot_json"]) if event.get("state_snapshot_json") else {}
            tick_seq = snapshot.get("tick_seq")
            if tick_seq is not None:
                current_tick_seq = int(tick_seq)
        elif event_type == EVENT_DECISION and current_tick_seq is not None:
            result = json.loads(event["result_payload_json"]) if event.get("result_payload_json") else {}
            agent_id = result.get("agent_id")
            if agent_id is not None:
                index[(agent_id, current_tick_seq)] = int(event["sequence_no"])
    return index


def value_proposals(run_result: RunResult, *, min_edge_bps: int) -> list[Proposal]:
    """Select law-approved, threshold-clearing taker proposals from a SEALED run.

    A score row becomes a proposal IFF ``valid is True`` AND its deterministic-law
    ``recomputed_edge_bps`` is at least ``min_edge_bps``. Abstentions (e.g. ``WAIT``, which
    carries no ``market_key``/``side``) are not takers and are skipped. The returned list is
    deterministically ordered by ``source_sequence_no``.

    Each proposal also carries the SEALED ``kelly_fraction`` (law output) for downstream stake
    sizing; a missing/non-numeric value coerces to ``0.0``.

    This function is PURE: it never mutates ``run_result``, never performs I/O, and never reads
    an LLM-claimed edge/confidence or a venue quote.

    Args:
        run_result: The frozen, sealed Phase-1 run.
        min_edge_bps: Minimum sealed edge (basis points) a row must clear to be proposed.

    Returns:
        The selected :class:`Proposal` objects, ascending by ``source_sequence_no``.
    """
    seq_index = _decision_seq_index(run_result.run_events)
    proposals: list[Proposal] = []
    for row in run_result.score_rows:
        if row.get("valid") is not True:
            continue
        edge = row.get("recomputed_edge_bps")
        if not isinstance(edge, int) or isinstance(edge, bool):
            continue
        if edge < min_edge_bps:
            continue
        params = row.get("raw_prescore", {}).get("raw_action", {}).get("params", {}) or {}
        market_key = params.get("market_key")
        side = params.get("side")
        if not market_key or not side:
            continue  # abstention / non-taker decision — nothing to route
        source_seq = seq_index.get((row["agent_id"], row["tick_seq"]))
        if source_seq is None:
            continue  # no sealed decision event correlates (defensive; should not happen)
        kelly_raw = row.get("kelly_fraction")
        kelly_fraction = (
            float(kelly_raw) if isinstance(kelly_raw, (int, float)) and not isinstance(kelly_raw, bool) else 0.0
        )
        proposals.append(
            Proposal(
                agent_id=row["agent_id"],
                source_sequence_no=source_seq,
                tick_seq=row["tick_seq"],
                market_key=market_key,
                side=side,
                recomputed_edge_bps=edge,
                kelly_fraction=kelly_fraction,
            )
        )
    proposals.sort(key=lambda proposal: proposal.source_sequence_no)
    return proposals
