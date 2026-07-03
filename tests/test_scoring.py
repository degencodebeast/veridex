"""B6 — scoring + metric stack (REQ-106 / AC-106, gate CON-002, CON-007).

Strict TDD: every test here was watched RED (``veridex.scoring`` did not exist) before
``score_run`` was implemented, then GREEN.

The load-bearing invariant under test (codex B3 carry-forward): an action is *scored* IFF
``valid is True`` AND ``clv_bps`` is a real ``int`` (NOT the ``"pending"`` sentinel). WAIT
(``wait_unscored``) and live-pending (``pending_closing``) are valid abstentions — excluded from
the CLV means, NEVER counted as 0. Invalid actions (``valid is False``) are excluded even though
their ``clv_bps`` is the int ``0``. Scoring keys on ``valid`` + numeric ``clv_bps`` and NEVER
pattern-matches ``reason``.

Trust path (CON-007): ``veridex/scoring.py`` imports no LLM SDK — asserted by the import audit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from veridex.runtime.orchestrator import RunResult
from veridex.runtime.window import CLV_FIELD_WINDOW
from veridex.scoring import score_run

# ---------------------------------------------------------------------------
# Helpers — build score rows in the exact shape run_competition produces.
# ---------------------------------------------------------------------------


def _row(
    agent_id: str,
    tick_seq: int,
    clv_bps: int | str,
    valid: bool,
    *,
    reason: str = "",
    confidence: float | None = None,
    proof_mode: str = "reproducible",
    action_type: str = "FLAG_VALUE",
) -> dict[str, Any]:
    """One score row mirroring ``orchestrator.run_competition``'s emitted dict shape."""
    params: dict[str, Any] = {"market_key": "OU_2_5", "side": "over"}
    if confidence is not None:
        params["confidence"] = confidence
    return {
        "raw_prescore_hash": f"{agent_id}-{tick_seq}",
        "recomputed_edge_bps": clv_bps if isinstance(clv_bps, int) else 0,
        "agent_id": agent_id,
        "tick_seq": tick_seq,
        "proof_mode": proof_mode,
        "clv_bps": clv_bps,
        "valid": valid,
        "reason": reason,
        "kelly_fraction": 0.0,
        "raw_prescore": {"raw_action": {"type": action_type, "params": params}},
    }


def _run(score_rows: list[dict[str, Any]], *, source_mode: str = "replay") -> RunResult:
    """Wrap crafted score rows in a minimal RunResult (only the scoring inputs matter)."""
    agent_ids: list[str] = []
    proof_mode_map: dict[str, str] = {}
    for row in score_rows:
        if row["agent_id"] not in proof_mode_map:
            agent_ids.append(row["agent_id"])
            proof_mode_map[row["agent_id"]] = row["proof_mode"]
    return RunResult(
        run_id="run-test",
        source_mode=source_mode,
        agent_ids=agent_ids,
        run_events=[],
        score_rows=score_rows,
        evidence_hash="",
        proof_mode_map=proof_mode_map,
    )


def _window_row(
    agent_id: str,
    tick_seq: int,
    window_clv_bps: int,
    *,
    confidence: float | None = None,
    proof_mode: str = "reproducible",
) -> dict[str, Any]:
    """A scored fixed_duration/manual_stop window row (DEC-2D-1 shape from finalize).

    Mirrors ``orchestrator`` exactly: finalize renames a SCORED window row's numeric CLV out of
    ``clv_bps`` into ``window_clv_bps`` (``row[clv_field] = row.pop("clv_bps")``), so the row carries
    ``window_clv_bps`` and NO ``clv_bps`` key — it is NOT ``is_scored`` (no numeric ``clv_bps``).
    """
    row = _row(agent_id, tick_seq, window_clv_bps, True, reason="value_flag", confidence=confidence, proof_mode=proof_mode)
    row[CLV_FIELD_WINDOW] = row.pop("clv_bps")
    return row


def _by_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {r["agent_id"]: r for r in rows}


# ---------------------------------------------------------------------------
# 1 — two agents ranked by avg CLV (primary axis)
# ---------------------------------------------------------------------------


