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

Honesty doctrine at the judge surface (REQ-2D-304): mode labels never overclaim, and a metric never
travels without its provenance. The offline demo is a **Backtest** over SYNTHETIC illustrative odds
(banked in the shipped pack) — NOT a live run, NOT real money, NOT a fabricated result. The pack
self-declares ``synthetic: true``, and that provenance rides INLINE with every CLV number on all
three machine/console surfaces (manifest run entry, printed summary, pack ``capture``) so the CLV can
never read as a real strategy edge. ``--pack`` points at a real captured pack for a real-odds
artifact (REQ-2D-503). No live network, no real orders, ``anchor_fn`` off — ZERO network by default.

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
from veridex.ingest.capture_chain import is_genuine_pack, synthetic_authority
from veridex.ingest.recorder import SessionMeta, envelope_line
from veridex.ingest.replay_pack import load_pack_marketstates, pack_from_session, verify_content_hash
from veridex.runtime.window import RunWindow
from veridex.store import InMemoryStore, Store
from veridex.strategies.momentum import SHARP_MOMENTUM_V2_LABEL, sharp_momentum_agent
from veridex_agent.run import standalone_run

# --- demo defaults ----------------------------------------------------------
#: The demo fixture id (shared with the Phase-1 demo fixture for continuity).
DEFAULT_FIXTURE_ID = 17588404
#: The banked SYNTHETIC ReplayPack that SHIPS with the repo — the offline demo default + CI fallback
#: (VISIBLY labeled ``synthetic-illustrative``; can NEVER read ``genuine`` and stays a single fixture).
DEFAULT_PACK_DIR = Path(__file__).parent / "fixtures" / "demo_pack"
#: The banked REAL World Cup demo pack (I-10): a curated slice of GENUINE TxLINE odds (real FIFA WC
#: 2026 quarter-final fixtures, backfilled from ``/odds/updates``). This is the judge replay
#: experience and the Sharp-Momentum (II-10) gate input. Reads ``genuine-txline`` through R-0a's
#: honesty machinery; self-declares the ``backfilled-price-history`` evidence rung.
DEMO_PACK_REAL_DIR = Path(__file__).parent / "fixtures" / "demo_pack_real"
#: PINNED ``content_hash`` of the banked real demo pack — the tamper-evidence contract binding this
#: script to exactly those banked bytes. Regenerate via ``scripts/fixtures/build_demo_pack_real.py``
#: (deterministic); a mismatch means the pack was tampered OR the pin was edited without rebuilding.
DEMO_PACK_REAL_CONTENT_HASH = "f16c3853a80fc6f0b4e5fe21d8f1c0dcfd4c66d732e5a915193988604f9ddb0b"
#: The Sharp-Momentum harness (II-10) needs >=2 distinct fixtures; a single-fixture pack would
#: silently disable its gate, so the resolver/guard REFUSES anything with fewer.
SHARP_MOMENTUM_MIN_FIXTURES = 2
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
#: Data-provenance marker the shipped pack self-declares (its odds are illustrative, not captured).
SYNTHETIC_PROVENANCE = "synthetic-illustrative"
#: Fail-safe label for a pack that declares NEITHER synthetic NOR a positive real marker — we must
#: never assert it was really captured, so it reads "unknown" and still carries a cautious caveat.
UNKNOWN_PROVENANCE = "unknown-provenance"
#: The three coherent CLV caveats — a metric never travels without its provenance, and none of the
#: three can silently read as a real, live-executed edge.
_SYNTHETIC_CLV_CAVEAT = (
    "CLV over SYNTHETIC illustrative odds — demonstrates the sealed-CLV metric pipeline, NOT a real strategy edge."
)
_REAL_CLV_CAVEAT = "Backtested CLV over captured odds — a paper/backtest signal, NOT a live-executed real-money edge."
_UNKNOWN_CLV_CAVEAT = "Provenance UNVERIFIED — a paper/backtest signal over unverified odds, NOT a claimed real edge."

# --- synthetic illustrative odds tape ---------------------------------------
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

    The pack SELF-DECLARES its synthetic authority in its ``capture`` block (``synthetic: true`` +
    ``provenance``) so a downstream reader can never separate the numbers from the fact that the odds
    are illustrative. ``endpoints`` is empty — a synthetic tape was never streamed from a real feed.
    MAJOR-1: those authority markers are folded INTO the v2 ``content_hash``, so the pack can never be
    relabeled genuine without breaking its own hash (and it can never read genuine).

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
            SessionMeta(started_ts=99, endpoints=[], tool_version="veridex-demo-phase2d-synthetic").model_dump_json()
        )
        if dst.exists():
            shutil.rmtree(dst)
        # MAJOR-1: the synthetic authority (provenance=synthetic-illustrative, synthetic=True) is
        # folded INTO the v2 content_hash by pack_from_session, so the synthetic label is
        # tamper-evident — a reader can never separate the numbers from the fact they're illustrative,
        # and the pack can NEVER be relabeled genuine without breaking its own hash.
        pack_from_session(session_dir, dst, authority=synthetic_authority())
    return dst


