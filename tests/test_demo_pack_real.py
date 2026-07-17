"""I-10 — the banked REAL World Cup demo ReplayPack: pinned, tamper-evident, provenance-honest.

The trust surface is PROVENANCE HONESTY: the banked demo pack is a curated slice of GENUINE
TxLINE odds (real FIFA World Cup 2026 quarter-final fixtures, backfilled from the real TxLINE
``/odds/updates`` endpoint), so it reads ``genuine-txline`` through R-0a's honesty machinery. The
retained synthetic fallback can NEVER read genuine, and a synthetic pack can NEVER masquerade as
genuine — the fail-closed direction. The pack's ``content_hash`` is PINNED in the demo script, so
any tamper (mutated data file, or an edited pin) fails verification.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from scripts.demo_phase2d import (
    DEFAULT_PACK_DIR,
    DEMO_PACK_REAL_CONTENT_HASH,
    DEMO_PACK_REAL_DIR,
    SHARP_MOMENTUM_MIN_FIXTURES,
    _pack_content_hash,
    require_min_fixtures,
    resolve_real_demo_pack,
)
from veridex.ingest.capture_chain import (
    GENUINE_TXLINE_PROVENANCE,
    TEST_FAKE_PROVENANCE,
    is_genuine_pack,
    read_pack_provenance,
    stamp_pack_provenance,
)
from veridex.ingest.recorder import SessionMeta, envelope_line
from veridex.ingest.replay_pack import pack_from_session, verify_content_hash


def _write_session(session_dir: Path, records: list[dict]) -> None:
    """Write a minimal recorder session (meta + enveloped records) for the frozen packer."""
    session_dir.mkdir(parents=True, exist_ok=True)
    lines = [envelope_line(r, int(r["Ts"])) for r in records]
    (session_dir / "records.jsonl").write_text("\n".join(lines) + "\n")
    (session_dir / "meta.json").write_text(
        SessionMeta(started_ts=1, endpoints=[], tool_version="test").model_dump_json()
    )


def _odds_record(fixture_id: int, ts_ms: int) -> dict:
    return {
        "FixtureId": fixture_id,
        "Ts": ts_ms,
        "InRunning": False,
        "SuperOddsType": "OU",
        "MarketPeriod": None,
        "MarketParameters": "line=2.5",
        "PriceNames": ["Over", "Under"],
        "Prices": [1900, 2000],
        "Pct": [52.6, 47.4],
    }


# --- RED 1: pinned pack verifies + content_hash MATCHES the demo-script pin (tamper-evident) ----


def test_real_pack_verifies_content_hash():
    assert verify_content_hash(DEMO_PACK_REAL_DIR) is True


def test_real_pack_content_hash_matches_the_pin():
    # A mismatch (data mutated, OR the pin edited without rebuilding) must fail — the pin is the
    # tamper-evidence contract that binds the demo script to exactly these banked bytes.
    assert _pack_content_hash(DEMO_PACK_REAL_DIR) == DEMO_PACK_REAL_CONTENT_HASH


def test_real_pack_tamper_is_detected(tmp_path: Path):
    # Mutate one banked data file in a copy: verify_content_hash must REFUSE it, and its recomputed
    # hash must diverge from the pin.
    copy = tmp_path / "tampered"
    shutil.copytree(DEMO_PACK_REAL_DIR, copy)
    manifest = json.loads((copy / "pack.json").read_text())
    victim = copy / manifest["fixtures"][0]["records"]
    victim.write_text(victim.read_text() + '{"FixtureId": 0, "Ts": 1, "Prices": [9999]}\n')
    assert verify_content_hash(copy) is False
    assert _pack_content_hash(copy) == DEMO_PACK_REAL_CONTENT_HASH  # stored value untouched...
    # ...but it no longer describes the (tampered) data files, which is exactly what verify catches.


# --- RED 2: provenance honesty — the banked pack reads EXACTLY genuine (verified-genuine) --------


def test_real_pack_reads_genuine_txline():
    assert read_pack_provenance(DEMO_PACK_REAL_DIR) == GENUINE_TXLINE_PROVENANCE
    assert is_genuine_pack(DEMO_PACK_REAL_DIR) is True


def test_real_pack_declares_backfill_evidence_rung_transparently():
    # Honesty: a genuine-txline pack that is a REST backfill (not a live SSE recording) must SAY so,
    # so no reader mistakes it for a live-recorded-quote tape.
    capture = json.loads((DEMO_PACK_REAL_DIR / "pack.json").read_text())["capture"]
    assert capture["evidence_rung"] == "backfilled-price-history"
    assert capture["test_capture"] is False


# --- RED 2b: the retained synthetic fallback can NEVER read genuine (fail-closed) ----------------


def test_synthetic_fallback_is_labeled_and_never_genuine():
    assert "synthetic" in read_pack_provenance(DEFAULT_PACK_DIR).lower()
    assert is_genuine_pack(DEFAULT_PACK_DIR) is False


def test_synthetic_pack_can_never_masquerade_as_genuine(tmp_path: Path):
    session = tmp_path / "session"
    pack_dir = tmp_path / "pack"
    _write_session(session, [_odds_record(17588404, 100), _odds_record(17588404, 200)])
    pack_from_session(session, pack_dir)

    # A test/synthetic capture stamps its own honest label — never genuine.
    stamp_pack_provenance(pack_dir, TEST_FAKE_PROVENANCE, test_capture=True)
    assert is_genuine_pack(pack_dir) is False

    # Fail-closed AND: even a genuine-txline provenance string flagged test_capture is NOT genuine.
    stamp_pack_provenance(pack_dir, GENUINE_TXLINE_PROVENANCE, test_capture=True)
    assert is_genuine_pack(pack_dir) is False


# --- RED 3: >=2 distinct WC fixtures; a single-fixture pack is REFUSED ---------------------------


def test_real_pack_exposes_at_least_two_fixtures():
    fixture_ids = require_min_fixtures(DEMO_PACK_REAL_DIR)
    assert len(fixture_ids) >= SHARP_MOMENTUM_MIN_FIXTURES
    assert len(set(fixture_ids)) == len(fixture_ids)  # distinct


def test_single_fixture_pack_is_refused():
    # The shipped synthetic fallback has ONE fixture — refusing it here is the guard that stops a
    # single-fixture pack from silently disabling II-10's Sharp-Momentum gate.
    with pytest.raises(ValueError, match="fixture"):
        require_min_fixtures(DEFAULT_PACK_DIR)


def test_resolver_returns_the_pinned_genuine_multi_fixture_pack():
    # The fail-closed resolver only returns the pack when ALL hold: hash verifies, hash == pin,
    # provenance genuine, and >=2 fixtures. It is the single honest entrypoint for the demo/harness.
    assert resolve_real_demo_pack() == DEMO_PACK_REAL_DIR
