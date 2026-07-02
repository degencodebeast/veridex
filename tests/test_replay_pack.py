"""T3: ReplayPack contract + session converter (REQ-2D-301)."""

from __future__ import annotations

from pathlib import Path

from veridex.ingest.recorder import SessionMeta, envelope_line, gap_line
from veridex.ingest.replay_pack import (
    load_pack_marketstates,
    pack_from_session,
    verify_content_hash,
)


def _odds_record(fixture_id: int, ts_ms: int) -> dict:
    return {
        "FixtureId": fixture_id,
        "Ts": ts_ms,
        "InRunning": False,
        "SuperOddsType": "1X2",
        "MarketPeriod": None,
        "MarketParameters": None,
        "PriceNames": ["Home", "Draw", "Away"],
        "Prices": [2500, 3200, 2800],
        "Pct": [35.5, 28.0, 36.5],
    }


def _write_session(tmp_path: Path, name: str = "session") -> Path:
    session_dir = tmp_path / name
    session_dir.mkdir()
    (session_dir / "records.jsonl").write_text(
        envelope_line(_odds_record(5, 100_000), 100)
        + "\n"
        + gap_line(100, 130)
        + "\n"
        + envelope_line(_odds_record(5, 131_000), 131)
        + "\n"
    )
    (session_dir / "meta.json").write_text(
        SessionMeta(started_ts=99, endpoints=["/odds/stream"], tool_version="t").model_dump_json()
    )
    return session_dir


def test_pack_from_session_is_self_describing_and_hashed(tmp_path):
    session_dir = _write_session(tmp_path)

    pack1 = pack_from_session(session_dir, tmp_path / "pack1")
    pack2 = pack_from_session(session_dir, tmp_path / "pack2")

    assert (tmp_path / "pack1" / "pack.json").exists()
    assert pack1.closing_policy == "con-040_last_pre_inrunning"
    assert [f["fixture_id"] for f in pack1.fixtures] == [5]
    assert pack1.content_hash == pack2.content_hash  # determinism across runs


def test_load_pack_marketstates_uses_same_normalizer(tmp_path):
    session_dir = _write_session(tmp_path)
    out_dir = tmp_path / "pack"
    pack_from_session(session_dir, out_dir)

    marketstates = load_pack_marketstates(out_dir, 5)

    assert [ms.tick_seq for ms in marketstates] == [0, 1]


def test_tampered_pack_detected(tmp_path):
    session_dir = _write_session(tmp_path)
    out_dir = tmp_path / "pack"
    pack_from_session(session_dir, out_dir)

    assert verify_content_hash(out_dir) is True

    data_file = out_dir / "odds_5.jsonl"
    tampered = bytearray(data_file.read_bytes())
    tampered[0] ^= 0xFF
    data_file.write_bytes(bytes(tampered))

    assert verify_content_hash(out_dir) is False
