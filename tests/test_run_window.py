"""T7 — RunWindow contract + windowed-CLV honesty semantics (DEC-2D-1/2, REQ-2D-104, §4.1).

Strict TDD: every test here was watched RED (``veridex.runtime.window`` did not exist and
``CompetitionRun.feed_closing`` / ``finalize(window=...)`` were unimplemented) before the code
was written, then GREEN.

The three load-bearing honesty invariants under test:

  1. **mode label never lies** — ``pre_match`` windows reconstruct the real close → TRUE CLV
     (``clv_bps``); ``fixed_duration``/``manual_stop`` close on the line AT window end → WINDOW CLV
     (``window_clv_bps``), named distinctly so downstream can never mistake it for true CLV.
  2. **pending_horizon (DEC-2D-2)** — an action entered within ``min_clv_horizon_s`` of close is
     excluded from CLV means like WAIT, via the EXISTING ``"pending"`` sentinel (not a numeric 0),
     so ``scoring.py`` drops it for free.
  3. **window=None is byte-identical** — the legacy path is untouched (the golden is the real gate).
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from veridex.ingest.marketstate import MarketState
from veridex.runtime.orchestrator import (
    Agent,
    CompetitionRun,
    RunResult,
    deterministic_agent,
    run_competition,
)
from veridex.runtime.schemas import AgentAction, SportsActionType
from veridex.runtime.window import (
    RunWindow,
    clv_field_name,
    is_pending_horizon,
)

KEY = "OU_2_5"


# ---------------------------------------------------------------------------
# Fixtures / helpers — mirror the orchestrator's MarketState + Agent shapes.
# ---------------------------------------------------------------------------


def _market(prob_bps: dict[str, int]) -> dict:
    return {"stable_prob_bps": dict(prob_bps), "stable_price": {"over": 1.6, "under": 2.4}, "suspended": False}


def _ms(prob_bps: dict[str, int], *, tick_seq: int, ts: int) -> MarketState:
    return MarketState(fixture_id=1, tick_seq=tick_seq, ts=ts, phase=2, markets={KEY: _market(prob_bps)}, scores={})


def _flag_agent(agent_id: str = "flagger") -> Agent:
    """An agent that always FLAG_VALUEs 'over' on the OU market (a scored, numeric-CLV action)."""

    async def decide(market_state: MarketState) -> AgentAction:
        return AgentAction(type=SportsActionType.FLAG_VALUE, params={"market_key": KEY, "side": "over"})

    return Agent(agent_id=agent_id, proof_mode="reproducible", decide=decide)


def _window(end_rule: str, **kw: object) -> RunWindow:
    base: dict[str, object] = {"window_id": "win-1", "fixture_id": 1, "market_allowlist": ["OU"], "end_rule": end_rule}
    base.update(kw)
    return RunWindow(**base)  # type: ignore[arg-type]


# ===========================================================================
# 1 — RunWindow validation: duration_s required IFF fixed_duration
# ===========================================================================


def test_fixed_duration_requires_duration_s() -> None:
    with pytest.raises(ValidationError):
        RunWindow(window_id="w", fixture_id=1, market_allowlist=["OU"], end_rule="fixed_duration")


def test_fixed_duration_with_duration_s_is_valid() -> None:
    w = RunWindow(window_id="w", fixture_id=1, market_allowlist=["OU"], end_rule="fixed_duration", duration_s=300)
    assert w.duration_s == 300


def test_non_fixed_duration_forbids_duration_s() -> None:
    # "iff" is strict: duration_s is meaningless (and misleading) on a pre_match / manual_stop window.
    with pytest.raises(ValidationError):
        RunWindow(window_id="w", fixture_id=1, market_allowlist=["OU"], end_rule="pre_match", duration_s=300)
    with pytest.raises(ValidationError):
        RunWindow(window_id="w", fixture_id=1, market_allowlist=["OU"], end_rule="manual_stop", duration_s=300)


def test_window_defaults() -> None:
    w = RunWindow(window_id="w", fixture_id=1, market_allowlist=["OU"], end_rule="pre_match")
    assert w.min_clv_horizon_s == 60  # DEC-2D-2 default
    assert w.started_ts is None  # None until the first accepted tick stamps it


# ===========================================================================
# 2 — clv_field_name: pre_match -> clv_bps, else window_clv_bps
# ===========================================================================


def test_clv_field_name_pre_match_is_true_clv() -> None:
    assert clv_field_name("pre_match") == "clv_bps"


def test_clv_field_name_fixed_and_manual_are_window_clv() -> None:
    assert clv_field_name("fixed_duration") == "window_clv_bps"
    assert clv_field_name("manual_stop") == "window_clv_bps"


# ===========================================================================
# 3 — is_pending_horizon: `< min` is pending (boundary documented)
# ===========================================================================


def test_is_pending_horizon_within_horizon_true() -> None:
    # entry 30s before close, min 60 -> 30 < 60 -> True
    assert is_pending_horizon(entry_ts=970, window_end_ts=1000, min_clv_horizon_s=60) is True


def test_is_pending_horizon_outside_horizon_false() -> None:
    # entry 90s before close, min 60 -> 90 < 60 -> False
    assert is_pending_horizon(entry_ts=910, window_end_ts=1000, min_clv_horizon_s=60) is False


def test_is_pending_horizon_boundary_is_exclusive() -> None:
    # exactly at horizon (60s before), min 60 -> 60 < 60 -> False (the boundary is NOT pending).
    assert is_pending_horizon(entry_ts=940, window_end_ts=1000, min_clv_horizon_s=60) is False
    # an entry AT close (0s of runway) is always pending.
    assert is_pending_horizon(entry_ts=1000, window_end_ts=1000, min_clv_horizon_s=60) is True


# ===========================================================================
# 4 — pending_horizon row shape + scoring.py exclusion (via existing "pending")
# ===========================================================================


async def test_pending_horizon_row_shape_and_scoring_exclusion() -> None:
    # pre_match window; closing supplied via feed_closing. tick0 is far from close (scored),
    # tick1 is 30s from close (< 60) -> pending_horizon.
    run = CompetitionRun([_flag_agent()], source_mode="replay", run_id="ph-1")
    await run.feed(_ms({"over": 6000}, tick_seq=0, ts=1000))  # far from close -> scored
    await run.feed(_ms({"over": 6300}, tick_seq=1, ts=1970))  # 30s before close -> pending_horizon
    await run.feed_closing(_ms({"over": 6600}, tick_seq=2, ts=2000))  # window_end_ts = 2000
    result = await run.finalize(window=_window("pre_match", min_clv_horizon_s=60))

    by_tick = {r["tick_seq"]: r for r in result.score_rows}
    scored = by_tick[0]
    horizoned = by_tick[1]

    # tick0: scored true CLV = 6600 - 6000 = 600
    assert scored["clv_bps"] == 600
    assert scored["valid"] is True

    # tick1: PINNED pending_horizon shape — EXACTLY these three field values.
    assert horizoned["valid"] is True
    assert horizoned["reason"] == "pending_horizon"
    assert horizoned["clv_bps"] == "pending"

    # scoring.py excludes the pending_horizon row from the CLV mean via the existing "pending" path,
    # exactly like a WAIT: only tick0 (600) enters the mean.
    from veridex.scoring import score_run

    metrics = {m["agent_id"]: m for m in score_run(result)}["flagger"]
    assert metrics["avg_clv_bps"] == pytest.approx(600.0)  # NOT 600/2 = 300 (pending excluded, not 0)
    assert metrics["action_count"] == 1
    # pending_horizon is a VALID abstention (like WAIT) -> counts toward valid_pct.
    assert metrics["valid_pct"] == pytest.approx(100.0)


# ===========================================================================
# 5 — window_clv naming: fixed_duration -> window_clv_bps (no clv_bps); pre_match -> clv_bps
# ===========================================================================


async def test_fixed_duration_rows_use_window_clv_bps_and_no_clv_bps() -> None:
    # fixed_duration: close = last fed snapshot. Small horizon so tick0 is scored (not pending).
    run = CompetitionRun([_flag_agent()], source_mode="replay", run_id="wc-1")
    await run.feed(_ms({"over": 6000}, tick_seq=0, ts=1000))
    await run.feed(_ms({"over": 6300}, tick_seq=1, ts=1100))  # window_end_ts = 1100
    result = await run.finalize(window=_window("fixed_duration", duration_s=100, min_clv_horizon_s=10))

    by_tick = {r["tick_seq"]: r for r in result.score_rows}
    scored = by_tick[0]  # 1100 - 1000 = 100 >= 10 -> scored window CLV
    # window CLV = closing(6300) - entry(6000) = 300, named window_clv_bps; clv_bps is GONE.
    assert scored["window_clv_bps"] == 300
    assert "clv_bps" not in scored


async def test_pending_horizon_wins_over_window_clv_rename_in_fixed_duration() -> None:
    # Precedence pin: in a fixed_duration window, a horizon'd row must come out clv_bps == "pending"
    # (the pending_horizon override) and NOT window_clv_bps — the if/elif guarantees pending_horizon
    # takes precedence over the window_clv rename. The final tick (entry AT close, 0s runway) is
    # always within any positive horizon, so it is the horizon'd row here.
    run = CompetitionRun([_flag_agent()], source_mode="replay", run_id="prec-1")
    await run.feed(_ms({"over": 6000}, tick_seq=0, ts=1000))
    await run.feed(_ms({"over": 6300}, tick_seq=1, ts=1100))  # window_end_ts = 1100; entry AT close
    result = await run.finalize(window=_window("fixed_duration", duration_s=100, min_clv_horizon_s=60))

    horizoned = {r["tick_seq"]: r for r in result.score_rows}[1]
    # pending_horizon WINS: the row carries the "pending" sentinel under clv_bps, never window_clv_bps.
    assert horizoned["reason"] == "pending_horizon"
    assert horizoned["clv_bps"] == "pending"
    assert "window_clv_bps" not in horizoned


async def test_pre_match_rows_use_clv_bps_not_window_clv_bps() -> None:
    run = CompetitionRun([_flag_agent()], source_mode="replay", run_id="pm-1")
    await run.feed(_ms({"over": 6000}, tick_seq=0, ts=1000))
    await run.feed_closing(_ms({"over": 6600}, tick_seq=1, ts=2000))
    result = await run.finalize(window=_window("pre_match", min_clv_horizon_s=60))

    scored = {r["tick_seq"]: r for r in result.score_rows}[0]
    assert scored["clv_bps"] == 600
    assert "window_clv_bps" not in scored


# ===========================================================================
# 6 — window=None is byte-identical to the legacy (no-window) path
# ===========================================================================


async def test_finalize_window_none_equals_no_window_arg() -> None:
    states = [_ms({"over": 6000}, tick_seq=0, ts=1000), _ms({"over": 6300}, tick_seq=1, ts=1100)]

    run_a = CompetitionRun([_flag_agent()], source_mode="replay", run_id="eq")
    for s in states:
        await run_a.feed(s)
    res_none = await run_a.finalize(window=None)

    run_b = CompetitionRun([_flag_agent()], source_mode="replay", run_id="eq")
    for s in states:
        await run_b.feed(s)
    res_default = await run_b.finalize()

    assert isinstance(res_none, RunResult)
    assert res_none == res_default


async def test_window_none_matches_batch_run_competition() -> None:
    # The batch wrapper passes no window; a window=None finalize must reproduce it byte-for-byte.
    states = [_ms({"over": 6000}, tick_seq=0, ts=1000), _ms({"over": 6300}, tick_seq=1, ts=1100)]
    batch = await run_competition(states, [deterministic_agent()], source_mode="replay", run_id="b1")

    run = CompetitionRun([deterministic_agent()], source_mode="replay", run_id="b1")
    for s in states:
        await run.feed(s)
    incremental = await run.finalize(window=None)

    assert incremental == batch


# ===========================================================================
# 7 — feed_closing: one tick event, no decisions, wins closing, sealed, guarded
# ===========================================================================


async def test_feed_closing_emits_one_tick_no_decision_or_error() -> None:
    run = CompetitionRun([_flag_agent()], source_mode="replay", run_id="fc-1")
    await run.feed(_ms({"over": 6000}, tick_seq=0, ts=1000))
    result = await run.finalize()  # no window needed to inspect the event log
    # Re-run with a closing feed to compare the event tally.

    run2 = CompetitionRun([_flag_agent()], source_mode="replay", run_id="fc-2")
    await run2.feed(_ms({"over": 6000}, tick_seq=0, ts=1000))
    await run2.feed_closing(_ms({"over": 6600}, tick_seq=1, ts=2000))
    result2 = await run2.finalize()

    ticks = [e for e in result2.run_events if e["event_type"] == "tick"]
    decisions = [e for e in result2.run_events if e["event_type"] == "decision"]
    errors = [e for e in result2.run_events if e["event_type"] == "error"]

    # feed_closing added exactly ONE tick event beyond the single feed().
    assert len(ticks) == len([e for e in result.run_events if e["event_type"] == "tick"]) + 1
    # NO decision/error event was gathered for the closing tick (only tick0's flag decision exists).
    assert len(decisions) == 1
    assert errors == []


async def test_feed_closing_snapshot_wins_closing_line() -> None:
    run = CompetitionRun([_flag_agent()], source_mode="replay", run_id="fc-3")
    await run.feed(_ms({"over": 6000}, tick_seq=0, ts=1000))
    await run.feed_closing(_ms({"over": 6600}, tick_seq=1, ts=2000))  # later snapshot wins as closing
    result = await run.finalize()

    scored = {r["tick_seq"]: r for r in result.score_rows}[0]
    # CLV computed against the feed_closing snapshot: 6600 - 6000 = 600.
    assert scored["clv_bps"] == 600


async def test_feed_closing_event_is_inside_sealed_run_events() -> None:
    run = CompetitionRun([_flag_agent()], source_mode="replay", run_id="fc-4")
    await run.feed(_ms({"over": 6000}, tick_seq=0, ts=1000))
    await run.feed_closing(_ms({"over": 6600}, tick_seq=1, ts=2000))
    result = await run.finalize()

    # The closing snapshot (over=6600) appears as a sealed tick event -> evidence-hash covered.
    closing_ticks = [
        e
        for e in result.run_events
        if e["event_type"] == "tick" and e.get("state_snapshot_json") and json.loads(e["state_snapshot_json"])["ts"] == 2000
    ]
    assert len(closing_ticks) == 1


async def test_feed_closing_after_finalize_raises() -> None:
    run = CompetitionRun([_flag_agent()], source_mode="replay", run_id="fc-5")
    await run.feed(_ms({"over": 6000}, tick_seq=0, ts=1000))
    await run.finalize()
    with pytest.raises(RuntimeError):
        await run.feed_closing(_ms({"over": 6600}, tick_seq=1, ts=2000))


# ===========================================================================
# 8 — started_ts stamped from the first fed tick + manifest round-trip
# ===========================================================================


async def test_started_ts_stamped_from_first_fed_tick_and_evidence_derived() -> None:
    window = _window("manual_stop")
    assert window.started_ts is None

    run = CompetitionRun([_flag_agent()], source_mode="replay", run_id="st-1")
    await run.feed(_ms({"over": 6000}, tick_seq=0, ts=1500))  # FIRST accepted tick
    await run.feed(_ms({"over": 6300}, tick_seq=1, ts=1600))
    result = await run.finalize(window=window)

    assert window.started_ts == 1500  # stamped from the first fed tick's ts
    # Evidence-derived: it equals the ts inside the first sealed tick event (what a manifest
    # coverage-window builder reads), not a wall-clock guess.
    first_tick = min((e for e in result.run_events if e["event_type"] == "tick"), key=lambda e: e["sequence_no"])
    assert json.loads(first_tick["state_snapshot_json"])["ts"] == window.started_ts


async def test_started_ts_not_overwritten_when_preset() -> None:
    window = _window("manual_stop", started_ts=999)
    run = CompetitionRun([_flag_agent()], source_mode="replay", run_id="st-2")
    await run.feed(_ms({"over": 6000}, tick_seq=0, ts=1500))
    await run.finalize(window=window)
    assert window.started_ts == 999  # a caller-supplied start is preserved, never clobbered


async def test_window_id_round_trips_into_manifest() -> None:
    from veridex.scoring import score_run
    from veridex.verifier.recompute import manifest_from_run, recompute_score_root

    window = _window("manual_stop")
    run = CompetitionRun([_flag_agent()], source_mode="replay", run_id="mani-1")
    await run.feed(_ms({"over": 6000}, tick_seq=0, ts=1500))
    await run.feed(_ms({"over": 6300}, tick_seq=1, ts=1600))
    result = await run.finalize(window=window)

    assert window.started_ts == 1500
    scores = score_run(result)
    manifest = manifest_from_run(result, fixture_or_window_id=window.window_id, score_root=recompute_score_root(scores))
    assert manifest["fixture_or_window_id"] == window.window_id
