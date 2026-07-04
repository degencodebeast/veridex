"""C-3 operator shell: Polymarket 1X2 ``prices-history`` backfill (REQ-003, CON-001-gated).

Reads C-1's content-hashed ``cp1-coverage.json`` as the ONLY gate input and REFUSES to run
(:class:`CoverageGateError`) if it is absent or has zero headline-eligible fixtures — the C/P1
feasibility gate must have PASSED first (CON-001 fail-closed). For each headline-eligible fixture
it resolves the three 1X2 tokens (home/away/draw), backfills each side's Polymarket price path via
the M0 :func:`veridex.venues.price_history.fetch_price_history` contract, and writes a JSONL frames
file + a sibling :class:`veridex.venues.price_history.VenuePriceHistoryPack` (``artifact_content_hash``,
NO ``evidence_hash``) under ``scripts/txline_live/cp1/frames/<fixture>/<side>.jsonl``.

Fixture identity (event slug + home/away team) is NOT carried by the coverage artifact — it lives
in the same operator ``fixtures.json`` the C-1 probe used — so this shell reads BOTH: the coverage
artifact decides WHICH fixtures (the gate), ``fixtures.json`` supplies HOW to resolve their tokens.

CON-010: every ``veridex``/network import is LAZY inside a function, so
``import scripts.txline_live.cp1_backfill`` stays network-free and credential-free; only
:class:`CoverageGateError` and :func:`load_headline_eligible_fixture_ids` (pure gate logic) are
unit-tested. The live fetch runs ONLY when the operator invokes this shell.

Run (after C-1's coverage gate passed):
    .venv/bin/python scripts/txline_live/cp1_backfill.py
    # or point at alternate inputs / output dir:
    .venv/bin/python scripts/txline_live/cp1_backfill.py \
        --coverage scripts/txline_live/cp1/cp1-coverage.json \
        --fixtures-config scripts/txline_live/cp1/fixtures.json \
        --out-dir scripts/txline_live/cp1/frames
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

DEFAULT_CP1_DIR = Path(__file__).parent / "cp1"
DEFAULT_COVERAGE = DEFAULT_CP1_DIR / "cp1-coverage.json"
DEFAULT_FIXTURES_CONFIG = DEFAULT_CP1_DIR / "fixtures.json"
DEFAULT_OUT_DIR = DEFAULT_CP1_DIR / "frames"

# The three 1X2 bet sides and their structured market refs (verified by the C-1 resolver path).
SIDES: tuple[tuple[str, str], ...] = (
    ("home", "1X2|home|full"),
    ("away", "1X2|away|full"),
    ("draw", "1X2|draw|full"),
)


class CoverageGateError(RuntimeError):
    """Raised when the C-1 coverage artifact is absent or gates the backfill closed (CON-001).

    C-3 is a downstream of C-1's hard feasibility gate: with no viable coverage there is nothing
    to price, so refusing (loudly) is the honest behavior — never a silent empty backfill that
    would read like "priced, no data".
    """


def load_headline_eligible_fixture_ids(coverage_path: Path) -> list[int]:
    """Load the headline-eligible fixture ids from C-1's coverage artifact, or fail closed.

    The coverage artifact is the ONLY gate input. Its absence, or a zero-length
    ``headline_eligible_fixture_ids``, both raise :class:`CoverageGateError` (CON-001): C/P1 fails
    closed rather than backfill an empty universe. A non-empty list is returned verbatim (order
    preserved) as the fixtures to backfill.
    """
    if not coverage_path.exists():
        raise CoverageGateError(
            f"coverage artifact {coverage_path} is absent — run the C-1 probe and pass its "
            f"CON-001 gate before backfilling (C/P1 fails closed with no viable coverage)"
        )
    artifact = json.loads(coverage_path.read_text())
    ids = artifact.get("headline_eligible_fixture_ids") or []
    if not ids:
        raise CoverageGateError(
            f"coverage artifact {coverage_path} has zero headline-eligible fixtures — C/P1 is "
            f"NOT VIABLE, so there is nothing to backfill (CON-001 fail-closed)"
        )
    return [int(fixture_id) for fixture_id in ids]


def _load_fixture_identities(fixtures_config: Path) -> dict[int, dict[str, Any]]:
    """Index the operator ``fixtures.json`` (slug/teams/kickoff) by fixture id; fail closed if absent."""
    if not fixtures_config.exists():
        raise SystemExit(
            f"missing fixtures config {fixtures_config} — it carries the event slug + team identity "
            f"needed to resolve tokens (the coverage artifact does not); write it first"
        )
    fixtures = json.loads(fixtures_config.read_text())
    if not isinstance(fixtures, list) or not fixtures:
        raise SystemExit(f"fixtures config {fixtures_config} must be a non-empty JSON list")
    return {int(f["fixture_id"]): f for f in fixtures}


async def _backfill_one(
    fixture: dict[str, Any],
    *,
    out_dir: Path,
    client: Any,
    gamma_client: Any,
    fidelity_s: int,
) -> dict[str, Any]:
    """Resolve + backfill all three 1X2 sides for one fixture; return a per-side summary.

    Each side is independent: a failed/empty side is recorded honestly (``ok=False`` /
    ``frames=0``) and never fabricated, so a partial fixture reads as a partial fixture.
    """
    from veridex.venues.polymarket_price_history import build_price_history_pack
    from veridex.venues.polymarket_resolver import (
        MarketUnavailable,
        resolve_market,
        side_to_token,
    )

    fixture_id = int(fixture["fixture_id"])
    slug = str(fixture["event_slug"])
    home_team = fixture.get("home_team")
    away_team = fixture.get("away_team")
    fixture_dir = out_dir / str(fixture_id)

    side_summaries: list[dict[str, Any]] = []
    for side, market_ref in SIDES:
        try:
            resolved = await resolve_market(
                market_ref, slug, home_team=home_team, away_team=away_team, client=gamma_client
            )
            side_to_token(resolved, side)  # fail closed early if the side can't map to a token
            frames, pack = await build_price_history_pack(
                resolved,
                side,
                fixture_id=fixture_id,
                market_ref=market_ref,
                fidelity_s=fidelity_s,
                client=client,
                pack_dir=fixture_dir,
                frames_file=f"{side}.jsonl",
            )
            (fixture_dir / f"{side}.pack.json").write_text(pack.model_dump_json(indent=1))
            side_summaries.append(
                {
                    "side": side,
                    "ok": True,
                    "frames": len(frames),
                    "artifact_content_hash": pack.artifact_content_hash,
                    "frames_file": str((fixture_dir / f"{side}.jsonl").relative_to(ROOT)),
                }
            )
            print(
                f"fixture {fixture_id} side {side}: {len(frames)} frames -> "
                f"artifact_content_hash={pack.artifact_content_hash[:12]}..."
            )
        except MarketUnavailable as exc:
            side_summaries.append({"side": side, "ok": False, "frames": 0, "error": str(exc)})
            print(f"fixture {fixture_id} side {side}: UNRESOLVED ({exc}) — recorded, not fabricated")
        except Exception as exc:  # noqa: BLE001 — one bad side must not abort the fixture
            side_summaries.append(
                {"side": side, "ok": False, "frames": 0, "error": f"{type(exc).__name__}: {exc}"}
            )
            print(f"fixture {fixture_id} side {side}: FAILED {type(exc).__name__}: {exc}")

    return {"fixture_id": fixture_id, "sides": side_summaries}


async def run(
    coverage_path: Path,
    fixtures_config: Path,
    out_dir: Path,
) -> dict[str, Any]:
    """Backfill every headline-eligible fixture from the C-1 coverage gate; write a run manifest."""
    from veridex.venues.polymarket_price_history import FIDELITY_S, PolymarketPricesHistoryClient

    headline_ids = load_headline_eligible_fixture_ids(coverage_path)  # CON-001 gate (fail closed)
    identities = _load_fixture_identities(fixtures_config)
    coverage_hash = json.loads(coverage_path.read_text()).get("artifact_content_hash")
    print(
        f"backfilling {len(headline_ids)} headline-eligible fixture(s) "
        f"(coverage_artifact_hash={str(coverage_hash)[:12]}..., fidelity_s={FIDELITY_S})"
    )

    client = PolymarketPricesHistoryClient()
    out_dir.mkdir(parents=True, exist_ok=True)

    fixture_summaries: list[dict[str, Any]] = []
    for fixture_id in headline_ids:
        fixture = identities.get(fixture_id)
        if fixture is None:
            print(f"fixture {fixture_id}: headline-eligible but MISSING from fixtures.json — skipped")
            fixture_summaries.append(
                {"fixture_id": fixture_id, "sides": [], "error": "identity missing from fixtures.json"}
            )
            continue
        summary = await _backfill_one(
            fixture, out_dir=out_dir, client=client, gamma_client=None, fidelity_s=FIDELITY_S
        )
        fixture_summaries.append(summary)

    manifest: dict[str, Any] = {
        "tool": "cp1_backfill/1",
        "coverage_artifact_hash": coverage_hash,
        "fidelity_s": FIDELITY_S,
        "fixtures": fixture_summaries,
    }
    manifest_path = out_dir / "backfill-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=1))
    _print_manifest_summary(manifest)
    print(f"wrote {manifest_path}")
    return manifest


def _print_manifest_summary(manifest: dict[str, Any]) -> None:
    """Print per-fixture frame totals + how many sides landed (honest partials surface here)."""
    total_frames = 0
    total_ok = 0
    for fixture in manifest["fixtures"]:
        sides = fixture.get("sides", [])
        frames = sum(int(s.get("frames", 0)) for s in sides)
        ok = sum(1 for s in sides if s.get("ok"))
        total_frames += frames
        total_ok += ok
        print(f"fixture {fixture['fixture_id']}: {ok}/{len(sides)} sides, {frames} frames")
    print(f"TOTAL: {total_ok} side(s) backfilled, {total_frames} frames across "
          f"{len(manifest['fixtures'])} fixture(s)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="C-3 Polymarket 1X2 prices-history backfill (REQ-003, CON-001-gated)."
    )
    parser.add_argument("--coverage", type=str, default=None, help=f"C-1 coverage artifact (default {DEFAULT_COVERAGE})")
    parser.add_argument("--fixtures-config", type=str, default=None, help=f"per-fixture identity JSON (default {DEFAULT_FIXTURES_CONFIG})")
    parser.add_argument("--out-dir", type=str, default=None, help=f"frames/Pack output dir (default {DEFAULT_OUT_DIR})")
    args = parser.parse_args()

    coverage_path = Path(args.coverage) if args.coverage else DEFAULT_COVERAGE
    fixtures_config = Path(args.fixtures_config) if args.fixtures_config else DEFAULT_FIXTURES_CONFIG
    out_dir = Path(args.out_dir) if args.out_dir else DEFAULT_OUT_DIR

    try:
        asyncio.run(run(coverage_path, fixtures_config, out_dir))
    except CoverageGateError as exc:
        raise SystemExit(f"C/P1 backfill refused: {exc}") from exc


if __name__ == "__main__":
    main()
