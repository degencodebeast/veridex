"""B5 — the ASYNC run orchestrator (REQ-105 / AC-105, gate CON-003/CON-006).

CON-010 (async shell / sync core): ``run_competition`` is the ASYNC SHELL. It gathers every
agent's decision CONCURRENTLY per tick, each wrapped in ``asyncio.timeout`` and fail-closed.
The deterministic law (``veridex.law.recompute``), evidence binding/hashing
(``veridex.runtime.evidence``) and the baseline strategy stay SYNC and are CALLED from the
async loop — concurrency NEVER reaches the deterministic core.

This module diverges deliberately from agent-rank's run model: **one run / N concurrent agents
on identical inputs** (head-to-head comparability) rather than one-agent-per-run. The agent-rank
pitfall we fix: their agent calls had NO timeout and could hang the loop — here EVERY decide is
``asyncio.timeout``-wrapped and a timeout/exception becomes an ``error`` event, never an abort.

The three codex carry-forwards baked in:
  1. RunEvents are validated/coerced through the ``RunEvent`` schema (int, unique ``sequence_no``)
     BEFORE hashing/persistence (preserves the ``compute_evidence_hash`` determinism invariant).
  2. ``source_mode`` is validated to ∈ {``replay``, ``live``} at the run boundary.
  3. The SAME closing-horizon snapshot per market is passed to EVERY agent (CLV comparability).

This module imports neither ``agno`` (the agent path imports it lazily) nor ``psycopg`` (the
store imports it lazily); ``import veridex.runtime.orchestrator`` is offline-safe. No proof card /
anchor / rank logic lives here (those are B6/B8/B9/B10).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from veridex.config import get_settings
from veridex.ingest.marketstate import MarketState
from veridex.law.recompute import LIVE, REPLAY, recompute
from veridex.runtime.agent import (
    AGENT_ACTION_SCHEMA_VERSION,
    DEFAULT_MODEL_ID,
    agent_config_hash,
    build_decision_prompt,
    emit_agent_action_async,
)
from veridex.runtime.baseline import deterministic_baseline_action
from veridex.runtime.evidence import (
    build_raw_prescore_record,
    compute_evidence_hash,
    score_row_from_prescore,
    serialize_payload,
)
from veridex.runtime.schemas import AgentAction, RunEvent

if TYPE_CHECKING:  # avoid a runtime import cycle (store lazily imports RunResult)
    from veridex.store import Store

PROOF_MODE_REPRODUCIBLE = "reproducible"
PROOF_MODE_LLM = "LLM/evidence-verified"

# RunEvent ``event_type`` values — single source of truth so downstream consumers (B6 scoring,
# B8 proof card) match on these constants instead of bare string literals (no typo-drift). The
# string VALUES are load-bearing (they enter the canonical evidence hash) and MUST NOT change.
EVENT_TICK = "tick"
EVENT_DECISION = "decision"
EVENT_ERROR = "error"


# ---------------------------------------------------------------------------
# Agent — the async decision contract gathered by the orchestrator
# ---------------------------------------------------------------------------


@dataclass
class Agent:
    """A run participant: an identity, a proof mode, and an async ``decide`` callable.

    Attributes:
        agent_id: Stable identifier used on events, score rows, and the proof-mode map.
        proof_mode: Eligibility label (e.g. ``"reproducible"`` / ``"LLM/evidence-verified"``).
        decide: ``async def decide(market_state) -> AgentAction`` — the only seam the loop calls.
        config_hash: Optional ``config_hash(market_state) -> str`` capturing model/prompt/schema
            identity bound into each pre-score record; falls back to an agent-id-derived hash.
    """

    agent_id: str
    proof_mode: str
    decide: Callable[[MarketState], Awaitable[AgentAction]]
    config_hash: Callable[[MarketState], str] | None = field(default=None)


def deterministic_agent(agent_id: str = "deterministic-baseline") -> Agent:
    """Build the reproducible-proof baseline contestant (real, no LLM).

    Wraps the SYNC ``deterministic_baseline_action`` in an ``async def`` so it can be gathered
    alongside LLM agents without ever touching the network.

    Args:
        agent_id: Identifier for this agent (defaults to ``"deterministic-baseline"``).

    Returns:
        An :class:`Agent` whose ``proof_mode`` is ``"reproducible"``.
    """

    async def decide(market_state: MarketState) -> AgentAction:
        return deterministic_baseline_action(market_state)

    def config_hash(market_state: MarketState) -> str:
        return agent_config_hash("deterministic-baseline", "deterministic_baseline_action", AGENT_ACTION_SCHEMA_VERSION)

    return Agent(agent_id=agent_id, proof_mode=PROOF_MODE_REPRODUCIBLE, decide=decide, config_hash=config_hash)


def llm_agent(
    agent_id: str,
    *,
    model: Any = None,
    model_id: str | None = None,
    agent_factory: Callable[..., Any] | None = None,
) -> Agent:
    """Build an LLM contestant whose decisions come from ``emit_agent_action_async``.

    The ``model`` / ``agent_factory`` seams are injectable so offline tests never import agno or
    hit the network. The pre-score ``config_hash`` records the resolved model id, the per-tick
    decision prompt, and the action-schema version as evidence (resolved from ``DEFAULT_MODEL_ID``
    when ``model_id`` is not supplied, keeping the hash offline-deterministic).

    Args:
        agent_id: Identifier for this agent.
        model: Pre-built Agno model instance; forwarded to ``emit_agent_action_async``.
        model_id: Anthropic model identifier; forwarded to the agent path.
        agent_factory: Optional Agno agent factory seam (defaults to the lazy Agno factory).

    Returns:
        An :class:`Agent` whose ``proof_mode`` is ``"LLM/evidence-verified"``.
    """
    resolved_model_id = model_id or DEFAULT_MODEL_ID

    async def decide(market_state: MarketState) -> AgentAction:
        return await emit_agent_action_async(market_state, model=model, model_id=model_id, agent_factory=agent_factory)

    def config_hash(market_state: MarketState) -> str:
        return agent_config_hash(resolved_model_id, build_decision_prompt(market_state), AGENT_ACTION_SCHEMA_VERSION)

    return Agent(agent_id=agent_id, proof_mode=PROOF_MODE_LLM, decide=decide, config_hash=config_hash)


# ---------------------------------------------------------------------------
# RunResult — the scored, evidence-backed output of one competition run
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunResult:
    """The persisted result of one competition run (round-trips through a :class:`Store`).

    Attributes:
        run_id: Unique run identifier.
        source_mode: ``"replay"`` or ``"live"`` (CON-005 — carried to leaderboard rows).
        agent_ids: Participating agent identifiers, in run order.
        run_events: Validated ``RunEvent`` dicts (the hashed evidence boundary).
        score_rows: Derived per-(tick, agent) score rows, each binding its raw pre-score record.
        evidence_hash: SHA-256 over the canonical, sequence-ordered ``run_events``.
        proof_mode_map: ``agent_id -> proof_mode``.
    """

    run_id: str
    source_mode: str
    agent_ids: list[str]
    run_events: list[dict[str, Any]]
    score_rows: list[dict[str, Any]]
    evidence_hash: str
    proof_mode_map: dict[str, str]


# ---------------------------------------------------------------------------
# Event validation (carry-forward 1)
# ---------------------------------------------------------------------------


def validate_run_events(raw_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Coerce raw event dicts through the ``RunEvent`` schema BEFORE hashing/persistence.

    Validation pins ``sequence_no`` to ``int`` (so a stringy or float seq can never reach the
    evidence boundary and silently break the ``compute_evidence_hash`` sort/determinism invariant)
    and normalizes every event to the canonical ``RunEvent`` field set.

    Args:
        raw_events: Loosely-typed event dicts emitted by the run loop.

    Returns:
        A list of validated, canonical ``RunEvent`` dicts.
    """
    return [RunEvent.model_validate(event).model_dump() for event in raw_events]


