"""Offline, deterministic arena fixtures for Phase-2A competition tests.

These helpers build a real, sealed Phase-1 :class:`~veridex.runtime.orchestrator.RunResult`
by driving the actual orchestrator over fixed ``MarketState`` ticks — no network, no LLM, no
DB. They are intentionally synchronous wrappers (``asyncio.run``) so the Phase-2A event-log
tests can call them directly.

Determinism contract: with identical arguments, :func:`finished_run_result` returns a
byte-stable ``RunResult`` (same ``run_id``, same evidence hash, same rows) on every call, so
``build_event_log`` is exercised against a reproducible source of truth. A FRESH ``RunResult``
instance is returned each call (the tamper test mutates one instance in place).
"""

from __future__ import annotations

import asyncio

from veridex.ingest.marketstate import MarketState
from veridex.runtime.orchestrator import (
    PROOF_MODE_LLM,
    Agent,
    RunResult,
    deterministic_agent,
    run_competition,
)
from veridex.runtime.schemas import AgentAction, SportsActionType

# The OVERUNDER market the two agents disagree on (alpha picks the higher-prob "under";
# beta always flags "over"). Mirrors the demo-surface market key format.
_DEMO_MARKET_KEY = "OVERUNDER_PARTICIPANT_GOALS|half=1|line=1"

# Fixed run id so the sealed RunResult — and therefore the derived event log — is byte-stable
# across calls. (run_id does not enter the evidence hash, but pinning it keeps the whole record
# identical between invocations.)
_FIXED_RUN_ID = "run_arena_test"


def _ticks() -> list[MarketState]:
    """Two deterministic ticks where "under" drifts up (+184 bps) on the OVERUNDER market.

    Returns:
        ``[tick0, tick1]`` — tick1 is the closing-horizon snapshot for both markets.
    """
    tick0 = MarketState(
        fixture_id=17588404,
        tick_seq=0,
        ts=1782518383,
        phase=0,
        markets={
            _DEMO_MARKET_KEY: {
                "stable_prob_bps": {"over": 4684, "under": 5316},
                "stable_price": {"over": 2.135, "under": 1.881},
                "suspended": False,
            },
            "1X2_PARTICIPANT_RESULT||": {
                "stable_prob_bps": {"home": 4500, "draw": 2500, "away": 3000},
                "stable_price": {"home": 2.222, "draw": 4.000, "away": 3.333},
                "suspended": False,
            },
        },
        scores={},
    )
    tick1 = MarketState(
        fixture_id=17588404,
        tick_seq=1,
        ts=1782518393,
        phase=0,
        markets={
            _DEMO_MARKET_KEY: {
                "stable_prob_bps": {"over": 4500, "under": 5500},
                "stable_price": {"over": 2.222, "under": 1.818},
                "suspended": False,
            },
            "1X2_PARTICIPANT_RESULT||": {
                "stable_prob_bps": {"home": 4600, "draw": 2400, "away": 3000},
                "stable_price": {"home": 2.174, "draw": 4.167, "away": 3.333},
                "suspended": False,
            },
        },
        scores={},
    )
    return [tick0, tick1]


def _beta_agent() -> Agent:
    """A second, ``verified``-proof-mode agent built by hand (no LLM / no network).

    It carries ``proof_mode=PROOF_MODE_LLM`` to exercise the verified branch, but its
    ``decide`` is a deterministic ``async def`` that always flags the "over" side — so the
    fixture stays byte-stable while still producing a multi-agent, multi-row run.

    Returns:
        A deterministic :class:`~veridex.runtime.orchestrator.Agent`.
    """

    async def decide(market_state: MarketState) -> AgentAction:
        return AgentAction(
            type=SportsActionType.FLAG_VALUE,
            params={"market_key": _DEMO_MARKET_KEY, "side": "over"},
        )

    return Agent(agent_id="agent-beta", proof_mode=PROOF_MODE_LLM, decide=decide)


def finished_run_result(source_mode: str = "replay") -> RunResult:
    """Run the real orchestrator over fixed ticks and return a sealed ``RunResult``.

    Two agents (the reproducible deterministic baseline plus a verified-mode hand-built
    agent) decide over two ticks, yielding 6 run events and 4 score rows. Fully offline and
    deterministic.

    Args:
        source_mode: ``"replay"`` or ``"live"`` (forwarded to ``run_competition``).

    Returns:
        A fresh, sealed :class:`~veridex.runtime.orchestrator.RunResult`.
    """
    agents = [deterministic_agent("agent-alpha"), _beta_agent()]
    return asyncio.run(run_competition(_ticks(), agents, source_mode=source_mode, run_id=_FIXED_RUN_ID))


def competition_meta() -> dict[str, object]:
    """Deterministic competition metadata consumed by ``build_event_log``.

    Returns:
        A dict with ``competition_id``, ``anchor_status``, and a deterministic ``event_ts``.
    """
    return {"competition_id": "c_test", "anchor_status": "not_anchored", "event_ts": 1782518383}
