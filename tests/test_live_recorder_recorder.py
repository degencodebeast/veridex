"""E3-T1 tests: append-only recorder sink with explicit gap markers (MM-R3).

The recorder appends one JSON object per line, assigns a monotonic ``sequence_no`` in
append order, and writes a gap as a LABELED ``RecorderGapEvent`` line — never a silent
splice. No network, no LLM import.
"""

import json

import pytest

from veridex.live_recorder.contracts import LiveRecorderSessionMeta, RecorderGapEvent
from veridex.live_recorder.recorder import LiveRecorder, resume_recorder
from veridex.live_recorder.replay import read_session, replay_reproduces


def _start_meta() -> LiveRecorderSessionMeta:
    return LiveRecorderSessionMeta(
        session_ts=1_700_000_000,
        endpoints={"venue": "wss://example.invalid"},
        tool_version="test-e3",
        config_hash="cfg-hash",
        source_provenance={"venue": "poly"},
        fixture_ids=(18209181,),
    )


def _heartbeat(poll_index: int) -> dict:
    # An E1 model dumped canonically; sequence_no is a placeholder the recorder reassigns.
    return {
        "sequence_no": 0,
        "event_type": "RecorderHeartbeatEvent",
        "source_ts": None,
        "recv_ts": 1_700_000_000_000 + poll_index,
        "poll_index": poll_index,
        "venue_mids_seen": 1,
        "fv_points_recv": 1,
        "fv_aligned": True,
    }


def test_recorder_appends_and_writes_explicit_gaps(tmp_path):
    rec = LiveRecorder(tmp_path, _start_meta())
    n = 3
    for i in range(n):
        rec.record(_heartbeat(i))
    rec.record_gap(
        from_ts=1_700_000_000_500,
        to_ts=1_700_000_000_900,
        source="venue",
        reason="disconnect",
    )
    rec.close()

    lines = (tmp_path / "records.jsonl").read_text().splitlines()
    assert len(lines) == n + 1  # N events + one gap line

    parsed = [json.loads(line) for line in lines]
    seqs = [p["sequence_no"] for p in parsed]
    # strictly increasing across the appended lines
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)
    assert all(b > a for a, b in zip(seqs, seqs[1:], strict=False))

    # the gap line is a labeled RecorderGapEvent — never a silent splice
    gap = parsed[-1]
    assert gap["event_type"] == "RecorderGapEvent"
    assert gap["from_ts"] == 1_700_000_000_500
    assert gap["to_ts"] == 1_700_000_000_900
    assert gap["source"] == "venue"
    assert gap["reason"] == "disconnect"


def test_crash_partial_session_is_readable(tmp_path):
    """FIX M1: a session that crashed BEFORE finalize is still readable.

    ``__init__`` writes a START ``meta.json`` immediately (post-start fields Optional), so a
    mid-session crash that never calls ``finalize`` leaves a parseable meta and the recorded
    events — ``read_session`` succeeds instead of raising ``FileNotFoundError``.
    """
    rec = LiveRecorder(tmp_path, _start_meta())
    rec.record(_heartbeat(0))
    rec.record(_heartbeat(1))
    # Simulate a crash: DO NOT call finalize(); just close the file handle.
    rec.close()

    meta, events, gaps = read_session(tmp_path)
    assert meta.content_hash is None  # never finalized
    assert meta.ended_ts is None
    assert meta.session_ts == 1_700_000_000
    assert len(events) == 2
    assert [e["poll_index"] for e in events] == [0, 1]


# --- E3-T2: recorder-global sequence authority + writer-resume (REQ-020b/027) --------------


def _gap_row(*, from_ts: int, to_ts: int) -> dict:
    # A gap dict shaped exactly like ``record_gap`` writes (sequence_no reassigned by the recorder).
    return RecorderGapEvent(
        sequence_no=0,
        event_type="RecorderGapEvent",
        source_ts=None,
        recv_ts=to_ts,
        from_ts=from_ts,
        to_ts=to_ts,
        source="venue",
        reason="disconnect",
    ).model_dump()


