"""Phase-2B Task 6 — +EV taker proposal selection tests (TDD).

``value_proposals`` is PURE / SYNC / LLM-free. It reads ONLY the sealed ``score_rows``
(``recomputed_edge_bps`` + ``valid``) and correlates each selected row to its source decision
``RunEvent.sequence_no`` from the sealed ``run_events`` — never the LLM-claimed edge/confidence.
"""

from __future__ import annotations

from tests._arena_fixtures import finished_run_result
from veridex.runtime.orchestrator import EVENT_DECISION
from veridex.strategies.value import Proposal, value_proposals


def test_value_proposals_reads_sealed_not_llm_claim() -> None:
    """Every proposal's ``recomputed_edge_bps`` equals a sealed score_row's law value."""
    rr = finished_run_result()
    props = value_proposals(rr, min_edge_bps=0)
    assert props  # the fixture yields law-approved, threshold-clearing takes

    sealed_edges = {(row["agent_id"], row["tick_seq"]): row["recomputed_edge_bps"] for row in rr.score_rows}
    for prop in props:
        assert isinstance(prop, Proposal)
        # The proposal edge is the SEALED law value, not any action-claimed/confidence value.
        assert prop.recomputed_edge_bps == sealed_edges[(prop.agent_id, prop.tick_seq)]


def test_value_proposals_threshold_filters_below_min() -> None:
    """A min_edge_bps above every sealed edge yields no proposals (law gate, not LLM claim)."""
    rr = finished_run_result()
    max_edge = max(row["recomputed_edge_bps"] for row in rr.score_rows)
    assert value_proposals(rr, min_edge_bps=max_edge + 1) == []


def test_value_proposals_selects_only_valid_clearing_rows() -> None:
    """Selection IFF ``valid is True`` AND ``recomputed_edge_bps >= min_edge_bps``."""
    rr = finished_run_result()
    props = value_proposals(rr, min_edge_bps=0)
    selected = {(p.agent_id, p.tick_seq) for p in props}
    for row in rr.score_rows:
        clears = row.get("valid") is True and row["recomputed_edge_bps"] >= 0
        has_take = bool(row.get("raw_prescore", {}).get("raw_action", {}).get("params", {}).get("market_key"))
        if clears and has_take:
            assert (row["agent_id"], row["tick_seq"]) in selected
        else:
            assert (row["agent_id"], row["tick_seq"]) not in selected


def test_value_proposals_deterministic_order_by_source_seq() -> None:
    """Proposals come back in ascending ``source_sequence_no`` order, deterministically."""
    rr = finished_run_result()
    props = value_proposals(rr, min_edge_bps=0)
    seqs = [p.source_sequence_no for p in props]
    assert seqs == sorted(seqs)
    # Stable across repeated calls.
    again = [p.source_sequence_no for p in value_proposals(rr, min_edge_bps=0)]
    assert seqs == again


def test_proposal_source_seq_points_at_matching_decision_event() -> None:
    """Each proposal's source_sequence_no resolves to a decision RunEvent for that agent."""
    rr = finished_run_result()
    by_seq = {e["sequence_no"]: e for e in rr.run_events}
    for prop in value_proposals(rr, min_edge_bps=0):
        event = by_seq[prop.source_sequence_no]
        assert event["event_type"] == EVENT_DECISION
        assert prop.agent_id in (event.get("result_payload_json") or "")
        # market_key/side come from the SEALED action payload too.
        assert prop.market_key in (event.get("action_payload_json") or "")


def test_value_proposals_carry_sealed_kelly_fraction() -> None:
    """Each proposal's ``kelly_fraction`` is the SEALED law value from its score row."""
    rr = finished_run_result()
    sealed_kelly = {(row["agent_id"], row["tick_seq"]): row["kelly_fraction"] for row in rr.score_rows}
    for prop in value_proposals(rr, min_edge_bps=0):
        assert prop.kelly_fraction == sealed_kelly[(prop.agent_id, prop.tick_seq)]


def test_value_proposals_pure_no_mutation() -> None:
    """Calling value_proposals never mutates the sealed run (evidence hash unchanged)."""
    rr = finished_run_result()
    before = rr.evidence_hash
    value_proposals(rr, min_edge_bps=0)
    assert rr.evidence_hash == before


def test_proposal_carries_sealed_reference_price_and_prob() -> None:
    # Build a run whose sealed entry tick has a priced market the agent takes.
    from tests._arena_fixtures import finished_run_result
    from veridex.strategies.value import value_proposals

    run = finished_run_result()
    proposals = value_proposals(run, min_edge_bps=-100000)  # accept everything to get a taker
    takers = [p for p in proposals if p.market_key and p.side]
    if takers:  # at least one priced taker in the fixture
        p = takers[0]
        assert isinstance(p.reference_price, float)
        assert isinstance(p.entry_prob_bps, int)
        assert p.entry_prob_bps >= 0


def test_proposal_reference_price_matches_sealed_entry_tick() -> None:
    """``reference_price`` / ``entry_prob_bps`` are sourced from the SEALED entry tick.

    Independently reconstructs the entry-tick ``markets`` per ``tick_seq`` straight from the
    sealed event log and asserts each proposal's fields equal those sealed values exactly —
    proving the strategy reads sealed evidence (never a live/LLM-claimed price).
    """
    import json

    from veridex.runtime.orchestrator import EVENT_TICK

    rr = finished_run_result()
    entry_markets: dict[int, dict[str, dict[str, object]]] = {}
    for ev in rr.run_events:
        if ev["event_type"] != EVENT_TICK:
            continue
        snap = json.loads(ev["state_snapshot_json"])
        entry_markets[int(snap["tick_seq"])] = snap.get("markets", {})

    takers = [p for p in value_proposals(rr, min_edge_bps=-100000) if p.market_key and p.side]
    assert takers  # the fixture yields priced takers
    for p in takers:
        market = entry_markets[p.tick_seq][p.market_key]
        assert p.reference_price == float(market["stable_price"][p.side])  # type: ignore[index]
        assert p.entry_prob_bps == int(market["stable_prob_bps"][p.side])  # type: ignore[index]
