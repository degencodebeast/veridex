"""Phase-2A Task 2 — canonical event log keystone tests (TDD).

These tests pin the trust invariant (CON-203): every ``evidence=True`` event is hash-bound to
a sealed Phase-1 ``RunEvent``; every ``evidence=False`` event is a deterministic derivation
carrying ``derived_from`` refs. ``build_event_log`` is pure / sync / deterministic.

Phase-2B Task 4 tests (below the 2A keystone block) cover the three new derived-event builders:
``build_policy_result_event``, ``build_execution_submitted_event``, ``build_execution_receipt_event``.
These builders are NEVER called by ``build_event_log`` — the 2A keystone tests remain the guard.
"""

from __future__ import annotations

from pathlib import Path

from tests._arena_fixtures import competition_meta, finished_run_result
from veridex.competition.events import (
    CompetitionEvent,
    EventType,
    build_event_log,
    build_execution_receipt_event,
    build_execution_submitted_event,
    build_policy_result_event,
    event_payload_hash,
    replay_from,
)
from veridex.runtime.evidence import serialize_payload
from veridex.verifier.import_audit import assert_no_llm_imports


def test_evidence_events_hash_bind_to_sealed_run() -> None:  # AC-203 KEYSTONE
    rr = finished_run_result()
    log = build_event_log(rr, competition_meta())
    for ev in log:
        if ev.evidence:
            src = next(r for r in rr.run_events if r["sequence_no"] == ev.source_sequence_no)
            assert ev.payload_hash == event_payload_hash(src)


def test_tamper_changes_exactly_one_evidence_event() -> None:  # AC-203 tamper
    rr = finished_run_result()
    log = build_event_log(rr, competition_meta())
    rr.run_events[1]["result_payload_json"] = serialize_payload({"tampered": True})
    log2 = build_event_log(rr, competition_meta())
    diffs = [a for a, b in zip(log, log2, strict=True) if a.payload_hash != b.payload_hash]
    assert len(diffs) == 1 and diffs[0].evidence


def test_build_event_log_deterministic_excludes_walltime() -> None:  # AC-214
    rr = finished_run_result()
    a = [e.canonical_dict() for e in build_event_log(rr, competition_meta())]
    b = [e.canonical_dict() for e in build_event_log(rr, competition_meta())]
    assert a == b
    assert all("persisted_at" not in d and "broadcasted_at" not in d for d in a)


def test_source_refs_present() -> None:  # AC-215
    log = build_event_log(finished_run_result(), competition_meta())
    for ev in log:
        if ev.evidence:
            assert ev.source_sequence_no is not None and ev.derived_from == []
        else:
            assert ev.source_sequence_no is None and ev.derived_from


def test_no_reserved_2b_event_types_emitted() -> None:  # AC-210 / AC-218
    log = build_event_log(finished_run_result(), competition_meta())
    emitted = {e.event_type for e in log}
    reserved = {
        EventType.POLICY_RESULT,
        EventType.EXECUTION_SUBMITTED,
        EventType.EXECUTION_RECEIPT,
        EventType.PAYOUT_STATUS,
    }
    assert emitted.isdisjoint(reserved)


def test_replay_since_seq() -> None:  # AC-204
    log = build_event_log(finished_run_result(), competition_meta())
    tail = replay_from(log, since_seq=log[2].seq)
    assert [e.seq for e in tail] == [e.seq for e in log if e.seq > log[2].seq]


def test_replay_and_live_emit_same_protocol() -> None:  # AC-205 replay≡live
    rep = build_event_log(finished_run_result(source_mode="replay"), competition_meta())
    liv = build_event_log(finished_run_result(source_mode="live"), competition_meta())

    def shape(log: list[CompetitionEvent]) -> list[tuple[EventType, bool, int | None, bool]]:
        return [(e.event_type, e.evidence, e.source_sequence_no, bool(e.derived_from)) for e in log]

    assert shape(rep) == shape(liv)


def test_events_import_audit_clean() -> None:
    assert_no_llm_imports(Path("veridex/competition/events.py"))


# --- additional real structural assertions -----------------------------------------------


def test_seq0_is_competition_started() -> None:
    log = build_event_log(finished_run_result(), competition_meta())
    assert log[0].seq == 0
    assert log[0].event_type == EventType.COMPETITION_STARTED
    assert log[0].evidence is False


def test_evidence_seqs_are_contiguous_1_to_n() -> None:
    rr = finished_run_result()
    log = build_event_log(rr, competition_meta())
    n = len(rr.run_events)
    evidence_seqs = [e.seq for e in log if e.evidence]
    assert evidence_seqs == list(range(1, n + 1))
    # competition_seq == source_sequence_no + 1 for every evidence event.
    for ev in log:
        if ev.evidence:
            assert ev.seq == (ev.source_sequence_no or 0) + 1


def test_finalized_is_last_and_anchor_precedes_it() -> None:
    log = build_event_log(finished_run_result(), competition_meta())
    assert log[-1].event_type == EventType.COMPETITION_FINALIZED
    assert log[-2].event_type == EventType.PROOF_ANCHOR


