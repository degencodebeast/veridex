"""C-1 operator probe shell: Polymarket 1X2 data-availability feasibility gate (CON-001).

For each Run-001 fixture this resolves the three 1X2 tokens (home/away/draw), fetches Polymarket
``prices-history`` per side, counts pre-kickoff quotes + freshness-bucket coverage, runs the pure
gate (:func:`veridex.venues.polymarket_coverage.evaluate_venue_coverage`), and writes a
content-hashed ``cp1-coverage.json`` under ``scripts/txline_live/cp1/``.

FAIL-CLOSED HONESTY (CON-001): the artifact is written even when ZERO fixtures are
headline-eligible — that is a legitimate (dead) result, and the coverage report IS the
deliverable. The shell then prints a loud ``C/P1 NOT VIABLE`` message. Partial-coverage
fixtures/sides are recorded as labeled diagnostics, NEVER promoted to the headline universe.
Thresholds are pinned BEFORE results and are never tuned after seeing estimated-edge outcomes.

NOT exercised by tests' network path — no network/creds in the offline suite. ALL ``veridex`` and
network imports live INSIDE functions (CON-010), so ``import scripts.txline_live.cp1_probe`` stays
network-free and credential-free; only ``build_coverage_artifact`` / ``render_summary`` (pure
artifact shaping) are unit-tested.

Fixture identity (event slug + home/away team + kickoff_ts) is NOT carried by the local Run-001
ReplayPacks — it comes from the TxLINE fixture snapshot — so the operator supplies it once via
``scripts/txline_live/cp1/fixtures.json`` (a list of
``{"fixture_id", "event_slug", "home_team", "away_team", "kickoff_ts"}``); the shell fails closed
if that config is absent.

Run:
    # 1) write scripts/txline_live/cp1/fixtures.json for the Run-001 fixtures (slug/teams/kickoff)
    # 2) probe Polymarket (network, read-only):
    .venv/bin/python scripts/txline_live/cp1_probe.py
    # or point at an alternate config / output dir:
    .venv/bin/python scripts/txline_live/cp1_probe.py --fixtures-config path/to/fixtures.json --out-dir scripts/txline_live/cp1
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

if TYPE_CHECKING:  # import-safe: never executed at runtime (CON-010)
    from veridex.venues.polymarket_coverage import VenueCoverage

DEFAULT_OUT_DIR = Path(__file__).parent / "cp1"
DEFAULT_FIXTURES_CONFIG = DEFAULT_OUT_DIR / "fixtures.json"
CLOB_BASE_URL = "https://clob.polymarket.com"

# Freshness bands (seconds) reported per side, exclusive so their counts partition the
# within-15m pre-kickoff quotes (matches the spec §4 "<=2m"/"<=5m"/"<=15m" buckets).
FRESHNESS_BANDS: tuple[tuple[str, int], ...] = (
    ("<=2m", 120),
    ("<=5m", 300),
    ("<=15m", 900),
)


def build_coverage_artifact(coverages: list[VenueCoverage], *, tool: str = "cp1_probe/1") -> dict[str, Any]:
    """Shape the probed per-fixture coverages into the content-hashed coverage artifact.

    Refuses an EMPTY input (nothing probed is a misconfiguration, not a result). Zero
    headline-eligible fixtures IS a legitimate result — the artifact still writes with
    ``viable=False`` (CON-001 fail-closed). Each fixture carries its own
    :func:`coverage_content_hash`, and the whole artifact is hashed for pinning.
    """
    from veridex.venues.polymarket_coverage import coverage_content_hash

    if not coverages:
        raise ValueError(
            "refuse to write an empty coverage artifact — no fixtures were probed "
            "(CON-001 fail-closed applies to zero-ELIGIBLE, not zero-PROBED)"
        )

    fixtures = [
        {"coverage": cov.model_dump(), "coverage_content_hash": coverage_content_hash(cov)}
        for cov in coverages
    ]
    headline_ids = [cov.fixture_id for cov in coverages if cov.headline_eligible]
    artifact: dict[str, Any] = {
        "tool": tool,
        "min_pre_kickoff": 5,
        "fixtures": fixtures,
        "headline_eligible_fixture_ids": headline_ids,
        "headline_eligible_count": len(headline_ids),
        "viable": bool(headline_ids),
    }
    canonical = json.dumps(artifact, sort_keys=True, separators=(",", ":"))
    artifact["artifact_content_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return artifact


def render_summary(artifact: dict[str, Any]) -> list[str]:
    """Human-readable per-fixture + overall summary lines (loud NOT VIABLE when zero-eligible)."""
    lines: list[str] = []
    for entry in artifact["fixtures"]:
        cov = entry["coverage"]
        tag = "HEADLINE-ELIGIBLE" if cov["headline_eligible"] else f"diagnostic-only ({cov['reason']})"
        lines.append(f"fixture {cov['fixture_id']}: {tag}")
    count = artifact["headline_eligible_count"]
    total = len(artifact["fixtures"])
    if count == 0:
        lines.append(f"C/P1 NOT VIABLE on current Polymarket coverage: 0/{total} fixtures headline-eligible")
    else:
        lines.append(f"C/P1 viable: {count}/{total} fixtures headline-eligible -> {artifact['headline_eligible_fixture_ids']}")
    lines.append(f"artifact_content_hash={artifact['artifact_content_hash'][:12]}...")
    return lines


def _bucket_pre_kickoff(quote_tss: list[int], kickoff_ts: int) -> tuple[int, dict[str, int], int | None, int | None]:
    """Count pre-kickoff quotes (ts <= kickoff) + partition them into freshness bands by age."""
    pre = sorted(ts for ts in quote_tss if ts <= kickoff_ts)
    buckets: dict[str, int] = {name: 0 for name, _ in FRESHNESS_BANDS}
    prev = 0
    for name, upper in FRESHNESS_BANDS:
        for ts in pre:
            age = kickoff_ts - ts
            if prev < age <= upper:
                buckets[name] += 1
        prev = upper
    first = pre[0] if pre else None
    last = pre[-1] if pre else None
    return len(pre), buckets, first, last


async def _probe_one(fixture: dict[str, Any], *, client: Any, gamma_client: Any) -> VenueCoverage:
    """Resolve the three 1X2 tokens for a fixture and count Polymarket pre-kickoff quotes per side."""
    from veridex.venues.polymarket_coverage import SideInput, evaluate_venue_coverage
    from veridex.venues.polymarket_resolver import MarketUnavailable, resolve_market, side_to_token

    fixture_id = int(fixture["fixture_id"])
    slug = str(fixture["event_slug"])
    kickoff_ts = int(fixture["kickoff_ts"])
    home_team = fixture.get("home_team")
    away_team = fixture.get("away_team")

    side_inputs: dict[str, SideInput] = {}
    for side, market_ref in (
        ("home", "1X2|home|full"),
        ("away", "1X2|away|full"),
        ("draw", "1X2|draw|full"),
    ):
        try:
            resolved = await resolve_market(
                market_ref, slug, home_team=home_team, away_team=away_team, client=gamma_client
            )
            token_id = side_to_token(resolved, side)
            points = await client.get_prices_history(token_id)
            quote_tss = [int(p["t"]) for p in points]
            pre_count, buckets, first, last = _bucket_pre_kickoff(quote_tss, kickoff_ts)
            side_inputs[side] = SideInput(
                pre_kickoff_quote_count=pre_count,
                quote_count=len(quote_tss),
                freshness_bucket_counts=buckets,
                first_quote_ts=first,
                last_quote_ts=last,
                token_resolved=True,
            )
        except MarketUnavailable as exc:
            print(f"fixture {fixture_id} side {side}: token unresolved ({exc}) — recorded uncovered")
            side_inputs[side] = SideInput(pre_kickoff_quote_count=0, token_resolved=False)

    return evaluate_venue_coverage(fixture_id, side_inputs, kickoff_ts=kickoff_ts, min_pre_kickoff=5)


class _DefaultPricesHistoryClient:
    """Live Polymarket CLOB ``/prices-history`` client (httpx imported lazily; offline-safe import)."""

    async def get_prices_history(self, token_id: str) -> list[dict[str, Any]]:
        import httpx

        async with httpx.AsyncClient(base_url=CLOB_BASE_URL, timeout=15.0) as http:
            response = await http.get("/prices-history", params={"market": token_id, "fidelity": 1})
            response.raise_for_status()
            data = response.json()
        history = data.get("history", data) if isinstance(data, dict) else data
        return list(history) if isinstance(history, list) else []


def _load_fixtures_config(path: Path) -> list[dict[str, Any]]:
    """Load the operator's per-fixture identity config; fail closed if absent/empty."""
    if not path.exists():
        raise SystemExit(
            f"missing fixtures config {path} — write it first with one entry per Run-001 fixture: "
            '{"fixture_id", "event_slug", "home_team", "away_team", "kickoff_ts"}'
        )
    fixtures = json.loads(path.read_text())
    if not isinstance(fixtures, list) or not fixtures:
        raise SystemExit(f"fixtures config {path} must be a non-empty JSON list")
    return fixtures


