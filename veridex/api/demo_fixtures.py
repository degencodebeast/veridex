"""Offline demo fixtures for the Veridex FastAPI surface (relocated — REQ-2B-33).

These deterministic builders were previously inlined in :mod:`veridex.api.router`. They are
moved here so the router stays a thin wiring layer and the demo/competition fixtures have a
single, importable home. Nothing here touches an LLM or the network — every agent is a
deterministic ``async def`` and the ticks are hard-coded snapshots.

TRUST PATH note: like the rest of the async shell, this module MUST NOT import any LLM SDK
(enforced by ``veridex.verifier.import_audit``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from pydantic import ValidationError

from veridex.competition.models import AgentEntry
from veridex.ingest.marketstate import MarketState
from veridex.runtime.orchestrator import (
    PROOF_MODE_REPRODUCIBLE,
    Agent,
)
from veridex.runtime.schemas import AgentAction, SportsActionType
from veridex_agent.config import AgentRunConfig, build_agent

if TYPE_CHECKING:
    from veridex.deploy.instance import AgentInstance

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


def _build_declared_agent(entry: AgentEntry) -> Agent:
    """Build the agent the roster entry DECLARES, by strategy (fail-closed on an unknown strategy).

    Constructs the agent through the SINGLE ``build_agent`` dispatch (I-6) from the entry's declared
    ``strategy`` — position-independent, with NO baseline/contrarian substitution. An unrecognised
    strategy raises an EXPLICIT :class:`ValueError` naming the strategy; it is NEVER silently mapped
    to a default/baseline agent (a silent substitution would let the arena run a contestant the owner
    did not declare — a demo-honesty lie).

    Args:
        entry: The roster entry to build (its ``strategy`` selects the agent; ``model`` is the
            optional LLM model slug forwarded to the ``llm`` strategy).

    Returns:
        The :class:`~veridex.runtime.orchestrator.Agent` for the declared strategy.

    Raises:
        ValueError: If ``entry.strategy`` is not a known strategy (fail-closed, no substitution).
    """
    # The offline demo/competition layer owns a "contrarian" contestant (:func:`contrarian_agent`) that
    # flags the OPPOSITE side — it is NOT a deployed ``build_agent`` strategy, it exists to guarantee the
    # offline arena's rank-1-vs-2 CLV split. A DECLARED "contrarian" entry builds that real agent (still
    # position-independent, still fail-closed on anything genuinely unknown below).
    if entry.strategy == "contrarian":
        return contrarian_agent(entry.agent_id)
    try:
        run_config = AgentRunConfig(agent_id=entry.agent_id, strategy=entry.strategy, model_id=entry.model)
    except ValidationError as exc:
        # An unknown strategy is rejected by the AgentRunConfig Literal — re-raise as an EXPLICIT,
        # legible ValueError (fail-closed) so the caller never silently substitutes a baseline.
        raise ValueError(f"unknown strategy {entry.strategy!r} for agent {entry.agent_id!r}") from exc
    return build_agent(run_config)


def bind_roster_instance(entry: AgentEntry, instance: AgentInstance) -> AgentRunConfig:
    """Bind a roster entry to its pinned Studio-deployed instance (the trust core; fail-closed).

    The arena must run the ACTUAL deployed contestant, not a freshly-reconstructed same-named agent.
    The entry PINS the deployed instance's ``config_hash`` (its identity commitment); this verifies the
    LIVE instance still carries that exact hash and returns the instance's own ``effective_config`` —
    so :func:`~veridex_agent.config.build_agent` constructs the contestant from the deployed config,
    NEVER from the entry's ``strategy`` label. A drift between the pinned hash and the live instance's
    ``config_hash`` is CAUGHT (raised), never silently run.

    Args:
        entry: The roster entry referencing a deployed instance (``entry.instance_id`` is set; its
            ``config_hash`` pins the instance's deployed identity).
        instance: The live :class:`~veridex.deploy.instance.AgentInstance` loaded from the store.

    Returns:
        The deployed instance's :class:`~veridex_agent.config.AgentRunConfig` (its ``effective_config``)
        — the ACTUAL deployed config the agent must be built from.

    Raises:
        ValueError: If the entry pins no ``config_hash`` (an instance-bound entry MUST commit to the
            deployed identity), or if the pinned hash has DRIFTED from the live instance's
            ``config_hash`` (fail-closed — the deployed contestant changed since it was rostered).
    """
    if entry.config_hash is None:
        raise ValueError(
            f"instance-bound roster entry {entry.agent_id!r} (instance {entry.instance_id!r}) "
            "must pin the deployed config_hash"
        )
    if entry.config_hash != instance.config_hash:
        # Config drift: the live deployed instance no longer matches the identity the roster pinned.
        raise ValueError(
            f"config drift for instance {entry.instance_id!r}: rostered config_hash "
            f"{entry.config_hash!r} != deployed config_hash {instance.config_hash!r}"
        )
    # Build from the instance's OWN effective config — the ACTUAL deployed contestant, not the label.
    return AgentRunConfig.model_validate(instance.effective_config)


async def build_agents_from_roster(
    entries: list[AgentEntry],
    *,
    get_instance: Callable[[str], Awaitable[AgentInstance]] | None = None,
) -> list[Agent]:
    """Build the DECLARED roster's Agent objects — by strategy, with roster->instance binding (I-7).

    Each entry is built by the strategy it DECLARES (position-independent; the old alternating
    baseline/contrarian substitution is gone). An entry that references a Studio-deployed instance
    (``entry.instance_id`` set) runs the ACTUAL deployed contestant: its agent is built from that
    instance's pinned ``effective_config`` (via :func:`bind_roster_instance`), and a config drift is
    caught fail-closed — never a same-named reconstruction, never a silent substitution.

    Args:
        entries: Registered :class:`~veridex.competition.models.AgentEntry` objects in roster order.
        get_instance: Async loader ``instance_id -> AgentInstance`` used to resolve instance-bound
            entries (the store's ``get_agent_instance``). Required only if any entry sets
            ``instance_id``; a purely declared roster needs none.

    Returns:
        A list of :class:`~veridex.runtime.orchestrator.Agent` objects, one per entry, in roster order.

    Raises:
        ValueError: If an entry declares an unknown strategy, or an instance-bound entry has drifted
            from / is missing its pinned identity, or references an instance with no ``get_instance``.
        KeyError: If an instance-bound entry references an instance ``get_instance`` cannot load.
    """
    agents: list[Agent] = []
    for entry in entries:
        if entry.instance_id is not None:
            if get_instance is None:
                raise ValueError(
                    f"roster entry {entry.agent_id!r} references deployed instance "
                    f"{entry.instance_id!r} but no instance loader was provided"
                )
            instance = await get_instance(entry.instance_id)
            run_config = bind_roster_instance(entry, instance)
            agents.append(build_agent(run_config))
        else:
            agents.append(_build_declared_agent(entry))
    return agents
