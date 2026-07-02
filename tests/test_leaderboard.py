"""B10 — leaderboard tests (REQ-114 / AC-114, gate CON-006, CON-008).

TDD: every test in this file was watched RED (``veridex/leaderboard.py`` did not exist)
before the implementation was written, then GREEN with zero regressions.

Trust path (CON-007): ``veridex/leaderboard.py`` imports no LLM SDK — asserted by
``test_leaderboard_import_audit_clean``.

Key invariant under test (CON-006/CON-008): *proof completeness is an eligibility badge,
NOT a performance score* — ``eligibility_badge`` must never enter the sort key
(``test_eligibility_badge_does_not_affect_rank``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from veridex.leaderboard import leaderboard

# ---------------------------------------------------------------------------
# Test helper — build one input record (a score_run row, optionally tagged).
# ---------------------------------------------------------------------------


def _row(
    agent_id: str,
    *,
    total_clv_bps: int,
    action_count: int,
    sim_pnl: int | None = None,
    brier: float | None = None,
    max_drawdown: float = 0.0,
    valid_pct: float = 100.0,
    proof_mode: str = "reproducible",
    rank: int = 1,
    anchor_status: str | None = None,
    source_mode: str | None = None,
) -> dict[str, Any]:
    """Build a leaderboard input row (a ``score_run`` row, optionally run-tagged).

    ``avg_clv_bps`` is derived from ``total_clv_bps / action_count`` (or ``None``
    when ``action_count == 0``) — mirroring the ``score_run`` contract exactly so
    tests exercise the same shape the orchestrator would produce.
    """
    avg_clv_bps: float | None = (total_clv_bps / action_count) if action_count > 0 else None
    row: dict[str, Any] = {
        "agent_id": agent_id,
        "avg_clv_bps": avg_clv_bps,
        "total_clv_bps": total_clv_bps,
        "sim_pnl": sim_pnl if sim_pnl is not None else total_clv_bps,
        "brier": brier,
        "max_drawdown": max_drawdown,
        "action_count": action_count,
        "valid_pct": valid_pct,
        "proof_mode": proof_mode,
        "rank": rank,
    }
    if anchor_status is not None:
        row["anchor_status"] = anchor_status
    if source_mode is not None:
        row["source_mode"] = source_mode
    return row


def _by_id(board: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {r["agent_id"]: r for r in board}


# ---------------------------------------------------------------------------
# 1 — aggregate one agent across ≥2 runs; POOLED avg, NOT mean-of-means
# ---------------------------------------------------------------------------


def test_aggregates_one_agent_two_runs_pooled_avg() -> None:
    """Pooled avg != mean-of-means when run sizes differ.

    run1: total_clv=100, action_count=2  → per-run avg = 50
    run2: total_clv=300, action_count=1  → per-run avg = 300

    Mean-of-run-means = (50 + 300) / 2 = 175  ← WRONG (what we must NOT compute).
    Pooled avg        = (100 + 300) / (2 + 1) = 400/3 ≈ 133.33  ← CORRECT.
    """
    records = [
        _row("A", total_clv_bps=100, action_count=2),
        _row("A", total_clv_bps=300, action_count=1),
    ]
    board = leaderboard(records)
    assert len(board) == 1
    row = board[0]
    assert row["agent_id"] == "A"
    assert row["runs"] == 2
    assert row["total_clv_bps"] == 400
    assert row["action_count"] == 3
    assert row["rank"] == 1
    # Pooled, not mean-of-means: 400/3 ≠ 175.
    assert row["avg_clv_bps"] == pytest.approx(400 / 3)
    assert row["avg_clv_bps"] != pytest.approx(175.0)


def test_runs_count_correct() -> None:
    """``runs`` field counts the number of input rows for that agent (one per run)."""
    records = [
        _row("A", total_clv_bps=100, action_count=1),
        _row("A", total_clv_bps=100, action_count=1),
        _row("A", total_clv_bps=100, action_count=1),
    ]
    board = leaderboard(records)
    assert board[0]["runs"] == 3


# ---------------------------------------------------------------------------
# 2 — ranks ≥2 agents by pooled avg CLV (primary axis)
# ---------------------------------------------------------------------------


def test_ranks_two_agents_primary_clv() -> None:
    """Primary sort: higher pooled avg CLV ranks first."""
    records = [
        _row("A", total_clv_bps=200, action_count=2),  # avg = 100
        _row("B", total_clv_bps=150, action_count=2),  # avg = 75
    ]
    board = leaderboard(records)
    assert [r["agent_id"] for r in board] == ["A", "B"]
    assert [r["rank"] for r in board] == [1, 2]


# ---------------------------------------------------------------------------
# 3 — tie-breaker chain: total_clv → brier (asc, None last) → drawdown → agent_id
# ---------------------------------------------------------------------------


def test_tiebreaker_total_clv_desc() -> None:
    """Equal pooled avg → higher total CLV ranks first."""
    records = [
        _row("A", total_clv_bps=200, action_count=2),  # avg=100, total=200
        _row("B", total_clv_bps=300, action_count=3),  # avg=100, total=300
    ]
    board = leaderboard(records)
    assert [r["agent_id"] for r in board] == ["B", "A"]


def test_tiebreaker_brier_asc() -> None:
    """Equal avg and total → lower Brier score ranks first (better calibration)."""
    records = [
        _row("A", total_clv_bps=100, action_count=1, brier=0.10),
        _row("B", total_clv_bps=100, action_count=1, brier=0.05),
    ]
    board = leaderboard(records)
    assert [r["agent_id"] for r in board] == ["B", "A"]


def test_tiebreaker_brier_none_ranks_last() -> None:
    """When one agent has a Brier and the other has None, real-Brier agent ranks first."""
    records = [
        _row("A", total_clv_bps=100, action_count=1, brier=None),
        _row("B", total_clv_bps=100, action_count=1, brier=0.99),
    ]
    board = leaderboard(records)
    # B has a real Brier (even a bad one); A has None → B ranks first.
    assert [r["agent_id"] for r in board] == ["B", "A"]


def test_tiebreaker_drawdown_less_severe_first() -> None:
    """Equal avg, total, and both Brier=None → less-severe drawdown (closer to 0) first."""
    records = [
        _row("A", total_clv_bps=100, action_count=1, max_drawdown=0.0),
        _row("B", total_clv_bps=100, action_count=1, max_drawdown=-50.0),
    ]
    board = leaderboard(records)
    assert [r["agent_id"] for r in board] == ["A", "B"]


def test_tiebreaker_agent_id_final() -> None:
    """Fully identical metrics → deterministic final tiebreak on agent_id ascending."""
    records = [
        _row("zeta", total_clv_bps=100, action_count=1),
        _row("alpha", total_clv_bps=100, action_count=1),
    ]
    board = leaderboard(records)
    assert [r["agent_id"] for r in board] == ["alpha", "zeta"]


# ---------------------------------------------------------------------------
# 4 — eligibility_badge does NOT change rank order (CON-006/CON-008)
# ---------------------------------------------------------------------------


def test_eligibility_badge_does_not_affect_rank() -> None:
    """Swapping badge assignments between agents leaves the rank order unchanged.

    Scenario 1: A (better CLV, unproven) vs B (worse CLV, fully-proven).
    Scenario 2: A (better CLV, fully-proven) vs B (worse CLV, unproven).
    Both scenarios must produce identical rank order [A, B] — badge never adds points.
    """
    records_unproven_winner = [
        _row("A", total_clv_bps=300, action_count=3, anchor_status="pending"),  # avg=100
        _row("B", total_clv_bps=100, action_count=2, anchor_status="anchored"),  # avg=50
    ]
    records_proven_winner = [
        _row("A", total_clv_bps=300, action_count=3, anchor_status="anchored"),  # avg=100
        _row("B", total_clv_bps=100, action_count=2, anchor_status="pending"),  # avg=50
    ]
    board_1 = leaderboard(records_unproven_winner)
    board_2 = leaderboard(records_proven_winner)

    assert [r["agent_id"] for r in board_1] == ["A", "B"], "rank order wrong in scenario 1"
    assert [r["agent_id"] for r in board_2] == ["A", "B"], "rank order wrong in scenario 2"
    assert [r["rank"] for r in board_1] == [1, 2]
    assert [r["rank"] for r in board_2] == [1, 2]

    # Badges differ between scenarios (proving the badge field changed).
    by_1 = _by_id(board_1)
    by_2 = _by_id(board_2)
    assert by_1["A"]["eligibility_badge"] == "unproven"
    assert by_2["A"]["eligibility_badge"] == "fully-proven"
    assert by_1["B"]["eligibility_badge"] == "fully-proven"
    assert by_2["B"]["eligibility_badge"] == "unproven"


def test_proven_and_partially_proven_rank_purely_by_clv() -> None:
    """A proven agent with lower CLV ranks below a partially-proven agent with higher CLV.

    "partial" has 2 runs (one confirmed, one pending) → partially-proven.
    "proven"  has 1 run  (confirmed)                  → fully-proven, but lower CLV.
    CLV governs rank; the fully-proven badge cannot compensate for lower CLV.
    """
    records = [
        # "partial": two runs → one confirmed + one pending = partially-proven
        _row("partial", total_clv_bps=300, action_count=3, anchor_status="anchored"),
        _row("partial", total_clv_bps=200, action_count=2, anchor_status="pending"),
        # "proven": one confirmed run → fully-proven, but lower avg CLV
        _row("proven", total_clv_bps=200, action_count=5, anchor_status="anchored"),
    ]
    # partial: total=500, count=5, avg=100  — proven: total=200, count=5, avg=40
    board = leaderboard(records)
    assert board[0]["agent_id"] == "partial"  # higher CLV wins despite partial proof
    assert board[0]["eligibility_badge"] == "partially-proven"
    assert board[1]["eligibility_badge"] == "fully-proven"


# ---------------------------------------------------------------------------
# 5 — badge derivation: anchor_status → eligibility_badge
# ---------------------------------------------------------------------------


def test_badge_fully_proven_all_anchored() -> None:
    """All runs with anchor_status='confirmed' → 'fully-proven'."""
    records = [
        _row("A", total_clv_bps=100, action_count=1, anchor_status="anchored"),
        _row("A", total_clv_bps=100, action_count=1, anchor_status="anchored"),
    ]
    board = leaderboard(records)
    assert board[0]["eligibility_badge"] == "fully-proven"
    assert board[0]["anchor_status"] == "all-anchored"


def test_badge_partially_proven_one_pending() -> None:
    """A mix of confirmed and pending → 'partially-proven'."""
    records = [
        _row("A", total_clv_bps=100, action_count=1, anchor_status="anchored"),
        _row("A", total_clv_bps=100, action_count=1, anchor_status="pending"),
    ]
    board = leaderboard(records)
    assert board[0]["eligibility_badge"] == "partially-proven"
    assert board[0]["anchor_status"] == "some-pending"


def test_badge_unproven_no_confirmed_run() -> None:
    """No confirmed runs → 'unproven'."""
    records = [
        _row("A", total_clv_bps=100, action_count=1, anchor_status="pending"),
        _row("A", total_clv_bps=100, action_count=1, anchor_status="pending"),
    ]
    board = leaderboard(records)
    assert board[0]["eligibility_badge"] == "unproven"
    assert board[0]["anchor_status"] == "none-anchored"


def test_badge_unproven_when_anchor_status_absent() -> None:
    """Records without anchor_status field → 'unproven' (absent treated as unanchored)."""
    records = [
        _row("A", total_clv_bps=100, action_count=1),  # no anchor_status tag
    ]
    board = leaderboard(records)
    assert board[0]["eligibility_badge"] == "unproven"


# ---------------------------------------------------------------------------
# 6 — agent with 0 scored actions: avg None, ranks last
# ---------------------------------------------------------------------------


def test_agent_zero_actions_avg_none_ranks_last() -> None:
    """An agent with no scored actions has avg_clv_bps=None and must rank last."""
    records = [
        _row("scorer", total_clv_bps=10, action_count=1),
        _row("no_acts", total_clv_bps=0, action_count=0),
    ]
    board = leaderboard(records)
    by = _by_id(board)
    assert by["scorer"]["rank"] == 1
    assert by["no_acts"]["rank"] == 2
    assert by["no_acts"]["avg_clv_bps"] is None


def test_agent_zero_actions_across_multiple_runs() -> None:
    """Zero total action_count across multiple runs → avg None, ranks last."""
    records = [
        _row("better", total_clv_bps=50, action_count=1),
        _row("waiter", total_clv_bps=0, action_count=0),
        _row("waiter", total_clv_bps=0, action_count=0),
    ]
    board = leaderboard(records)
    assert board[-1]["agent_id"] == "waiter"
    assert board[-1]["avg_clv_bps"] is None
    assert board[-1]["runs"] == 2


# ---------------------------------------------------------------------------
# 7 — aggregation helpers: brier, drawdown, sim_pnl
# ---------------------------------------------------------------------------


def test_brier_mean_of_non_none_values() -> None:
    """Leaderboard brier = mean of per-run brier values that are not None."""
    records = [
        _row("A", total_clv_bps=100, action_count=1, brier=0.10),
        _row("A", total_clv_bps=100, action_count=1, brier=0.20),
        _row("A", total_clv_bps=100, action_count=1, brier=None),  # excluded
    ]
    board = leaderboard(records)
    assert board[0]["brier"] == pytest.approx(0.15)


def test_brier_none_when_all_runs_have_no_brier() -> None:
    """When no run contributes a brier, leaderboard brier is None."""
    records = [
        _row("A", total_clv_bps=100, action_count=1, brier=None),
        _row("A", total_clv_bps=100, action_count=1, brier=None),
    ]
    board = leaderboard(records)
    assert board[0]["brier"] is None


def test_max_drawdown_is_worst_across_runs() -> None:
    """max_drawdown = min (most-negative) across runs — the worst episode."""
    records = [
        _row("A", total_clv_bps=100, action_count=1, max_drawdown=-10.0),
        _row("A", total_clv_bps=100, action_count=1, max_drawdown=-50.0),
        _row("A", total_clv_bps=100, action_count=1, max_drawdown=0.0),
    ]
    board = leaderboard(records)
    assert board[0]["max_drawdown"] == pytest.approx(-50.0)


def test_sim_pnl_summed_across_runs() -> None:
    """sim_pnl is summed across all runs for the agent."""
    records = [
        _row("A", total_clv_bps=100, action_count=1, sim_pnl=100),
        _row("A", total_clv_bps=200, action_count=2, sim_pnl=200),
    ]
    board = leaderboard(records)
    assert board[0]["sim_pnl"] == 300


# ---------------------------------------------------------------------------
# 8 — deterministic / reproducible
# ---------------------------------------------------------------------------


def test_deterministic_same_input_same_output() -> None:
    """Calling leaderboard twice on the same records produces identical output."""
    records = [
        _row("A", total_clv_bps=200, action_count=2, anchor_status="anchored"),
        _row("B", total_clv_bps=100, action_count=1, anchor_status="pending"),
        _row("A", total_clv_bps=50, action_count=1, anchor_status="anchored"),
    ]
    assert leaderboard(records) == leaderboard(records)


def test_empty_records_returns_empty_list() -> None:
    """leaderboard([]) returns an empty list without error."""
    assert leaderboard([]) == []


# ---------------------------------------------------------------------------
# 9 — output row carries the full contract keys
# ---------------------------------------------------------------------------


def test_output_row_has_all_required_keys() -> None:
    """Every output row carries the full leaderboard contract schema."""
    records = [_row("A", total_clv_bps=100, action_count=1, anchor_status="anchored", source_mode="replay")]
    board = leaderboard(records)
    assert set(board[0]) == {
        "agent_id",
        "runs",
        "avg_clv_bps",
        "total_clv_bps",
        "sim_pnl",
        "brier",
        "max_drawdown",
        "action_count",
        "valid_pct",
        "proof_mode",
        "eligibility_badge",
        "anchor_status",
        "source_mode",
        "valid_count",
        "clv_confidence",
        "low_sample",
        "sample_size",
        "rank",
    }


# ---------------------------------------------------------------------------
# 10 — import-audit clean over veridex/leaderboard.py (CON-007 trust path)
# ---------------------------------------------------------------------------


def test_leaderboard_import_audit_clean() -> None:
    """veridex/leaderboard.py contains no forbidden LLM SDK imports (CON-007)."""
    import veridex.leaderboard as lb_mod
    from veridex.verifier.import_audit import assert_no_llm_imports

    assert_no_llm_imports(Path(lb_mod.__file__))