async def run(fixtures_config: Path, out_dir: Path) -> dict[str, Any]:
    fixtures = _load_fixtures_config(fixtures_config)
    print(f"probing Polymarket coverage for {len(fixtures)} fixture(s)")

    client = _DefaultPricesHistoryClient()
    coverages: list[VenueCoverage] = []
    for fixture in fixtures:
        try:
            cov = await _probe_one(fixture, client=client, gamma_client=None)
            coverages.append(cov)
        except Exception as exc:  # noqa: BLE001 — one bad fixture must not abort the probe
            print(f"fixture {fixture.get('fixture_id')}: FAILED {type(exc).__name__}: {exc}")

    artifact = build_coverage_artifact(coverages)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "cp1-coverage.json"
    out_path.write_text(json.dumps(artifact, indent=1))
    for line in render_summary(artifact):
        print(line)
    print(f"wrote {out_path}")
    return artifact


def main() -> None:
    parser = argparse.ArgumentParser(description="C-1 Polymarket feasibility probe + coverage gate (CON-001).")
    parser.add_argument("--fixtures-config", type=str, default=None, help=f"per-fixture identity JSON (default {DEFAULT_FIXTURES_CONFIG})")
    parser.add_argument("--out-dir", type=str, default=None, help=f"coverage artifact output dir (default {DEFAULT_OUT_DIR})")
    args = parser.parse_args()

    fixtures_config = Path(args.fixtures_config) if args.fixtures_config else DEFAULT_FIXTURES_CONFIG
    out_dir = Path(args.out_dir) if args.out_dir else DEFAULT_OUT_DIR
    asyncio.run(run(fixtures_config, out_dir))


if __name__ == "__main__":
    main()
