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
from veridex.config import require_privy_provisioning
from veridex.deploy.attempt import AttemptStatus, DeploymentAttempt, DuplicateAttemptError
from veridex.deploy.instance import AgentInstance, DeployFailureReason, DeployStatus
from veridex.deploy.preflight import MM_STRATEGY_FAMILY, DeployConfig, PreflightCheck, run_deploy_preflight
from veridex.ingest.feed_health import FeedHealthReport
from veridex.ingest.marketstate import MarketState
from veridex.mm_strategy.session_factory import build_maker_policy_envelope, reconstruct_mm_session
from veridex.runtime.mm_agent_adapter import VeridexAgentAdapter, build_market_maker_driver
from veridex.runtime.orchestrator import Agent
from veridex.runtime.runtime_events import RuntimeEventSink
from veridex.runtime.window import RunWindow
from veridex_agent.config import AgentRunConfig, build_agent
from veridex_agent.run import standalone_run

if TYPE_CHECKING:
    from pathlib import Path

    from veridex.config import Settings
    from veridex.dust_execution.facade import MMExecutionToolResult
    from veridex.mm_strategy.contracts import StrategyState
    from veridex.mm_strategy.session_factory import MakerReplayTape
    from veridex.runtime.mm_agent_adapter import RunContext
    from veridex.store import Store

logger = logging.getLogger(__name__)

#: The states in which an MM deploy's wallet-provisioning saga is INCOMPLETE (II-5b). A duplicate/retry
#: deploy for such an attempt must re-drive provisioning recovery (reconcile by the recorded external_id)
#: rather than returning the instance as if it were complete. ``PENDING`` is included: a crash AFTER the
#: instance is persisted but BEFORE ``_launch_mm``'s first provisioning advance leaves the attempt at
#: ``PENDING`` with no external_id yet — and ``provision_execution_wallet`` derives + persists the
#: external_id before its first provider mutation, so re-entering from ``PENDING`` is safe (MAJOR-1).
_INCOMPLETE_PROVISIONING_STATES: frozenset[AttemptStatus] = frozenset(
    {
        AttemptStatus.PENDING,
        AttemptStatus.WALLET_REQUESTED,
        AttemptStatus.WALLET_CREATE_UNCERTAIN,
        AttemptStatus.WALLET_CREATED,
        AttemptStatus.WALLET_BOUND,
        AttemptStatus.BINDING_PERSIST_FAILED,
    }
)