def test_record_and_return_pair_equals_persisted_row(tmp_path):
    """The pair ``record_and_return_pair`` returns IS the ``(recv_ts, sequence_no)`` on disk."""
    rec = LiveRecorder(tmp_path, _start_meta())
    hb0 = _heartbeat(0)
    hb1 = _heartbeat(1)
    pair0 = rec.record_and_return_pair(hb0)
    pair1 = rec.record_and_return_pair(hb1)
    rec.close()

    parsed = [json.loads(line) for line in (tmp_path / "records.jsonl").read_text().splitlines()]
    assert pair0 == (parsed[0]["recv_ts"], parsed[0]["sequence_no"])
    assert pair1 == (parsed[1]["recv_ts"], parsed[1]["sequence_no"])
    # the persisted seqs are the recorder-minted global sequence — NOT the incoming placeholder 0
    assert [pair0[1], pair1[1]] == [1, 2]
    assert pair0[0] == hb0["recv_ts"] and pair1[0] == hb1["recv_ts"]


def test_writer_resume_continues_sequence(tmp_path):
    """CLEAN restart: resume continues the global sequence — durable tape is [1,2], never [1,1]."""
    rec = LiveRecorder(tmp_path, _start_meta())
    rec.record(_heartbeat(0))
    rec.close()  # crash: never finalized

    meta, _, _ = read_session(tmp_path)
    rec2 = resume_recorder(tmp_path, meta)
    rec2.record(_heartbeat(1))
    rec2.close()

    _, events, _ = read_session(tmp_path)
    seqs = [e["sequence_no"] for e in events]
    assert seqs == [1, 2]  # strictly monotonic, no duplicate
    assert len(set(seqs)) == len(seqs)


def test_writer_resume_finalize_reproduces_full_stream(tmp_path):
    """FULL-STREAM finalize/replay identity WITH a gap row in the pre-restart prefix."""
    rec = LiveRecorder(tmp_path, _start_meta())
    rec.record(_heartbeat(0))  # seq 1
    rec.record_gap(from_ts=200, to_ts=300, source="venue", reason="disconnect")  # seq 2
    rec.record(_heartbeat(1))  # seq 3
    rec.close()  # crash before finalize

    meta, _, _ = read_session(tmp_path)
    rec2 = resume_recorder(tmp_path, meta)
    rec2.record(_heartbeat(2))  # seq 4
    sealed = rec2.finalize(ended_ts=1_700_000_900)
    rec2.close()

    # event_count covers the FULL pre+post stream INCLUDING the gap; hash covers all durable rows
    assert sealed.event_count == 4
    assert replay_reproduces(tmp_path) is True
    _, events, gaps = read_session(tmp_path)
    assert len(events) == 3 and len(gaps) == 1
    assert [e["sequence_no"] for e in events] + [g["sequence_no"] for g in gaps] == [1, 3, 4, 2]


def test_resume_recovers_truncated_final_line_then_appends(tmp_path):
    """TRUNCATED-FINAL-LINE recovery: drop the partial tail, append max+1, finalize/replay hold."""
    rec = LiveRecorder(tmp_path, _start_meta())
    rec.record(_heartbeat(0))  # seq 1
    rec.record(_heartbeat(1))  # seq 2
    rec.close()

    # Simulate a crash mid-write of a THIRD line: append a truncated partial JSON fragment.
    records_path = tmp_path / "records.jsonl"
    records_path.write_text(records_path.read_text() + '{"event_type":"RecorderHeartbeatEvent","sequ')

    meta, _, _ = read_session(tmp_path)
    rec2 = resume_recorder(tmp_path, meta)
    rec2.record(_heartbeat(2))  # must land as seq 3, cleanly, NOT merged onto the partial bytes
    sealed = rec2.finalize(ended_ts=1_700_000_900)
    rec2.close()

    _, events, _ = read_session(tmp_path)
    assert [e["sequence_no"] for e in events] == [1, 2, 3]
    assert sealed.event_count == 3
    assert replay_reproduces(tmp_path) is True
    # the partial fragment is GONE — no line begins with the truncated bytes
    assert '"sequ' not in records_path.read_text().replace('"sequence_no"', "")


