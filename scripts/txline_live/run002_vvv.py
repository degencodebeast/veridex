"""Run-002-VvV — pinned, predeclared value-vs-venue estimated-edge run over the headline-eligible WC packs.

This script IS the executable pre-run stamp for the C/P1 rung-2 lane (REQ-007): the committed
`EvalProtocol` (the 18 headline-eligible fixtures, the value-vs-venue roster, the window/close
semantics, `committed_at`) + the pinned `VENUE_SOURCE_ID` + `COVERAGE_ARTIFACT_HASH` + the headline
freshness bound + the haircut ladder are ALL fixed BEFORE any estimated-edge number exists. Committing
this file = committing the invocation.

Two self-verifies VOID the run on any drift from the stamp (mirroring `run001.py`):
  1. the C-1 coverage artifact recomputes to `COVERAGE_ARTIFACT_HASH` (probe canonicalization), and
  2. the C-4 source built over the committed backfill frames reproduces the pinned `VENUE_SOURCE_ID`.

Honesty framing (CON-012 / spec §6a): the headline is **"pre-match runway dislocation under bounded
staleness"** — NEVER "near-kickoff" unless the used-quote freshness buckets prove clustering near
kickoff; a low `quote_matched_pct` reads as a COVERAGE statement ("could not price most decisions
under the 15m bound"), never a "no edge" MEASUREMENT; the headline freshness stays `freshness_s=900`
and any wider bound is emitted ONLY under `stale_runway_diagnostic` (tagged, non-headline). The run's
`decision_quote_coverage` is a FIRST-CLASS top-level result. `real_executable_edge_bps` is always
`None` (CON-003). All `veridex` imports are lazy inside `run()` (CON-010) so importing this module
stays network/credential free.

Run: .venv/bin/python scripts/txline_live/run002_vvv.py   (operator step — reads local frames+packs;
no network, no creds). DO NOT run in CI; it is gated by a Codex milestone review.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # import for type-checkers only — keep the runtime module import network/veridex-free
    from veridex.backtest.venue_behavior_report import (
        DecisionQuoteCoverage,
        VenueBehaviorReport,
    )

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# --- PINNED PROTOCOL (pre-run stamp) ---
PROTOCOL_ID = "run-002-vvv-headline-18fx"
#: The C-1 headline-eligible universe (18/18 viable; CON-001 gate PASSED).
FIXTURE_IDS = [
    18179763, 18179551, 18176123, 17588229, 17588234, 17926593, 17588245, 17588391, 17588404,
    17588325, 18167317, 18172469, 18175983, 18172280, 18175981, 18179550, 18179759, 18175918,
]
STRATEGY_CONFIGS = ["value-vs-venue"]
WINDOW_ID = "run002-vvv-pre_match-con040"
CLOSE_SEMANTICS = "pre_match"
COMMITTED_AT = "2026-07-04T13:00:00Z"

#: The C-1 coverage-artifact content hash (probe canonicalization) — the source-identity anchor.
COVERAGE_ARTIFACT_HASH = "f88bd42ed70299abdaa6ae4f99cfa010671a6eb0f77871040e760251640b4077"
#: The C-4 `venue_source_id` the committed frames + coverage + bound + ladder + version MUST reproduce.
VENUE_SOURCE_ID = "0f1755d0affea663d034744cc8d881ca01f8e72c10ef85b2a7aa350d12c41c92"
SOURCE_CONFIG_VERSION = "cp1-v1"

#: The HEADLINE staleness bound (s). Wider bounds are `stale_runway_diagnostic`-only (never headline).
HEADLINE_FRESHNESS_S = 900
#: A wider, DIAGNOSTIC-ONLY sensitivity bound (never headline, never a claim).
DIAGNOSTIC_FRESHNESS_S = 1800
#: The report-only round-trip haircut ladder (bps).
HAIRCUT_LADDER_BPS = [0, 100, 200, 300]

#: Report slice partitions.
PROB_BANDS = [(0, 20), (20, 40), (40, 60), (60, 80), (80, 100)]
TTC_BUCKETS = [">24h", "6-24h", "1-6h", "<1h"]
FRESHNESS_BUCKETS = ["<=2m", "<=5m", "<=15m"]

# --- FRAMING (CON-012 / spec §6a) — fixed honesty strings + gates ---
HEADLINE_RUNWAY_LABEL = "pre-match runway dislocation under bounded staleness"
NEAR_KICKOFF_LABEL = "near-kickoff dislocation under bounded staleness"
LOW_COVERAGE_CONCLUSION = "VvV could not price most drift decisions under the 15m freshness bound"
#: Below this matched-quote share the headline is a COVERAGE statement, never a measurement.
LOW_COVERAGE_THRESHOLD_PCT = 50.0
#: Used quotes must cluster at/under this age (s) for a "near-kickoff" headline to be permitted.
NEAR_KICKOFF_MAX_S = 300

CP1_DIR = Path(__file__).parent / "cp1"
COVERAGE_PATH = CP1_DIR / "cp1-coverage.json"
MANIFEST_PATH = CP1_DIR / "frames" / "backfill-manifest.json"
PACKS_DIR = Path(__file__).parent / "packs"
OUT_DIR = ROOT.parent / ".omc" / "research" / "edge-validation-runs"
LEDGER_PATH = OUT_DIR / "run-002-vvv-hypothesis-ledger.jsonl"


class RunVoidError(RuntimeError):
    """Raised when a self-verify fails — the run diverged from its pre-run stamp and must NOT be reported."""


# ==========================================================================================
# Self-verify gates (pure stdlib — no veridex import, so they are import-safe + unit-testable).
# ==========================================================================================


def verify_coverage_artifact_hash(coverage_path: Path, *, committed: str) -> str:
    """Recompute the C-1 coverage artifact hash EXACTLY as the probe built it; VOID on any mismatch.

    The probe hashes the artifact dict (minus ``artifact_content_hash``) canonically
    (``sort_keys=True``, compact separators). This re-derives that hash, checks it matches BOTH the
    file's embedded ``artifact_content_hash`` (integrity) AND the committed stamp (predeclaration), and
    returns it. Either mismatch raises :class:`RunVoidError` — the coverage universe diverged.
    """
    artifact: dict[str, Any] = json.loads(Path(coverage_path).read_text())
    embedded = artifact.pop("artifact_content_hash", None)
    canonical = json.dumps(artifact, sort_keys=True, separators=(",", ":"))
    recomputed = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if embedded != recomputed:
        raise RunVoidError(
            f"VOID: coverage artifact self-hash mismatch — embedded {embedded}, recomputed {recomputed}. "
            "The coverage file was altered after it was stamped; do NOT report this result."
        )
    if recomputed != committed:
        raise RunVoidError(
            f"VOID: coverage-artifact hash diverged from the pre-run stamp — committed {committed}, "
            f"got {recomputed}. The coverage universe changed since the stamp; do NOT report this result."
        )
    return recomputed


def verify_venue_source_id(built_id: str, *, committed: str) -> None:
    """VOID unless the source built over the committed frames reproduces the pinned ``venue_source_id``."""
    if built_id != committed:
        raise RunVoidError(
            f"VOID: venue_source_id mismatch — committed {committed}, built {built_id}. The backfill "
            "frames/coverage/bound/ladder/version diverged from the stamp; do NOT report this result."
        )


# ==========================================================================================
# Framing gates (CON-012 / spec §6a) — duck-typed over DecisionQuoteCoverage so imports stay lazy.
# ==========================================================================================


def _bucket_seconds(label: str) -> int:
    """Parse a ``"<=Nm"`` / ``"<=Ns"`` freshness-bucket label into a seconds threshold."""
    body = label.replace("<=", "").strip()
    return int(body[:-1]) * 60 if body.endswith("m") else int(body[:-1])


def near_kickoff_supported(coverage: DecisionQuoteCoverage) -> bool:
    """True iff a strict majority of USED quotes are fresh enough (<= 5m) to be near-kickoff.

    This is the ONLY gate that permits a "near-kickoff" headline (spec §6a): decision-level used-quote
    freshness — never fixture-level coverage — must prove the clustering. The C-1 gate found dense
    runway depth but SPARSE near-kickoff quotes, so this returns ``False`` for a runway-stale run.
    """
    matched = coverage.quote_matched_count
    if matched <= 0:
        return False
    near = sum(
        count
        for label, count in coverage.freshness_bucket_counts_for_used_quotes.items()
        if _bucket_seconds(label) <= NEAR_KICKOFF_MAX_S
    )
    return near * 2 > matched


def runway_framing_label(coverage: DecisionQuoteCoverage) -> str:
    """The headline framing label — the runway phrase, upgraded to near-kickoff ONLY if the buckets prove it."""
    return NEAR_KICKOFF_LABEL if near_kickoff_supported(coverage) else HEADLINE_RUNWAY_LABEL


def headline_conclusion(coverage: DecisionQuoteCoverage) -> str:
    """A low ``quote_matched_pct`` is a COVERAGE statement; adequate coverage reports the distribution.

    NEVER "no edge" / "no dislocation": a mostly-null run could not PRICE most decisions under the 15m
    bound (coverage), which is not the same as HAVING MEASURED no dislocation (measurement) — CON-012.
    """
    if coverage.quote_matched_pct < LOW_COVERAGE_THRESHOLD_PCT:
        return LOW_COVERAGE_CONCLUSION
    return (
        f"VvV priced {coverage.quote_matched_pct:g}% of drift decisions under the 15m freshness bound; "
        "estimated dislocation distribution reported over headline slices"
    )


def stale_runway_diagnostic(*, freshness_s: int, cost_survival: dict[int, bool] | None = None) -> dict[str, Any]:
    """A wider-than-headline freshness lane — TAGGED diagnostic, never headline, never a claim (spec §6a)."""
    return {
        "lane": "diagnostic",
        "label": "stale-runway sensitivity (report-only, non-headline)",
        "headline": False,
        "freshness_s": int(freshness_s),
        "cost_survival": cost_survival,
        "note": "wider-than-headline freshness bound; sensitivity only, never a headline conclusion",
    }


def build_run_result(
    *,
    protocol_id: str,
    committed_at: str,
    venue_source_id: str,
    coverage_artifact_hash: str,
    freshness_s: int,
    behavior_report: VenueBehaviorReport,
    decision_quote_coverage: DecisionQuoteCoverage,
    haircut_ladder_bps: list[int],
    stale_runway_diagnostic_lane: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the predeclared run result — ``decision_quote_coverage`` FIRST-CLASS, real edge ``None``.

    The runway framing label + the coverage-honest conclusion are computed from
    ``decision_quote_coverage`` (spec §6a). The full :class:`VenueBehaviorReport` (slices +
    cost-survival + freshness-artifact warning) rides alongside; any wider-bound sensitivity is nested
    under ``stale_runway_diagnostic`` (tagged, non-headline). ``real_executable_edge_bps`` is ``None``
    (CON-003 — no live fill in C/P1).
    """
    return {
        "protocol_id": protocol_id,
        "committed_at": committed_at,
        "venue_source_id": venue_source_id,
        "coverage_artifact_hash": coverage_artifact_hash,
        "freshness_s": int(freshness_s),
        "run_note": HEADLINE_RUNWAY_LABEL,
        "headline_label": runway_framing_label(decision_quote_coverage),
        "headline_conclusion": headline_conclusion(decision_quote_coverage),
        # FIRST-CLASS: the self-falsifying coverage instrument, top-level (not buried in a slice).
        "decision_quote_coverage": decision_quote_coverage.model_dump(),
        "venue_behavior_report": behavior_report.model_dump(),
        "cost_survival": {str(k): v for k, v in behavior_report.cost_survival.items()},
        "freshness_artifact_warning": behavior_report.freshness_artifact_warning,
        "haircut_ladder_bps": list(haircut_ladder_bps),
        "real_executable_edge_bps": None,
        "stale_runway_diagnostic": stale_runway_diagnostic_lane,
    }


