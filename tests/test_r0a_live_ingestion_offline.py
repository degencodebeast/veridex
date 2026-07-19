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
    FakeStreamClient,
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
    assert _authority_for_source(RecordingFakeSource()).provenance == TEST_FAKE_PROVENANCE
    assert _authority_for_source(RecordingFakeSource()).provenance != GENUINE_TXLINE_PROVENANCE
    assert "provenance" not in inspect.signature(run_capture_chain).parameters
    # ONLY the real live source's concrete TYPE maps to genuine authority.
    assert _authority_for_source(LiveCaptureSource()).provenance == GENUINE_TXLINE_PROVENANCE


# --- RED (F-residual): a fake SUBCLASS of LiveCaptureSource must NOT inherit genuine authority ------
def test_fake_livecapturesource_subclass_maps_to_test_not_genuine(tmp_path):
    """F-residual (Codex re-gate): a caller can subclass ``LiveCaptureSource`` and override
    ``credentials`` / ``stream_client`` with canned data, then route it through the ordinary
    ``run_capture_chain`` entrypoint. EXACT-type membership (``type(source) is LiveCaptureSource``,
    not ``isinstance``) must map the subclass to the fail-safe TEST authority — never genuine.
    (RED before the fix: the subclass passed ``isinstance`` and received ``genuine-txline``.)
    """

    class FakeLive(LiveCaptureSource):
        def credentials(self) -> tuple[str, str]:
            return ("jwt-x", "tok-x")

        def stream_client(self) -> FakeStreamClient:
            return FakeStreamClient(odds_frames(fixture_id=9, count=3))

    # Structural: the exact-type gate rejects the subclass.
    assert _authority_for_source(FakeLive()).provenance == TEST_FAKE_PROVENANCE
    assert _authority_for_source(FakeLive()).provenance != GENUINE_TXLINE_PROVENANCE
    # End-to-end through the ordinary entrypoint: the banked pack reads TEST, never genuine.
    result = _run_chain(FakeLive(), tmp_path)
    assert result.pack_dir is not None
    assert is_genuine_pack(result.pack_dir) is False
    assert read_pack_provenance(result.pack_dir) == TEST_FAKE_PROVENANCE


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


# =================================================================================================
# R-0b — GRACEFUL BOUNDED STOP (eligibility-critical): a live SSE stream never ends on its own, so
# without a bounded stop an operator can only kill the process mid-stream (SIGTERM) which cancels
# BEFORE the post-loop mint -> NO pack. These RED tests drive a clean BREAK of the `async for` loop
# on any of three conditions (records_target / duration_s / a stop-requested callable for SIGINT),
# after which the UNCHANGED finalize+mint path runs and finalizes a pack from the records seen so
# far. The honesty guards (authority-by-type, content-hash, scrub, heartbeat->no-pack) stay intact.
# =================================================================================================


class _DelayedResponse:
    """A streaming response whose deadline is driven by an INJECTED monotonic clock (deterministic).

    The stream itself yields frames instantly; the ``duration_s`` branch reads ``run_capture_chain``'s
    injected ``_clock`` seam, so the elapsed-time stop is tested WITHOUT wall-clock sleeps (no CI
    flakiness — a slow tick can never break before the first record).
    """

    def __init__(self, frames):
        self._frames = list(frames)
        self.status_code = 200

    async def aiter_lines(self):
        for line in self._frames:
            yield line


class _DelayedCtx:
    def __init__(self, frames):
        self._resp = _DelayedResponse(frames)

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class _DelayedClient:
    def __init__(self, frames):
        self._frames = list(frames)

    def stream(self, method, url, *, headers=None):
        return _DelayedCtx(self._frames)


class DurationFakeSource(RecordingFakeSource):
    """A recording-fake whose stream is consumed under an injected clock (still TEST authority).

    Subclassing ``RecordingFakeSource`` (NOT ``LiveCaptureSource``) keeps it on the fail-safe TEST
    authority path — the exact-type gate maps it to ``test-fake-recording``, never genuine.
    """

    def stream_client(self) -> _DelayedClient:
        return _DelayedClient(self._frames)


def _make_clock(values):
    """A deterministic monotonic-clock stand-in: pops each value, then repeats the last one."""
    seq = list(values)

    def clock() -> float:
        return seq.pop(0) if len(seq) > 1 else seq[0]

    return clock


