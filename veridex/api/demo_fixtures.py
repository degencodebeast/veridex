"""Offline demo fixtures for the Veridex FastAPI surface (relocated — REQ-2B-33).

These deterministic builders were previously inlined in :mod:`veridex.api.router`. They are
moved here so the router stays a thin wiring layer and the demo/competition fixtures have a
single, importable home. Nothing here touches an LLM or the network — every agent is a
deterministic ``async def`` and the ticks are hard-coded snapshots.

TRUST PATH note: like the rest of the async shell, this module MUST NOT import any LLM SDK
(enforced by ``veridex.verifier.import_audit``).
"""

from __future__ import annotations

from veridex.competition.models import AgentEntry
from veridex.ingest.marketstate import MarketState
from veridex.runtime.orchestrator import (
    PROOF_MODE_REPRODUCIBLE,
    Agent,
    deterministic_agent,
)
from veridex.runtime.schemas import AgentAction, SportsActionType

# The OVERUNDER market the demo agents disagree on (see :func:`build_demo_ticks`).
DEMO_MARKET_KEY = "OVERUNDER_PARTICIPANT_GOALS|half=1|line=1"


def contrarian_agent(agent_id: str = "agent-beta") -> Agent:
    """Build a SECOND, differentiated deterministic agent (no LLM, no network).

    Where :func:`~veridex.runtime.orchestrator.deterministic_agent` picks the
    highest-probability side (the demo's "under"), this agent always FLAG_VALUEs the
    OTHER side ("over") of the same OVERUNDER market. On the demo ticks "over" drifts
    DOWN while "under" drifts UP, so the contrarian earns a NEGATIVE CLV — giving the
    leaderboard a genuine rank-1 vs rank-2 split instead of a tie. Fully deterministic
    and §4-scorable (market_key + side present on a non-suspended market).

    Args:
        agent_id: Identifier for this agent (defaults to ``"agent-beta"``).

    Returns:
        An :class:`~veridex.runtime.orchestrator.Agent` whose ``proof_mode`` is
        ``"reproducible"``.
    """

    async def decide(market_state: MarketState) -> AgentAction:
        return AgentAction(
            type=SportsActionType.FLAG_VALUE,
            params={"market_key": DEMO_MARKET_KEY, "side": "over"},
        )

    return Agent(agent_id=agent_id, proof_mode=PROOF_MODE_REPRODUCIBLE, decide=decide)


def build_demo_ticks() -> list[MarketState]:
    """Build two deterministic MarketState ticks for the offline demo fixture.

    Tick 0 reflects the TxLINE fixture (17588404) opening snapshot. Tick 1 models a
    later update where the "under" probability on the OVERUNDER market drifts up,
    ensuring a positive closing-line CLV (+184 bps) for the deterministic agents'
    tick-0 decisions and a 0-bps CLV for tick-1 decisions.

    Market keys follow the ``market_key()`` format:
    ``{SuperOddsType}|{MarketPeriod or ''}|{MarketParameters or ''}``.

    Returns:
        A two-element list of ``MarketState`` snapshots (tick 0 then tick 1).
    """
    tick0 = MarketState(
        fixture_id=17588404,
        tick_seq=0,
        ts=1782518383,
        phase=0,
        markets={
            # OVERUNDER_PARTICIPANT_GOALS, half=1, line=1 — from txline_native_messages[0]
            "OVERUNDER_PARTICIPANT_GOALS|half=1|line=1": {
                "stable_prob_bps": {"over": 4684, "under": 5316},
                "stable_price": {"over": 2.135, "under": 1.881},
                "suspended": False,
            },
            # 1X2_PARTICIPANT_RESULT — from txline_native_messages[1] (null period/params → "")
            "1X2_PARTICIPANT_RESULT||": {
                "stable_prob_bps": {"home": 4500, "draw": 2500, "away": 3000},
                "stable_price": {"home": 2.222, "draw": 4.000, "away": 3.333},
                "suspended": False,
            },
        },
        scores={},
    )

    # Tick 1: "under" drifts to 5500 (+184 bps vs tick 0) — positive CLV for tick-0 decisions.
    tick1 = MarketState(
        fixture_id=17588404,
        tick_seq=1,
        ts=1782518393,
        phase=0,
        markets={
            "OVERUNDER_PARTICIPANT_GOALS|half=1|line=1": {
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


def build_agents_from_roster(entries: list[AgentEntry]) -> list[Agent]:
    """Build offline Agent objects from registered roster entries.

    Offline simplification: each entry is mapped to a deterministic or contrarian agent
    (alternating by roster position) so the run is fully reproducible and produces a real
    ≥2-row leaderboard with distinct CLV. Even-indexed entries → ``deterministic_agent``;
    odd-indexed entries → ``contrarian_agent``.

    Note: This is a deliberate wiring simplification. Real BYOA / live agent execution will
    route each entry to its actual execution environment.

    Args:
        entries: Registered :class:`~veridex.competition.models.AgentEntry` objects in
            roster order.

    Returns:
        A list of :class:`~veridex.runtime.orchestrator.Agent` objects, one per entry.
    """
    agents: list[Agent] = []
    for i, entry in enumerate(entries):
        if i % 2 == 0:
            agents.append(deterministic_agent(entry.agent_id))
        else:
            agents.append(contrarian_agent(entry.agent_id))
    return agents
