"""M6 (S5) Task 16 — estimated-edge fields on BacktestReport (never ranked).

The estimated executable edge is a VENUE-DERIVED, EXPLANATORY quantity attached to a report
only AFTER the pure venue-free build (via ``model_copy``). It is DISTINCT from
``real_executable_edge_bps`` (which stays ``None`` on the paper venue — there is no live fill),
carries a MACHINE-READABLE evidence rung + explicit assumptions, and NEVER enters the leaderboard
rank axis (CLV is the only ranked axis — SEC-005).
"""

from __future__ import annotations

from tests.test_backtest_report import _report, _run_with_rows, _scored_row


def test_estimated_edge_is_distinct_from_real_and_carries_machine_readable_rung() -> None:
    """Estimated edge is attached post-build and never collides with the (None) real edge."""
    base = _report(_run_with_rows([_scored_row(0, 10)]))
    assumptions = {"no_interpolation": True, "quote_source": "backfilled-price-history"}

    report = base.model_copy(
        update={
            "estimated_executable_edge_bps": 42,
            "estimated_edge_rung": "backfilled-price-history",
            "estimated_edge_assumptions": assumptions,
        }
    )

    # Assert THROUGH model_dump so the values must live on DECLARED fields — a stray attribute set
    # by model_copy on an undeclared name is silently dropped by model_dump (proven RED otherwise).
    dumped = report.model_dump()
    assert dumped["estimated_executable_edge_bps"] == 42
    assert dumped["estimated_edge_rung"] == "backfilled-price-history"
    assert dumped["estimated_edge_assumptions"] == assumptions
    assert report.estimated_executable_edge_bps == 42
    # The venue-estimated edge is a SEPARATE axis from the (always-null) real executable edge.
    assert report.real_executable_edge_bps is None


def test_estimated_edge_never_enters_leaderboard_rank() -> None:
    """SEC-005: even a huge estimated edge is invisible to every ranked leaderboard row."""
    base = _report(_run_with_rows([_scored_row(0, 10)]))

    report = base.model_copy(update={"estimated_executable_edge_bps": 9999})

    # The estimated edge IS carried on the report (declared field)...
    assert report.model_dump()["estimated_executable_edge_bps"] == 9999
    # ...but is invisible to every ranked leaderboard row.
    for row in report.leaderboard:
        assert "estimated_executable_edge_bps" not in row
