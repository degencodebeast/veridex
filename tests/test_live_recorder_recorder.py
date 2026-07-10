"""E3-T1 tests: append-only recorder sink with explicit gap markers (MM-R3).

The recorder appends one JSON object per line, assigns a monotonic ``sequence_no`` in
append order, and writes a gap as a LABELED ``RecorderGapEvent`` line — never a silent
splice. No network, no LLM import.
"""

import json

from veridex.live_recorder.contracts import LiveRecorderSessionMeta
from veridex.live_recorder.recorder import LiveRecorder


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