def _pack_provenance(pack_dir: Path) -> tuple[str, bool, str]:
    """Read a pack's SELF-DECLARED data provenance from its ``capture`` block (coherent + fail-safe).

    Provenance TRAVELS from the pack into the manifest/console rather than being asserted by the
    demo. Three coherent cases, none of which can silently read as a real, live-executed edge:

    * **synthetic** — the ``synthetic`` bool is set OR the ``provenance`` string contains "synthetic".
      Deriving the flag from EITHER signal means the bool and the string can never disagree in the
      unsafe (caveat-dropped) direction: a synthetic pack ALWAYS keeps its caveat. (The shipped pack.)
    * **positively-stamped real** — a non-synthetic ``provenance`` string the producer stamped (e.g.
      a real captured fixture): an honest real-odds label, still a paper/backtest — never a live edge.
    * **unmarked/ambiguous** — an empty/missing capture: we CANNOT verify it was really captured, so
      it reads ``unknown-provenance`` with a cautious caveat rather than FALSELY asserting "captured"
      (a genuinely real pack must be POSITIVELY stamped by its producer — unmarked never means real).

    Returns:
        ``(data_provenance, is_synthetic, clv_caveat)`` — the label, the synthetic flag, and the
        inline caveat that must ride with every CLV number derived from this pack.
    """
    capture = json.loads((pack_dir / "pack.json").read_text()).get("capture", {})
    provenance_str = str(capture.get("provenance", "")).strip()
    # Coherent by construction: bool OR provenance-string — the two cannot disagree caveat-off.
    is_synthetic = capture.get("synthetic") is True or "synthetic" in provenance_str.lower()
    if is_synthetic:
        return provenance_str or SYNTHETIC_PROVENANCE, True, _SYNTHETIC_CLV_CAVEAT
    if provenance_str:
        # Positively stamped non-synthetic provenance — honest real-odds label, never a live edge.
        return provenance_str, False, _REAL_CLV_CAVEAT
    # Fail-safe: unmarked/empty capture never asserts "captured" — it reads unknown + a cautious caveat.
    return UNKNOWN_PROVENANCE, False, _UNKNOWN_CLV_CAVEAT


def _odds_desc(data_provenance: str, is_synthetic: bool) -> str:
    """The honest console/notes phrase for a pack's odds — three-way, keyed off ``data_provenance``.

    Matches the STRUCTURED provenance so the prose never overclaims, mirroring the three
    :func:`_pack_provenance` cases:

    * **synthetic** → ``"SYNTHETIC illustrative odds"``.
    * **positively-stamped real** → ``"captured odds"`` (an honest real-odds label).
    * **unmarked/unknown** → ``"unverified-provenance odds"`` — NEVER "captured", because an
      unmarked pack cannot be asserted to have been really captured.

    Keying off ``data_provenance`` (not the ``is_synthetic`` bool alone) is what keeps the rare
    unknown-provenance case OUT of the "captured" branch — the prose then matches the structured
    ``data_provenance`` a machine reader sees.
    """
    if is_synthetic:
        return "SYNTHETIC illustrative odds"
    if data_provenance == UNKNOWN_PROVENANCE:
        return "unverified-provenance odds"
    return "captured odds"


def _pack_content_hash(pack_dir: Path) -> str:
    """Read the pack's stored ``content_hash`` (bound into the demo's deterministic run ids)."""
    return str(json.loads((pack_dir / "pack.json").read_text())["content_hash"])


def require_min_fixtures(pack_dir: Path, minimum: int = SHARP_MOMENTUM_MIN_FIXTURES) -> list[int]:
    """Return the pack's distinct fixture ids, REFUSING a pack with fewer than ``minimum``.

    The Sharp-Momentum harness (II-10) consumes >=2 distinct fixtures; a single-fixture pack would
    silently disable its gate, so we fail loud here rather than let it through.

    Args:
        pack_dir: The ReplayPack directory (must contain ``pack.json``).
        minimum: The minimum distinct-fixture count required (default :data:`SHARP_MOMENTUM_MIN_FIXTURES`).

    Returns:
        The sorted distinct fixture ids in the pack manifest.

    Raises:
        ValueError: If the pack exposes fewer than ``minimum`` distinct fixtures.
    """
    manifest = json.loads((pack_dir / "pack.json").read_text())
    fixture_ids = sorted({int(f["fixture_id"]) for f in manifest["fixtures"]})
    if len(fixture_ids) < minimum:
        raise ValueError(
            f"pack at {pack_dir} exposes {len(fixture_ids)} distinct fixture(s) "
            f"({fixture_ids}); the Sharp-Momentum harness needs >= {minimum}"
        )
    return fixture_ids


