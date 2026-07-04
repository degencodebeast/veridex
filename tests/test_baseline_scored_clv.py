"""FU-3 (TDD, strict RED→GREEN) — baselines are SCORED through the same law/scoring seam as drift.

Before FU-3 the four baselines emitted rows with ``clv_bps`` HARDCODED ``None`` (evaluation.py:380):
they were null references, not scored competitors, so no "drift beats baselines on CLV" comparison
was possible. FU-3 wraps each ACTING baseline as an orchestrator ``Agent`` and runs it through the
SAME pre_match scored path drift uses (``run_backtest`` → ``CompetitionRun.finalize`` →
``veridex.law.recompute`` vs the per-market CON-040 kickoff close). ``no_trade`` always abstains and
stays null; a pick with no valid close FAILS CLOSED (never a fabricated CLV).

All tests are OFFLINE and synthetic: the pack loaders (evaluation + runner) and the pack content-hash
read are patched to return hand-built states with EXACT ``stable_prob_bps`` so the expected CLV can be
asserted to the bps against the CON-040 close.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.test_multi_fixture_eval import _proto
from veridex.backtest.evaluation import produce_results_by_fixture, run_multi_fixture_evaluation
from veridex.backtest.pre_match import plan_pre_match_backtest
from veridex.ingest.marketstate import MarketState
from veridex.runtime.schemas import SportsActionType

_ACTING = ["favorite", "threshold_move", "seeded_random"]
_ALL_BASELINES = ["no_trade", *_ACTING]
_KEY = "1X2||"


def _tick(fixture_id: int, *, tick_seq: int, ts: int, phase: int, home_bps: int) -> MarketState:
    """A usable 1X2 tick (Home prob in bps; Away is the complement; flat 2.0 decimal prices)."""
    return MarketState(
        fixture_id=fixture_id,
        tick_seq=tick_seq,
        ts=ts,
        phase=phase,
        markets={
            _KEY: {
                "stable_prob_bps": {"Home": home_bps, "Away": 10_000 - home_bps},
                "stable_price": {"Home": 2.0, "Away": 2.0},
                "suspended": False,
            }
        },
        scores={},
    )


def _patch_pack(monkeypatch: pytest.MonkeyPatch, states: list[MarketState]) -> None:
    """Make BOTH pack loaders (evaluation + runner) and the runner's content-hash read synthetic.

    The producer loads states via ``evaluation.load_pack_marketstates`` (for the window allowlist)
    and each acting baseline is now scored by ``run_backtest`` which independently loads via
    ``runner.load_pack_marketstates`` and reads ``runner._pack_content_hash`` — all three are patched
    so the same synthetic tape drives the whole path without a real on-disk pack.
    """
    monkeypatch.setattr(
        "veridex.backtest.evaluation.load_pack_marketstates", lambda pack_dir, fid, **kw: states
    )
    monkeypatch.setattr(
        "veridex.backtest.runner.load_pack_marketstates", lambda pack_dir, fid, **kw: states
    )
    monkeypatch.setattr("veridex.backtest.runner._pack_content_hash", lambda pack_dir: "deadbeefcafe0000")


def _full_match_rising() -> list[MarketState]:
    """Full-match pack: Home drifts UP pre-kickoff, then a degenerate in-running final (kickoff)."""
    return [
        _tick(1, tick_seq=0, ts=100, phase=0, home_bps=6_000),  # favorite → Home @ entry 6000
        _tick(1, tick_seq=1, ts=110, phase=0, home_bps=6_500),
        _tick(1, tick_seq=2, ts=120, phase=0, home_bps=7_000),  # last pre-kickoff → CON-040 close 7000
        MarketState(fixture_id=1, tick_seq=3, ts=200, phase=1, markets={}, scores={}),  # kickoff
    ]


# ------------------------------------------------------------------------------------------
# 1) An acting baseline that fires now carries a REAL numeric clv_bps vs the CON-040 close.
# ------------------------------------------------------------------------------------------


def test_baselines_are_scored_clv_not_null(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """favorite backs Home @6000 (first pre-kickoff tick); scored vs the folded close Home=7000.

    RED against ``clv_bps=None``: the favorite row must now carry the EXACT +1000 bps CLV
    (7000 − 6000) recomputed against the per-market CON-040 close — not a null reference.
    """
    _patch_pack(monkeypatch, _full_match_rising())
    proto = _proto(fixture_ids=[1], strategy_configs=[], baselines=["favorite"])

    results = asyncio.run(produce_results_by_fixture(proto, packs={1: tmp_path}))

    fav_scored = [r for r in results[1] if r["kind"] == "favorite" and r["clv_bps"] is not None]
    assert fav_scored, "favorite must produce a REAL scored clv_bps (was hardcoded None)"
    assert len(fav_scored) == 1, "favorite takes ONE position → exactly one scored row"
    row = fav_scored[0]
    assert row["action"] == "FOLLOW_MOMENTUM"
    assert row["market"] == _KEY
    assert row["clv_bps"] == 1000  # 7000 (close Home) − 6000 (entry Home), via recompute


# ------------------------------------------------------------------------------------------
# 2) baseline_comparison is populated: drift + each acting baseline with comparable scored CLV.
# ------------------------------------------------------------------------------------------


def test_baseline_comparison_populated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The calibration report's ``baseline_comparison`` is no longer ``{}`` — it carries drift and
    each baseline with a comparable bucket (avg CLV + scored count)."""
    _patch_pack(monkeypatch, _full_match_rising())
    proto = _proto(fixture_ids=[1], strategy_configs=["cumulative-drift"], baselines=_ALL_BASELINES)

    results = asyncio.run(produce_results_by_fixture(proto, packs={1: tmp_path}))
    out = run_multi_fixture_evaluation(proto, results_by_fixture=results, cadence_ok=True)

    comparison = out["calibration"].baseline_comparison
    assert comparison != {}, "baseline_comparison must be populated (was hardcoded {})"
    assert "cumulative-drift" in comparison, "drift must appear in the comparison"
    assert set(_ALL_BASELINES) <= set(comparison), f"every baseline must appear; got {set(comparison)}"
    # favorite's single scored pick is +1000 bps; the bucket carries the avg + a scored count of 1.
    assert comparison["favorite"].avg_clv_bps == 1000.0
    # no_trade never scores → its comparison bucket has NO avg CLV (all abstentions).
    assert comparison["no_trade"].avg_clv_bps is None


