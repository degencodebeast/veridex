"""M5 (S4) — CalibrationReport models + builder: Tasks 13-14."""

from __future__ import annotations

from veridex.backtest.calibration import (
    CalibrationBreadth,
    CalibrationBucket,
    CalibrationReport,
)


def test_models_instantiate_with_ported_agenthesis_fields():
    bucket = CalibrationBucket(n=10, right=6, hit_rate=0.6, avg_clv_bps=8.0, pending=2)
    breadth = CalibrationBreadth(
        matches=3, matches_net_positive=2, top_match_share_of_net_pct=40.0, fixtures=[]
    )
    rep = CalibrationReport(
        overall=bucket,
        by_kind={},
        by_market={},
        by_action={},
        breadth=breadth,
        baseline_comparison={},
        provenance="txline-only",
        headline="ok",
    )
    assert rep.overall.hit_rate == 0.6
    assert rep.breadth.top_match_share_of_net_pct == 40.0