# ==========================================================================================
# The operator run (lazy veridex imports, CON-010). DO NOT run in CI — Codex-milestone gated.
# ==========================================================================================


def _load_backfill() -> tuple[list[str], list[Any]]:
    """Load the committed backfill: the sorted 54 pack hashes + the decimal price-history frames."""
    from veridex.venues.price_history import VenuePriceHistoryFrame  # noqa: PLC0415

    manifest = json.loads(MANIFEST_PATH.read_text())
    pack_hashes: list[str] = []
    frames: list[Any] = []
    for fixture in manifest["fixtures"]:
        for side in fixture["sides"]:
            if not side.get("ok"):
                continue
            pack_hashes.append(side["artifact_content_hash"])
            frames_file = ROOT / side["frames_file"]
            for line in frames_file.read_text().splitlines():
                if line.strip():
                    frames.append(VenuePriceHistoryFrame.model_validate_json(line))
    return sorted(pack_hashes), frames


async def _collect_over_bound(
    protocol: Any, packs: dict[int, Path], frames: list[Any], pack_hashes: list[str], *, freshness_s: int
) -> tuple[list[Any], list[Any], str]:
    """Build a bounded-staleness source at ``freshness_s`` and collect its rows + decisions over the roster."""
    from veridex.backtest.evaluation import produce_results_by_fixture  # noqa: PLC0415
    from veridex.venues.venue_price_source import build_backfilled_venue_source  # noqa: PLC0415

    source, venue_source_id = build_backfilled_venue_source(
        frames,
        price_history_artifact_hashes=pack_hashes,
        coverage_artifact_hash=COVERAGE_ARTIFACT_HASH,
        freshness_s=freshness_s,
        haircut_ladder_bps=HAIRCUT_LADDER_BPS,
        source_config_version=SOURCE_CONFIG_VERSION,
    )
    decision_sink: dict[int, list[Any]] = {}
    row_sink: dict[int, list[Any]] = {}
    await produce_results_by_fixture(
        protocol,
        packs=packs,
        venue_price_source=source,
        venue_source_id=venue_source_id,
        venue_decision_sink=decision_sink,
        venue_behavior_row_sink=row_sink,
    )
    rows = [row for fid in FIXTURE_IDS for row in row_sink.get(fid, [])]
    decisions = [dec for fid in FIXTURE_IDS for dec in decision_sink.get(fid, [])]
    return rows, decisions, venue_source_id


