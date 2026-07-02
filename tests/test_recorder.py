"""T2: pure session-file core for the continuous capture recorder (REQ-2D-002)."""

from __future__ import annotations

from veridex.ingest.recorder import SessionMeta, envelope_line, gap_line, read_session


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
