"""M5 (S4) — CalibrationReport models + builder: Tasks 13-14."""

from __future__ import annotations

from veridex.backtest.calibration import (
    CalibrationBreadth,
    CalibrationBucket,
    CalibrationReport,
    build_calibration_report,
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


def test_single_fixture_carrying_net_clv_is_visible_as_concentration():
    settled = [
        {"fixture_id": 1, "kind": "momentum", "market": "1X2", "action": "FOLLOW_MOMENTUM", "clv_bps": 90},
        {"fixture_id": 2, "kind": "momentum", "market": "1X2", "action": "FOLLOW_MOMENTUM", "clv_bps": 5},
        {"fixture_id": 3, "kind": "momentum", "market": "1X2", "action": "FOLLOW_MOMENTUM", "clv_bps": 5},
    ]
    rep = build_calibration_report(settled, provenance="txline-only")
    assert rep.overall.n == 3
    assert rep.breadth.matches == 3
    assert rep.breadth.top_match_share_of_net_pct == 90.0
    assert rep.provenance == "txline-only"


def test_clv_right_is_strictly_positive_clv():
    settled = [
        {"fixture_id": 1, "kind": "momentum", "market": "1X2", "action": "FOLLOW_MOMENTUM", "clv_bps": 0},
    ]
    rep = build_calibration_report(settled, provenance="txline-only")
    assert rep.overall.right == 0