# ------------------------------------------------------------------------------------------
# 3) no_trade never gets a numeric CLV — it abstains (WAIT), stays null.
# ------------------------------------------------------------------------------------------


def test_no_trade_stays_abstention_null(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_pack(monkeypatch, _full_match_rising())
    proto = _proto(fixture_ids=[1], strategy_configs=[], baselines=["no_trade"])

    results = asyncio.run(produce_results_by_fixture(proto, packs={1: tmp_path}))

    no_trade_rows = [r for r in results[1] if r["kind"] == "no_trade"]
    assert no_trade_rows, "no_trade must still emit rows"
    for row in no_trade_rows:
        assert row["action"] == "WAIT"
        assert row["clv_bps"] is None  # abstention → never a numeric (never a fabricated) CLV


# ------------------------------------------------------------------------------------------
# 4) FU-1 NIT (now load-bearing): the pre-match baseline ACTION is pinned + D2-consistent.
#    The baseline decides over the decision series ONLY — it can NOT peek at the held-out close.
# ------------------------------------------------------------------------------------------


def _pre_match_only_flip() -> list[MarketState]:
    """Pre-match-only pack where the FAVORITE flips across ticks: Home leads early, Away at the close.

    decision_states hold out the LAST tick as the close (D2). A no-lookahead favorite decides from
    the first decision tick (Home leads) → backs Home; a lookahead bug would peek at the held-out
    close (Away leads) and back Away.
    """
    return [
        _tick(2, tick_seq=0, ts=100, phase=0, home_bps=6_000),  # decision tick: Home leads → back Home
        _tick(2, tick_seq=1, ts=110, phase=0, home_bps=5_000),  # decision tick
        _tick(2, tick_seq=2, ts=120, phase=0, home_bps=3_000),  # HELD-OUT close: Away leads (7000)
    ]


def test_pre_match_baseline_action_pinned(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Pin the exact emitted favorite action AND its scored CLV — proving the D2 decision/close split.

    Agent level: driven over ``plan.decision_states`` (close held out), favorite fires FOLLOW_MOMENTUM
    on Home (the early leader) — never Away (the held-out close leader).
    Producer level: the scored CLV is −3000 (close Home 3000 − entry Home 6000). A lookahead bug that
    entered Away would instead score +3000, so this bps value is the load-bearing no-lookahead pin.
    """
    from veridex.backtest.baseline_agents import baseline_agent

    states = _pre_match_only_flip()
    plan = plan_pre_match_backtest(states)
    # The held-out close is NOT a decision tick (D2): favorite can only see Home-leading ticks.
    assert [s.tick_seq for s in plan.decision_states] == [0, 1]

    agent = baseline_agent("favorite", seed=2)
    fired = None
    for state in plan.decision_states:
        action = asyncio.run(agent.decide(state))
        if action.type == SportsActionType.FOLLOW_MOMENTUM:
            fired = action
            break
    assert fired is not None, "favorite must fire on the decision series"
    assert fired.params["market_key"] == _KEY
    assert fired.params["side"] == "Home", "no-lookahead: decided from the first decision tick, not the close"

    _patch_pack(monkeypatch, states)
    proto = _proto(fixture_ids=[2], strategy_configs=[], baselines=["favorite"])
    results = asyncio.run(produce_results_by_fixture(proto, packs={2: tmp_path}))
    fav_scored = [r for r in results[2] if r["kind"] == "favorite" and r["clv_bps"] is not None]
    assert fav_scored and fav_scored[0]["clv_bps"] == -3000  # Home: 3000 (close) − 6000 (entry)


# ------------------------------------------------------------------------------------------
# 5) Regression: baselines do NOT perturb drift's rows (drift is scored in its own run, unchanged).
# ------------------------------------------------------------------------------------------


def test_drift_rows_unchanged_by_baselines(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Drift's score rows are byte-identical whether or not baselines are in the roster."""
    _patch_pack(monkeypatch, _full_match_rising())

    proto_solo = _proto(fixture_ids=[1], strategy_configs=["cumulative-drift"], baselines=[])
    proto_with = _proto(fixture_ids=[1], strategy_configs=["cumulative-drift"], baselines=_ALL_BASELINES)

    drift_solo = [r for r in asyncio.run(produce_results_by_fixture(proto_solo, packs={1: tmp_path}))[1]
                  if r["kind"] == "cumulative-drift"]
    drift_with = [r for r in asyncio.run(produce_results_by_fixture(proto_with, packs={1: tmp_path}))[1]
                  if r["kind"] == "cumulative-drift"]

    assert drift_solo == drift_with, "adding baselines must not change drift's scored rows"
