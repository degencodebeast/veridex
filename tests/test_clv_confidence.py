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


def _row_scored(agent_id: str, *, action_count: int, valid_count: int) -> dict:
    # A PHYSICALLY-VALID leaderboard row: valid_count >= action_count (every scored pick is a valid
    # decision, PLUS any valid WAIT abstentions). avg CLV is fixed at 10.0 so the CLV rank axis is
    # tied across agents; only the SCORED sample (action_count) and the WAIT count (valid_count) vary.
    assert valid_count >= action_count, "a scored pick is always a valid decision: valid_count >= action_count"
    return {
        "agent_id": agent_id,
        "avg_clv_bps": 10.0,
        "total_clv_bps": 10 * action_count,  # keeps the pooled avg == 10.0 for any action_count
        "sim_pnl": 10 * action_count,
        "brier": None,
        "max_drawdown": 0.0,
        "action_count": action_count,
        "valid_pct": 100.0,
        "valid_count": valid_count,
        "proof_mode": "reproducible",
    }


def _waits_only_row(agent_id: str, valid_count: int) -> dict:
    # ZERO scored picks (action_count == 0) but many law-valid WAIT abstentions. The honest CLV
    # confidence for this row is LOW (no scored sample) — NEVER 'high' off the WAIT count.
    return {
        "agent_id": agent_id,
        "avg_clv_bps": None,
        "total_clv_bps": 0,
        "sim_pnl": 0,
        "brier": None,
        "max_drawdown": 0.0,
        "action_count": 0,
        "valid_pct": 100.0,
        "valid_count": valid_count,
        "proof_mode": "reproducible",
    }


def test_leaderboard_confidence_keys_off_scored_picks_not_valid_waits() -> None:
    # HONESTY (same overclaim the report fix closed): a leaderboard agent that abstained (valid WAIT)
    # on 500 decisions and scored ZERO picks must read LOW confidence, not 'high'. Confidence keys off
    # the scored-pick count (action_count), never valid_count.
    board = leaderboard([_waits_only_row("waiter", 500)])
    row = board[0]
    assert row["valid_count"] == 500  # law-acceptance is still reported (a distinct, honest metric)
    assert row["clv_confidence"] == "low"  # NOT 'high' — no scored sample backs a CLV claim
    assert row["low_sample"] is True


def test_rank_order_is_clv_only_not_sample_size() -> None:
    # SEC-005: two agents IDENTICAL on every rank-key field (incl. action_count) but with different
    # valid_count (WAIT abstentions) rank by the deterministic agent_id tiebreak — NEVER by the
    # larger law-valid/WAIT sample. Physically valid now (valid_count >= action_count).
    board = leaderboard(
        [
            _row_scored("zeta", action_count=10, valid_count=100),
            _row_scored("alpha", action_count=10, valid_count=10),
        ]
    )
    assert [r["agent_id"] for r in board] == ["alpha", "zeta"]  # agent_id asc, not valid_count desc
    by_id = {r["agent_id"]: r for r in board}
    # Confidence no longer keys off valid_count: both have the SAME scored count (10) → SAME tier,
    # even though zeta logged 100 law-valid decisions to alpha's 10. The WAIT count moves NEITHER
    # rank NOR confidence.
    assert by_id["alpha"]["clv_confidence"] == by_id["zeta"]["clv_confidence"]
    assert by_id["alpha"]["low_sample"] is False
    assert by_id["zeta"]["low_sample"] is False


def test_rank_key_ignores_confidence_and_kelly_is_absent() -> None:
    # The rank key reads CLV/Brier/drawdown/action_count/agent_id only — never the confidence fields.
    base = _row_scored("a", action_count=10, valid_count=10)
    key_before = _rank_key(base)
    mutated = {**base, "valid_count": 999, "clv_confidence": "high", "low_sample": False, "sample_size": 999}
    assert _rank_key(mutated) == key_before
    # Kelly is NEVER a leaderboard metric/score/rank field.
    board = leaderboard([_row("x", 5.0, 12)])
    assert all("kelly" not in r for r in board)
