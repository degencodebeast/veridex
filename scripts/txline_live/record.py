"""T2 — continuous TxLINE capture recorder: async operator shell (REQ-2D-002).

Streams the live TxLINE odds SSE endpoint and banks every RAW record (not normalized —
see veridex.ingest.marketstate for that) into an append-only session directory under
captures/<session_ts>/. Supersedes the one-shot 45s smoke in capture.py, which this file
does not modify.

Disconnects are never silently spliced over: each reconnect writes an explicit gap marker,
then retries with exponential backoff (1s -> cap 60s). At shutdown (SIGINT or --minutes
timer), fetches the full odds-updates history for every fixture seen this session (needed
for CON-040 closing-line reconstruction in ReplayPacks).

NOT exercised by tests — no network/creds in the offline suite. httpx is imported lazily,
only inside the async shell below (CON-010).

Run:
    .venv/bin/python scripts/txline_live/record.py --minutes 30
    .venv/bin/python scripts/txline_live/record.py                 # runs until Ctrl-C
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from veridex.ingest.marketstate import parse_sse_line  # noqa: E402
from veridex.ingest.recorder import SessionMeta, envelope_line, finalize_meta, gap_line  # noqa: E402

TOOL_VERSION = "record.py/1"
ENDPOINT = "/odds/stream"
BACKOFF_INITIAL_S = 1.0
BACKOFF_CAP_S = 60.0
DEFAULT_SESSIONS_DIR = Path(__file__).parent / "captures"


class _Session:
    """Owns the append-only records.jsonl file handle for one capture session."""

    def __init__(self, session_dir: Path) -> None:
        self.session_dir = session_dir
        self.seen_fixture_ids: set[int] = set()
        self.record_counts: dict[str, int] = {}
        self.start_meta: SessionMeta | None = None
        self._fh = None

    def start(self, started_ts: int, endpoints: list[str]) -> None:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.start_meta = SessionMeta(started_ts=started_ts, endpoints=endpoints, tool_version=TOOL_VERSION)
        (self.session_dir / "meta.json").write_text(self.start_meta.model_dump_json())
        self._fh = (self.session_dir / "records.jsonl").open("a")

    def write_record(self, record: dict[str, Any], received_ts: int) -> None:
        fid = record.get("FixtureId")
        if fid is not None:
            try:
                fid_int = int(fid)
            except (TypeError, ValueError):
                fid_int = None
            if fid_int is not None:
                self.seen_fixture_ids.add(fid_int)
                key = str(fid_int)
                self.record_counts[key] = self.record_counts.get(key, 0) + 1
        self._append(envelope_line(record, received_ts))

    def write_gap(self, from_ts: int, to_ts: int) -> None:
        self._append(gap_line(from_ts, to_ts))

    def _append(self, line: str) -> None:
        if self._fh is None:
            raise RuntimeError("session not started")
        self._fh.write(line + "\n")
        self._fh.flush()

    def write_finalized_meta(self, ended_ts: int) -> None:
        """Rewrite meta.json at shutdown with ended_ts/fixture_ids/record_counts (REQ-2D-002(e))."""
        if self.start_meta is None:
            raise RuntimeError("session not started")
        finalized = finalize_meta(self.start_meta, ended_ts=ended_ts, record_counts=self.record_counts)
        (self.session_dir / "meta.json").write_text(finalized.model_dump_json())

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


async def _write_fixture_updates(session: _Session, base_url: str, creds: tuple[str, str]) -> None:
    from veridex.ingest.txline_client import fetch_odds_updates

    for fid in sorted(session.seen_fixture_ids):
        try:
            updates = await fetch_odds_updates(fid, base_url=base_url, creds=creds)
        except Exception as e:  # noqa: BLE001
            print(f"  updates fetch failed for fixture {fid}: {type(e).__name__}: {e}")
            continue
        (session.session_dir / f"updates_{fid}.json").write_text(json.dumps(updates))
        print(f"  wrote updates_{fid}.json ({len(updates)} updates)")


async def run(sessions_dir: Path, minutes: float | None) -> None:
    import httpx

    from veridex.config import get_settings, require_txline
    from veridex.ingest.live_client import build_auth_headers

    settings = get_settings()
    jwt, token = require_txline(settings)
    headers = build_auth_headers(jwt, token)
    url = f"{settings.txline_base_url}{ENDPOINT}"

    started_ts = int(time.time())
    session = _Session(sessions_dir / str(started_ts))
    session.start(started_ts, [ENDPOINT])
    print(f"recording to {session.session_dir} (started_ts={started_ts})")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, stop.set)
    except NotImplementedError:
        pass  # platform doesn't support event-loop signal handlers; SIGINT falls through below

    deadline = (started_ts + minutes * 60) if minutes is not None else None
    last_received_ts = started_ts
    backoff = BACKOFF_INITIAL_S

    try:
        async with httpx.AsyncClient() as client:
            while not stop.is_set() and (deadline is None or time.time() < deadline):
                try:
                    async with client.stream("GET", url, headers=headers) as resp:
                        backoff = BACKOFF_INITIAL_S  # reset after a successful connect
                        async for line in resp.aiter_lines():
                            if stop.is_set() or (deadline is not None and time.time() >= deadline):
                                break
                            record = parse_sse_line(line)
                            if record is None:
                                continue
                            last_received_ts = int(time.time())
                            session.write_record(record, last_received_ts)
                except Exception as e:  # noqa: BLE001
                    print(f"  stream disconnected: {type(e).__name__}: {e}")

                if stop.is_set() or (deadline is not None and time.time() >= deadline):
                    break

                reconnect_ts = int(time.time())
                session.write_gap(last_received_ts, reconnect_ts)  # never silently splice a gap
                print(f"  gap [{last_received_ts}, {reconnect_ts}], reconnecting in {backoff:.0f}s")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                except TimeoutError:
                    pass
                backoff = min(backoff * 2, BACKOFF_CAP_S)
                last_received_ts = int(time.time())
    except KeyboardInterrupt:
        print("\nKeyboardInterrupt received, shutting down...")
    finally:
        # Always runs — clean exit, SIGINT, or an escaped error — so the finalized meta,
        # per-fixture updates fetch, and file close are never skipped.
        session.write_finalized_meta(last_received_ts)
        print(f"fetching per-fixture updates for {len(session.seen_fixture_ids)} fixtures...")
        await _write_fixture_updates(session, settings.txline_base_url, (jwt, token))
        session.close()
        print("done.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Continuous TxLINE capture recorder (T2).")
    parser.add_argument("--minutes", type=float, default=None, help="record for N minutes, else until SIGINT")
    parser.add_argument("--session-dir", type=str, default=None, help=f"sessions base dir (default {DEFAULT_SESSIONS_DIR})")
    args = parser.parse_args()

    sessions_dir = Path(args.session_dir) if args.session_dir else DEFAULT_SESSIONS_DIR
    asyncio.run(run(sessions_dir, args.minutes))


if __name__ == "__main__":
    main()