def test_build_event_log_does_not_mutate_run_result() -> None:
    rr = finished_run_result()
    before = serialize_payload([dict(e) for e in rr.run_events])
    build_event_log(rr, competition_meta())
    after = serialize_payload([dict(e) for e in rr.run_events])
    assert before == after


# ---------------------------------------------------------------------------
# Phase-2B Task 4 — derived event builder tests
# ---------------------------------------------------------------------------


def test_policy_result_event_is_derived_nonevidence() -> None:
    """build_policy_result_event produces evidence=False with non-empty derived_from."""
    payload = {"decision": "approved", "reason_codes": [], "policy_hash": "ph"}
    ev = build_policy_result_event(
        competition_id="c",
        run_id="r",
        seq=20,
        event_ts=0,
        agent_id="a",
        source_sequence_no_ref=3,
        policy_result_payload=payload,
    )
    assert ev.event_type is EventType.POLICY_RESULT
    assert ev.evidence is False
    assert ev.source_sequence_no is None
    assert ev.derived_from  # non-empty
    assert ev.payload_hash == event_payload_hash(payload)


def test_policy_result_event_derived_from_ref() -> None:
    """build_policy_result_event derived_from encodes the score_row agent/seq reference."""
    payload = {"decision": "rejected", "reason_codes": ["size_limit"], "policy_hash": "ph2"}
    ev = build_policy_result_event(
        competition_id="c",
        run_id="r",
        seq=30,
        event_ts=100,
        agent_id="agent-x",
        source_sequence_no_ref=7,
        policy_result_payload=payload,
    )
    assert ev.derived_from == ["score_row:agent-x:seq-7"]


def test_policy_result_event_secret_free() -> None:
    """policy_result payload must not contain token/auth keys."""
    payload = {"decision": "approved", "reason_codes": [], "policy_hash": "ph"}
    ev = build_policy_result_event(
        competition_id="c",
        run_id="r",
        seq=20,
        event_ts=0,
        agent_id="a",
        source_sequence_no_ref=3,
        policy_result_payload=payload,
    )
    secret_keys = {"token", "auth", "secret", "password", "key", "api_key"}
    assert secret_keys.isdisjoint(ev.payload.keys())


def test_execution_submitted_event_is_derived() -> None:
    """build_execution_submitted_event produces evidence=False with correct derived_from."""
    payload = {"venue": "sx_bet", "market_ref": "OU|2.5|full", "side": "over", "size": 100.0}
    ev = build_execution_submitted_event(
        competition_id="c",
        run_id="r",
        seq=21,
        event_ts=0,
        execution_id="e1",
        payload=payload,
    )
    assert ev.event_type is EventType.EXECUTION_SUBMITTED
    assert ev.evidence is False
    assert ev.derived_from == ["execution_record:e1"]
    assert ev.payload_hash == event_payload_hash(payload)


def test_execution_submitted_event_source_sequence_no_none() -> None:
    """build_execution_submitted_event always sets source_sequence_no=None."""
    payload = {"venue": "pinnacle", "market_ref": "1X2||home", "side": "home", "size": 50.0}
    ev = build_execution_submitted_event(
        competition_id="comp-1",
        run_id="run-1",
        seq=5,
        event_ts=999,
        execution_id="exec-42",
        payload=payload,
    )
    assert ev.source_sequence_no is None


def test_execution_receipt_event_is_derived() -> None:
    """build_execution_receipt_event produces evidence=False with correct derived_from."""
    payload = {
        "execution_id": "e1",
        "venue": "sx_bet",
        "status": "filled",
        "filled_size": 100.0,
        "mode": "dry_run",
    }
    ev = build_execution_receipt_event(
        competition_id="c",
        run_id="r",
        seq=22,
        event_ts=0,
        execution_id="e1",
        receipt_payload=payload,
    )
    assert ev.event_type is EventType.EXECUTION_RECEIPT
    assert ev.evidence is False
    assert ev.derived_from == ["execution_record:e1"]
    assert ev.payload_hash == event_payload_hash(payload)


def test_execution_receipt_event_secret_free() -> None:
    """execution_receipt payload must not contain token/auth keys."""
    payload = {
        "execution_id": "e2",
        "venue": "pinnacle",
        "status": "partial",
        "filled_size": 30.0,
        "mode": "live",
    }
    ev = build_execution_receipt_event(
        competition_id="c",
        run_id="r",
        seq=22,
        event_ts=0,
        execution_id="e2",
        receipt_payload=payload,
    )
    secret_keys = {"token", "auth", "secret", "password", "key", "api_key"}
    assert secret_keys.isdisjoint(ev.payload.keys())


def test_build_event_log_still_does_not_emit_2b_events() -> None:
    """build_event_log MUST NOT emit any Phase-2B or 2D reserved event types."""
    log = build_event_log(finished_run_result(), competition_meta())
    emitted = {e.event_type for e in log}
    assert emitted.isdisjoint(
        {
            EventType.POLICY_RESULT,
            EventType.EXECUTION_SUBMITTED,
            EventType.EXECUTION_RECEIPT,
            EventType.PAYOUT_STATUS,
        }
    )
