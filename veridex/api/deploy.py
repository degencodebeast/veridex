"""Studio deploy route — ``POST /agents/deploy`` (REQ-2D-701 / REQ-2D-702 / REQ-2D-703).

The core product loop: configure → preflight → deploy → observe → verify. A submitted
:class:`~veridex.deploy.preflight.DeployConfig` is TYPED at the wire, BOUNDED by the fail-closed
NAMED preflight, and only then pinned as an :class:`AgentInstance` (config_hash + policy_hash +
template + allowlist + modes). The run is launched ASYNCHRONOUSLY through the SINGLE runner seam
(:func:`veridex_agent.run.standalone_run`) — the response returns ``run_id`` WITHOUT awaiting the
window seal; the background task is tracked on ``app.state`` and cancelled on shutdown. The deployed
run's sealed window verifies via the SAME ``/runs/{id}/verify`` path as an arena run (one flow to
proof). There is NO parallel runner and NO pause/resume/kill beyond shutdown-cancel (CON-2D-701).

Dependency injection: :class:`DeployDeps` lets tests supply an offline feed report, a market-resolved
flag, a fake tick stream + close fetch, and ``anchor_fn=None`` so the whole path runs with ZERO
network. The defaults represent the real live path (real stream/fetch, real anchor).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from veridex.chain.anchor import anchor_memo
from veridex.deploy.bundled_replay import load_bundled_replay_marketstates
from veridex.deploy.preflight import DeployConfig, PreflightCheck, run_deploy_preflight
from veridex.ingest.feed_health import FeedHealthReport
from veridex.ingest.marketstate import MarketState
from veridex.runtime.orchestrator import Agent
from veridex.runtime.window import RunWindow
from veridex_agent.config import AgentRunConfig, build_agent
from veridex_agent.run import standalone_run

if TYPE_CHECKING:
    from veridex.store import Store

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pinned instance + response envelope
# ---------------------------------------------------------------------------


class AgentInstance(BaseModel):
    """The PINNED deployment record — the instance IS the deployment (no separate deployment_id).

    Attributes:
        instance_id: Stable identifier for this deployed instance.
        agent_id: The deployed agent's identifier.
        template_id: The strategy-archetype template the instance was configured from.
        config_hash: SHA-256 of the submitted (validated) config — pinned only after preflight.
        policy_hash: SHA-256 of the committed policy envelope.
        market_allowlist: The pinned market universe.
        source_mode: ``replay`` or ``live``.
        execution_mode: ``paper`` | ``dry_run`` | ``live_guarded``.
        run_id: The run this instance launched (known before the seal — the async handle).
    """

    instance_id: str
    agent_id: str
    template_id: str
    config_hash: str
    policy_hash: str
    market_allowlist: list[str]
    source_mode: str
    execution_mode: str
    run_id: str


class DeployResponse(BaseModel):
    """Response envelope for ``POST /agents/deploy``.

    Attributes:
        instance_id: The pinned :class:`AgentInstance` id.
        config_hash: Pinned config hash.
        policy_hash: Pinned policy-envelope hash.
        run_id: The launched run id (returned WITHOUT awaiting the seal).
    """

    instance_id: str
    config_hash: str
    policy_hash: str
    run_id: str


# ---------------------------------------------------------------------------
# Dependency injection (defaults = real live path; tests inject offline fakes)
# ---------------------------------------------------------------------------


@dataclass
class DeployDeps:
    """Injectable inputs for the deploy route (tests supply offline fakes).

    Attributes:
        feed_report: The live/replay feed-health report used by preflight. ``None`` → an offline
            report is treated as not-connected for a live deploy (fail-closed).
        market_resolved: Whether the target market resolved to concrete identifiers (only gates a
            ``live_guarded`` deploy). ``None`` → unknown (fails a ``live_guarded`` deploy closed).
        stream_factory: ``config -> AsyncIterator[MarketState]`` for the live launch path; ``None``
            → the real TxLINE stream inside the runner seam.
        fetch_updates: ``async (fixture_id) -> updates`` for the CON-040 close; ``None`` → the real
            fetch inside the seam.
        marketstates: Replay tick snapshots for a ``source_mode == "replay"`` deploy (tests).
        adapter: Injected venue adapter for the dry_run execution lane; ``None`` → picked by mode.
        anchor_fn: ``async (manifest_hash) -> signature``; defaults to the real on-chain anchor.
            Tests pass ``None`` to skip anchoring (offline).
    """

    feed_report: FeedHealthReport | None = None
    market_resolved: bool | None = None
    stream_factory: Callable[[DeployConfig], AsyncIterator[MarketState]] | None = None
    fetch_updates: Callable[[int], Awaitable[list[dict[str, Any]]]] | None = None
    marketstates: list[MarketState] | None = None
    adapter: Any | None = None
    anchor_fn: Callable[[str], Awaitable[str]] | None = field(default=anchor_memo)


# ---------------------------------------------------------------------------
# Agent + window builders
# ---------------------------------------------------------------------------


def _build_agent(config: DeployConfig) -> Agent:
    """Construct the deployed agent through the SINGLE ``build_agent`` dispatch (no parallel builder).

    Maps the validated wire config onto an :class:`~veridex_agent.config.AgentRunConfig` (the typed,
    bounded config the CLI also uses) and delegates to :func:`~veridex_agent.config.build_agent`, so
    the flagship ``momentum-sharp`` v2 (and every strategy) is constructed in exactly one place. The
    AgentRunConfig's own Field bounds re-validate here as defense-in-depth — preflight already passed,
    so construction is safe.
    """
    run_config = AgentRunConfig(
        agent_id=config.agent_id,
        strategy=config.strategy,
        source_mode=config.source_mode,
        execution_mode=config.execution_mode,
        market_allowlist=list(config.market_allowlist),
        venue_allowlist=list(config.venue_allowlist),
        min_edge_bps=config.min_edge_bps,
        max_stake=config.max_stake,
        window_id=config.window_id,
        fixture_id=config.fixture_id,
        end_rule=config.end_rule,
        duration_s=config.duration_s,
        min_clv_horizon_s=config.min_clv_horizon_s,
        lookback=config.lookback,
        alpha=config.alpha,
        z_threshold=config.z_threshold,
        ph_delta=config.ph_delta,
        ph_lambda=config.ph_lambda,
        cooldown_ticks=config.cooldown_ticks,
        warmup_ticks=config.warmup_ticks,
        min_movements=config.min_movements,
        scale_floor=config.scale_floor,
        persistence_logit=config.persistence_logit,
    )
    return build_agent(run_config)


def _build_window(config: DeployConfig) -> RunWindow:
    """Build the live coverage window from the config (shares the market allowlist with policy)."""
    return RunWindow(
        window_id=config.window_id,
        fixture_id=config.fixture_id,
        market_allowlist=config.market_allowlist,
        end_rule=config.end_rule,
        duration_s=config.duration_s,
        min_clv_horizon_s=config.min_clv_horizon_s,
    )


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_deploy_routes(app: FastAPI, *, store: Store, deploy_deps: DeployDeps | None = None) -> None:
    """Mount ``POST /agents/deploy`` and its background-task lifecycle on ``app``.

    Args:
        app: The FastAPI application to mount on.
        store: The shared async store — the deployed run is persisted here (under the pre-known
            ``run_id``) at seal time so ``/runs/{id}/verify`` can load it (one flow to proof).
        deploy_deps: Injected offline dependencies (tests); ``None`` → the real live path.
    """
    deps = deploy_deps if deploy_deps is not None else DeployDeps()

    # app.state registries: tracked background run tasks (cancellable on shutdown) + pinned instances.
    background_tasks: set[asyncio.Task[None]] = set()
    instances: dict[str, AgentInstance] = {}
    app.state.deploy_background_tasks = background_tasks
    app.state.deploy_instances = instances

    def _resolve_replay_marketstates() -> list[MarketState]:
        """Resolve the REPLAY source ticks: injected fakes (tests) or the REAL bundled ReplayPack.

        The default mounted route has no injected ``deps.marketstates``, so a ``replay`` deploy
        sources a REAL bundled ReplayPack (recorded demo ticks; NEVER live) — this is what makes the
        headline flow demonstrable from the real app without any test injection (REQ-2D-703).
        """
        if deps.marketstates is not None:
            return list(deps.marketstates)
        return list(load_bundled_replay_marketstates())

    async def _launch(config: DeployConfig, run_id: str, marketstates: list[MarketState]) -> None:
        """Run the deployed agent through the SINGLE seam, persisting the seal under ``run_id``."""
        agent = _build_agent(config)
        envelope = config.to_policy_envelope()
        # paper is proof-only (no envelope → no execution lane); non-paper engages the lane.
        lane_envelope = None if config.execution_mode == "paper" else envelope
        config_hash = config.config_hash()

        if config.source_mode == "live":
            await standalone_run(
                [],
                agent,
                window=_build_window(config),
                stream=deps.stream_factory(config) if deps.stream_factory is not None else None,
                fetch_updates=deps.fetch_updates,
                policy_envelope=lane_envelope,
                execution_mode=config.execution_mode,
                adapter=deps.adapter,
                config_hash=config_hash,
                run_id=run_id,
                store=store,
                anchor_fn=deps.anchor_fn,
            )
        else:
            await standalone_run(
                marketstates,
                agent,
                source_mode="replay",
                policy_envelope=lane_envelope,
                execution_mode=config.execution_mode,
                adapter=deps.adapter,
                config_hash=config_hash,
                run_id=run_id,
                store=store,
                anchor_fn=deps.anchor_fn,
            )

    @app.post("/agents/deploy", response_model=DeployResponse)
    async def deploy_agent(config: DeployConfig) -> DeployResponse:
        """Preflight, pin the instance, and launch the run asynchronously (fail-closed).

        Args:
            config: The typed, submitted Studio config (bad types are rejected 422 by pydantic).

        Returns:
            A :class:`DeployResponse` with the pinned instance + the launched ``run_id`` — returned
            WITHOUT awaiting the window seal.

        Raises:
            HTTPException: 422 naming every failing preflight check; NO run starts on failure.
        """
        envelope = config.to_policy_envelope()
        # MODE-AWARE source resolution: a replay deploy resolves its SOURCE (bundled/injected pack)
        # BEFORE preflight, so the named feed_health check verifies the replay source resolves
        # (non-empty) rather than a live feed. Live resolves nothing here — it is gated fail-closed
        # by the feed report (the correct 422 until a live feed is wired).
        replay_marketstates: list[MarketState] = []
        source_resolved: bool | None = None
        if config.source_mode == "replay":
            replay_marketstates = _resolve_replay_marketstates()
            source_resolved = len(replay_marketstates) > 0
        checks: list[PreflightCheck] = run_deploy_preflight(
            config,
            feed_report=deps.feed_report,
            market_resolved=deps.market_resolved,
            envelope=envelope,
            source_resolved=source_resolved,
        )
        failed = [c.name for c in checks if c.ok is False]
        if failed:
            # Fail-closed: name every failing check; no instance is pinned, no run is launched.
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "preflight_failed",
                    "failed_checks": failed,
                    "checks": [c.model_dump() for c in checks],
                },
            )

        # Preflight passed → pin config_hash (only now) + policy_hash into the AgentInstance.
        run_id = uuid.uuid4().hex
        instance = AgentInstance(
            instance_id=f"inst_{uuid.uuid4().hex}",
            agent_id=config.agent_id,
            template_id=config.template_id,
            config_hash=config.config_hash(),
            policy_hash=envelope.policy_hash(),
            market_allowlist=list(config.market_allowlist),
            source_mode=config.source_mode,
            execution_mode=config.execution_mode,
            run_id=run_id,
        )
        instances[instance.instance_id] = instance

        # Launch ASYNCHRONOUSLY: track the task + auto-discard on completion (cancellable on shutdown).
        task: asyncio.Task[None] = asyncio.create_task(_launch(config, run_id, replay_marketstates))
        background_tasks.add(task)

        def _on_done(finished: asyncio.Task[None], *, launched_run_id: str = run_id) -> None:
            """Discard the finished task AND SURFACE a pre-seal failure (never lose it to GC).

            A cancelled task (shutdown) carries no error. Any other exception is a real failure of
            the seal/verify/anchor/persist path (the execution lane is already isolated inside
            ``standalone_run``) — log it with the ``run_id`` so an operator can explain why a
            subsequent ``/runs/{id}/verify`` 404s, instead of relying on asyncio's GC warning.
            """
            background_tasks.discard(finished)
            if finished.cancelled():
                return
            exc = finished.exception()
            if exc is not None:
                logger.error("deployed run %s failed pre-seal", launched_run_id, exc_info=exc)

        task.add_done_callback(_on_done)

        return DeployResponse(
            instance_id=instance.instance_id,
            config_hash=instance.config_hash,
            policy_hash=instance.policy_hash,
            run_id=run_id,
        )


async def cancel_deploy_tasks(app: FastAPI) -> None:
    """Cancel every tracked background deploy-run task (bounded shutdown; no orphaned runs).

    Called from the app lifespan's shutdown phase (see :func:`veridex.api.router.create_app`). Safe
    to call when no deploy routes were mounted — an absent registry is treated as empty.

    Args:
        app: The FastAPI application whose ``state.deploy_background_tasks`` to drain.
    """
    tasks: set[asyncio.Task[None]] = getattr(app.state, "deploy_background_tasks", set())
    for task in list(tasks):
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