def test_ranks_two_agents_by_avg_clv() -> None:
    rows = score_run(
        _run(
            [
                _row("A", 0, 100, True),
                _row("A", 1, 200, True),  # A avg = 150
                _row("B", 0, 50, True),
                _row("B", 1, 60, True),  # B avg = 55
            ]
        )
    )
    assert [r["agent_id"] for r in rows] == ["A", "B"]
    assert [r["rank"] for r in rows] == [1, 2]
    out = _by_id(rows)
    assert out["A"]["avg_clv_bps"] == pytest.approx(150.0)
    assert out["A"]["total_clv_bps"] == 300
    assert out["B"]["avg_clv_bps"] == pytest.approx(55.0)
    assert out["A"]["proof_mode"] == "reproducible"


# ---------------------------------------------------------------------------
# 2 — scored IFF valid AND numeric clv: WAIT, live-pending AND invalid excluded
#     (each would change the mean if wrongly counted as 0)
# ---------------------------------------------------------------------------


def test_scored_iff_excludes_wait_pending_and_invalid() -> None:
    rows = score_run(
        _run(
            [
                _row("A", 0, 100, True),  # the ONE scored action
                _row("A", 1, "pending", True, reason="wait_unscored", action_type="WAIT"),
                _row("A", 2, "pending", True, reason="pending_closing"),
                _row("A", 3, 0, False, reason="closing_missing"),  # invalid: clv int 0 but valid False
            ]
        )
    )
    out = _by_id(rows)["A"]
    # If WAIT/pending/invalid were counted as 0, the mean would be 100/4 = 25.0, not 100.0.
    assert out["avg_clv_bps"] == pytest.approx(100.0)
    assert out["total_clv_bps"] == 100
    assert out["action_count"] == 1
    # valid_pct = law-accepted (valid is True) / total decisions = 3 / 4 -> 75%
    # (scored action + WAIT + live-pending are all valid; only the invalid row is excluded).
    assert out["valid_pct"] == pytest.approx(75.0)
    # valid_pct (law-acceptance) is a DISTINCT metric from scored coverage (action_count / total).
    coverage_pct = out["action_count"] / 4 * 100.0  # = 25.0
    assert out["valid_pct"] != pytest.approx(coverage_pct)


# ---------------------------------------------------------------------------
# 2b — DEC-2D-1 window CLV: a labeled aggregation, NEVER dropped, NEVER blended
#      into the true-CLV (leaderboard-rank) axis.
# ---------------------------------------------------------------------------


def test_windowed_run_scores_window_clv_not_dropped() -> None:
    # A fixed_duration/manual_stop run's scored rows carry numeric window_clv_bps (no clv_bps).
    # Before T10c these rows were SILENTLY DROPPED (is_scored is False for them), leaving the
    # metric stack with avg_clv_bps=None AND no window aggregate at all — "the worst middle".
    rows = score_run(_run([_window_row("A", 0, 100), _window_row("A", 1, 200)]))
    out = _by_id(rows)["A"]
    # Window CLV is aggregated under its OWN labeled fields — the mean of the window values.
    assert out["avg_window_clv_bps"] == pytest.approx(150.0)
    assert out["total_window_clv_bps"] == 300
    assert out["window_action_count"] == 2
    # True CLV (the leaderboard rank axis) is EMPTY for this run — window CLV never blended in.
    assert out["avg_clv_bps"] is None
    assert out["action_count"] == 0
    assert out["total_clv_bps"] == 0


def test_true_clv_run_has_empty_window_aggregate() -> None:
    # The reciprocal: a pre_match (true-CLV) run's window aggregate stays empty (None / 0) —
    # true CLV is NEVER counted as window CLV.
    rows = score_run(_run([_row("A", 0, 100, True), _row("A", 1, 200, True)]))
    out = _by_id(rows)["A"]
    assert out["avg_clv_bps"] == pytest.approx(150.0)
    assert out["action_count"] == 2
    assert out["avg_window_clv_bps"] is None
    assert out["total_window_clv_bps"] == 0
    assert out["window_action_count"] == 0