def test_resume_fails_closed_on_malformed_middle_line_no_append(tmp_path):
    """MALFORMED-MIDDLE refusal: a malformed NON-final row → RAISE before writer-open, bytes unchanged."""
    rec = LiveRecorder(tmp_path, _start_meta())
    rec.record(_heartbeat(0))
    rec.record(_heartbeat(1))
    rec.close()

    records_path = tmp_path / "records.jsonl"
    lines = records_path.read_text().splitlines()
    corrupt = "\n".join([lines[0], "NOT JSON {{{", lines[1]]) + "\n"
    records_path.write_text(corrupt)
    before = records_path.read_bytes()

    # read the crash-partial START meta directly (read_session would itself raise on the corruption)
    meta = LiveRecorderSessionMeta.model_validate_json((tmp_path / "meta.json").read_text())
    # the RESUME guard fails closed with its own diagnostic BEFORE opening the writer — the message
    # is asserted so this catches skipping the guard even if a later reader would also have raised.
    with pytest.raises(ValueError, match="malformed NON-final durable row"):
        resume_recorder(tmp_path, meta)
    assert records_path.read_bytes() == before  # zero append, byte-unchanged


def test_resume_fails_closed_on_duplicate_nonmonotonic_or_missing_seq(tmp_path):
    """DUPLICATE / NON-MONOTONIC / MISSING sequence → each RAISES before any append."""
    meta = _start_meta()

    def _session(name: str, rows: list[dict]) -> "object":
        session = tmp_path / name
        session.mkdir()
        (session / "meta.json").write_text(meta.model_dump_json())
        (session / "records.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows)
        )
        return session

    dup = _session("dup", [{**_heartbeat(0), "sequence_no": 1}, {**_heartbeat(1), "sequence_no": 1}])
    regress = _session("regress", [{**_heartbeat(0), "sequence_no": 2}, {**_heartbeat(1), "sequence_no": 1}])
    missing_hb = {k: v for k, v in _heartbeat(0).items() if k != "sequence_no"}
    missing = _session("missing", [missing_hb])

    for session in (dup, regress, missing):
        before = (session / "records.jsonl").read_bytes()
        with pytest.raises(ValueError):
            resume_recorder(session, meta)
        assert (session / "records.jsonl").read_bytes() == before


def test_resume_refuses_finalized_session_no_write(tmp_path):
    """TERMINAL-SEAL guard: a consistently-finalized session → RAISE, both files byte-unchanged."""
    rec = LiveRecorder(tmp_path, _start_meta())
    rec.record(_heartbeat(0))
    rec.finalize(ended_ts=1_700_000_900)  # SEALED
    rec.close()

    sealed_meta, _, _ = read_session(tmp_path)
    assert sealed_meta.content_hash is not None  # genuinely finalized
    records_before = (tmp_path / "records.jsonl").read_bytes()
    meta_before = (tmp_path / "meta.json").read_bytes()

    with pytest.raises(ValueError):
        resume_recorder(tmp_path, sealed_meta)

    # a partially/mixed-finalized meta likewise RAISES
    partial = sealed_meta.model_copy(update={"content_hash": None})  # event_count/ended_ts present
    with pytest.raises(ValueError):
        resume_recorder(tmp_path, partial)

    assert (tmp_path / "records.jsonl").read_bytes() == records_before
    assert (tmp_path / "meta.json").read_bytes() == meta_before


def test_resume_seeds_from_max_including_gap_tail(tmp_path):
    """GAP-AT-TAIL: the highest-sequence durable row is a gap → next append is max+1, no collision."""
    rec = LiveRecorder(tmp_path, _start_meta())
    rec.record(_heartbeat(0))  # seq 1
    rec.record_gap(from_ts=200, to_ts=300, source="venue", reason="disconnect")  # seq 2 (last row)
    rec.close()

    meta, _, _ = read_session(tmp_path)
    rec2 = resume_recorder(tmp_path, meta)
    rec2.record(_heartbeat(1))  # must be seq 3, NOT a duplicate 2
    sealed = rec2.finalize(ended_ts=1_700_000_900)
    rec2.close()

    _, events, gaps = read_session(tmp_path)
    all_seqs = sorted([e["sequence_no"] for e in events] + [g["sequence_no"] for g in gaps])
    assert all_seqs == [1, 2, 3]
    assert sealed.event_count == 3
    assert replay_reproduces(tmp_path) is True
