"""M5 (S4) — CalibrationReport: hit-rate/CLV calibration + concentration breadth (Task 13-14).

REPORT-ONLY (SEC-002): a ``CalibrationReport`` is derived, descriptive evidence over ALREADY
SETTLED picks — it is never a check, a rank, or a proof surface. It imports nothing from
``veridex.checks`` or ``veridex.verifier`` and computes no trust decision; it only summarizes
numbers that were already scored elsewhere (mirroring ``veridex.backtest.report``'s own
derived-only posture, REQ-2D-303).

Fields are ported from agenthesis's own calibration report shape (bucket/breadth/baseline
naming) so a reviewer familiar with that prior art recognizes the structure immediately — but
every number here comes from TxLINE-scored rows, never agenthesis code or data.
"""

from __future__ import annotations

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