def resolve_real_demo_pack() -> Path:
    """Return :data:`DEMO_PACK_REAL_DIR` only when it is verifiably the pinned GENUINE demo pack.

    Fail-closed on EVERY guard, so the judge/harness never runs the real-odds surface over a
    tampered, mislabeled, or under-sized pack:

    * ``pack.json`` exists;
    * ``content_hash`` recomputes (:func:`~veridex.ingest.replay_pack.verify_content_hash`);
    * the stored hash equals the pinned :data:`DEMO_PACK_REAL_CONTENT_HASH` (tamper-evidence);
    * the pack reads ``genuine-txline`` (:func:`~veridex.ingest.capture_chain.is_genuine_pack`);
    * it exposes >= :data:`SHARP_MOMENTUM_MIN_FIXTURES` distinct fixtures.

    Returns:
        :data:`DEMO_PACK_REAL_DIR`.

    Raises:
        FileNotFoundError: If the banked pack is absent.
        ValueError: If any hash/provenance/fixture-count guard fails.
    """
    pack_dir = DEMO_PACK_REAL_DIR
    if not (pack_dir / "pack.json").exists():
        raise FileNotFoundError(f"real demo pack not banked at {pack_dir}")
    if not verify_content_hash(pack_dir):
        raise ValueError(f"real demo pack at {pack_dir} failed content_hash verification (tampered or corrupt)")
    stored = _pack_content_hash(pack_dir)
    if stored != DEMO_PACK_REAL_CONTENT_HASH:
        raise ValueError(
            f"real demo pack content_hash {stored} != pinned {DEMO_PACK_REAL_CONTENT_HASH} "
            f"(pack tampered, or the pin was edited without rebuilding)"
        )
    if not is_genuine_pack(pack_dir):
        raise ValueError(f"real demo pack at {pack_dir} does not read as genuine TxLINE — refusing to label it real")
    require_min_fixtures(pack_dir)
    return pack_dir


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
    # Provenance (label + synthetic flag + inline CLV caveat) travels FROM the pack, coherent and
    # fail-safe — so every avg_clv number carries its caveat and can never read as a real edge.
    data_provenance, is_synthetic, clv_caveat = _pack_provenance(pack_dir)
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
            # sample_size = TOTAL decisions evaluated (WAIT-inclusive); scored_count = the SCORED-pick
            # count clv_confidence is actually keyed off (== clv_distribution.count). Both travel so a
            # reader never mistakes sample_size for the confidence basis (an overclaim otherwise).
            "sample_size": bt_report.sample_size,
            "scored_count": bt_report.clv_distribution.count,
            "valid_count": bt_report.valid_count,
            "clv_confidence": bt_report.clv_confidence,
            "avg_clv_bps": bt_report.avg_clv,
            # Provenance travels IN the same dict as avg_clv_bps — a parser reading this run entry
            # gets the caveat with the number (never a bare metric that could read as a real edge).
            "data_provenance": data_provenance,
            "synthetic_data": is_synthetic,
            "clv_caveat": clv_caveat,
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
            "data_provenance": data_provenance,
            "synthetic_data": is_synthetic,
            "verified": paper.verified,
            "anchor_status": paper.anchor_status,  # "not_anchored" (offline)
        }
    )

    odds_desc = _odds_desc(data_provenance, is_synthetic)
    manifest: dict[str, Any] = {
        "generated_ts": int(time.time()),
        "pack_id": pack_dir.name,
        "content_hash": content_hash,
        "fixture_id": fixture_id,
        "flagship_strategy": FLAGSHIP_STRATEGY_LABEL,
        "offline": True,
        # Structured provenance at the top level AND on every run — never prose-only.
        "data_provenance": data_provenance,
        "synthetic_data": is_synthetic,
        "notes": (
            "Proof-only demo: no live network, no real-money orders (anchor off). Mode labels are "
            f"honest — a Backtest over {odds_desc}, never 'Live'. "
            + (
                "The default pack ships SYNTHETIC illustrative odds: the runs over them are genuine "
                "sealed proofs and the CLV demonstrates the sealed pipeline, NOT a real strategy edge. "
                if is_synthetic
                else "This pack's odds provenance is UNVERIFIED; the CLV is a paper/backtest signal over "
                "unverified odds, not a live-executed edge. "
                if data_provenance == UNKNOWN_PROVENANCE
                else "This pack carries captured odds; the CLV is a paper/backtest signal, not a live-executed edge. "
            )
            + "Point --pack at a real captured ReplayPack for a real-odds artifact."
        ),
        "runs": runs,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2, default=str) + "\n")
    return manifest


