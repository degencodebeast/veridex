"""Phase-2A Task 2 — canonical event log keystone tests (TDD).

These tests pin the trust invariant (CON-203): every ``evidence=True`` event is hash-bound to
a sealed Phase-1 ``RunEvent``; every ``evidence=False`` event is a deterministic derivation
carrying ``derived_from`` refs. ``build_event_log`` is pure / sync / deterministic.

Phase-2B Task 4 tests (below the 2A keystone block) cover the three new derived-event builders:
``build_policy_result_event``, ``build_execution_submitted_event``, ``build_execution_receipt_event``.
These builders are NEVER called by ``build_event_log`` — the 2A keystone tests remain the guard.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

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
from veridex.law.recompute import PENDING
from veridex.runtime.evidence import serialize_payload
from veridex.runtime.orchestrator import RunResult
from veridex.runtime.window import CLV_FIELD_WINDOW
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


def test_policy_result_event_threads_execution_id() -> None:
    """Plan-A Task 4: execution_id is threaded into the policy_result payload so POLICY_OBEYED
    can correlate a DENIED decision with a submit for the same execution."""
    payload = {"decision": "denied", "reason_codes": ["slippage_over_max"], "policy_hash": "ph"}
    ev = build_policy_result_event(
        competition_id="c",
        run_id="r",
        seq=20,
        event_ts=0,
        agent_id="a",
        source_sequence_no_ref=3,
        policy_result_payload=payload,
        execution_id="r:3",
    )
    assert ev.payload["execution_id"] == "r:3"
    assert ev.payload_hash == event_payload_hash({**payload, "execution_id": "r:3"})
    assert "execution_id" not in payload  # caller's dict is not mutated


def test_policy_result_execution_id_is_additive_and_evidence_safe() -> None:
    """AC-213: threading execution_id changes ONLY this derived event's own payload_hash and
    never the sealed prefix — the event stays evidence=False / non-evidence."""
    payload = {"decision": "denied", "reason_codes": [], "policy_hash": "ph"}
    bare = build_policy_result_event(
        competition_id="c",
        run_id="r",
        seq=20,
        event_ts=0,
        agent_id="a",
        source_sequence_no_ref=3,
        policy_result_payload=payload,
    )
    enriched = build_policy_result_event(
        competition_id="c",
        run_id="r",
        seq=20,
        event_ts=0,
        agent_id="a",
        source_sequence_no_ref=3,
        policy_result_payload=payload,
        execution_id="r:3",
    )
    assert bare.evidence is False and enriched.evidence is False
    assert "execution_id" not in bare.payload  # default omits the key entirely
    assert enriched.payload_hash != bare.payload_hash  # additive change is confined to this event


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


# ---------------------------------------------------------------------------
# T10b — build_event_log is window-aware (DEC-2D-1/2 honesty; REQ-2D-105)
# ---------------------------------------------------------------------------
#
# A fixed_duration/manual_stop window's finalize renames a SCORED row's numeric CLV out of
# clv_bps into window_clv_bps (row.pop("clv_bps")); a pending_horizon row keeps the "pending"
# sentinel under clv_bps. build_event_log must project BOTH without a KeyError, must emit window
# CLV under window_clv_bps (never mislabelled as true clv_bps), and must NEVER blend window CLV
# into the true-CLV SCORE_UPDATE mean — exactly like scoring.is_scored / score_run.


def _score_row(agent_id: str, tick_seq: int, **overrides: Any) -> dict[str, Any]:
    """A minimal score_rows entry carrying every key build_event_log reads."""
    row: dict[str, Any] = {
        "agent_id": agent_id,
        "tick_seq": tick_seq,
        "valid": True,
        "reason": "value_flag",
        "recomputed_edge_bps": 0,
        "clv_bps": 0,
    }
    row.update(overrides)
    return row


def _run_with_rows(rows: list[dict[str, Any]]) -> RunResult:
    """A real sealed RunResult (fixture run_events) with hand-built windowed score_rows."""
    base = finished_run_result()
    return dataclasses.replace(base, score_rows=rows)


def _law_result_events(log: list[CompetitionEvent]) -> list[CompetitionEvent]:
    return [e for e in log if e.event_type == EventType.LAW_RESULT]


def _score_update_events(log: list[CompetitionEvent]) -> list[CompetitionEvent]:
    return [e for e in log if e.event_type == EventType.SCORE_UPDATE]


def test_windowed_run_does_not_crash() -> None:
    """A windowed run whose scored rows carry window_clv_bps (no clv_bps) must project cleanly.

    This is the RED for the T10b crash: build_event_log read row["clv_bps"] unconditionally, so a
    finalize()-renamed window row (which pops clv_bps) raised KeyError.
    """
    rows = [
        _score_row("agent-alpha", 0, clv_bps=None, **{CLV_FIELD_WINDOW: 184}),
        _score_row("agent-alpha", 1, clv_bps=None, **{CLV_FIELD_WINDOW: 42}),
    ]
    # The renamed row never carries both fields (finalize did window_clv_bps = row.pop("clv_bps")).
    for row in rows:
        del row["clv_bps"]

    log = build_event_log(_run_with_rows(rows), competition_meta())
    assert log[-1].event_type == EventType.COMPETITION_FINALIZED  # completed without KeyError


def test_law_result_emits_window_clv_under_window_field() -> None:
    """A window row's LAW_RESULT carries the value under window_clv_bps, never as true clv_bps."""
    row = _score_row("agent-alpha", 0, **{CLV_FIELD_WINDOW: 184})
    del row["clv_bps"]

    log = build_event_log(_run_with_rows([row]), competition_meta())
    (law,) = _law_result_events(log)
    assert law.payload[CLV_FIELD_WINDOW] == 184
    assert "clv_bps" not in law.payload  # window CLV is NEVER mislabelled as true CLV