# --- RED (R-0b-1): records_target bounded stop mints a pack from the records seen so far -----------
def test_records_target_bounded_stop_mints_pack(tmp_path):
    # A 5-odds-record stream (plus a leading heartbeat); a bound of 2 must BREAK after 2 records.
    source = RecordingFakeSource(odds_frames(fixture_id=5, count=5))
    result = asyncio.run(
        run_capture_chain(
            source,
            session_dir=tmp_path / "session",
            out_dir=tmp_path / "pack",
            records_target=2,
        )
    )

    # The loop stopped exactly at the target, and the UNCHANGED post-loop mint produced a valid pack.
    assert result.odds_records == 2
    assert result.pack is not None
    assert result.pack_dir is not None
    assert verify_content_hash(result.pack_dir) is True
    marketstates = load_pack_marketstates(result.pack_dir, 5)
    assert [ms.tick_seq for ms in marketstates] == [0, 1]
    # Bounding the stream does NOT change provenance: a fake seam is still a TEST pack, never genuine.
    assert read_pack_provenance(result.pack_dir) == TEST_FAKE_PROVENANCE
    assert is_genuine_pack(result.pack_dir) is False


# --- RED (R-0b-2): duration_s bounded stop mints a pack from the records seen so far ---------------
def test_duration_bounded_stop_mints_pack(tmp_path):
    # Clock reads: connect=0 (deadline=10); heartbeat check=1 (<10, no stop); rec1 check=20 (>=10 ->
    # BREAK). So exactly ONE odds record is captured before the elapsed-time stop fires.
    source = DurationFakeSource(odds_frames(fixture_id=7, count=3))
    result = asyncio.run(
        run_capture_chain(
            source,
            session_dir=tmp_path / "session",
            out_dir=tmp_path / "pack",
            duration_s=10.0,
            _clock=_make_clock([0.0, 1.0, 20.0]),
        )
    )

    assert result.odds_records == 1
    assert result.pack is not None
    assert result.pack_dir is not None
    assert verify_content_hash(result.pack_dir) is True
    assert read_pack_provenance(result.pack_dir) == TEST_FAKE_PROVENANCE
    assert is_genuine_pack(result.pack_dir) is False


# --- RED (R-0b-3): a bound on a HEARTBEAT-ONLY stream still mints NO pack (guard intact) -----------
def test_bounded_stop_heartbeat_only_still_no_pack(tmp_path):
    # Heartbeats never count toward records_target (they are not odds records), so the target is
    # never reached; the stream ends with 0 odds records and the :487 guard mints NO pack.
    result = _run_chain_bounded(
        RecordingFakeSource(heartbeat_only_frames(count=4)), tmp_path, records_target=2
    )

    assert result.pack is None
    assert result.pack_dir is None
    assert result.odds_records == 0
    assert result.heartbeats == 4


# --- RED (R-0b-4): SIGINT seam — a stop-requested callable breaks the loop cleanly, then mints -----
def test_stop_requested_bounded_stop_mints_pack(tmp_path):
    # Models the SIGINT signal handler: the callable returns True on its 2nd poll (after the leading
    # heartbeat + the first odds record), requesting a clean stop mid-stream.
    calls = {"n": 0}

    def stop_requested() -> bool:
        calls["n"] += 1
        return calls["n"] >= 2

    source = RecordingFakeSource(odds_frames(fixture_id=8, count=4))
    result = asyncio.run(
        run_capture_chain(
            source,
            session_dir=tmp_path / "session",
            out_dir=tmp_path / "pack",
            stop_requested=stop_requested,
        )
    )

    assert result.odds_records == 1
    assert result.pack is not None
    assert result.pack_dir is not None
    assert verify_content_hash(result.pack_dir) is True
    assert read_pack_provenance(result.pack_dir) == TEST_FAKE_PROVENANCE


# --- RED (R-0b-5): a bounded stop preserves the authority/hash/scrub honesty guards ----------------
def test_bounded_stop_preserves_authority_hash_and_scrub_guards(tmp_path, capsys):
    # A genuine-typed source still maps to genuine authority (structural, unchanged by bounding).
    assert _authority_for_source(LiveCaptureSource()).provenance == GENUINE_TXLINE_PROVENANCE
    # A fake still maps to TEST authority even under a bound.
    assert _authority_for_source(RecordingFakeSource()).provenance == TEST_FAKE_PROVENANCE

    source = RecordingFakeSource(odds_frames(fixture_id=6, count=5))
    jwt, token = source.credentials()
    assert SENTINEL_SECRET in jwt and SENTINEL_SECRET in token
    result = _run_chain_bounded(source, tmp_path, records_target=3)

    # Bounding changed only WHEN the stream ends — not provenance, not the hash, not secret hygiene.
    assert result.odds_records == 3
    assert result.pack_dir is not None
    assert verify_content_hash(result.pack_dir) is True
    assert is_genuine_pack(result.pack_dir) is False
    captured = capsys.readouterr()
    assert SENTINEL_SECRET not in captured.out and SENTINEL_SECRET not in captured.err
    for path in sorted(tmp_path.rglob("*")):
        if path.is_file():
            body = path.read_bytes().decode("utf-8", "replace")
            assert SENTINEL_SECRET not in body, f"sentinel secret leaked into {path}"


def _run_chain_bounded(source, tmp_path, **bounds):
    return asyncio.run(
        run_capture_chain(
            source, session_dir=tmp_path / "session", out_dir=tmp_path / "pack", **bounds
        )
    )
