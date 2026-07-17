"""R-0a — live-ingestion chain + provenance-honesty + secret-hygiene, all OFFLINE on fakes.

Every test here runs on the RECORDING-FAKE seam (fake auth + fake ``/odds/stream`` yielding canned
frames) — NO network, NO real TxLINE. The trust surface under test:

1. the full chain (auth -> stream -> normalize -> record -> pack) produces a hash-verified pack;
2. PROVENANCE HONESTY — a fake-backed pack is a TEST pack and NO code path can mint a genuine one;
3. SECRET SCRUBBING — an injected sentinel secret leaks into no artifact/log;
4. the five feed states derive/transition correctly (feeds III-3);
5. a heartbeat-only stream mints NO pack (liveness without market data).
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from scripts.txline_live_capture_accept import (
    SENTINEL_SECRET,
    RecordingFakeSource,
    heartbeat_only_frames,
    odds_frames,
)
from veridex.ingest.capture_chain import (
    GENUINE_TXLINE_PROVENANCE,
    TEST_FAKE_PROVENANCE,
    LiveCaptureSource,
    _authority_for_source,
    is_genuine_pack,
    read_pack_provenance,
    run_capture_chain,
)
from veridex.ingest.feed_health import FeedState, derive_feed_state
from veridex.ingest.replay_pack import load_pack_marketstates, verify_content_hash


def _run_chain(source: RecordingFakeSource, tmp_path: Path):
    return asyncio.run(
        run_capture_chain(source, session_dir=tmp_path / "session", out_dir=tmp_path / "pack")
    )


# --- RED 1: full chain on fakes -> content-hashed pack --------------------------------------------
def test_full_chain_on_fakes_produces_hash_verified_pack(tmp_path):
    result = _run_chain(RecordingFakeSource(odds_frames(fixture_id=5, count=3)), tmp_path)

    assert result.pack is not None
    assert result.pack_dir is not None
    assert verify_content_hash(result.pack_dir) is True
    # Replays through the SAME normalizer live uses (one projection) — 3 records -> 3 MarketStates.
    marketstates = load_pack_marketstates(result.pack_dir, 5)
    assert [ms.tick_seq for ms in marketstates] == [0, 1, 2]
    assert result.feed_state is FeedState.LIVE
    assert result.odds_records == 3


# --- RED 2: provenance honesty — a fake can NEVER be genuine --------------------------------------
def test_fake_pack_is_test_provenance_never_genuine(tmp_path):
    result = _run_chain(RecordingFakeSource(odds_frames()), tmp_path)

    assert read_pack_provenance(result.pack_dir) == TEST_FAKE_PROVENANCE
    assert read_pack_provenance(result.pack_dir) != GENUINE_TXLINE_PROVENANCE
    assert is_genuine_pack(result.pack_dir) is False

    # Structural invariant (MAJOR-1): authority is derived from the CLOSED producer set BY TYPE, not
    # from anything the source declares — so a fake/unknown source maps to test, never genuine, and
    # run_capture_chain has NO provenance parameter to inject "genuine" through.
    assert _authority_for_source(RecordingFakeSource())["provenance"] == TEST_FAKE_PROVENANCE
    assert _authority_for_source(RecordingFakeSource())["provenance"] != GENUINE_TXLINE_PROVENANCE
    assert "provenance" not in inspect.signature(run_capture_chain).parameters
    # ONLY the real live source's concrete TYPE maps to genuine authority.
    assert _authority_for_source(LiveCaptureSource())["provenance"] == GENUINE_TXLINE_PROVENANCE


# --- RED 3: secret scrubbing — sentinel leaks into no artifact/log --------------------------------
def test_no_secret_leaks_into_any_artifact_or_log(tmp_path, capsys):
    source = RecordingFakeSource(odds_frames())
    jwt, token = source.credentials()
    assert SENTINEL_SECRET in jwt and SENTINEL_SECRET in token  # the fake really carries the secret

    result = _run_chain(source, tmp_path)
    assert result.pack_dir is not None  # a pack was produced, so there is something to scan

    captured = capsys.readouterr()
    assert SENTINEL_SECRET not in captured.out
    assert SENTINEL_SECRET not in captured.err

    for path in sorted(tmp_path.rglob("*")):
        if path.is_file():
            body = path.read_bytes().decode("utf-8", "replace")
            assert SENTINEL_SECRET not in body, f"sentinel secret leaked into {path}"


# --- RED 4: the five feed states derive correctly -------------------------------------------------
def test_five_feed_states_derive_correctly():
    common = {"heartbeats_seen": 0, "odds_records_seen": 0, "last_frame_ts": None, "now_ts": 1000}

    assert (
        derive_feed_state(connecting=False, connected=False, **common) is FeedState.DISCONNECTED
    )
    assert derive_feed_state(connecting=True, connected=False, **common) is FeedState.CONNECTING
    assert (
        derive_feed_state(
            connecting=False,
            connected=True,
            odds_records_seen=4,
            heartbeats_seen=1,
            last_frame_ts=1000,
            now_ts=1000,
        )
        is FeedState.LIVE
    )
    assert (
        derive_feed_state(
            connecting=False,
            connected=True,
            odds_records_seen=0,
            heartbeats_seen=3,
            last_frame_ts=1000,
            now_ts=1000,
        )
        is FeedState.HEARTBEAT_ONLY
    )
    # Connected but the last frame is older than the staleness budget -> STALE dominates.
    assert (
        derive_feed_state(
            connecting=False,
            connected=True,
            odds_records_seen=4,
            heartbeats_seen=3,
            last_frame_ts=1000,
            now_ts=1000 + 999,
            stale_after_s=30,
        )
        is FeedState.STALE
    )


# --- RED 5: heartbeat-only stream mints NO pack ---------------------------------------------------
def test_heartbeat_only_stream_makes_no_pack(tmp_path):
    result = _run_chain(RecordingFakeSource(heartbeat_only_frames(count=4)), tmp_path)

    assert result.pack is None
    assert result.pack_dir is None
    assert result.odds_records == 0
    assert result.heartbeats == 4
    assert result.feed_state is FeedState.HEARTBEAT_ONLY


# --- Acceptance guard (beyond the 5 RED): the live branch stays FAIL-CLOSED on missing creds ------
def test_live_source_fails_closed_without_creds():
    source = LiveCaptureSource(env={})  # no JWT / TXLINE_X_API_TOKEN
    with pytest.raises(ValueError, match="live creds missing"):
        source.credentials()
