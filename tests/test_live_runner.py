"""T8 — the windowed LIVE RUNNER shell over the incremental core (REQ-2D-103/104).

Strict TDD: every test here is OFFLINE. The live stream is an injected async iterator and the
CON-040 closing fetch is an injected ``fetch_updates`` coroutine — ZERO network, ZERO creds, ZERO
LLM SDK. What these tests pin is the SHELL + the honesty of the closing-line handling:

  1. **pre_match end detection** — the in-running (kickoff) tick TERMINATES the window and is NOT
     fed; every prior pre-kickoff tick was fed.
  2. **fixture filter** — a tick for another ``fixture_id`` is dropped (never fed).
  3. **market allowlist** — markets whose key does not match an allowlist prefix are filtered out
     of the fed snapshot (never scored).
  4. **fixed_duration end** — a tick whose ``ts`` is past ``started_ts + duration_s`` terminates the
     window and is NOT fed.
  5. **CLOSING DIVERGENCE (Codex-required AC)** — when the reconstructed ``/odds/updates`` close
     differs from the last stream tick, pre_match CLV is computed against the RECONSTRUCTED close
     (sealed via ``feed_closing``), NOT the stream tick.
  6. **fetch-failure degrade** — a raising/empty ``fetch_updates`` NEVER fabricates a close: the run
     still finalizes with ``window_clv_bps`` (not true ``clv_bps``) and an ops marker
     ``closing_source: "stream_observed_fallback"`` that is NOT inside the sealed evidence.
  7. **seal-once / no-card-before-seal** — the proof card is built strictly AFTER ``finalize``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from veridex.ingest.marketstate import MarketState
from veridex.runtime.live_runner import LiveRunResult, run_live_window
from veridex.runtime.orchestrator import Agent
from veridex.runtime.schemas import AgentAction, SportsActionType
from veridex.runtime.window import RunWindow

# The market key the TxLINE normalizer derives for SuperOddsType=OU / MarketPeriod=FT /
# MarketParameters=2.5 (``{SuperOddsType}|{MarketPeriod}|{MarketParameters}``). Stream ticks are
# constructed with this exact key so a close folded through the normalizer aligns with them.
KEY = "OU|FT|2.5"


# ---------------------------------------------------------------------------
# Fixtures / helpers — offline stream + offline TxLINE-native closing updates.
# ---------------------------------------------------------------------------


def _market(over_bps: int, under_bps: int | None = None) -> dict[str, Any]:
    prob: dict[str, int] = {"over": over_bps}
    if under_bps is not None:
        prob["under"] = under_bps
    return {"stable_prob_bps": prob, "stable_price": {"over": 1.6, "under": 2.4}, "suspended": False}


def _ms(
    over_bps: int,
    *,
    tick_seq: int,
    ts: int,
    phase: int = 0,
    fixture_id: int = 1,
    markets: dict[str, Any] | None = None,
) -> MarketState:
    mk = markets if markets is not None else {KEY: _market(over_bps)}
    return MarketState(fixture_id=fixture_id, tick_seq=tick_seq, ts=ts, phase=phase, markets=mk, scores={})


async def _astream(items: list[MarketState]) -> AsyncIterator[MarketState]:
    for item in items:
        yield item


def _flag_agent(agent_id: str = "flagger", *, side: str = "over", market_key: str = KEY) -> Agent:
    """An agent that always FLAG_VALUEs a fixed side/market (a scored, numeric-CLV action)."""

    async def decide(market_state: MarketState) -> AgentAction:
        return AgentAction(type=SportsActionType.FLAG_VALUE, params={"market_key": market_key, "side": side})

    return Agent(agent_id=agent_id, proof_mode="reproducible", decide=decide)


def _window(end_rule: str, **kw: Any) -> RunWindow:
    base: dict[str, Any] = {"window_id": "w1", "fixture_id": 1, "market_allowlist": ["OU"], "end_rule": end_rule}
    base.update(kw)
    return RunWindow(**base)


def _upd(
    over_pct: float,
    under_pct: float,
    *,
    ts_ms: int,
    in_running: int = 0,
    super_type: str = "OU",
    period: str = "FT",
    params: str = "2.5",
) -> dict[str, Any]:
    """A TxLINE-native odds update (what ``/odds/updates`` returns), foldable by the normalizer."""
    return {
        "FixtureId": 1,
        "Ts": ts_ms,
        "InRunning": in_running,
        "SuperOddsType": super_type,
        "MarketPeriod": period,
        "MarketParameters": params,
        "PriceNames": ["over", "under"],
        "Prices": [1600, 2400],
        "Pct": [over_pct, under_pct],
    }


def _tick_snaps(run_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [json.loads(e["state_snapshot_json"]) for e in run_events if e["event_type"] == "tick"]


def _decisions(run_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in run_events if e["event_type"] == "decision"]


# ===========================================================================
# 1 — pre_match end detection: the in-running flip tick terminates and is NOT fed
# ===========================================================================


async def test_pre_match_flip_tick_not_fed_and_run_seals() -> None:
    ticks = [
        _ms(6000, tick_seq=0, ts=1000, phase=0),
        _ms(6100, tick_seq=1, ts=1100, phase=0),
        _ms(6200, tick_seq=2, ts=9999, phase=1),  # kickoff -> terminates, must NOT be fed
    ]

    async def fetch(fid: int) -> list[dict[str, Any]]:
        return [_upd(60, 40, ts_ms=1_200_000)]  # a valid pre-InRunning close

    bundle = await run_live_window(
        _window("pre_match"), [_flag_agent()], stream=_astream(ticks), fetch_updates=fetch, anchor_fn=None
    )

    assert isinstance(bundle, LiveRunResult)
    run = bundle.run
    tss = [t["ts"] for t in _tick_snaps(run.run_events)]
    assert 9999 not in tss  # the in-running flip tick was never fed
    # Only the 2 pre-kickoff ticks produced decisions (the closing tick gathers none).
    assert len(_decisions(run.run_events)) == 2
    assert run.evidence_hash  # the run sealed


# ===========================================================================
# 2 — fixture filter: a tick for another fixture is dropped
# ===========================================================================


async def test_foreign_fixture_tick_dropped() -> None:
    ticks = [
        _ms(6000, tick_seq=0, ts=1000, phase=0),
        _ms(7000, tick_seq=0, ts=5555, phase=0, fixture_id=2),  # foreign fixture -> dropped
        _ms(6100, tick_seq=1, ts=1100, phase=0),
    ]

    async def fetch(fid: int) -> list[dict[str, Any]]:
        return [_upd(66, 34, ts_ms=2_000_000)]

    bundle = await run_live_window(
        _window("pre_match"), [_flag_agent()], stream=_astream(ticks), fetch_updates=fetch, anchor_fn=None
    )

    tss = [t["ts"] for t in _tick_snaps(bundle.run.run_events)]
    assert 5555 not in tss  # foreign fixture never fed
    assert len(_decisions(bundle.run.run_events)) == 2  # only the two fixture-1 ticks decided


# ===========================================================================
# 3 — market allowlist: non-allowlisted markets are filtered out of the fed snapshot
# ===========================================================================


async def test_non_allowlisted_market_filtered_out() -> None:
    markets = {"OU|FT|2.5": _market(6000, 4000), "1X2|FT|": _market(5000, 3000)}
    ticks = [_ms(0, tick_seq=0, ts=1000, phase=0, markets=markets)]

    async def fetch(fid: int) -> list[dict[str, Any]]:
        return [_upd(66, 34, ts_ms=2_000_000)]

    bundle = await run_live_window(
        _window("pre_match", market_allowlist=["OU"]),
        [_flag_agent()],
        stream=_astream(ticks),
        fetch_updates=fetch,
        anchor_fn=None,
    )

    fed = [t for t in _tick_snaps(bundle.run.run_events) if t["ts"] == 1000]
    assert len(fed) == 1
    assert set(fed[0]["markets"].keys()) == {"OU|FT|2.5"}  # 1X2 filtered out (not in allowlist)


# ===========================================================================
# 4 — fixed_duration end: an over-duration tick terminates and is NOT fed
# ===========================================================================


async def test_fixed_duration_over_duration_tick_not_fed() -> None:
    ticks = [
        _ms(6000, tick_seq=0, ts=1000, phase=0),  # started_ts = 1000
        _ms(6100, tick_seq=1, ts=1050, phase=0),  # 1050 <= 1100 -> fed
        _ms(6200, tick_seq=2, ts=1201, phase=0),  # 1201 > 1100 -> terminate, NOT fed
    ]

    bundle = await run_live_window(
        _window("fixed_duration", duration_s=100, min_clv_horizon_s=10),
        [_flag_agent()],
        stream=_astream(ticks),
        anchor_fn=None,
    )

    tss = [t["ts"] for t in _tick_snaps(bundle.run.run_events)]
    assert 1201 not in tss
    assert 1000 in tss and 1050 in tss
    # fixed_duration -> WINDOW CLV naming on the scored row (never true clv_bps).
    rows = {r["tick_seq"]: r for r in bundle.run.score_rows}
    assert "window_clv_bps" in rows[0]
    assert "clv_bps" not in rows[0]
    assert bundle.ops.get("closing_source") is None  # fixed_duration is honest window CLV, no fallback marker


# ===========================================================================
# 5 — CLOSING DIVERGENCE: pre_match CLV uses the reconstructed close, not the stream tick
# ===========================================================================


async def test_closing_divergence_uses_reconstructed_close_and_seals_it() -> None:
    ticks = [
        _ms(6000, tick_seq=0, ts=1000, phase=0),  # entry
        _ms(6300, tick_seq=1, ts=1100, phase=0),  # LAST STREAM tick = 6300 (the stale line)
    ]

    async def fetch(fid: int) -> list[dict[str, Any]]:
        # Reconstructed close (last pre-InRunning) = 6600, DIFFERENT from the last stream tick 6300.
        return [
            _upd(60, 40, ts_ms=1_000_000, in_running=0),
            _upd(66, 34, ts_ms=1_900_000, in_running=0),  # <- the authoritative close (over=6600)
            _upd(70, 30, ts_ms=2_000_000, in_running=1),  # in-running -> excluded by CON-040
        ]

    bundle = await run_live_window(
        _window("pre_match"), [_flag_agent()], stream=_astream(ticks), fetch_updates=fetch, anchor_fn=None
    )

    rows = {r["tick_seq"]: r for r in bundle.run.score_rows}
    # True CLV for tick0 = reconstructed close 6600 - entry 6000 = 600 (NOT 6300 - 6000 = 300).
    assert rows[0]["clv_bps"] == 600
    assert "window_clv_bps" not in rows[0]  # pre_match success -> TRUE clv_bps label

    # The reconstructed close (ts=1900, over=6600) is inside the SEALED run_events as a tick.
    closing = [t for t in _tick_snaps(bundle.run.run_events) if t["ts"] == 1900]
    assert len(closing) == 1
    assert closing[0]["markets"][KEY]["stable_prob_bps"]["over"] == 6600
    # honesty: a true-CLV run carries NO fallback marker.
    assert bundle.ops.get("closing_source") != "stream_observed_fallback"


# ===========================================================================
# 5b — completeness gate: a close must cover EVERY scored market to yield true CLV
# ===========================================================================

KEY_B = "OU|HT|1.5"  # a second allowlisted market (prefix "OU"), scored by a second agent


def _two_market_ms(a_over: int, b_over: int, *, tick_seq: int, ts: int) -> MarketState:
    return _ms(0, tick_seq=tick_seq, ts=ts, phase=0, markets={KEY: _market(a_over), KEY_B: _market(b_over)})


async def test_complete_close_over_all_scored_markets_stays_true_clv() -> None:
    # Two agents score two markets (A, B); the reconstructed close covers BOTH -> TRUE clv_bps.
    ticks = [
        _two_market_ms(6000, 5000, tick_seq=0, ts=1000),
        _two_market_ms(6100, 5200, tick_seq=1, ts=1100),
    ]

    async def fetch(fid: int) -> list[dict[str, Any]]:
        # Pre-InRunning closes for BOTH markets (A: over=6600 @ FT/2.5, B: over=5500 @ HT/1.5).
        return [
            _upd(66, 34, ts_ms=1_900_000, in_running=0, period="FT", params="2.5"),
            _upd(55, 45, ts_ms=1_900_000, in_running=0, period="HT", params="1.5"),
        ]

    agents = [_flag_agent("agentA", market_key=KEY), _flag_agent("agentB", market_key=KEY_B)]
    bundle = await run_live_window(
        _window("pre_match"), agents, stream=_astream(ticks), fetch_updates=fetch, anchor_fn=None
    )

    # COMPLETE close -> every row carries TRUE clv_bps, no window_clv_bps, no degrade marker.
    for row in bundle.run.score_rows:
        assert "clv_bps" in row
        assert "window_clv_bps" not in row
    assert bundle.ops.get("closing_source") is None
    assert "closing_incomplete_markets" not in bundle.ops
    # Sanity: agentB's tick0 true CLV = B close 5500 - entry 5000 = 500.
    b0 = next(r for r in bundle.run.score_rows if r["agent_id"] == "agentB" and r["tick_seq"] == 0)
    assert b0["clv_bps"] == 500


async def test_incomplete_close_degrades_to_window_clv_never_true_clv() -> None:
    # The reconstructed close covers ONLY market A, but market B was ALSO scored during the window.
    # Honesty: B would otherwise close against its last STREAM tick while being labeled true CLV.
    ticks = [
        _two_market_ms(6000, 5000, tick_seq=0, ts=1000),
        _two_market_ms(6100, 5200, tick_seq=1, ts=1100),
    ]

    async def fetch(fid: int) -> list[dict[str, Any]]:
        # Only market A has a pre-InRunning close; market B is MISSING from the close.
        return [_upd(66, 34, ts_ms=1_900_000, in_running=0, period="FT", params="2.5")]

    agents = [_flag_agent("agentA", market_key=KEY), _flag_agent("agentB", market_key=KEY_B)]
    bundle = await run_live_window(
        _window("pre_match"), agents, stream=_astream(ticks), fetch_updates=fetch, anchor_fn=None
    )

    # DEGRADE: NO row may be labeled true clv_bps -> every row is WINDOW CLV.
    for row in bundle.run.score_rows:
        assert "window_clv_bps" in row
        assert "clv_bps" not in row
    # The ops marker names the uncovered scored market ...
    assert bundle.ops.get("closing_source") == "stream_observed_fallback"
    assert bundle.ops.get("closing_incomplete_markets") == [KEY_B]
    # ... and NEITHER the marker NOR the incomplete-markets annotation is inside the sealed evidence.
    assert "stream_observed_fallback" not in json.dumps(bundle.run.run_events)
    assert "closing_incomplete_markets" not in json.dumps(bundle.run.run_events)
    assert "stream_observed_fallback" not in json.dumps(bundle.run.score_rows)


# ===========================================================================
# 6 — fetch-failure degrade: no fabricated close, window_clv_bps + honest ops marker
# ===========================================================================


async def test_fetch_failure_degrades_to_window_clv_with_ops_marker() -> None:
    ticks = [
        _ms(6000, tick_seq=0, ts=1000, phase=0),
        _ms(6300, tick_seq=1, ts=1100, phase=0),  # de-facto close = last STREAM tick
    ]

    async def fetch(fid: int) -> list[dict[str, Any]]:
        raise RuntimeError("txline updates endpoint unavailable")

    bundle = await run_live_window(
        _window("pre_match", min_clv_horizon_s=10),
        [_flag_agent()],
        stream=_astream(ticks),
        fetch_updates=fetch,
        anchor_fn=None,
    )

    rows = {r["tick_seq"]: r for r in bundle.run.score_rows}
    # No fabricated close: window CLV from the last STREAM tick = 6300 - 6000 = 300, labeled WINDOW CLV.
    assert rows[0]["window_clv_bps"] == 300
    assert "clv_bps" not in rows[0]  # never presented as true CLV

    # Honest ops marker present ...
    assert bundle.ops.get("closing_source") == "stream_observed_fallback"
    # ... and it is NOT inside the sealed evidence (a non-sealed OPS annotation only).
    assert "stream_observed_fallback" not in json.dumps(bundle.run.run_events)
    assert "stream_observed_fallback" not in json.dumps(bundle.run.score_rows)

    # No feed_closing tick was added: only the two stream ticks are sealed.
    assert len(_tick_snaps(bundle.run.run_events)) == 2


async def test_reconstruct_none_degrades_without_fabricated_close() -> None:
    ticks = [
        _ms(6000, tick_seq=0, ts=1000, phase=0),
        _ms(6300, tick_seq=1, ts=1100, phase=0),
    ]

    async def fetch(fid: int) -> list[dict[str, Any]]:
        # Every update is already in-running -> reconstruct_closing returns None (no pre-kickoff close).
        return [_upd(70, 30, ts_ms=2_000_000, in_running=1)]

    bundle = await run_live_window(
        _window("pre_match", min_clv_horizon_s=10),
        [_flag_agent()],
        stream=_astream(ticks),
        fetch_updates=fetch,
        anchor_fn=None,
    )

    assert bundle.ops.get("closing_source") == "stream_observed_fallback"
    rows = {r["tick_seq"]: r for r in bundle.run.score_rows}
    assert "window_clv_bps" in rows[0]
    assert "clv_bps" not in rows[0]
    assert len(_tick_snaps(bundle.run.run_events)) == 2  # no fabricated closing tick


# ===========================================================================
# 7 — seal-once / no-card-before-seal: the proof card is built strictly AFTER finalize
# ===========================================================================


async def test_proof_card_built_after_seal_only_once(monkeypatch: Any) -> None:
    timeline: list[str] = []

    async def sink(ev: dict[str, Any]) -> None:
        timeline.append("event")  # every sealed RunEvent is emitted during feed(), before the seal

    import veridex.runtime.live_runner as lr

    original = lr.proof_card_from_run_result

    def spy(*args: Any, **kwargs: Any) -> dict[str, Any]:
        timeline.append("card")
        return original(*args, **kwargs)

    monkeypatch.setattr(lr, "proof_card_from_run_result", spy)

    ticks = [_ms(6000, tick_seq=0, ts=1000, phase=0)]

    async def fetch(fid: int) -> list[dict[str, Any]]:
        return [_upd(66, 34, ts_ms=2_000_000)]

    bundle = await run_live_window(
        _window("pre_match"),
        [_flag_agent()],
        stream=_astream(ticks),
        fetch_updates=fetch,
        event_sink=sink,
        anchor_fn=None,
    )

    assert "card" in timeline
    assert timeline[-1] == "card"  # the card is built AFTER every sealed event (i.e. after the seal)
    assert timeline.count("card") == 1  # sealed / carded exactly once
    assert bundle.proof_card is not None
    # The card binds the sealed evidence hash (proof lineage over the sealed run).
    assert bundle.proof_card["evidence"]["evidence_hash"] == bundle.run.evidence_hash


# ===========================================================================
# 8 — manual_stop: stop_event terminates the loop between ticks (interface completeness)
# ===========================================================================


async def test_manual_stop_event_terminates_between_ticks() -> None:
    stop = asyncio.Event()

    async def _streaming() -> AsyncIterator[MarketState]:
        yield _ms(6000, tick_seq=0, ts=1000, phase=0)
        yield _ms(6300, tick_seq=1, ts=1100, phase=0)
        stop.set()  # request stop; the NEXT tick must not be fed
        yield _ms(6600, tick_seq=2, ts=1200, phase=0)

    bundle = await run_live_window(
        _window("manual_stop", min_clv_horizon_s=10),
        [_flag_agent()],
        stream=_streaming(),
        stop_event=stop,
        anchor_fn=None,
    )

    tss = [t["ts"] for t in _tick_snaps(bundle.run.run_events)]
    assert 1200 not in tss  # the post-stop tick was never fed
    assert tss == [1000, 1100]
    # manual_stop -> WINDOW CLV naming (never true clv_bps), and no closing fetch/fallback marker.
    rows = {r["tick_seq"]: r for r in bundle.run.score_rows}
    assert "window_clv_bps" in rows[0]
    assert "clv_bps" not in rows[0]
    assert bundle.ops.get("closing_source") is None