# ---------------------------------------------------------------------------
# Closing-horizon snapshots (carry-forward 3)
# ---------------------------------------------------------------------------


def _closing_snapshots(marketstates: list[MarketState]) -> dict[str, MarketState]:
    """Map each ``market_key`` to its closing-horizon snapshot (the final tick containing it).

    Computed ONCE per run and shared across all agents so cross-agent CLV is comparable.

    Args:
        marketstates: The ordered tick snapshots of the run.

    Returns:
        ``market_key -> MarketState`` where the value is the last tick that carried the market.
    """
    closing_by_market: dict[str, MarketState] = {}
    for state in marketstates:  # ordered; later ticks overwrite earlier ones
        for market_key in state.markets:
            closing_by_market[market_key] = state
    return closing_by_market


# ---------------------------------------------------------------------------
# Per-agent decide — timeout-wrapped, fail-closed (CON-010)
# ---------------------------------------------------------------------------


async def _decide(
    agent: Agent, snapshot: MarketState, timeout_s: float
) -> tuple[Agent, AgentAction | None, BaseException | None]:
    """Run one agent's decide under ``asyncio.timeout``; never raise (fail-closed).

    A timeout or any exception is captured and returned as the third tuple element so the
    surrounding ``asyncio.gather`` can record an ``error`` event without aborting the run.

    Args:
        agent: The participant to decide.
        snapshot: The immutable per-tick ``MarketState`` (identical for every agent).
        timeout_s: Per-decide wall-clock budget.

    Returns:
        ``(agent, action, None)`` on success, or ``(agent, None, exception)`` on failure.
    """
    try:
        async with asyncio.timeout(timeout_s):
            action = await agent.decide(snapshot)
        return (agent, action, None)
    except Exception as exc:  # fail-closed: timeout/error → error event, never abort the run
        return (agent, None, exc)