def test_window_abstentions_excluded_from_both_means() -> None:
    # pending_horizon (DEC-2D-2) and WAIT keep the "pending" sentinel — honest abstentions excluded
    # from BOTH the true-CLV mean AND the window-CLV mean (never scored as a numeric 0).
    rows = score_run(
        _run(
            [
                _window_row("A", 0, 80),  # the ONE scored window action
                _row("A", 1, "pending", True, reason="pending_horizon"),
                _row("A", 2, "pending", True, reason="wait_unscored", action_type="WAIT"),
            ]
        )
    )
    out = _by_id(rows)["A"]
    # If the two pending rows leaked in as 0, avg_window would be 80/3 ≈ 26.7, not 80.0.
    assert out["avg_window_clv_bps"] == pytest.approx(80.0)
    assert out["window_action_count"] == 1
    assert out["avg_clv_bps"] is None
    assert out["action_count"] == 0


# ---------------------------------------------------------------------------
# 3 — tie-breaker chain: total CLV -> Brier -> max drawdown -> agent_id
# ---------------------------------------------------------------------------


def test_tiebreaker_total_clv() -> None:
    rows = score_run(
        _run(
            [
                _row("A", 0, 100, True),
                _row("A", 1, 200, True),  # avg 150, total 300
                _row("B", 0, 150, True),  # avg 150, total 150
            ]
        )
    )
    assert [r["agent_id"] for r in rows] == ["A", "B"]  # equal avg -> higher total wins


def test_tiebreaker_brier_then_drawdown() -> None:
    # Equal avg (150) and equal total (300); Brier breaks the tie (lower better).
    rows = score_run(
        _run(
            [
                # A: confident & well-calibrated -> low Brier.
                _row("A", 0, 100, True, confidence=0.9),  # clv>0 -> outcome 1 -> (0.9-1)^2=0.01
                _row("A", 1, 200, True, confidence=0.9),  # 0.01  -> Brier 0.01
                # B: confident but mis-calibrated on a loser -> higher Brier.
                _row("B", 0, 400, True, confidence=0.9),  # clv>0 -> (0.9-1)^2 = 0.01
                _row("B", 1, -100, True, confidence=0.9),  # clv<=0 -> (0.9-0)^2 = 0.81 -> Brier 0.41
            ]
        )
    )
    assert [r["agent_id"] for r in rows] == ["A", "B"]


def test_tiebreaker_max_drawdown() -> None:
    # Equal avg (150), total (300), no Brier (no confidence); drawdown breaks the tie.
    rows = score_run(
        _run(
            [
                _row("A", 0, 100, True),
                _row("A", 1, 200, True),  # cumulative [100, 300] -> drawdown 0
                _row("B", 0, 400, True),
                _row("B", 1, -100, True),  # cumulative [400, 300] -> drawdown -100
            ]
        )
    )
    assert [r["agent_id"] for r in rows] == ["A", "B"]  # less-severe drawdown ranks first


def test_tiebreaker_stable_by_agent_id() -> None:
    # Fully identical metrics -> deterministic final tiebreak on agent_id ascending.
    rows = score_run(
        _run(
            [
                _row("zeta", 0, 100, True),
                _row("alpha", 0, 100, True),
            ]
        )
    )
    assert [r["agent_id"] for r in rows] == ["alpha", "zeta"]


# ---------------------------------------------------------------------------
# 4 — agents with no scored actions: avg None, ranked last
# ---------------------------------------------------------------------------


def test_unscored_agent_ranks_last_with_none_avg() -> None:
    rows = score_run(
        _run(
            [
                _row("waiter", 0, "pending", True, reason="wait_unscored", action_type="WAIT"),
                _row("scorer", 0, 10, True),
            ]
        )
    )
    out = _by_id(rows)
    assert out["scorer"]["rank"] == 1
    assert out["waiter"]["rank"] == 2
    assert out["waiter"]["avg_clv_bps"] is None
    assert out["waiter"]["action_count"] == 0
    assert out["waiter"]["total_clv_bps"] == 0


# ---------------------------------------------------------------------------
# 5 — sim_pnl + max_drawdown from the per-action cumulative series (tick order)
# ---------------------------------------------------------------------------


def test_sim_pnl_and_drawdown_from_series() -> None:
    # clv ordered by tick_seq: [100, -300, 50] -> cumulative [100, -200, -150].
    rows = score_run(
        _run(
            [
                _row("A", 2, 50, True),  # deliberately out of order to prove tick-seq sorting
                _row("A", 0, 100, True),
                _row("A", 1, -300, True),
            ]
        )
    )
    out = _by_id(rows)["A"]
    assert out["sim_pnl"] == -150  # final cumulative == total CLV (closing-referenced proxy)
    assert out["total_clv_bps"] == -150
    assert out["max_drawdown"] == pytest.approx(-300.0)  # peak 100 -> trough -200