#: Bounded re-entries for a provisioning CAS loss to a CONCURRENT coherent driver (same attempt). The
#: attempt status advances monotonically through a small fixed set of states, so a coherent race
#: converges in a handful of re-entries; exhausting them fails closed.
_PROVISION_CAS_MAX_REENTRIES: int = 8


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
        mm_tape_resolver: SERVER-SIDE injectable ``tape_ref -> MakerReplayTape`` seam for the
            ``quoteguard-mm`` session factory (II-5 requirement 3) — NEVER a request field. ``None``
            → the production catalog resolver (``fu-ii5-demo-tape`` owed; fails closed until banked).
        mm_proposer: SERVER-SIDE injectable dry-run R4-A proposer. ``None`` → the production
            :class:`~veridex.mm_strategy.offline_proposer.OfflineRecordingProposer` (offline, no wire).
        mm_seed_state: SERVER-SIDE injectable warm ``StrategyState`` the maker fold starts from.
            ``None`` → a fresh (cold-start) state — the honest default for a brand-new deploy.
        mm_session_dir: SERVER-SIDE injectable durable recorder session directory. ``None`` → a
            per-run temp directory.
        provisioning_provider: SERVER-SIDE injectable II-5b Privy execution-wallet provisioning
            provider (a recording fake at Levels 1-2). ``None`` → provisioning is not attempted
            (the II-5 behavior). Provisioning ALSO requires the operator-pinned custody config
            (:func:`~veridex.config.require_privy_provisioning`); either absent → no wallet is
            provisioned. NEVER a live Privy client here, and NEVER a request field.
    """

    feed_report: FeedHealthReport | None = None
    market_resolved: bool | None = None
    stream_factory: Callable[[DeployConfig], AsyncIterator[MarketState]] | None = None
    fetch_updates: Callable[[int], Awaitable[list[dict[str, Any]]]] | None = None
    marketstates: list[MarketState] | None = None
    adapter: Any | None = None
    anchor_fn: Callable[[str], Awaitable[str]] | None = field(default=anchor_memo)
    mm_tape_resolver: Callable[[str], MakerReplayTape] | None = None
    mm_proposer: Callable[..., Awaitable[MMExecutionToolResult]] | None = None
    mm_seed_state: StrategyState | None = None
    mm_session_dir: Path | None = None
    provisioning_provider: Any | None = None


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


async def check_provisioning_readiness(settings: Settings, provider: Any | None) -> None:
    """Async II-5b custody READINESS probe (MAJOR-3): verify the LIVE policy + quorum, not just presence.

    When provisioning is ENABLED, this fetches the pinned policy AND key quorum from the provider and
    admits them against the pinned custody contract (:func:`~veridex.dust_execution.privy_provisioning.
    admit_policy_and_quorum`) — so an unreachable provider or a drifted policy/quorum is caught at
    readiness rather than only on the first deploy. Fully INERT when provisioning is disabled (returns
    immediately; no probe, no behavior change). Fails CLOSED (raises) on a missing provider, provider
    error, or drift, so a readiness surface / startup can refuse to advertise ready.

    Args:
        settings: Resolved settings (the pinned custody contract is read from here).
        provider: The injected provisioning provider (``None`` when not wired).

    Raises:
        RuntimeError: If provisioning is enabled but no provider is wired.
        ProvisioningError: (or a subclass) if the provider is unreachable or the live policy/quorum
            drifted from the pinned contract.
    """
    pinned = require_privy_provisioning(settings)
    if pinned is None:
        return  # provisioning disabled → inert, no probe
    if provider is None:
        raise RuntimeError("PRIVY provisioning is enabled but no provisioning provider is wired (readiness fail closed)")
    from veridex.dust_execution.privy_provisioning import admit_policy_and_quorum  # noqa: PLC0415

    await admit_policy_and_quorum(provider, pinned, request_auth=pinned.request_auth())


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
    # Composition/readiness fail-closed (MAJOR-3): in a production-equivalent env, enabling II-5b
    # provisioning REQUIRES the production provider to be wired — refuse to compose the app otherwise so
    # a "enabled but no provider" process cannot start and silently skip custody. (require_privy_provisioning
    # itself raises if enabled with pins absent.) In dev/test the app still composes so the per-deploy
    # fail-closed guard in _launch_mm is exercised.
    if require_privy_provisioning(settings) is not None and deps.provisioning_provider is None and settings.is_production:
        raise RuntimeError(
            "PRIVY provisioning is enabled but no provisioning provider is wired (fail closed at composition)"
        )
    # I-1 auth boundary: a valid Privy access token is required (in ``privy`` mode) BEFORE the deploy
    # body handler runs, so a 401 precedes every persistence/wallet/runtime side effect.
    require_principal = make_require_principal(settings)

    # app.state holds ONLY the live background run-task handles (cancellable on shutdown) — the
    # cancellation bookkeeping. The durable AgentInstance record is the STORE's job (source of
    # truth), NOT app.state: it survives a process restart and an app.state clear.
    background_tasks: set[asyncio.Task[None]] = set()
    app.state.deploy_background_tasks = background_tasks

    # MAJOR-3: expose the async custody-readiness probe on app.state so an app lifespan/startup CAN
    # fail closed on provider error or drift (an integrator wires it into the boot sequence), and mount
    # a fail-closed readiness route below. Inert when provisioning is disabled.
    async def _provisioning_readiness() -> None:
        await check_provisioning_readiness(settings, deps.provisioning_provider)

    app.state.check_provisioning_readiness = _provisioning_readiness

    @app.get("/agents/provisioning/readyz")
    async def provisioning_readyz() -> dict[str, Any]:
        """Fail-closed II-5b custody readiness (MAJOR-3): 200 only if the live policy/quorum admit.

        Unauthenticated readiness surface (like a container ``/readyz``). When provisioning is enabled
        it re-admits the LIVE policy + quorum against the pinned contract; an unreachable provider or a
        drifted policy/quorum returns 503 so an orchestrator never advertises this pod as ready while
        custody is unverifiable. When provisioning is disabled it is inert (``provisioning: false``).
        """
        enabled = require_privy_provisioning(settings) is not None
        try:
            await check_provisioning_readiness(settings, deps.provisioning_provider)
        except Exception as exc:  # noqa: BLE001 — any custody-readiness failure must fail closed (503)
            raise HTTPException(
                status_code=503,
                detail={"error": "provisioning_not_ready", "provisioning_enabled": enabled},
            ) from exc
        return {"status": "ok", "provisioning_enabled": enabled}

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

    async def _launch_mm(instance: AgentInstance) -> None:
        """Run the ``quoteguard-mm`` deployed instance through the SHARED start service (II-5).

        Family-driven, fail-closed dispatch (req 5): NEVER calls :func:`_build_agent` /
        :func:`standalone_run` — a fresh :class:`VeridexAgentAdapter` is wired to a session factory
        that reconstructs the run's authority ONLY from ``instance`` (never a request field, II-5
        req 3), then driven through the SAME :func:`~veridex.runtime.agentos_service.
        start_owned_instance_run` the II-4 wrapper uses (req 1) — with NO in-process HTTP call to
        that wrapper route. The ONE authoritative ``run_id`` is ``instance.run_id`` — the SAME
        identity already persisted on the instance/lease/response (req 2). This is a Veridex-owned
        background coroutine that calls the shared service INLINE (req 6) — the AgentOS call is not
        a separately "detached" background task.
        """
        # Lazy import (breaks the deploy -> agentos_service -> router -> deploy module cycle; mirrors
        # the existing lazy-import convention used at composition.py / mm_agent_adapter.py boundaries).
        from veridex.runtime.agentos_service import start_owned_instance_run  # noqa: PLC0415

        await store.update_agent_instance_status(instance.instance_id, DeployStatus.RUNNING, updated_at=_now_iso())
        # Controlled failure taxonomy (never a raw trace): failing before the shared service starts
        # (session-factory authority mismatch, unresolved tape, duplicate lease) is a RUNTIME_ERROR;
        # once the shared service is driving the adapter it is a SEAL_FAILED.
        phase = DeployFailureReason.RUNTIME_ERROR
        try:
            # II-5b — provision ONE idempotent, policy-bound, keyless Privy execution wallet BEFORE the
            # run starts (fail-closed). Gated twice: a server-injected provisioning provider AND the
            # operator-pinned custody config (require_privy_provisioning). With either absent this is a
            # no-op and the launch is exactly the II-5 behavior. The wallet CLAIM stays WITHHELD — this
            # only persists the binding + immutable record (R4-A only); it never arms live execution,
            # and any custody violation raises here → the instance is marked FAILED and NO run starts.
            pinned = require_privy_provisioning(settings)
            if pinned is not None:
                # FAIL CLOSED (MAJOR-3): provisioning is ENABLED, so a missing provider is a
                # misconfiguration — never a silent skip that seals the run with no wallet binding.
                if deps.provisioning_provider is None:
                    raise RuntimeError(
                        "PRIVY provisioning is enabled but no provisioning provider is wired (fail closed)"
                    )
                from veridex.dust_execution.privy_provisioning import provision_execution_wallet  # noqa: PLC0415
                from veridex.store import DeploymentAttemptTransitionError  # noqa: PLC0415

                # A concurrent recovery driver for the SAME attempt (a duplicate-deploy retry) may WIN a
                # status CAS while this original launch is mid-provisioning, surfacing as a
                # DeploymentAttemptTransitionError. Because the external_id + provider idempotency key are
                # DETERMINISTIC from the attempt id, a same-attempt concurrent advance is COHERENT by
                # construction (never a second wallet / run) — so treat it as IDEMPOTENT RE-ENTRY: resume
                # the resumable saga, which reconciles from the winner's verified state (its own admission
                # / wallet / record checks still fail closed on a genuinely incompatible state). Do NOT
                # mark the shared instance FAILED for a coherent CAS loss; then proceed to seal the run.
                for _reentry in range(_PROVISION_CAS_MAX_REENTRIES):
                    try:
                        await provision_execution_wallet(
                            store=store,
                            provider=deps.provisioning_provider,
                            instance=instance,
                            pinned=pinned,
                            request_auth=pinned.request_auth(),
                            now_fn=lambda: datetime.now(tz=UTC),
                        )
                        break
                    except DeploymentAttemptTransitionError:
                        continue  # a concurrent coherent driver advanced this attempt — resume from its state
                else:
                    raise RuntimeError("execution-wallet provisioning did not converge under concurrent recovery")

            def _session_factory(ctx: RunContext) -> tuple[Any, Any, str, bool]:
                return reconstruct_mm_session(
                    instance,
                    ctx,
                    tape_resolver=deps.mm_tape_resolver,
                    proposer=deps.mm_proposer,
                    seed_state=deps.mm_seed_state,
                    session_dir=deps.mm_session_dir,
                )

            adapter = VeridexAgentAdapter(
                run_driver=build_market_maker_driver(_session_factory),
                id=f"veridex-mm-{instance.instance_id}",
                event_sink=runtime_event_sink,
            )
            session_id = f"sess_{uuid.uuid4().hex}"

            phase = DeployFailureReason.SEAL_FAILED
            await start_owned_instance_run(
                store,
                adapter,
                instance=instance,
                run_id=instance.run_id,
                session_id=session_id,
                input=None,
                event_sink=runtime_event_sink,
            )
        except asyncio.CancelledError:
            # Shutdown cancellation is neither a seal nor a failure — leave the record RUNNING.
            raise
        except Exception:
            # Pre-seal failure (mismatch/tape/duplicate-lease/adapter): durably mark FAILED with the
            # CONTROLLED reason (no raw trace, no legacy fallback), then re-raise so the done-callback
            # surfaces the FULL diagnostic on the server log.
            await store.update_agent_instance_status(
                instance.instance_id,
                DeployStatus.FAILED,
                last_failure_reason=phase,
                updated_at=_now_iso(),
            )
            raise
        # Clean seal: durably mark the instance SEALED.
        await store.update_agent_instance_status(instance.instance_id, DeployStatus.SEALED, updated_at=_now_iso())

    def _discard_recovery_task(finished: asyncio.Task[None]) -> None:
        """Discard a finished provisioning-recovery task and surface any failure to the server log."""
        from veridex.store import DeploymentAttemptTransitionError  # noqa: PLC0415

        background_tasks.discard(finished)
        if finished.cancelled():
            return
        exc = finished.exception()
        if exc is None:
            return
        if isinstance(exc, DeploymentAttemptTransitionError):
            # BENIGN: this recovery LOST a status CAS to the concurrent original launch (which is the
            # coherent winner and completes provisioning itself) — informational, not a failure.
            logger.info("mm provisioning recovery lost a CAS to the concurrent launch (benign): %s", exc)
            return
        logger.error("mm provisioning recovery task failed", exc_info=exc)

    async def _recover_mm_provisioning(instance: AgentInstance) -> None:
        """Re-drive ONLY the wallet-provisioning recovery for an MM instance whose saga is incomplete.

        MAJOR-2: the real HTTP retry path must reach the external-id reconciliation. This is
        PROVISIONING-ONLY — it never starts a run (a run, if owed, is governed separately by the II-4
        lease). Reconciliation is idempotent and driven through the store's atomic CAS forward-transition,
        which prevents an inconsistent concurrent advance. Gated exactly like ``_launch_mm``: a no-op if
        provisioning is disabled or no provider is wired.
        """
        pinned = require_privy_provisioning(settings)
        if pinned is None or deps.provisioning_provider is None:
            return
        from veridex.dust_execution.privy_provisioning import provision_execution_wallet  # noqa: PLC0415

        await provision_execution_wallet(
            store=store,
            provider=deps.provisioning_provider,
            instance=instance,
            pinned=pinned,
            request_auth=pinned.request_auth(),
            now_fn=lambda: datetime.now(tz=UTC),
        )

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
        # Family-driven envelope selection (req 5): quoteguard-mm NEVER reuses the directional
        # single-order ``to_policy_envelope()`` — it gets its own bounded, multi-order maker envelope.
        # A missing ``mm`` block falls back to the inert directional envelope ONLY so this call never
        # crashes; the ``mm_family`` preflight check independently fails closed on ``mm is None``.
        if config.strategy == MM_STRATEGY_FAMILY and config.mm is not None:
            envelope = build_maker_policy_envelope(config, config.mm)
        else:
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
                # An MM deploy whose provisioning saga is INCOMPLETE (PENDING or any nonterminal wallet
                # state) must RE-DRIVE the owned provisioning recovery in the background — reconciling by
                # the recorded external_id — rather than returning as if complete. It starts NO second run
                # (the II-4 lease governs that); the store CAS guards against inconsistent concurrent
                # advance if the original _launch_mm is still mid-flight. A COMPLETE instance (provisioning
                # done/terminal, or provisioning not applicable) returns unchanged, and directional
                # (non-MM) idempotent replay is untouched.
                if (
                    config.strategy == MM_STRATEGY_FAMILY
                    and recorded.status in _INCOMPLETE_PROVISIONING_STATES
                    and require_privy_provisioning(settings) is not None
                    and deps.provisioning_provider is not None
                ):
                    recovery_task = asyncio.create_task(_recover_mm_provisioning(existing))
                    background_tasks.add(recovery_task)
                    recovery_task.add_done_callback(_discard_recovery_task)
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
        # Family-driven effective_config (req 5): quoteguard-mm's effective config is EXACTLY its
        # bounded ``mm`` block (the session factory re-parses it with ``extra="forbid"``) — never the
        # directional AgentRunConfig normalization, and never _build_agent/standalone_run's shape.
        if config.strategy == MM_STRATEGY_FAMILY:
            assert config.mm is not None  # preflight's mm_family check already fails closed on None
            effective_config = config.mm.model_dump(mode="json")
        else:
            effective_config = _build_run_config(config).model_dump(mode="json")
        instance = AgentInstance(
            instance_id=instance_id,
            template_id=config.template_id,
            agent_id=config.agent_id,
            submitted_config=config.model_dump(mode="json"),
            effective_config=effective_config,
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
        # Family-driven dispatch (req 5): quoteguard-mm launches through the SHARED AgentOS start
        # service (_launch_mm); every directional family launches through the UNCHANGED single seam.
        task: asyncio.Task[None]
        if config.strategy == MM_STRATEGY_FAMILY:
            task = asyncio.create_task(_launch_mm(instance))
        else:
            task = asyncio.create_task(_launch(config, run_id, instance.instance_id, replay_marketstates))
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
