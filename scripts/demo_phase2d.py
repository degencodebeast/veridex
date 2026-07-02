"""Phase-2D judge demo runner (T22 — REQ-2D-602 / REQ-2D-107 / AC-2D-106).

The single command a hackathon judge runs to see the flagship story end-to-end, OFFLINE and
DETERMINISTIC by default:

    python scripts/demo_phase2d.py            # offline: banked ReplayPack, writes demo_manifest.json
    python scripts/demo_phase2d.py --serve    # + boots the read API so the verify URLs resolve
    python scripts/demo_phase2d.py --pack DIR # point at a real captured ReplayPack (operator)

It produces REAL sealed runs (never a hand-written manifest): the flagship **Sharp Momentum v2**
(:func:`~veridex.strategies.momentum.sharp_momentum_agent`) is replayed through the SAME incremental
core the live loop uses, scored into an honest ``BacktestReport``, and persisted to a store so its
``/runs/{run_id}/verify`` URL recomputes the sealed evidence hash and re-derives the edge. A second
run exercises the standalone PAPER lane (proof-only, no venue orders) over the same banked ticks.

Honesty doctrine at the judge surface (REQ-2D-304): mode labels never overclaim. The offline demo is
a **Backtest** over BANKED odds — NOT a live run, NOT real money, NOT a fabricated result. The
default pack ships SYNTHETIC illustrative odds (clearly documented); ``--pack`` points at a real
captured pack for a real-odds artifact (REQ-2D-503). No live network, no real orders, ``anchor_fn``
off — the whole default path runs with ZERO network.

Importable: ``from scripts.demo_phase2d import run_demo`` lets the test suite invoke it in-process
against an injected store with no network.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from veridex.backtest import mode_ladder_label
from veridex.backtest.runner import run_backtest
from veridex.ingest.recorder import SessionMeta, envelope_line
from veridex.ingest.replay_pack import load_pack_marketstates, pack_from_session
from veridex.runtime.window import RunWindow
from veridex.store import InMemoryStore, Store
from veridex.strategies.momentum import SHARP_MOMENTUM_V2_LABEL, sharp_momentum_agent
from veridex_agent.run import standalone_run

# --- demo defaults ----------------------------------------------------------
#: The demo fixture id (shared with the Phase-1 demo fixture for continuity).
DEFAULT_FIXTURE_ID = 17588404
#: The banked ReplayPack that SHIPS with the repo — the offline demo default.
DEFAULT_PACK_DIR = Path(__file__).parent / "fixtures" / "demo_pack"
#: Where the manifest is written by default (repo root, next to the README).
DEFAULT_MANIFEST_PATH = Path(__file__).parent.parent / "demo_manifest.json"
#: Coverage-window id for the flagship backtest.
DEFAULT_WINDOW_ID = "wc_demo"
#: The one honest agent-strategy label surfaced for the flagship (never a v1/other alias).
FLAGSHIP_STRATEGY_LABEL = SHARP_MOMENTUM_V2_LABEL
#: The market family the demo pack carries (a clean totals family Sharp Momentum v2 acts on).
_MARKET_ALLOWLIST = ("OU",)
#: The truthful ``kind`` labels a manifest row may carry — a proof-only demo NEVER emits "live".
HONEST_KINDS: frozenset[str] = frozenset({"backtest", "paper", "replay"})

# --- illustrative banked odds tape ------------------------------------------
# The "Under" de-vigged probability (bps) per tick for the OU line: a flat pre-move baseline then a
# SHARP, SUSTAINED repricing UP — the exact shape Sharp Momentum v2 is built to catch (its v1
# predecessor false-fires on noise; v2 confirms with robust-z + Page-Hinkley + persistence). These
# are SYNTHETIC illustrative odds, clearly documented — NOT captured market history. The RUN over
# them is a genuine sealed proof; point ``--pack`` at a real captured pack for a real-odds artifact.
# fmt: off
_UNDER_BPS_TAPE: tuple[int, ...] = (
    5037, 4954, 5008, 4990, 4992, 4996, 4964, 4996, 4984, 5060, 5004, 4994, 4995, 4988, 4981, 4993,
    5150, 5289, 5457, 5590, 5743, 5916, 6051, 6185, 6340, 6501, 6540, 6496, 6496, 6521, 6483, 6495,
    6519, 6513, 6503, 6514,
)
# fmt: on


def _ou_record(ts_ms: int, under_bps: int) -> dict[str, Any]:
    """One raw native TxLINE OU record for the demo tape (de-vigged pct + coherent decimal odds)."""
    under_pct = round(under_bps / 100.0, 2)
    over_pct = round(100.0 - under_pct, 2)
    # Prices are decimal odds x1000 (the normalizer divides by 1000); derive them from the pct so
    # price and probability stay coherent. They never enter the sealed CLV (scored off prob_bps).
    return {
        "FixtureId": DEFAULT_FIXTURE_ID,
        "Ts": ts_ms,
        "InRunning": False,
        "SuperOddsType": "OU",
        "MarketPeriod": None,
        "MarketParameters": "line=2.5",
        "PriceNames": ["Over", "Under"],
        "Prices": [round(100_000.0 / max(over_pct, 0.01)), round(100_000.0 / max(under_pct, 0.01))],
        "Pct": [over_pct, under_pct],
    }


def build_reference_pack(dst: Path) -> Path:
    """(Re)build the shipped illustrative banked ReplayPack at ``dst`` — deterministic and offline.

    Writes a recorder session from :data:`_UNDER_BPS_TAPE`, then runs the SAME ``pack_from_session``
    transform live capture uses, so the shipped pack is a genuine, content-hashed ReplayPack (its
    ``run_backtest`` replay path verifies the hash and refuses a tampered pack).

    Args:
        dst: Destination pack directory (recreated from scratch if it already exists).

    Returns:
        ``dst`` (now containing ``pack.json`` + ``odds_<fixture>.jsonl``).
    """
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = Path(tmp) / "session"
        session_dir.mkdir()
        lines = [
            envelope_line(_ou_record(100_000 + i * 10_000, under_bps), 100 + i * 10)
            for i, under_bps in enumerate(_UNDER_BPS_TAPE)
        ]
        (session_dir / "records.jsonl").write_text("\n".join(lines) + "\n")
        (session_dir / "meta.json").write_text(
            SessionMeta(
                started_ts=99, endpoints=["/odds/stream"], tool_version="veridex-demo-phase2d"
            ).model_dump_json()
        )
        if dst.exists():
            shutil.rmtree(dst)
        pack_from_session(session_dir, dst)
    return dst


def _pack_content_hash(pack_dir: Path) -> str:
    """Read the pack's stored ``content_hash`` (bound into the demo's deterministic run ids)."""
    return str(json.loads((pack_dir / "pack.json").read_text())["content_hash"])


def verify_url(run_id: str, *, base_url: str = "") -> str:
    """The proof URL for ``run_id`` — the SAME ``/runs/{id}/verify`` path an arena run uses.

    Args:
        run_id: The sealed run id.
        base_url: Optional origin (e.g. ``http://localhost:8080``); empty → a relative path.
    """
    return f"{base_url}/runs/{run_id}/verify"


async def run_demo(
    pack_dir: Path,
    fixture_id: int,
    *,
    out_path: Path,
    store: Store | None = None,
    window_id: str = DEFAULT_WINDOW_ID,
    base_url: str = "",
) -> dict[str, Any]:
    """Run the demo over ``pack_dir``, persist the sealed runs, and write ``demo_manifest.json``.

    Produces two REAL sealed runs, both offline and both resolvable via ``/runs/{id}/verify``:

    1. **backtest** — the flagship Sharp Momentum v2 scored against the pack's reconstructed
       pre-match close (an honest ``BacktestReport``; mode label ``"Backtest"``).
    2. **paper** — the same flagship through the standalone PAPER lane (``execution_mode="paper"``:
       proof-only, NO venue orders). On a replay source this too honestly reads ``"Backtest"`` — the
       ``kind`` names the lane, the mode label names the source x execution honesty.

    Args:
        pack_dir: The banked ReplayPack directory (default: the shipped illustrative pack).
        fixture_id: The fixture within the pack to replay.
        out_path: Where to write ``demo_manifest.json``.
        store: Store the sealed runs are persisted to (so the verify URLs resolve); ``None`` → a
            fresh in-memory store.
        window_id: Coverage-window id for the flagship backtest.
        base_url: Optional origin folded into each ``verify_url`` (empty → relative paths).

    Returns:
        The manifest dict (identical to what is written to ``out_path``).
    """
    resolved_store: Store = store if store is not None else InMemoryStore()
    content_hash = _pack_content_hash(pack_dir)
    runs: list[dict[str, Any]] = []

    # --- (1) flagship BACKTEST: Sharp Momentum v2 over the banked ReplayPack ---------------------
    window = RunWindow(
        window_id=window_id,
        fixture_id=fixture_id,
        market_allowlist=list(_MARKET_ALLOWLIST),
        end_rule="pre_match",
    )
    bt_result, bt_report = await run_backtest(
        pack_dir, fixture_id, [sharp_momentum_agent("momentum-sharp")], window=window
    )
    await resolved_store.persist_run(bt_result)  # so /runs/{id}/verify can load + recompute it
    runs.append(
        {
            "run_id": bt_report.run_id,
            "kind": "backtest",
            "verify_url": verify_url(bt_report.run_id, base_url=base_url),
            "strategy_label": FLAGSHIP_STRATEGY_LABEL,  # "Sharp Momentum v2"
            "mode_label": bt_report.mode_label,  # "Backtest" — honest, never "Live"
            "source_mode": bt_report.source_mode,  # "replay"
            "execution_mode": bt_report.execution_mode,  # "paper"
            "sample_size": bt_report.sample_size,
            "valid_count": bt_report.valid_count,
            "clv_confidence": bt_report.clv_confidence,
            "avg_clv_bps": bt_report.avg_clv,
            "real_executable_edge_bps": bt_report.real_executable_edge_bps,  # None on paper (honest)
            "content_hash": content_hash,
            "evidence_hash": bt_report.evidence_hash,
        }
    )

    # --- (2) standalone PAPER lane: same flagship, proof-only (NO venue orders) -------------------
    marketstates = load_pack_marketstates(pack_dir, fixture_id, verify=True)
    paper_run_id = f"paper_{content_hash[:12]}"
    paper = await standalone_run(
        marketstates,
        sharp_momentum_agent("momentum-sharp"),
        source_mode="replay",
        execution_mode="paper",  # proof-only: no policy envelope, no execution lane, no orders
        policy_envelope=None,
        anchor_fn=None,  # offline: skip the on-chain anchor
        run_id=paper_run_id,
        store=resolved_store,  # persisted at seal so the verify URL resolves
    )
    runs.append(
        {
            "run_id": paper.run_id,
            "kind": "paper",
            "verify_url": verify_url(paper.run_id, base_url=base_url),
            "strategy_label": FLAGSHIP_STRATEGY_LABEL,
            # Honest source x execution label (replay + paper -> "Backtest"); NEVER "Live".
            "mode_label": mode_ladder_label(paper.source_mode, paper.execution_mode),
            "source_mode": paper.source_mode,  # "replay"
            "execution_mode": paper.execution_mode,  # "paper" — proof-only, no orders
            "verified": paper.verified,
            "anchor_status": paper.anchor_status,  # "not_anchored" (offline)
        }
    )

    manifest: dict[str, Any] = {
        "generated_ts": int(time.time()),
        "pack_id": pack_dir.name,
        "content_hash": content_hash,
        "fixture_id": fixture_id,
        "flagship_strategy": FLAGSHIP_STRATEGY_LABEL,
        "offline": True,
        "notes": (
            "Proof-only demo: no live network, no real-money orders (anchor off). Mode labels are "
            "honest — a Backtest over BANKED odds, never 'Live'. The default pack ships SYNTHETIC "
            "illustrative odds; the runs over them are genuine sealed proofs. Point --pack at a real "
            "captured ReplayPack for a real-odds artifact."
        ),
        "runs": runs,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2, default=str) + "\n")
    return manifest


def _print_summary(manifest: dict[str, Any], out_path: Path, *, base_url: str) -> None:
    """Print a judge-facing summary: the flagship, the honest labels, and the verify URLs."""
    print("=== Veridex Phase-2D demo ===")
    print(f"flagship strategy : {manifest['flagship_strategy']}")
    print(f"pack              : {manifest['pack_id']}  (content_hash {manifest['content_hash'][:12]}…)")
    print(f"manifest          : {out_path}")
    print("mode labels are HONEST — a Backtest over BANKED odds, never 'Live'; no real-money orders.\n")
    for run in manifest["runs"]:
        print(f"  [{run['kind']:<8}] {run['mode_label']:<9} run_id={run['run_id']}")
        clv = run.get("avg_clv_bps")
        if clv is not None:
            print(f"             avg_clv={clv} bps  sample={run.get('sample_size')}  ({run.get('clv_confidence')})")
        url = run["verify_url"] if base_url else f"{base_url}{run['verify_url']}"
        print(f"             verify → {url}")
    print()


def main() -> None:
    """CLI entry point: build/locate the pack, run the demo, print the verify URLs, optionally serve.

    Flags:
        ``--pack DIR``     Point at a real captured ReplayPack (default: the shipped illustrative pack).
        ``--fixture-id N`` Fixture within the pack to replay.
        ``--out PATH``     Where to write ``demo_manifest.json``.
        ``--rebuild-pack`` Regenerate the shipped illustrative pack before running.
        ``--serve``        Boot the read API on ``--port`` so the printed verify URLs resolve.
        ``--port N``       Serve port (default 8080).
    """
    parser = argparse.ArgumentParser(description="Veridex Phase-2D judge demo runner (offline).")
    parser.add_argument("--pack", type=Path, default=DEFAULT_PACK_DIR, help="ReplayPack directory.")
    parser.add_argument("--fixture-id", type=int, default=DEFAULT_FIXTURE_ID, help="Fixture to replay.")
    parser.add_argument("--out", type=Path, default=DEFAULT_MANIFEST_PATH, help="Manifest output path.")
    parser.add_argument("--rebuild-pack", action="store_true", help="Rebuild the shipped demo pack first.")
    parser.add_argument("--serve", action="store_true", help="Serve the read API so verify URLs resolve.")
    parser.add_argument("--port", type=int, default=8080, help="Port for --serve (default 8080).")
    args = parser.parse_args()

    if args.rebuild_pack or (args.pack == DEFAULT_PACK_DIR and not (args.pack / "pack.json").exists()):
        build_reference_pack(DEFAULT_PACK_DIR)

    base_url = f"http://localhost:{args.port}" if args.serve else ""
    store = InMemoryStore()
    manifest = asyncio.run(run_demo(args.pack, args.fixture_id, out_path=args.out, store=store, base_url=base_url))
    _print_summary(manifest, args.out, base_url=base_url)

    if args.serve:
        # Boot the read API against the SAME store the runs were persisted to, so every printed
        # /runs/{id}/verify URL recomputes a real sealed run (Ctrl-C to stop). fastapi/uvicorn are
        # the optional [api] extra — imported lazily so the offline demo needs neither.
        import uvicorn  # noqa: PLC0415

        from veridex.api.router import create_app  # noqa: PLC0415

        print(f"Serving verify API on {base_url} — open the URLs above (Ctrl-C to stop).\n")
        uvicorn.run(create_app(store=store), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
