"""E3-T2/T3 tests: crash-safe replay reader, gap-crossing exclusion, and sealed
content-hash byte-determinism with a duplicate-sequence guard (MM-R3, milestone E3).
"""

import json

import pytest

from veridex.live_recorder.contracts import (
    FairValueEvent,
    LiveRecorderSessionMeta,
    RecorderGapEvent,
)
from veridex.live_recorder.recorder import LiveRecorder, session_content_hash
from veridex.live_recorder.replay import (
    iter_change_series,
    max_sequence_no,
    read_session,
    read_session_strict,
    replay_reproduces,
)
from veridex.runtime.evidence import serialize_payload


def _start_meta() -> LiveRecorderSessionMeta:
    return LiveRecorderSessionMeta(
        session_ts=1_700_000_000,
        endpoints={"venue": "wss://example.invalid"},
        tool_version="test-e3",
        config_hash="cfg-hash",
        source_provenance={"venue": "poly"},
        fixture_ids=(18209181,),
    )


def _fv_dict(*, sequence_no: int, recv_ts: int, fv: float) -> dict:
    return FairValueEvent(
        sequence_no=sequence_no,
        event_type="FairValueEvent",
        source_ts=recv_ts // 1000,
        recv_ts=recv_ts,
        fixture_id=18209181,
        market_ref="1X2|home|full",
        side="part1",
        fv=fv,
        phase=1,
        suspended=False,
        message_id=None,
        proof_ts=None,
        proof_status="unavailable_no_message_id",
    ).model_dump()


def _gap_dict(*, sequence_no: int, from_ts: int, to_ts: int) -> dict:
    return RecorderGapEvent(
        sequence_no=sequence_no,
        event_type="RecorderGapEvent",
        source_ts=None,
        recv_ts=to_ts,
        from_ts=from_ts,
        to_ts=to_ts,
        source="venue",
        reason="disconnect",
    ).model_dump()


def test_replay_drops_truncated_final_line_and_excludes_gap_crossing(tmp_path):
    session = tmp_path / "s1"
    session.mkdir()
    (session / "meta.json").write_text(_start_meta().model_dump_json())

    lines = [
        serialize_payload(_fv_dict(sequence_no=1, recv_ts=100, fv=0.60)),
        serialize_payload(_fv_dict(sequence_no=2, recv_ts=200, fv=0.61)),
        serialize_payload(_gap_dict(sequence_no=3, from_ts=250, to_ts=350)),
        serialize_payload(_fv_dict(sequence_no=4, recv_ts=400, fv=0.62)),
    ]
    # a deliberately TRUNCATED final line (process killed mid-write)
    truncated = '{"event_type":"FairValueEvent","sequ'
    (session / "records.jsonl").write_text("\n".join(lines) + "\n" + truncated)

    meta, events, gaps = read_session(session)
    # truncated final line is DROPPED, no raise
    assert len(events) == 3
    assert len(gaps) == 1
    assert meta.tool_version == "test-e3"

    # a change event whose interval crosses a gap is excluded from the analysis series
    changes = list(iter_change_series(events, gaps))
    pairs = [(prev["sequence_no"], curr["sequence_no"]) for _, prev, curr in changes]
    assert (1, 2) in pairs  # interval [100,200] does not cross gap [250,350]
    assert (2, 4) not in pairs  # interval [200,400] straddles gap [250,350] → excluded

    # a malformed MIDDLE line RAISES
    session2 = tmp_path / "s2"
    session2.mkdir()
    (session2 / "meta.json").write_text(_start_meta().model_dump_json())
    (session2 / "records.jsonl").write_text(
        "\n".join([lines[0], "NOT JSON {{{", lines[1]]) + "\n"
    )
    with pytest.raises(ValueError):
        read_session(session2)


