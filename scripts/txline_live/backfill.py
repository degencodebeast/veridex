"""EV-2 — historical backfill operator shell: /odds/updates + /scores/updates -> ReplayPacks.

Async network shell around the PURE :func:`veridex.ingest.backfill.build_pack_from_fixture`
core. For each requested fixture id it fetches the full TxLINE odds movement history and score
history, then builds a verified, self-describing :class:`~veridex.ingest.replay_pack.ReplayPack`
under ``packs/<fixture_id>/`` (loadable with ``load_pack_marketstates(..., verify=True)``).

NOT exercised by tests — no network/creds in the offline suite. All ``veridex`` imports (and thus
the lazily-imported ``httpx`` beneath ``fetch_*``) live INSIDE the async functions (CON-010), so
``import scripts.txline_live.backfill`` stays network-free and credential-free.

Credentials come from the environment via ``veridex.config`` (e.g. ``TXLINE_X_API_TOKEN`` + JWT in
``veridex/.env``), never argv.

Run:
    # credentials in veridex/.env (TXLINE_X_API_TOKEN=..., JWT=...)
    .venv/bin/python scripts/txline_live/backfill.py 17588404 17588314
    # or via env:
    TXLINE_BACKFILL_FIXTURE_IDS="17588404,17588314" .venv/bin/python scripts/txline_live/backfill.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

DEFAULT_PACKS_DIR = Path(__file__).parent / "packs"


async def _backfill_one(fid: int, base_url: str, creds: tuple[str, str], packs_dir: Path) -> None:
    # Imports kept local (CON-010): keeps module import network-free; httpx is lazy under fetch_*.
    from veridex.ingest.backfill import build_pack_from_fixture
    from veridex.ingest.txline_client import fetch_odds_updates, fetch_scores_updates

    odds = await fetch_odds_updates(fid, base_url=base_url, creds=creds)
    scores = await fetch_scores_updates(fid, base_url=base_url, creds=creds)
    print(f"fixture {fid}: fetched {len(odds)} odds updates, {len(scores)} score updates")

    out_dir = packs_dir / str(fid)
    pack = build_pack_from_fixture(fid, odds, scores, out_dir)
    print(f"  wrote pack -> {out_dir} (content_hash={pack.content_hash[:12]}..., {len(pack.fixtures)} fixture leg)")


async def run(fixture_ids: list[int], packs_dir: Path) -> None:
    from veridex.config import get_settings, require_txline

    settings = get_settings()
    creds = require_txline(settings)
    base_url = settings.txline_base_url

    packs_dir.mkdir(parents=True, exist_ok=True)
    print(f"backfilling {len(fixture_ids)} fixture(s) into {packs_dir}")
    for fid in fixture_ids:
        try:
            await _backfill_one(fid, base_url, creds, packs_dir)
        except Exception as e:  # noqa: BLE001 — one bad fixture must not abort the batch
            print(f"fixture {fid}: FAILED {type(e).__name__}: {e}")
    print("done.")


def _parse_fixture_ids(argv_ids: list[str]) -> list[int]:
    raw = argv_ids or [tok for tok in os.environ.get("TXLINE_BACKFILL_FIXTURE_IDS", "").replace(",", " ").split() if tok]
    return [int(tok) for tok in raw]


def main() -> None:
    parser = argparse.ArgumentParser(description="Historical TxLINE backfill -> verified ReplayPacks (EV-2).")
    parser.add_argument("fixture_ids", nargs="*", help="fixture ids to backfill (or set TXLINE_BACKFILL_FIXTURE_IDS)")
    parser.add_argument("--packs-dir", type=str, default=None, help=f"output dir (default {DEFAULT_PACKS_DIR})")
    args = parser.parse_args()

    fixture_ids = _parse_fixture_ids(args.fixture_ids)
    if not fixture_ids:
        parser.error("no fixture ids given (pass as args or set TXLINE_BACKFILL_FIXTURE_IDS)")

    packs_dir = Path(args.packs_dir) if args.packs_dir else DEFAULT_PACKS_DIR
    asyncio.run(run(fixture_ids, packs_dir))


if __name__ == "__main__":
    main()
