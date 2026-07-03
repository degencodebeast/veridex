"""M5 (S4) — CalibrationReport: hit-rate/CLV calibration + concentration breadth (Task 13-14).

REPORT-ONLY (SEC-002): a ``CalibrationReport`` is derived, descriptive evidence over ALREADY
SETTLED picks — it is never a check, a rank, or a proof surface. This module imports nothing
from either trust-decision package under ``veridex`` (the checks/reconciliation package or the
recompute/proof-card package) and computes no trust decision itself; it only summarizes numbers
that were already scored elsewhere (mirroring ``veridex.backtest.report``'s own derived-only
posture, REQ-2D-303).

Fields are ported from agenthesis's own calibration report shape (bucket/breadth/baseline
naming) so a reviewer familiar with that prior art recognizes the structure immediately — but
every number here comes from TxLINE-scored rows, never agenthesis code or data.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from pydantic import BaseModel


class CalibrationBucket(BaseModel):
    """Hit-rate + CLV summary over one slice of settled picks (e.g. overall, or one kind/market/action)."""

    n: int
    right: int
    hit_rate: float
    avg_clv_bps: float | None
    pending: int


class FixtureCalibration(BaseModel):
    """Per-fixture hit-rate + CLV, used to compute concentration (breadth) across matches."""

    fixture_id: int
    n: int
    hit_rate: float
    avg_clv_bps: float | None
    net_positive: bool


class CalibrationBreadth(BaseModel):
    """How spread out net-positive CLV is across fixtures — a concentration/breadth check.

    A high ``top_match_share_of_net_pct`` means the report's positive CLV rides on one or two
    matches, not a broad edge — the kind of fragility a reviewer should see plainly.
    """

    matches: int
    matches_net_positive: int
    top_match_share_of_net_pct: float
    fixtures: list[FixtureCalibration]


class CalibrationReport(BaseModel):
    """Full calibration report: overall + sliced buckets, breadth, and a baseline comparison.

    REPORT-ONLY (SEC-002): never a check/rank/proof surface — a derived summary over settled,
    already-scored picks (see module docstring).
    """

    overall: CalibrationBucket
    by_kind: dict[str, CalibrationBucket]
    by_market: dict[str, CalibrationBucket]
    by_action: dict[str, CalibrationBucket]
    breadth: CalibrationBreadth
    baseline_comparison: dict[str, CalibrationBucket]
    provenance: str
    headline: str


def _clv_right(clv_bps: int | None) -> bool:
    """A pick is "right" only when its CLV is STRICTLY positive — ``clv_bps == 0`` is a push, not a win."""
    return clv_bps is not None and clv_bps > 0


def _bucket(rows: list[dict[str, Any]]) -> CalibrationBucket:
    """Aggregate one slice of settled rows into a hit-rate + CLV bucket."""
    n = len(rows)
    right = sum(1 for row in rows if _clv_right(row["clv_bps"]))
    pending = sum(1 for row in rows if row["clv_bps"] is None)
    scored_clv = [row["clv_bps"] for row in rows if row["clv_bps"] is not None]
    avg_clv_bps = sum(scored_clv) / len(scored_clv) if scored_clv else None
    hit_rate = right / n if n else 0.0
    return CalibrationBucket(n=n, right=right, hit_rate=hit_rate, avg_clv_bps=avg_clv_bps, pending=pending)


def _group_by(rows: list[dict[str, Any]], key: str) -> dict[Any, list[dict[str, Any]]]:
    grouped: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row[key]].append(row)
    return dict(grouped)


def _fixture_calibration(fixture_id: int, rows: list[dict[str, Any]], net: float) -> FixtureCalibration:
    bucket = _bucket(rows)
    return FixtureCalibration(
        fixture_id=fixture_id,
        n=bucket.n,
        hit_rate=bucket.hit_rate,
        avg_clv_bps=bucket.avg_clv_bps,
        net_positive=net > 0,
    )


def _breadth(settled: list[dict[str, Any]]) -> CalibrationBreadth:
    """Concentration of net-positive CLV across fixtures (module docstring: fragility check)."""
    by_fixture = _group_by(settled, "fixture_id")
    nets = {
        fixture_id: sum(row["clv_bps"] for row in rows if row["clv_bps"] is not None)
        for fixture_id, rows in by_fixture.items()
    }
    fixtures = [
        _fixture_calibration(fixture_id, rows, nets[fixture_id])
        for fixture_id, rows in sorted(by_fixture.items())
    ]
    positive_nets = [net for net in nets.values() if net > 0]
    total_net = sum(positive_nets)
    top_match_share_of_net_pct = 100.0 * max(positive_nets) / total_net if total_net > 0 else 0.0
    return CalibrationBreadth(
        matches=len(by_fixture),
        matches_net_positive=len(positive_nets),
        top_match_share_of_net_pct=top_match_share_of_net_pct,
        fixtures=fixtures,
    )


def build_calibration_report(settled: list[dict[str, Any]], *, provenance: str) -> CalibrationReport:
    """Aggregate settled picks into a `CalibrationReport` (REPORT-ONLY, SEC-002 — module docstring).

    Each row of ``settled`` is ``{"fixture_id": int, "kind": str, "market": str, "action": str,
    "clv_bps": int | None}`` — already-scored picks; this function invents no scored number, it
    only aggregates what's given.
    """
    overall = _bucket(settled)
    by_kind = {key: _bucket(rows) for key, rows in _group_by(settled, "kind").items()}
    by_market = {key: _bucket(rows) for key, rows in _group_by(settled, "market").items()}
    by_action = {key: _bucket(rows) for key, rows in _group_by(settled, "action").items()}
    breadth = _breadth(settled)
    headline = f"{overall.hit_rate:.1%} hit rate over {overall.n} settled picks ({provenance})"
    return CalibrationReport(
        overall=overall,
        by_kind=by_kind,
        by_market=by_market,
        by_action=by_action,
        breadth=breadth,
        baseline_comparison={},
        provenance=provenance,
        headline=headline,
    )