def test_gap_window_tamper_breaks_replay_reproduces(tmp_path):
    session = tmp_path / "s1"
    rec = LiveRecorder(session, _start_meta())
    rec.record(_fv_dict(sequence_no=0, recv_ts=100, fv=0.60))
    rec.record_gap(from_ts=200, to_ts=300, source="venue", reason="disconnect")
    rec.record(_fv_dict(sequence_no=0, recv_ts=400, fv=0.62))
    meta = rec.finalize(ended_ts=1_700_000_900)
    rec.close()

    # pristine artifact reproduces its sealed content hash
    assert meta.content_hash is not None
    assert replay_reproduces(session) is True

    # tamper with ONLY the persisted gap line — widen the window and change the reason
    records_path = session / "records.jsonl"
    lines = records_path.read_text().splitlines()
    tampered = []
    for line in lines:
        entry = json.loads(line)
        if entry.get("event_type") == "RecorderGapEvent":
            entry["to_ts"] = 950
            entry["reason"] = "tampered"
            tampered.append(serialize_payload(entry))
        else:
            tampered.append(line)
    records_path.write_text("\n".join(tampered) + "\n")

    # the sealed content hash MUST cover the gap window → tamper is detected
    assert replay_reproduces(session) is False


def test_replay_byte_determinism_and_duplicate_sequence_raises(tmp_path):
    session = tmp_path / "s1"
    rec = LiveRecorder(session, _start_meta())
    rec.record(_fv_dict(sequence_no=0, recv_ts=100, fv=0.60))
    rec.record(_fv_dict(sequence_no=0, recv_ts=200, fv=0.61))
    rec.record(_fv_dict(sequence_no=0, recv_ts=300, fv=0.62))
    meta = rec.finalize(ended_ts=1_700_000_900)
    rec.close()

    # replay from the sealed bytes reproduces the same ordered stream + same content_hash
    assert meta.content_hash is not None
    assert replay_reproduces(session) is True

    # inject a duplicate sequence_no → the content-hash computation RAISES
    _, events, _ = read_session(session)
    events_with_dup_sequence_no = events + [dict(events[-1])]
    with pytest.raises(ValueError):
        session_content_hash(events_with_dup_sequence_no)


# --- E3-T2: strict fail-closed reader + durable-tail max (REQ-020b/027) --------------------


def test_strict_reader_rejects_row_missing_sequence_no(tmp_path):
    """``read_session_strict`` FAILS CLOSED on a non-gap event row lacking ``sequence_no``."""
    session = tmp_path / "s1"
    session.mkdir()
    (session / "meta.json").write_text(_start_meta().model_dump_json())

    good = _fv_dict(sequence_no=1, recv_ts=100, fv=0.60)
    no_seq = {k: v for k, v in _fv_dict(sequence_no=2, recv_ts=200, fv=0.61).items() if k != "sequence_no"}
    (session / "records.jsonl").write_text(
        serialize_payload(good) + "\n" + json.dumps(no_seq) + "\n"
    )

    with pytest.raises(ValueError):
        read_session_strict(session)

    # a gap row legitimately always carries a sequence_no; the plain reader stays permissive
    _, events, _ = read_session(session)
    assert len(events) == 2  # read_session is unchanged for R3 back-compat


def test_persisted_pair_survives_restart_read_strict(tmp_path):
    """The pair written by ``record_and_return_pair`` == the pair ``read_session_strict`` returns."""
    rec = LiveRecorder(tmp_path, _start_meta())
    persisted = rec.record_and_return_pair(
        _fv_dict(sequence_no=0, recv_ts=100, fv=0.60)
    )
    rec.close()

    _, events, _ = read_session_strict(tmp_path)
    assert len(events) == 1
    row = events[0]
    assert persisted == (row["recv_ts"], row["sequence_no"])


def test_max_sequence_no_spans_events_and_gap_tail(tmp_path):
    """``max_sequence_no`` is the durable max over ALL rows — events AND a gap-at-tail."""
    rec = LiveRecorder(tmp_path, _start_meta())
    rec.record(_fv_dict(sequence_no=0, recv_ts=100, fv=0.60))  # seq 1
    rec.record_gap(from_ts=200, to_ts=300, source="venue", reason="disconnect")  # seq 2 (last)
    rec.close()
    assert max_sequence_no(tmp_path) == 2  # NOT 1 (events-only would miss the gap tail)