async def run() -> dict[str, Any]:
    from veridex.backtest.evaluation import EvalProtocol  # noqa: PLC0415
    from veridex.backtest.venue_behavior_report import (  # noqa: PLC0415
        build_venue_behavior_report,
        register_hypothesis_ledger_entry,
    )

    # SELF-VERIFY 1: the committed coverage universe (CON-001 anchor) reproduces its stamped hash.
    cov_hash = verify_coverage_artifact_hash(COVERAGE_PATH, committed=COVERAGE_ARTIFACT_HASH)

    pack_hashes, frames = _load_backfill()
    packs = {fid: PACKS_DIR / str(fid) for fid in FIXTURE_IDS}
    for fid, path in packs.items():
        if not (path / "pack.json").exists():
            raise SystemExit(f"missing pack for fixture {fid} at {path}")

    protocol = EvalProtocol(
        protocol_id=PROTOCOL_ID,
        fixture_ids=FIXTURE_IDS,
        strategy_configs=STRATEGY_CONFIGS,
        window=WINDOW_ID,
        close_semantics=CLOSE_SEMANTICS,
        baselines=[],
        committed_at=COMMITTED_AT,
    )

    # HEADLINE bound (900): collect rows + decisions and SELF-VERIFY 2 (venue_source_id reproduces).
    rows, decisions, venue_source_id = await _collect_over_bound(
        protocol, packs, frames, pack_hashes, freshness_s=HEADLINE_FRESHNESS_S
    )
    verify_venue_source_id(venue_source_id, committed=VENUE_SOURCE_ID)

    report = build_venue_behavior_report(
        rows,
        decisions,
        haircut_ladder_bps=HAIRCUT_LADDER_BPS,
        prob_bands=PROB_BANDS,
        ttc_buckets=TTC_BUCKETS,
        freshness_buckets=FRESHNESS_BUCKETS,
    )
    register_hypothesis_ledger_entry(report, ledger_path=LEDGER_PATH, run_id=PROTOCOL_ID)

    # DIAGNOSTIC bound (1800): report-only sensitivity — tagged, NEVER headline (spec §6a).
    _, diag_decisions, _ = await _collect_over_bound(
        protocol, packs, frames, pack_hashes, freshness_s=DIAGNOSTIC_FRESHNESS_S
    )
    diag_report = build_venue_behavior_report(
        [], diag_decisions, haircut_ladder_bps=HAIRCUT_LADDER_BPS, prob_bands=PROB_BANDS,
        ttc_buckets=TTC_BUCKETS, freshness_buckets=FRESHNESS_BUCKETS,
    )
    diagnostic = stale_runway_diagnostic(
        freshness_s=DIAGNOSTIC_FRESHNESS_S, cost_survival=diag_report.cost_survival
    )
    diagnostic["decision_quote_coverage"] = diag_report.decision_quote_coverage.model_dump()

    return build_run_result(
        protocol_id=PROTOCOL_ID,
        committed_at=COMMITTED_AT,
        venue_source_id=venue_source_id,
        coverage_artifact_hash=cov_hash,
        freshness_s=HEADLINE_FRESHNESS_S,
        behavior_report=report,
        decision_quote_coverage=report.decision_quote_coverage,
        haircut_ladder_bps=HAIRCUT_LADDER_BPS,
        stale_runway_diagnostic_lane=diagnostic,
    )


def main() -> None:
    out = asyncio.run(run())
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "run-002-vvv-result.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(out, indent=2, default=str))
    print(f"\nheadline_label={out['headline_label']!r} | venue_source_id={out['venue_source_id'][:12]}")


if __name__ == "__main__":
    main()