def test_score_update_excludes_window_clv_from_true_mean() -> None:
    """The true-CLV total/mean aggregate ONLY numeric clv_bps rows — window_clv_bps is excluded."""
    true_row = _score_row("agent-alpha", 0, clv_bps=100)
    window_row = _score_row("agent-alpha", 1, **{CLV_FIELD_WINDOW: 999})
    del window_row["clv_bps"]

    log = build_event_log(_run_with_rows([true_row, window_row]), competition_meta())
    (score_update,) = _score_update_events(log)
    # 999 (window CLV) must NOT be blended into the true-CLV mean/total.
    assert score_update.payload["total_clv_bps"] == 100
    assert score_update.payload["mean_clv_bps"] == 100


def test_score_update_all_window_rows_true_mean_is_none() -> None:
    """An agent with only window_clv rows has NO true CLV — mean is honest None, no crash."""
    rows = [
        _score_row("agent-alpha", 0, **{CLV_FIELD_WINDOW: 184}),
        _score_row("agent-alpha", 1, **{CLV_FIELD_WINDOW: 42}),
    ]
    for row in rows:
        del row["clv_bps"]

    log = build_event_log(_run_with_rows(rows), competition_meta())
    (score_update,) = _score_update_events(log)
    assert score_update.payload["mean_clv_bps"] is None
    assert score_update.payload["total_clv_bps"] == 0


def test_pending_horizon_row_projects_without_crash() -> None:
    """A pending_horizon row keeps the "pending" sentinel; it is emitted honestly and excluded."""
    pending_row = _score_row("agent-alpha", 0, clv_bps=PENDING, reason="pending_horizon")
    numeric_row = _score_row("agent-alpha", 1, clv_bps=50)

    log = build_event_log(_run_with_rows([pending_row, numeric_row]), competition_meta())

    laws = {law.payload["tick_seq"]: law for law in _law_result_events(log)}
    assert laws[0].payload["clv_bps"] == PENDING  # sentinel emitted honestly, not a numeric 0
    (score_update,) = _score_update_events(log)
    assert score_update.payload["mean_clv_bps"] == 50  # pending excluded from the numeric mean


# ---------------------------------------------------------------------------
# T10c — SCORE_UPDATE carries the window-CLV aggregate alongside true CLV
# (DEC-2D-1: window CLV is labeled + never dropped, never blended into the true mean)
# ---------------------------------------------------------------------------


def test_score_update_carries_window_aggregate() -> None:
    """A windowed run's SCORE_UPDATE carries mean/total window CLV; true CLV stays honest None."""
    rows = [
        _score_row("agent-alpha", 0, **{CLV_FIELD_WINDOW: 184}),
        _score_row("agent-alpha", 1, **{CLV_FIELD_WINDOW: 42}),
    ]
    for row in rows:
        del row["clv_bps"]

    log = build_event_log(_run_with_rows(rows), competition_meta())
    (score_update,) = _score_update_events(log)
    # Window CLV is aggregated under its OWN labeled fields — never dropped.
    assert score_update.payload["total_window_clv_bps"] == 226
    assert score_update.payload["mean_window_clv_bps"] == 113.0  # (184 + 42) / 2, exact
    # True CLV is empty for this window run — window CLV is NEVER blended into the true mean.
    assert score_update.payload["mean_clv_bps"] is None
    assert score_update.payload["total_clv_bps"] == 0


def test_score_update_window_aggregate_excludes_true_clv() -> None:
    """The reciprocal separation: a numeric true clv_bps is NOT counted into the window aggregate."""
    true_row = _score_row("agent-alpha", 0, clv_bps=100)
    window_row = _score_row("agent-alpha", 1, **{CLV_FIELD_WINDOW: 999})
    del window_row["clv_bps"]

    log = build_event_log(_run_with_rows([true_row, window_row]), competition_meta())
    (score_update,) = _score_update_events(log)
    # window aggregate sees ONLY the window row (999), never the true 100.
    assert score_update.payload["total_window_clv_bps"] == 999
    assert score_update.payload["mean_window_clv_bps"] == 999
    # true aggregate sees ONLY the true row (100), never the window 999.
    assert score_update.payload["total_clv_bps"] == 100
    assert score_update.payload["mean_clv_bps"] == 100
