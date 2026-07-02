"""T2: pure session-file core for the continuous capture recorder (REQ-2D-002)."""

from __future__ import annotations

import json

from veridex.ingest.recorder import SessionMeta, envelope_line, finalize_meta, gap_line, read_session


def test_envelope_and_gap_lines_roundtrip(tmp_path):
    p = tmp_path / "records.jsonl"
    p.write_text(
        envelope_line({"FixtureId": 5, "x": 1}, 100)
        + "\n"
        + gap_line(101, 130)
        + "\n"
        + envelope_line({"FixtureId": 5, "x": 2}, 131)
        + "\n"
    )
    (tmp_path / "meta.json").write_text(
        SessionMeta(started_ts=99, endpoints=["/odds/stream"], tool_version="t").model_dump_json()
    )
    meta, records, gaps = read_session(tmp_path)
    assert meta.started_ts == 99
    assert [r["record"]["x"] for r in records] == [1, 2]
    assert gaps == [{"from_ts": 101, "to_ts": 130}]  # gap marker, NOT a silent splice


def test_no_secrets_in_lines():
    line = envelope_line({"FixtureId": 5}, 100)
    assert "Bearer" not in line and "X-Api-Token" not in line


def test_read_session_tolerates_truncated_final_line(tmp_path):
    p = tmp_path / "records.jsonl"
    p.write_text(
        envelope_line({"FixtureId": 5, "x": 1}, 100)
        + "\n"
        + envelope_line({"FixtureId": 5, "x": 2}, 101)
        + "\n"
        + '{"received_ts": 5, "reco'
    )
    (tmp_path / "meta.json").write_text(
        SessionMeta(started_ts=99, endpoints=["/odds/stream"], tool_version="t").model_dump_json()
    )
    meta, records, gaps = read_session(tmp_path)
    assert [r["record"]["x"] for r in records] == [1, 2]


def test_read_session_empty_and_gap_only(tmp_path):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    (empty_dir / "records.jsonl").write_text("")
    (empty_dir / "meta.json").write_text(
        SessionMeta(started_ts=1, endpoints=["/odds/stream"], tool_version="t").model_dump_json()
    )
    meta, records, gaps = read_session(empty_dir)
    assert (records, gaps) == ([], [])

    gap_only_dir = tmp_path / "gap_only"
    gap_only_dir.mkdir()
    (gap_only_dir / "records.jsonl").write_text(gap_line(10, 20) + "\n")
    (gap_only_dir / "meta.json").write_text(
        SessionMeta(started_ts=1, endpoints=["/odds/stream"], tool_version="t").model_dump_json()
    )
    meta, records, gaps = read_session(gap_only_dir)
    assert records == []
    assert gaps == [{"from_ts": 10, "to_ts": 20}]


def test_session_meta_roundtrips_with_new_fields():
    meta = SessionMeta(
        started_ts=1,
        endpoints=["/odds/stream"],
        tool_version="t",
        ended_ts=50,
        fixture_ids=[3, 7],
        record_counts={"3": 10, "7": 4},
    )
    parsed = SessionMeta.model_validate_json(meta.model_dump_json())
    assert parsed.ended_ts == 50
    assert parsed.fixture_ids == [3, 7]
    assert parsed.record_counts == {"3": 10, "7": 4}


def test_session_meta_defaults_when_new_fields_absent():
    # a crash-partial meta.json written before shutdown never had the new fields
    raw = json.dumps({"started_ts": 1, "endpoints": ["/odds/stream"], "tool_version": "t"})
    meta = SessionMeta.model_validate_json(raw)
    assert meta.ended_ts is None
    assert meta.fixture_ids == []
    assert meta.record_counts == {}


def test_finalize_meta_builds_sorted_fixture_ids_and_counts():
    start_meta = SessionMeta(started_ts=1, endpoints=["/odds/stream"], tool_version="t")
    finalized = finalize_meta(start_meta, ended_ts=99, record_counts={"7": 4, "3": 10})
    assert finalized.started_ts == 1
    assert finalized.endpoints == ["/odds/stream"]
    assert finalized.tool_version == "t"
    assert finalized.ended_ts == 99
    assert finalized.fixture_ids == [3, 7]
    assert finalized.record_counts == {"7": 4, "3": 10}


def test_read_session_parses_meta_with_only_old_fields(tmp_path):
    (tmp_path / "records.jsonl").write_text("")
    (tmp_path / "meta.json").write_text(
        json.dumps({"started_ts": 5, "endpoints": ["/odds/stream"], "tool_version": "old"})
    )
    meta, records, gaps = read_session(tmp_path)
    assert meta.started_ts == 5
    assert meta.fixture_ids == []
    assert meta.record_counts == {}
    assert meta.ended_ts is None