# ---------------------------------------------------------------------------
# CompetitionRun — the incremental core (feed per tick, finalize once)
# ---------------------------------------------------------------------------


class CompetitionRun:
    """The incremental heart of a competition: ``feed()`` a snapshot per tick, ``finalize()`` once.

    This is the per-tick loop of the batch ``run_competition`` extracted into an object with an
    explicit lifecycle so live drivers (Tasks 8/15/20) can push snapshots as they arrive rather
    than pre-materializing the whole ``marketstates`` list. The split is TRUST-CRITICAL and purely
    MECHANICAL: ``feed()`` holds the ASYNC concurrency (per-tick ``asyncio.gather`` of every
    agent's fail-closed ``_decide``, REQ-2D-101 — decisions are gathered in REAL TIME, never
    buffered to finalize), and ``finalize()`` holds the SYNC deterministic seal
    (``validate_run_events`` → ``compute_evidence_hash``) and scoring pass (CON-2D-102). Driving a
    ``CompetitionRun`` via ``feed()``* + one ``finalize()`` produces a ``RunResult`` byte-identical
    to the batch wrapper on identical inputs (pinned by the golden fixtures).

    Args:
        agents: Participating agents (≥1; typically ≥1 LLM + the deterministic baseline).
        source_mode: ``"replay"`` or ``"live"`` (validated at construction — CON-005).
        run_id: Optional explicit run id (defaults to a fresh UUID hex, resolved at construction).
        decision_timeout_s: Per-decide timeout (defaults to ``get_settings().decision_timeout_s``).
        event_sink: Optional async observer. When given, EACH event appended to the run is also
            validated through ``RunEvent`` and awaited on the sink (in ``sequence_no`` order) for
            live observation/persistence. This is an ADDITIVE SHELL: the sink NEVER feeds back into
            the deterministic seal (the seal + sync scoring pass are byte-identical with or without).

    Raises:
        ValueError: If ``source_mode`` is not ``"replay"`` or ``"live"``.
    """

    def __init__(
        self,
        agents: list[Agent],
        *,
        source_mode: str,
        run_id: str | None = None,
        decision_timeout_s: float | None = None,
        event_sink: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        if source_mode not in (REPLAY, LIVE):
            raise ValueError(f"source_mode must be 'replay' or 'live', got {source_mode!r}")

        self._agents = agents
        self._source_mode = source_mode
        self._run_id = run_id or uuid.uuid4().hex
        self._timeout_s = decision_timeout_s if decision_timeout_s is not None else get_settings().decision_timeout_s
        self._event_sink = event_sink

        self._raw_events: list[dict[str, Any]] = []
        # Successful decisions deferred to the SYNC scoring pass (after the evidence hash is sealed).
        self._decisions: list[tuple[Agent, AgentAction, MarketState]] = []
        # Ordered fed snapshots — finalize() builds the closing-horizon map over the full list.
        self._snapshots: list[MarketState] = []
        self._sequence_no = 0
        self._finalized = False

    async def _emit(self, ev: dict[str, Any]) -> None:
        """Append ``ev`` to the run AND (when present) mirror it to the live ``event_sink``.

        The sink only ever receives ``RunEvent``-validated dicts — identical in shape to what
        ``validate_run_events`` later produces for the seal — so the live stream stays a faithful
        projection of the sealed record. ``_raw_events`` keeps the original (unvalidated) dict so
        the finalize-time seal is byte-identical to the no-sink path.
        """
        self._raw_events.append(ev)
        if self._event_sink is not None:
            await self._event_sink(RunEvent.model_validate(ev).model_dump())

    async def feed(self, snapshot: MarketState) -> None:
        """Ingest one tick: emit the tick event, gather ALL agents' decisions NOW, record them.

        Every agent decides CONCURRENTLY (``asyncio.gather``), each wrapped in ``asyncio.timeout``
        and fail-closed (timeout/exception → an ``error`` event). Successful ``(agent, action,
        snapshot)`` triples accumulate for the finalize-time scoring pass; the snapshot itself is
        retained so ``finalize()`` can rebuild the closing-horizon map over the full run.
        """
        self._snapshots.append(snapshot)
        await self._emit(
            {
                "sequence_no": self._sequence_no,
                "event_type": EVENT_TICK,
                "state_snapshot_json": serialize_payload(snapshot.model_dump()),
            }
        )
        self._sequence_no += 1

        results = await asyncio.gather(*[_decide(agent, snapshot, self._timeout_s) for agent in self._agents])

        for agent, action, error in results:
            if error is not None or action is None:
                await self._emit(
                    {
                        "sequence_no": self._sequence_no,
                        "event_type": EVENT_ERROR,
                        "result_payload_json": serialize_payload(
                            {
                                "agent_id": agent.agent_id,
                                "error": type(error).__name__ if error is not None else "NoAction",
                                "message": str(error) if error is not None else "agent returned no action",
                            }
                        ),
                    }
                )
            else:
                await self._emit(
                    {
                        "sequence_no": self._sequence_no,
                        "event_type": EVENT_DECISION,
                        "action_payload_json": serialize_payload(action.model_dump()),
                        "result_payload_json": serialize_payload({"agent_id": agent.agent_id}),
                    }
                )
                self._decisions.append((agent, action, snapshot))
            self._sequence_no += 1

    async def finalize(self, *, store: Store | None = None) -> RunResult:
        """Seal the run EXACTLY once: validate → hash → SYNC score → build (and persist) RunResult.

        The evidence boundary is sealed by validating every emitted event through ``RunEvent`` then
        hashing the canonical, sequence-ordered list. Scoring then runs SYNC: per successful (tick,
        agent) decision, ``recompute`` derives CLV against the SAME per-market closing snapshot
        (built over ALL fed snapshots), a raw pre-score record binds the run evidence + action +
        order + proof mode, and the score row derives ONLY from that bound hash + recomputed values
        (gate-3 ordering: tick → decision → pre-score → score).

        Raises:
            RuntimeError: If called more than once (a run seals exactly once).
        """
        if self._finalized:
            raise RuntimeError("run already finalized")

        closing_by_market = _closing_snapshots(self._snapshots)

        # --- seal the evidence boundary: validate THEN hash (carry-forward 1) --------------
        run_events = validate_run_events(self._raw_events)
        evidence_hash = compute_evidence_hash(run_events)

        # --- SYNC scoring pass: pre-score precedes score (gate-3) --------------------------
        score_rows: list[dict[str, Any]] = []
        for agent, action, snapshot in self._decisions:
            market_key = (action.params or {}).get("market_key")
            closing = closing_by_market.get(market_key) if market_key else None
            result = recompute(snapshot, action, closing=closing, source_mode=self._source_mode)

            config_hash = (
                agent.config_hash(snapshot)
                if agent.config_hash is not None
                else agent_config_hash(agent.agent_id, "", AGENT_ACTION_SCHEMA_VERSION)
            )
            prescore = build_raw_prescore_record(
                evidence_hash=evidence_hash,
                raw_action=action.model_dump(),
                action_schema_version=AGENT_ACTION_SCHEMA_VERSION,
                agent_id=agent.agent_id,
                model_prompt_config_hash=config_hash,
                tick_seq=snapshot.tick_seq,
                proof_mode=agent.proof_mode,
            )
            score = score_row_from_prescore(
                raw_prescore_hash=prescore["raw_prescore_hash"],
                recomputed_edge_bps=int(result["edge_bps"]),
            )
            score_rows.append(
                {
                    **score,
                    "agent_id": agent.agent_id,
                    "tick_seq": snapshot.tick_seq,
                    "proof_mode": agent.proof_mode,
                    "clv_bps": result["clv_bps"],
                    "valid": result["valid"],
                    "reason": result["reason"],
                    "kelly_fraction": result["kelly_fraction"],
                    "raw_prescore": prescore,
                }
            )

        run_result = RunResult(
            run_id=self._run_id,
            source_mode=self._source_mode,
            agent_ids=[agent.agent_id for agent in self._agents],
            run_events=run_events,
            score_rows=score_rows,
            evidence_hash=evidence_hash,
            proof_mode_map={agent.agent_id: agent.proof_mode for agent in self._agents},
        )

        if store is not None:
            await store.persist_run(run_result)

        self._finalized = True
        return run_result


# ---------------------------------------------------------------------------
# run_competition — the batch wrapper over CompetitionRun
# ---------------------------------------------------------------------------


async def run_competition(
    marketstates: list[MarketState],
    agents: list[Agent],
    *,
    source_mode: str,
    store: Store | None = None,
    decision_timeout_s: float | None = None,
    run_id: str | None = None,
    event_sink: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> RunResult:
    """Drive ingest → concurrent agent decisions → law recompute → evidence → score rows.

    Thin batch wrapper over :class:`CompetitionRun`: construct the run, ``feed()`` every snapshot
    in order, then ``finalize()`` once. Byte-identical output to the pre-extraction loop (pinned by
    ``tests/test_orchestrator_golden.py``). For each tick, all agents decide CONCURRENTLY
    (``asyncio.gather``), each wrapped in ``asyncio.timeout`` and fail-closed (timeout/exception →
    an ``error`` event). RunEvents (tick + decision + error) form the hashed evidence boundary;
    they are validated through ``RunEvent`` before hashing. Scoring then runs SYNC: per successful
    (tick, agent) decision, ``recompute`` derives CLV against the SAME per-market closing snapshot,
    a raw pre-score record binds the run evidence + action + order + proof mode, and the score row
    derives ONLY from that bound hash + recomputed values (gate-3 ordering: tick → decision →
    pre-score → score).

    Args:
        marketstates: Ordered tick snapshots of the run (identical inputs for every agent).
        agents: Participating agents (≥1; typically ≥1 LLM + the deterministic baseline).
        source_mode: ``"replay"`` or ``"live"`` (validated at the boundary).
        store: Optional async store; when given, the run is persisted before returning.
        decision_timeout_s: Per-decide timeout (defaults to ``get_settings().decision_timeout_s``).
        run_id: Optional explicit run id (defaults to a fresh UUID hex).
        event_sink: Optional async observer. When given, EACH event appended to the run is also
            validated through ``RunEvent`` and awaited on the sink (in ``sequence_no`` order) for
            live observation/persistence. This is an ADDITIVE SHELL: the sink NEVER feeds back
            into the deterministic seal (``validate_run_events`` / ``compute_evidence_hash`` / the
            sync scoring pass remain byte-identical whether or not a sink is supplied).

    Returns:
        The scored, evidence-backed :class:`RunResult`.

    Raises:
        ValueError: If ``source_mode`` is not ``"replay"`` or ``"live"``.
    """
    run = CompetitionRun(
        agents,
        source_mode=source_mode,
        run_id=run_id,
        decision_timeout_s=decision_timeout_s,
        event_sink=event_sink,
    )
    for snapshot in marketstates:
        await run.feed(snapshot)
    return await run.finalize(store=store)
