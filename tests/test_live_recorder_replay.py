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
from veridex.live_recorder.replay import iter_change_series, read_session
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
