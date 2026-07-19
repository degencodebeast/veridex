"""III-3 — connection-derived feed health (the honest FIVE-STATE surface).

The ``/feed/health`` honesty claim (R-0a/R-0b): the reported state must reflect the ACTIVE
stream's real last-seen — not credential/param presence. This module pins the five mutually
exclusive states, each produced DISTINCTLY from a fake last-seen input (no network):

  1. LIVE            — a recent odds RECORD was received.
  2. HEARTBEAT_ONLY  — a recent HEARTBEAT (no recent odds): liveness proven, no market data.
  3. STALE           — the last-seen frame is older than the staleness budget.
  4. DISCONNECTED    — no active connection.
  5. RECORDED_REPLAY — replay mode; NEVER shown as live regardless of last-seen.

Plus the live-runner last-seen HOOK: as the active stream runs, ``run_live_window`` records the
last odds-record timestamp into an observable :class:`LiveFeedStatus` the endpoint reads.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from veridex.ingest.feed_health import (
    DEFAULT_STALE_AFTER_S,
    FeedState,
    LiveFeedStatus,
    derive_live_feed_state,
)
from veridex.ingest.marketstate import MarketState
from veridex.runtime.live_runner import run_live_window
from veridex.runtime.orchestrator import Agent
from veridex.runtime.schemas import AgentAction, SportsActionType
from veridex.runtime.window import RunWindow

KEY = "OU|FT|2.5"


# --- offline fake-stream helpers (mirrors test_live_runner) ---------------------------------------
def _ms(over_bps: int, *, tick_seq: int, ts: int, fixture_id: int = 1) -> MarketState:
    market = {"stable_prob_bps": {"over": over_bps}, "stable_price": {"over": 1.6}, "suspended": False}
    return MarketState(
        fixture_id=fixture_id, tick_seq=tick_seq, ts=ts, phase=0, markets={KEY: market}, scores={}
    )


async def _astream(items: list[MarketState]) -> AsyncIterator[MarketState]:
    for item in items:
        yield item


def _flag_agent(agent_id: str = "flagger") -> Agent:
    async def decide(market_state: MarketState) -> AgentAction:
        return AgentAction(type=SportsActionType.FLAG_VALUE, params={"market_key": KEY, "side": "over"})

    return Agent(agent_id=agent_id, proof_mode="reproducible", decide=decide)


def _window(end_rule: str, **kw: Any) -> RunWindow:
    base: dict[str, Any] = {"window_id": "w1", "fixture_id": 1, "market_allowlist": ["OU"], "end_rule": end_rule}
    base.update(kw)
    return RunWindow(**base)


# =================================================================================================
# The five states, each from a fake last-seen input.
# =================================================================================================
def test_state_1_recent_odds_record_is_live() -> None:
    status = LiveFeedStatus()
    status.mark_connected()
    status.record_odds_record(ts=1000, fixture_id=42)
    assert status.feed_state(source_mode="live", now_ts=1005) is FeedState.LIVE


def test_state_2_fresh_heartbeat_only_counts_as_liveness() -> None:
    # A fresh heartbeat is liveness — NOT disconnected, NOT stale — but has no market data.
    status = LiveFeedStatus()
    status.mark_connected()
    status.record_heartbeat(ts=1000)
    state = status.feed_state(source_mode="live", now_ts=1005)
    assert state is FeedState.HEARTBEAT_ONLY
    assert state is not FeedState.DISCONNECTED
    assert state is not FeedState.STALE


def test_state_3_last_seen_beyond_threshold_is_stale() -> None:
    status = LiveFeedStatus()
    status.mark_connected()
    status.record_odds_record(ts=1000, fixture_id=1)
    now = 1000 + DEFAULT_STALE_AFTER_S + 5
    assert status.feed_state(source_mode="live", now_ts=now) is FeedState.STALE


def test_state_4_no_connection_is_disconnected() -> None:
    status = LiveFeedStatus()  # never connected
    assert status.feed_state(source_mode="live", now_ts=1000) is FeedState.DISCONNECTED


def test_state_5_replay_mode_is_recorded_replay_never_live() -> None:
    status = LiveFeedStatus()
    status.mark_connected()
    status.record_odds_record(ts=1000, fixture_id=1)  # even with a fresh odds record present...
    state = status.feed_state(source_mode="replay", now_ts=1000)
    assert state is FeedState.RECORDED_REPLAY
    assert state is not FeedState.LIVE


def test_per_channel_recency_stale_odds_but_fresh_heartbeat_is_heartbeat_only() -> None:
    # THE honest distinction the old count-based derivation cannot make: odds went stale while the
    # heartbeat stays fresh -> HEARTBEAT_ONLY (liveness, no market data), never LIVE.
    status = LiveFeedStatus()
    status.mark_connected()
    status.record_odds_record(ts=1000, fixture_id=1)  # old odds record
    status.record_heartbeat(ts=1000 + 100)  # a much fresher heartbeat
    now = 1000 + 100 + 5
    assert status.feed_state(source_mode="live", now_ts=now) is FeedState.HEARTBEAT_ONLY


# =================================================================================================
# The live-runner last-seen HOOK — the active stream records into an observable place.
# =================================================================================================
async def test_run_live_window_records_last_seen_into_feed_status() -> None:
    status = LiveFeedStatus()
    ticks = [_ms(6000, tick_seq=0, ts=1000), _ms(6100, tick_seq=1, ts=1100)]

    bundle = await run_live_window(
        _window("manual_stop"),
        [_flag_agent()],
        stream=_astream(ticks),
        anchor_fn=None,
        feed_status=status,
        clock=lambda: 5000,
    )

    assert bundle.run.evidence_hash  # the run still sealed
    # The hook recorded each fed odds record's last-seen (wall) time + the fixture it follows.
    assert status.odds_records_seen == 2
    assert status.last_odds_ts == 5000
    assert status.fixture_id == 1
    # The stream ended -> the runner honestly marks the feed disconnected.
    assert status.connected is False
    # While the window was active (connected + receiving), the endpoint would derive LIVE:
    live = derive_live_feed_state(
        source_mode="live",
        connected=True,
        connecting=False,
        last_odds_ts=status.last_odds_ts,
        last_heartbeat_ts=status.last_heartbeat_ts,
        now_ts=5000,
        stale_after_s=DEFAULT_STALE_AFTER_S,
    )
    assert live is FeedState.LIVE


async def test_run_live_window_without_feed_status_still_seals() -> None:
    # Backward compatible: feed_status is optional; omitting it changes nothing about the seal.
    ticks = [_ms(6000, tick_seq=0, ts=1000)]
    bundle = await run_live_window(
        _window("manual_stop"), [_flag_agent()], stream=_astream(ticks), anchor_fn=None
    )
    assert bundle.run.evidence_hash