def _print_summary(manifest: dict[str, Any], out_path: Path, *, base_url: str) -> None:
    """Print a judge-facing summary: the flagship, the honest labels, and the verify URLs."""
    is_synthetic = bool(manifest.get("synthetic_data", False))
    odds_desc = _odds_desc(str(manifest.get("data_provenance", UNKNOWN_PROVENANCE)), is_synthetic)
    print("=== Veridex Phase-2D demo ===")
    print(f"flagship strategy : {manifest['flagship_strategy']}")
    print(f"pack              : {manifest['pack_id']}  (content_hash {manifest['content_hash'][:12]}…)")
    print(f"data provenance   : {manifest.get('data_provenance', 'unknown')}")
    print(f"manifest          : {out_path}")
    print(f"mode labels are HONEST — a Backtest over {odds_desc}, never 'Live'; no real-money orders.\n")
    for run in manifest["runs"]:
        print(f"  [{run['kind']:<8}] {run['mode_label']:<9} run_id={run['run_id']}")
        clv = run.get("avg_clv_bps")
        if clv is not None:
            # Render the SCORED-pick count next to the confidence tier (clv_confidence is keyed off
            # it), with total decisions labelled separately — so "sample=N (low)" can't read as an
            # overclaim (a run can be law-valid on many WAITs yet score few picks -> honest "low").
            scored = run.get("scored_count")
            print(
                f"             avg_clv={clv} bps  scored={scored} picks of "
                f"{run.get('sample_size')} decisions  ({run.get('clv_confidence')})"
            )
            # The provenance caveat prints IN the CLV block so the number never stands alone.
            caveat = run.get("clv_caveat")
            if caveat:
                print(f"             ↳ {caveat}")
        url = run["verify_url"] if base_url else f"{base_url}{run['verify_url']}"
        print(f"             verify → {url}")
    print()


def main() -> None:
    """CLI entry point: build/locate the pack, run the demo, print the verify URLs, optionally serve.

    Flags:
        ``--pack DIR``     Point at a real captured ReplayPack (default: the shipped illustrative pack).
        ``--real``         Use the banked GENUINE World Cup demo pack (fail-closed, pinned hash).
        ``--fixture-id N`` Fixture within the pack to replay.
        ``--out PATH``     Where to write ``demo_manifest.json``.
        ``--rebuild-pack`` Regenerate the shipped illustrative pack before running.
        ``--serve``        Boot the read API on ``--port`` so the printed verify URLs resolve.
        ``--port N``       Serve port (default 8080).
    """
    parser = argparse.ArgumentParser(description="Veridex Phase-2D judge demo runner (offline).")
    parser.add_argument("--pack", type=Path, default=DEFAULT_PACK_DIR, help="ReplayPack directory.")
    parser.add_argument(
        "--real",
        action="store_true",
        help="Replay the banked GENUINE World Cup demo pack (pinned + fail-closed verified).",
    )
    parser.add_argument("--fixture-id", type=int, default=None, help="Fixture to replay (default: pack-appropriate).")
    parser.add_argument("--out", type=Path, default=DEFAULT_MANIFEST_PATH, help="Manifest output path.")
    parser.add_argument("--rebuild-pack", action="store_true", help="Rebuild the shipped demo pack first.")
    parser.add_argument("--serve", action="store_true", help="Serve the read API so verify URLs resolve.")
    parser.add_argument("--port", type=int, default=8080, help="Port for --serve (default 8080).")
    args = parser.parse_args()

    # --real replays the banked GENUINE WC pack, resolved fail-closed (verified hash == pin, genuine
    # provenance, >= 2 fixtures). Its default fixture is the first genuine WC fixture, not the
    # synthetic demo fixture. An explicit --pack still wins (operator override).
    if args.real and args.pack == DEFAULT_PACK_DIR:
        args.pack = resolve_real_demo_pack()
    if args.fixture_id is None:
        args.fixture_id = require_min_fixtures(args.pack)[0] if args.real else DEFAULT_FIXTURE_ID

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
