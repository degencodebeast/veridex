"""WD-7 — sample-size confidence tiers + leaderboard aggregation (never affects rank)."""

from __future__ import annotations

from veridex.clv_confidence import clv_confidence
from veridex.leaderboard import _rank_key, leaderboard


def test_tiers_by_sample_size() -> None:
    assert clv_confidence(0) == {"sample_size": 0, "clv_confidence": "low", "low_sample": True}
    assert clv_confidence(9)["clv_confidence"] == "low"
    assert clv_confidence(10)["clv_confidence"] == "medium"
    assert clv_confidence(29)["clv_confidence"] == "medium"
    assert clv_confidence(30) == {"sample_size": 30, "clv_confidence": "high", "low_sample": False}


def _row(agent_id: str, avg: float, valid_count: int) -> dict:
    return {
        "agent_id": agent_id,
        "avg_clv_bps": avg,
        "total_clv_bps": int(avg * valid_count),
        "sim_pnl": int(avg * valid_count),
        "brier": None,
        "max_drawdown": 0.0,
        "action_count": valid_count,
        "valid_pct": 100.0,
        "valid_count": valid_count,
        "proof_mode": "reproducible",
    }


def test_leaderboard_aggregates_valid_count_and_confidence() -> None:
    board = leaderboard([_row("big", 12.0, 40), _row("small", 50.0, 3)])
    by_id = {r["agent_id"]: r for r in board}
    # High-CLV small-sample agent still outranks (rank = avg CLV only, SEC-005)…
    assert by_id["small"]["rank"] == 1
    # …but is flagged low-sample; the large-sample agent reads high-confidence.
    assert by_id["small"]["valid_count"] == 3
    assert by_id["small"]["low_sample"] is True
    assert by_id["small"]["clv_confidence"] == "low"
    assert by_id["big"]["valid_count"] == 40
    assert by_id["big"]["clv_confidence"] == "high"
    assert by_id["big"]["low_sample"] is False


def _identical_except_sample(agent_id: str, valid_count: int) -> dict:
    # Identical on EVERY rank-key field (avg/total CLV, brier, drawdown, action_count); only the
    # sample size differs — so the rank tiebreak is agent_id, never valid_count.
    return {
        "agent_id": agent_id,
        "avg_clv_bps": 10.0,
        "total_clv_bps": 100,
        "sim_pnl": 100,
        "brier": None,
        "max_drawdown": 0.0,
        "action_count": 10,
        "valid_pct": 100.0,
        "valid_count": valid_count,
        "proof_mode": "reproducible",
    }


def test_rank_order_is_clv_only_not_sample_size() -> None:
    # SEC-005: two agents identical on all rank-key fields but different valid_count rank by the
    # deterministic agent_id tiebreak, NEVER by the larger sample. Confidence is not a rank input.
    board = leaderboard([_identical_except_sample("zeta", 100), _identical_except_sample("alpha", 1)])
    assert [r["agent_id"] for r in board] == ["alpha", "zeta"]  # agent_id asc, not valid_count desc
    by_id = {r["agent_id"]: r for r in board}
    # Confidence rides along as DISPLAY context only.
    assert by_id["alpha"]["low_sample"] is True
    assert by_id["zeta"]["low_sample"] is False


def test_rank_key_ignores_confidence_and_kelly_is_absent() -> None:
    # The rank key reads CLV/Brier/drawdown/action_count/agent_id only — never the confidence fields.
    base = _identical_except_sample("a", 1)
    key_before = _rank_key(base)
    mutated = {**base, "valid_count": 999, "clv_confidence": "high", "low_sample": False, "sample_size": 999}
    assert _rank_key(mutated) == key_before
    # Kelly is NEVER a leaderboard metric/score/rank field.
    board = leaderboard([_row("x", 5.0, 12)])
    assert all("kelly" not in r for r in board)
