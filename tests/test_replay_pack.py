"""T3: ReplayPack contract + session converter (REQ-2D-301)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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


def test_stale_file_excluded_from_hash(tmp_path):
    session_dir = _write_session(tmp_path)
    out_dir = tmp_path / "pack"
    pack_from_session(session_dir, out_dir)

    # A leftover file from a prior build into the same out_dir, NOT part of this session's
    # fixtures manifest — must not affect the content_hash (manifest-scoped, not glob-scoped).
    (out_dir / "odds_99.jsonl").write_text(json.dumps({"FixtureId": 99, "Ts": 1}) + "\n")

    assert verify_content_hash(out_dir) is True


def test_load_pack_marketstates_unknown_fixture_raises_clear_error(tmp_path):
    session_dir = _write_session(tmp_path)
    out_dir = tmp_path / "pack"
    pack_from_session(session_dir, out_dir)

    with pytest.raises(FileNotFoundError, match="fixture_id 99"):
        load_pack_marketstates(out_dir, 99)


def test_capture_gaps_populated(tmp_path):
    session_dir = _write_session(tmp_path)
    out_dir = tmp_path / "pack"
    pack = pack_from_session(session_dir, out_dir)

    assert pack.capture["gaps"] == [{"from_ts": 100, "to_ts": 130}]


def test_load_pack_marketstates_rejects_unmanifested_stale_fixture(tmp_path):
    session_dir = _write_session(tmp_path)
    out_dir = tmp_path / "pack"
    pack_from_session(session_dir, out_dir)

    # WELL-FORMED stale file dropped in AFTER build — content that WOULD normalize cleanly if
    # loaded, so a rejection here proves the MANIFEST gate rejects it, not an incidental
    # normalizer crash on garbage data.
    (out_dir / "odds_99.jsonl").write_text(json.dumps(_odds_record(99, 200_000)) + "\n")

    assert verify_content_hash(out_dir) is True  # stale file still excluded from the hash

    with pytest.raises(FileNotFoundError, match="fixture_id 99"):
        load_pack_marketstates(out_dir, 99)  # manifest rejects it despite being loadable

    assert [ms.tick_seq for ms in load_pack_marketstates(out_dir, 5)] == [0, 1]  # manifested fixture still works


def test_load_pack_marketstates_refuses_tampered_pack_by_default(tmp_path):
    session_dir = _write_session(tmp_path)
    out_dir = tmp_path / "pack"
    pack_from_session(session_dir, out_dir)

    # A tamper that stays valid, parseable JSON (unlike a raw byte flip) — proves the rejection
    # below is verify's doing, not an incidental JSON/UTF-8 parse error.
    data_file = out_dir / "odds_5.jsonl"
    tampered_text = data_file.read_text().replace('"InRunning": false', '"InRunning": true')
    assert tampered_text != data_file.read_text()
    data_file.write_text(tampered_text)

    with pytest.raises(ValueError, match="content_hash"):
        load_pack_marketstates(out_dir, 5)

    # verify=False opts out for trusted/perf paths — same tampered content still loads.
    states = load_pack_marketstates(out_dir, 5, verify=False)
    assert len(states) == 2
