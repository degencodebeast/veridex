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
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from veridex.api.auth_privy import PrivyPrincipal, make_require_principal
from veridex.api.demo_fixtures import build_demo_ticks
from veridex.chain.anchor import anchor_memo
from veridex.deploy.attempt import AttemptStatus, DeploymentAttempt, DuplicateAttemptError
from veridex.deploy.instance import AgentInstance, DeployFailureReason, DeployStatus
from veridex.deploy.preflight import DeployConfig, PreflightCheck, run_deploy_preflight
from veridex.ingest.feed_health import FeedHealthReport
from veridex.ingest.marketstate import MarketState
from veridex.runtime.orchestrator import Agent
from veridex.runtime.runtime_events import RuntimeEventSink
from veridex.runtime.window import RunWindow
from veridex_agent.config import AgentRunConfig, build_agent
from veridex_agent.run import standalone_run

if TYPE_CHECKING:
    from veridex.config import Settings
    from veridex.store import Store

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (the deploy-record timestamp stamp)."""
    return datetime.now(tz=UTC).isoformat()


# ---------------------------------------------------------------------------
# Response envelope (the durable AgentInstance record lives in veridex.deploy.instance)
# ---------------------------------------------------------------------------


class DeployResponse(BaseModel):
    """Response envelope for ``POST /agents/deploy``.

    Attributes:
        instance_id: The pinned :class:`AgentInstance` id.
        config_hash: Pinned config hash.
        policy_hash: Pinned policy-envelope hash.
        run_id: The launched run id (returned WITHOUT awaiting the seal).
        owner: The SERVER-DERIVED owner identity — the authenticated Privy principal's DID
            (``did:privy:...``). Derived from the verified access token, NEVER the request body.
    """

    instance_id: str
    config_hash: str
    policy_hash: str
    run_id: str
    owner: str


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


def _build_run_config(config: DeployConfig) -> AgentRunConfig:
    """Map the validated wire config onto the typed, bounded :class:`AgentRunConfig` (single seam).

    This is the ONE place a :class:`DeployConfig` becomes the runner's config — used both to build
    the agent and to snapshot the EFFECTIVE (normalized) config onto the durable instance record.
    The AgentRunConfig's own Field bounds re-validate here as defense-in-depth (preflight passed).
    """
    return AgentRunConfig(
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


def _build_agent(config: DeployConfig) -> Agent:
    """Construct the deployed agent through the SINGLE ``build_agent`` dispatch (no parallel builder).

    Delegates to :func:`~veridex_agent.config.build_agent` via :func:`_build_run_config`, so the
    flagship ``momentum-sharp`` v2 (and every strategy) is constructed in exactly one place.
    """
    return build_agent(_build_run_config(config))


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


def register_deploy_routes(
    app: FastAPI,
    *,
    store: Store,
    settings: Settings,
    deploy_deps: DeployDeps | None = None,
    runtime_event_sink: RuntimeEventSink | None = None,
) -> None:
    """Mount ``POST /agents/deploy`` and its background-task lifecycle on ``app``.

    Args:
        app: The FastAPI application to mount on.
        store: The shared async store — the deployed run is persisted here (under the pre-known
            ``run_id``) at seal time so ``/runs/{id}/verify`` can load it (one flow to proof).
        settings: Resolved settings carrying the Privy auth boundary (``auth_mode`` + verifier
            material) used to build the ``require_principal`` dependency (I-1).
        deploy_deps: Injected offline dependencies (tests); ``None`` → the real live path.
        runtime_event_sink: The ONE shared OPS-channel sink (I-4) the launched ``standalone_run``
            emits lifecycle/decision telemetry through — durably spooled by the app's
            :class:`~veridex.runtime.runtime_store.DurableRuntimeEventStore`. ``None`` emits nothing.
    """
    deps = deploy_deps if deploy_deps is not None else DeployDeps()
    # I-1 auth boundary: a valid Privy access token is required (in ``privy`` mode) BEFORE the deploy
    # body handler runs, so a 401 precedes every persistence/wallet/runtime side effect.
    require_principal = make_require_principal(settings)

    # app.state holds ONLY the live background run-task handles (cancellable on shutdown) — the
    # cancellation bookkeeping. The durable AgentInstance record is the STORE's job (source of
    # truth), NOT app.state: it survives a process restart and an app.state clear.
    background_tasks: set[asyncio.Task[None]] = set()
    app.state.deploy_background_tasks = background_tasks

    def _resolve_replay_marketstates() -> list[MarketState]:
        """Resolve the REPLAY source ticks: injected fakes (tests) or the in-code demo fixture.

        The default mounted route has no injected ``deps.marketstates``, so a ``replay`` deploy
        sources the SAME deterministic, zero-I/O demo fixture the ``/demo/run`` route uses
        (:func:`~veridex.api.demo_fixtures.build_demo_ticks`). This is a REAL replay over recorded
        demo ticks (honestly labeled REPLAY, NEVER live) — it makes the headline flow demonstrable
        from the real app with no injected deps and no bundled-pack file on disk (REQ-2D-703).
        """
        if deps.marketstates is not None:
            return list(deps.marketstates)
        return build_demo_ticks()

    async def _launch(config: DeployConfig, run_id: str, instance_id: str, marketstates: list[MarketState]) -> None:
        """Run the deployed agent through the SINGLE seam, durably tracking the instance status.

        Advances the STORED instance ``running`` → ``sealed`` (clean seal, run persisted under
        ``run_id``) or ``failed`` (pre-seal error, with a bounded ``last_failure_reason``) — so the
        outcome survives beyond process memory. A shutdown cancellation is neither: the record is
        left in ``running`` (honest — it was running when the process was cancelled).
        """
        await store.update_agent_instance_status(instance_id, DeployStatus.RUNNING, updated_at=_now_iso())
        # Controlled failure taxonomy (never a raw trace): a failure while constructing the agent is
        # a RUNTIME_ERROR; once we enter the runner seam it is a SEAL_FAILED. The FULL diagnostic is
        # logged with exc_info by the done-callback below — only this short reason is persisted.
        phase = DeployFailureReason.RUNTIME_ERROR
        try:
            agent = _build_agent(config)
            envelope = config.to_policy_envelope()
            # paper is proof-only (no envelope → no execution lane); non-paper engages the lane.
            lane_envelope = None if config.execution_mode == "paper" else envelope
            config_hash = config.config_hash()

            phase = DeployFailureReason.SEAL_FAILED
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
                    runtime_event_sink=runtime_event_sink,
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
                    runtime_event_sink=runtime_event_sink,
                    anchor_fn=deps.anchor_fn,
                )
        except asyncio.CancelledError:
            # Shutdown cancellation is neither a seal nor a failure — leave the record RUNNING.
            raise
        except Exception:
            # Pre-seal failure: durably mark FAILED with the CONTROLLED reason (no raw trace), then
            # re-raise so the done-callback surfaces the FULL diagnostic on the server log (never
            # lost to GC, never persisted to the record).
            await store.update_agent_instance_status(
                instance_id,
                DeployStatus.FAILED,
                last_failure_reason=phase,
                updated_at=_now_iso(),
            )
            raise
        # Clean seal: the run is persisted under run_id — durably mark the instance SEALED.
        await store.update_agent_instance_status(instance_id, DeployStatus.SEALED, updated_at=_now_iso())

    @app.post("/agents/deploy", response_model=DeployResponse)
    async def deploy_agent(
        config: DeployConfig,
        principal: PrivyPrincipal = Depends(require_principal),  # noqa: B008
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),  # noqa: B008
    ) -> DeployResponse:
        """Preflight, pin the instance, and launch the run asynchronously (fail-closed).

        The ``require_principal`` dependency resolves BEFORE this body — an unauthenticated or
        bad-token request 401s before any preflight, persistence, or run launch. ``principal.did``
        is the SERVER-DERIVED owner; a client-supplied owner (which ``DeployConfig`` drops anyway)
        is never trusted.

        Args:
            config: The typed, submitted Studio config (bad types are rejected 422 by pydantic).
            principal: The authenticated Privy principal (injected by ``require_principal``).
            idempotency_key: The optional ``Idempotency-Key`` header — a caller-scoped key that makes
                the deploy idempotent (same key ⇒ same instance, never a duplicate). Absent ⇒ a fresh
                per-request key is minted (each call is a distinct deploy — the prior behavior).

        Returns:
            A :class:`DeployResponse` with the pinned instance, the launched ``run_id``, and the
            server-derived ``owner`` — returned WITHOUT awaiting the window seal.

        Raises:
            HTTPException: 401 before any side effect if auth fails; 422 naming every failing
                preflight check; 409 if the idempotency key is reused with a different config or the
                recorded attempt is unrecoverable; NO run starts on failure.
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

        # ATTEMPT-FIRST saga backbone (I-3): persist a durable DeploymentAttempt BEFORE the instance
        # side effect. The attempt CLAIMS (operator_id, idempotency_key) under a UNIQUE constraint and
        # pre-allocates the deterministic instance_id it targets — so a retry (or a concurrent
        # duplicate) reconciles to the SAME instance via the recorded state, never a blind re-execute.
        operator_id = principal.did
        idem_key = idempotency_key if idempotency_key is not None else uuid.uuid4().hex
        config_fingerprint = config.config_hash()
        now = _now_iso()
        attempt_id = uuid.uuid4().hex
        instance_id = f"inst_{attempt_id}"
        try:
            await store.persist_deployment_attempt(
                DeploymentAttempt(
                    attempt_id=attempt_id,
                    operator_id=operator_id,
                    idempotency_key=idem_key,
                    config_fingerprint=config_fingerprint,
                    status=AttemptStatus.PENDING,
                    created_at=now,
                    instance_id=instance_id,
                    external_id=None,
                )
            )
        except DuplicateAttemptError:
            # The key is already claimed (a prior deploy or a concurrent duplicate) — reconcile to the
            # recorded attempt; NEVER mint a second instance for the same logical deploy.
            recorded = await store.get_deployment_attempt_by_key(operator_id, idem_key)
            assert recorded is not None  # the UNIQUE claim we just collided with exists
            if recorded.config_fingerprint != config_fingerprint:
                # Same key, different config → refuse (never silently reuse or overwrite).
                raise HTTPException(
                    status_code=409,
                    detail={"error": "idempotency_key_conflict", "idempotency_key": idem_key},
                ) from None
            # Fail-closed: a claim with no pre-allocated target instance (never produced by this
            # route) cannot be safely reconciled — refuse rather than guess.
            target_instance_id = recorded.instance_id
            if target_instance_id is None:
                raise HTTPException(
                    status_code=409,
                    detail={"error": "deployment_attempt_unrecoverable", "status": recorded.status.value},
                ) from None
            try:
                existing = await store.get_agent_instance(target_instance_id)
            except KeyError:
                existing = None
            if existing is not None:
                # The side effect already completed — return the SAME instance (idempotent replay /
                # crash-recovered), launching NO second run.
                return DeployResponse(
                    instance_id=existing.instance_id,
                    config_hash=existing.config_hash,
                    policy_hash=existing.policy_hash,
                    run_id=existing.run_id,
                    owner=operator_id,
                )
            # No instance yet. Fail-closed: only re-drive from the known-safe PENDING claim; any other
            # recorded state is treated as unrecoverable and NEVER auto-retries a side effect.
            if recorded.status is not AttemptStatus.PENDING:
                raise HTTPException(
                    status_code=409,
                    detail={"error": "deployment_attempt_unrecoverable", "status": recorded.status.value},
                ) from None
            # Re-drive under the recorded attempt: reuse its write-once deterministic instance_id so
            # the idempotent (upsert) instance write reconciles to exactly ONE instance.
            instance_id = target_instance_id

        # Preflight passed → pin config_hash (only now) + policy_hash into the AgentInstance.
        run_id = uuid.uuid4().hex
        instance = AgentInstance(
            instance_id=instance_id,
            template_id=config.template_id,
            agent_id=config.agent_id,
            submitted_config=config.model_dump(mode="json"),
            effective_config=_build_run_config(config).model_dump(mode="json"),
            config_hash=config.config_hash(),
            policy_hash=envelope.policy_hash(),
            source_mode=config.source_mode,
            execution_mode=config.execution_mode,
            market_allowlist=list(config.market_allowlist),
            venue_allowlist=list(config.venue_allowlist),
            run_id=run_id,
            # Durable "why did this launch?" audit: the named preflight verdicts that GATED it
            # (all passing/not-applicable — a failing preflight never reaches here).
            preflight_checks=list(checks),
            status=DeployStatus.PENDING,
            last_failure_reason=None,
            # AC-18: persist the SERVER-DERIVED owner (the verified token's DID). A client-supplied
            # owner/operator_id in the body is never read — DeployConfig drops it and we never look.
            operator_id=principal.did,
            # runtime_handle is minted LATER by the runtime infra (replaceable, re-mintable under the
            # same run_id) — None at deploy time; it is never the ownership/result authority.
            runtime_handle=None,
            created_at=now,
            updated_at=now,
        )
        # PERSIST-THEN-LAUNCH: the durable record is written to the STORE (source of truth) AFTER
        # preflight passes and BEFORE the run launches — so a preflight failure leaves no row, and a
        # deployed instance is never app.state-only. app.state carries only the task handle below.
        await store.persist_agent_instance(instance)

        # Launch ASYNCHRONOUSLY: track the task + auto-discard on completion (cancellable on shutdown).
        task: asyncio.Task[None] = asyncio.create_task(
            _launch(config, run_id, instance.instance_id, replay_marketstates)
        )
        background_tasks.add(task)

        def _on_done(finished: asyncio.Task[None], *, launched_run_id: str = run_id) -> None:
            """Discard the finished task AND SURFACE a background-task failure (never lose it to GC).

            A cancelled task (shutdown) carries no error. Any other exception is a real failure of
            the deploy background task — the pre-seal seal/verify/anchor/persist path OR the POST-seal
            status write to ``SEALED`` (the execution lane is already isolated inside
            ``standalone_run``). Log it with the ``run_id`` so an operator can explain why a
            subsequent ``/runs/{id}/verify`` 404s (a pre-seal failure) or why the instance status is
            stale (a post-seal write failure), instead of relying on asyncio's GC warning.
            """
            background_tasks.discard(finished)
            if finished.cancelled():
                return
            exc = finished.exception()
            if exc is not None:
                # "background task failed" — accurate whether the exception was raised pre-seal or in
                # the post-seal status write; asserting "pre-seal" here would mislabel the latter.
                logger.error("deployed run %s background task failed", launched_run_id, exc_info=exc)

        task.add_done_callback(_on_done)

        return DeployResponse(
            instance_id=instance.instance_id,
            config_hash=instance.config_hash,
            policy_hash=instance.policy_hash,
            run_id=run_id,
            owner=principal.did,
        )

    @app.get("/agents/instances", response_model=list[AgentInstance])
    async def list_agent_instances(
        principal: PrivyPrincipal = Depends(require_principal),  # noqa: B008
    ) -> list[AgentInstance]:
        """List the AUTHENTICATED caller's own deployed instances (owner-scoped, fail-closed).

        The ``require_principal`` dependency 401s an unauthenticated request before this body. Only
        instances whose SERVER-PERSISTED ``operator_id`` equals ``principal.did`` are returned — an
        UNOWNED / legacy row (``operator_id is None``) is NEVER inherited by any caller (fail-closed).

        Args:
            principal: The authenticated Privy principal (injected by ``require_principal``).

        Returns:
            The caller's own :class:`AgentInstance` records (possibly empty), newest-order-agnostic.
        """
        instances = await store.list_agent_instances()
        return [inst for inst in instances if inst.operator_id is not None and inst.operator_id == principal.did]

    @app.get("/agents/instances/{instance_id}", response_model=AgentInstance)
    async def get_agent_instance(
        instance_id: str,
        principal: PrivyPrincipal = Depends(require_principal),  # noqa: B008
    ) -> AgentInstance:
        """Fetch ONE deployed instance the caller owns (owner resolved server-side, fail-closed).

        Ownership is derived from the STORED ``operator_id`` (never the request): a missing instance,
        an UNOWNED / legacy row (``operator_id is None`` — never inherited), or a mismatch each refuses
        access. A 404 is returned for "absent or not yours" so a non-owner cannot even probe existence;
        a genuinely owned-by-another row 403s.

        Args:
            instance_id: The instance to load.
            principal: The authenticated Privy principal (injected by ``require_principal``).

        Returns:
            The caller's :class:`AgentInstance`.

        Raises:
            HTTPException: 404 if absent or UNOWNED (fail-closed, no existence leak); 403 if owned by
                another principal.
        """
        try:
            instance = await store.get_agent_instance(instance_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="agent instance not found") from exc
        # Fail-closed: an unowned / legacy row is never inherited — hide it as if it does not exist.
        if instance.operator_id is None:
            raise HTTPException(status_code=404, detail="agent instance not found")
        if instance.operator_id != principal.did:
            raise HTTPException(status_code=403, detail="principal does not own this agent instance")
        return instance


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
