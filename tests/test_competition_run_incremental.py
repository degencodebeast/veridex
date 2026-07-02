"""T5 — incremental `CompetitionRun` core equivalence + real-time-decision pins (REQ-2D-101/102).

The batch `run_competition` is (post-T5) a thin wrapper over an incremental `CompetitionRun`
driven by `feed()` per snapshot + a single `finalize()`. These tests pin the three properties
that make the extraction TRUST-safe:

  1. feed()* + finalize() yields a `RunResult` byte-equal to the batch wrapper on identical
     inputs (internal equivalence; the real byte-pin is `tests/test_orchestrator_golden.py`).
  2. Each agent's `decide` is awaited DURING `feed()` (real-time), never buffered to
     `finalize()` — buffered decide-at-finalize would be replay-as-live (REQ-2D-101, forbidden).
  3. `finalize()` seals EXACTLY once; a second call raises.
"""

from __future__ import annotations

import pytest

from veridex.ingest.marketstate import MarketState
from veridex.runtime.orchestrator import (
    Agent,
    CompetitionRun,
    RunResult,
    deterministic_agent,
    run_competition,
)
from veridex.runtime.schemas import AgentAction, SportsActionType

KEY = "OU_2_5"


def _market(prob_bps: dict[str, int]) -> dict:
    return {
        "stable_prob_bps": dict(prob_bps),
        "stable_price": {"over": 1.6, "under": 2.4},
        "suspended": False,
    }


def _ms(prob_bps: dict[str, int], *, tick_seq: int) -> MarketState:
    return MarketState(
        fixture_id=1,
        tick_seq=tick_seq,
        ts=1000 + tick_seq,
        phase=2,
        markets={KEY: _market(prob_bps)},
        scores={},
    )


def _marketstates() -> list[MarketState]:
    return [
        _ms({"over": 6000, "under": 4000}, tick_seq=0),
        _ms({"over": 6300, "under": 3700}, tick_seq=1),
    ]


async def test_feed_finalize_matches_batch() -> None:
    """Driving CompetitionRun via feed()*+finalize() equals the batch wrapper (same run_id)."""
    marketstates = _marketstates()

    batch = await run_competition(
        marketstates, [deterministic_agent()], source_mode="replay", run_id="equiv-run-1"
    )

    run = CompetitionRun([deterministic_agent()], source_mode="replay", run_id="equiv-run-1")
    for snapshot in marketstates:
        await run.feed(snapshot)
    incremental = await run.finalize()

    assert isinstance(incremental, RunResult)
    assert incremental == batch


async def test_decisions_happen_at_feed_time_not_finalize() -> None:
    """An agent's decide is awaited DURING feed(), not deferred to finalize() (REQ-2D-101)."""
    decided_at: list[int] = []

    def _recording_agent() -> Agent:
        async def decide(market_state: MarketState) -> AgentAction:
            decided_at.append(market_state.tick_seq)
            return AgentAction(type=SportsActionType.WAIT, params={})

        return Agent(agent_id="recorder", proof_mode="reproducible", decide=decide)

    marketstates = _marketstates()
    run = CompetitionRun([_recording_agent()], source_mode="replay", run_id="realtime-run-1")

    assert decided_at == []  # nothing decided before the first feed
    await run.feed(marketstates[0])
    # The decision was gathered during feed() — the list is non-empty BEFORE finalize().
    assert decided_at == [0]

    await run.feed(marketstates[1])
    assert decided_at == [0, 1]

    await run.finalize()
    assert decided_at == [0, 1]  # finalize added no further decide() calls


async def test_feed_after_finalize_raises() -> None:
    """A sealed run rejects further feed(): no phantom decisions/events past the seal (REQ-2D-101)."""
    marketstates = _marketstates()
    run = CompetitionRun([deterministic_agent()], source_mode="replay", run_id="feed-after-seal-1")
    await run.feed(marketstates[0])
    await run.finalize()

    with pytest.raises(RuntimeError):
        await run.feed(marketstates[1])


async def test_finalize_twice_raises() -> None:
    """A run seals exactly once: the second finalize() raises."""
    run = CompetitionRun([deterministic_agent()], source_mode="replay", run_id="seal-once-1")
    for snapshot in _marketstates():
        await run.feed(snapshot)

    first = await run.finalize()
    assert isinstance(first, RunResult)

    with pytest.raises(RuntimeError):
        await run.finalize()
