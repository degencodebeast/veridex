"""R-0a/R-0b — TxLINE live-ingestion capture ACCEPTANCE entry (dual-mode).

Wraps :mod:`veridex.ingest.capture_chain` for the two modes ``scripts/live_txline_capture_accept.sh``
drives:

* ``--offline`` (R-0a / CI): builds a RECORDING-FAKE source (canned auth + canned ``/odds/stream``
  frames — NO network) and runs the real capture chain. Asserts the produced pack is a TEST pack
  (provenance is :data:`~veridex.ingest.capture_chain.TEST_FAKE_PROVENANCE`, NEVER genuine) and that
  the injected sentinel secret leaks into NO artifact. This is the recording-fake seam the offline
  tests import.
* ``--live`` (R-0b / operator-run): resolves REAL creds FAIL-CLOSED via ``require_live_creds`` and
  runs the chain against the deployed feed → a GENUINE pack. R-0b runs this; R-0a only scaffolds it.

Credentials are NEVER logged: every diagnostic is scrubbed of the raw secret values.

Run:
    python scripts/txline_live_capture_accept.py --offline
    python scripts/txline_live_capture_accept.py --live      # R-0b, needs real creds in env
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
import tempfile
from collections.abc import AsyncIterator, Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from veridex.ingest.capture_chain import (  # noqa: E402
    TEST_FAKE_PROVENANCE,
    CaptureResult,
    LiveCaptureSource,
    _scrub,
    is_genuine_pack,
    read_pack_provenance,
    run_capture_chain,
)

#: A recognisable sentinel injected into the FAKE credentials so a leak-scan can assert it never
#: appears in any log line, diagnostic, or written artifact (the secret-hygiene proof).
SENTINEL_SECRET = "SENTINEL-SECRET-DO-NOT-LEAK-4c1f9e"


def _odds_sse_line(fixture_id: int, ts_ms: int, under_pct: float = 45.0) -> str:
    """One canned ``data:`` SSE line carrying a native TxLINE 1X2 odds record."""
    record = {
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
    return f"data: {json.dumps(record)}"


def odds_frames(fixture_id: int = 5, count: int = 3) -> list[str]:
    """Canned SSE frames: a heartbeat, then *count* odds records (a normal live-ish stream)."""
    frames = [": keepalive"]
    for i in range(count):
        frames.append(_odds_sse_line(fixture_id, 100_000 + i * 1_000))
    return frames


def heartbeat_only_frames(count: int = 3) -> list[str]:
    """Canned SSE frames: ONLY heartbeats, no odds records (liveness without market data)."""
    return [": keepalive" for _ in range(count)]


class _FakeResponse:
    """Mimics the ``httpx`` streaming response contract ``run_capture_chain`` consumes."""

    def __init__(self, frames: Iterable[str]) -> None:
        self._frames = list(frames)
        self.status_code = 200

    async def aiter_lines(self) -> AsyncIterator[str]:
        for line in self._frames:
            yield line


class _FakeStreamCtx:
    """Async context manager returned by ``FakeStreamClient.stream(...)``."""

    def __init__(self, frames: Iterable[str]) -> None:
        self._resp = _FakeResponse(frames)

    async def __aenter__(self) -> _FakeResponse:
        return self._resp

    async def __aexit__(self, *exc: object) -> bool:
        return False


class FakeStreamClient:
    """A no-network stand-in for ``httpx.AsyncClient`` yielding canned SSE frames."""

    def __init__(self, frames: Iterable[str]) -> None:
        self._frames = list(frames)

    def stream(self, method: str, url: str, *, headers: dict[str, str] | None = None) -> _FakeStreamCtx:
        return _FakeStreamCtx(self._frames)


class RecordingFakeSource:
    """The recording-fake capture seam: canned auth + canned ``/odds/stream``, NO network.

    It declares NO ``provenance`` of its own: authority is derived structurally from the concrete
    producer TYPE by :func:`~veridex.ingest.capture_chain._authority_for_source` (this fake maps to
    :data:`TEST_FAKE_PROVENANCE`, never genuine), never from anything the source says about itself.
    The fake credentials embed :data:`SENTINEL_SECRET` so a leak-scan can prove secret hygiene.
    """

    def __init__(self, frames: Iterable[str] | None = None) -> None:
        self._frames = list(frames) if frames is not None else odds_frames()

    def credentials(self) -> tuple[str, str]:
        return (f"jwt-{SENTINEL_SECRET}", f"tok-{SENTINEL_SECRET}")

    def stream_client(self) -> FakeStreamClient:
        return FakeStreamClient(self._frames)


async def _run_offline(out_root: Path) -> CaptureResult:
    """Run the chain on the recording-fake source and enforce the honesty + hygiene gates."""
    source = RecordingFakeSource()
    result = await run_capture_chain(
        source,
        session_dir=out_root / "session",
        out_dir=out_root / "pack",
    )
    if result.pack is None or result.pack_dir is None:
        raise SystemExit("offline accept FAILED: chain produced no pack from canned odds frames")
    provenance = read_pack_provenance(result.pack_dir)
    if provenance != TEST_FAKE_PROVENANCE or is_genuine_pack(result.pack_dir):
        raise SystemExit(f"offline accept FAILED: fake pack must be TEST, got provenance={provenance!r}")

    # Secret-hygiene scan: the sentinel must not appear in ANY produced artifact.
    for path in sorted(out_root.rglob("*")):
        if path.is_file() and SENTINEL_SECRET in path.read_bytes().decode("utf-8", "replace"):
            raise SystemExit(f"offline accept FAILED: sentinel secret leaked into {path}")

    jwt, token = source.credentials()
    print(_scrub(f"offline accept OK: TEST pack {result.pack_dir} (creds jwt={jwt} token={token})", jwt, token))
    print(f"  provenance={provenance} feed_state={result.feed_state.value} odds_records={result.odds_records}")
    return result


async def _run_live(
    *, duration_s: float | None = None, records_target: int | None = None
) -> CaptureResult:
    """R-0b live path: fail-closed real creds, then capture a GENUINE pack from the deployed feed.

    A live SSE stream never ends on its own, so the operator supplies a GRACEFUL BOUNDED STOP —
    ``--duration-s`` and/or ``--records-target``, and/or Ctrl-C (SIGINT). Each cleanly BREAKS the
    capture loop so the genuine pack is minted from the records seen so far. The bounds only shorten
    the stream; the genuine authority/hash/scrub guards inside ``run_capture_chain`` are untouched.
    """
    source = LiveCaptureSource()
    jwt, token = source.credentials()  # require_live_creds — raises here if creds are absent
    out_root = Path(tempfile.mkdtemp(prefix="txline-live-capture-"))

    # SIGINT (Ctrl-C) -> request a CLEAN stop (break the loop, then mint), never an abrupt cancel.
    stop_flag = {"stop": False}
    loop = asyncio.get_running_loop()
    signal_installed = False
    try:
        loop.add_signal_handler(signal.SIGINT, lambda: stop_flag.__setitem__("stop", True))
        signal_installed = True
    except (NotImplementedError, RuntimeError):
        # add_signal_handler is unavailable on some platforms / non-main threads; the other bounds
        # (duration_s / records_target) still apply.
        pass

    try:
        result = await run_capture_chain(
            source,
            session_dir=out_root / "session",
            out_dir=out_root / "pack",
            duration_s=duration_s,
            records_target=records_target,
            stop_requested=lambda: stop_flag["stop"],
        )
    finally:
        if signal_installed:
            loop.remove_signal_handler(signal.SIGINT)

    if result.pack_dir is None or not is_genuine_pack(result.pack_dir):
        raise SystemExit("live accept FAILED: no genuine pack produced")
    print(_scrub(f"live accept OK: GENUINE pack {result.pack_dir}", jwt, token))
    print(f"  provenance={read_pack_provenance(result.pack_dir)} feed_state={result.feed_state.value}")
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="TxLINE live-ingestion capture acceptance gate.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="recording-fakes, no network (R-0a / CI)")
    mode.add_argument("--live", action="store_true", help="real creds fail-closed (R-0b / operator)")
    parser.add_argument(
        "--duration-s",
        type=float,
        default=None,
        help="live only: stop the capture cleanly after this many seconds (graceful bounded stop)",
    )
    parser.add_argument(
        "--records-target",
        type=int,
        default=None,
        help="live only: stop the capture cleanly after this many odds records (>=1 finalizes R-0b)",
    )
    args = parser.parse_args(argv)

    if args.live:
        asyncio.run(_run_live(duration_s=args.duration_s, records_target=args.records_target))
    else:
        with tempfile.TemporaryDirectory(prefix="txline-offline-accept-") as tmp:
            asyncio.run(_run_offline(Path(tmp)))


if __name__ == "__main__":
    main()