def test_drawdown_zero_when_monotonic() -> None:
    rows = score_run(_run([_row("A", 0, 10, True), _row("A", 1, 20, True)]))
    assert _by_id(rows)["A"]["max_drawdown"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 6 — Brier present IFF confidence emitted on scored actions
# ---------------------------------------------------------------------------


def test_brier_present_iff_confidence() -> None:
    rows = score_run(
        _run(
            [
                _row("withconf", 0, 100, True, confidence=0.8),  # outcome 1 -> (0.8-1)^2 = 0.04
                _row("withconf", 1, -50, True, confidence=0.3),  # outcome 0 -> (0.3-0)^2 = 0.09
                _row("noconf", 0, 100, True),  # no confidence
                _row("noconf", 1, -50, True),
            ]
        )
    )
    out = _by_id(rows)
    assert out["withconf"]["brier"] == pytest.approx((0.04 + 0.09) / 2)
    assert out["noconf"]["brier"] is None


def test_brier_ignores_confidence_on_unscored_actions() -> None:
    # A WAIT with a confidence value must NOT contribute to Brier (it isn't scored).
    rows = score_run(
        _run(
            [
                _row("A", 0, 100, True, confidence=1.0),  # outcome 1 -> (1-1)^2 = 0
                _row("A", 1, "pending", True, reason="wait_unscored", action_type="WAIT", confidence=0.0),
            ]
        )
    )
    assert _by_id(rows)["A"]["brier"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 7 — deterministic / reproducible
# ---------------------------------------------------------------------------


def test_reproducible() -> None:
    run = _run(
        [
            _row("A", 0, 100, True),
            _row("B", 0, 200, True),
            _row("A", 1, 50, True),
        ]
    )
    assert score_run(run) == score_run(run)


# ---------------------------------------------------------------------------
# 8 — every output row carries the full metric-stack contract
# ---------------------------------------------------------------------------


def test_row_has_full_metric_stack_keys() -> None:
    rows = score_run(_run([_row("A", 0, 100, True)]))
    assert set(rows[0]) == {
        "agent_id",
        "avg_clv_bps",
        "total_clv_bps",
        "sim_pnl",
        "brier",
        "max_drawdown",
        "action_count",
        "valid_pct",
        "valid_count",
        "avg_window_clv_bps",
        "total_window_clv_bps",
        "window_action_count",
        "rank",
        "proof_mode",
    }


# ---------------------------------------------------------------------------
# 9 — end-to-end against the real orchestrator output shape
# ---------------------------------------------------------------------------


def test_end_to_end_from_run_competition() -> None:
    import asyncio

    from veridex.ingest.marketstate import MarketState
    from veridex.runtime.orchestrator import deterministic_agent, run_competition

    def _ms(prob_bps: dict[str, int], *, tick_seq: int) -> MarketState:
        return MarketState(
            fixture_id=1,
            tick_seq=tick_seq,
            ts=1000 + tick_seq,
            phase=2,
            markets={"OU_2_5": {"stable_prob_bps": dict(prob_bps), "stable_price": {"over": 1.6}, "suspended": False}},
            scores={},
        )

    states = [_ms({"over": 6000}, tick_seq=0), _ms({"over": 6300}, tick_seq=1)]
    run = asyncio.run(
        run_competition(states, [deterministic_agent("d1"), deterministic_agent("d2")], source_mode="replay")
    )
    rows = score_run(run)
    assert {r["agent_id"] for r in rows} == {"d1", "d2"}
    assert sorted(r["rank"] for r in rows) == [1, 2]
    for r in rows:
        assert r["proof_mode"] == "reproducible"
        assert r["action_count"] >= 1  # FLAG_VALUE at the entry tick scored vs the closing tick


# ---------------------------------------------------------------------------
# 10 — import-audit clean over veridex/scoring.py (CON-007 trust path)
# ---------------------------------------------------------------------------


def test_scoring_import_audit_clean() -> None:
    import veridex.scoring as scoring_mod
    from veridex.verifier.import_audit import assert_no_llm_imports

    assert_no_llm_imports(Path(scoring_mod.__file__))  # raises AssertionError if dirty
