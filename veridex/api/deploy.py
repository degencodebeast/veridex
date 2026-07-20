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
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from veridex.api.auth_privy import PrivyPrincipal, make_require_principal
from veridex.api.fixture_labels import fixture_label, market_label
from veridex.chain.anchor import anchor_memo
from veridex.config import require_privy_provisioning
from veridex.deploy.attempt import AttemptStatus, DeploymentAttempt, DuplicateAttemptError
from veridex.deploy.instance import AgentInstance, DeployFailureReason, DeployStatus
from veridex.deploy.preflight import MM_STRATEGY_FAMILY, DeployConfig, PreflightCheck, run_deploy_preflight
from veridex.ingest.feed_health import FeedHealthReport
from veridex.ingest.marketstate import MarketState
from veridex.ingest.replay_catalog import (
    ReplayResolutionError,
    ResolvedReplaySource,
    build_catalog,
    load_resolved_marketstates,
    resolve_replay_source,
)
from veridex.mm_strategy.session_factory import (
    MMTapeNotFoundError,
    build_maker_policy_envelope,
    compute_tape_content_hash,
    default_mm_tape_resolver,
    reconstruct_mm_session,
)
from veridex.runtime.mm_agent_adapter import (
    OwnerMismatchError,
    VeridexAgentAdapter,
    build_market_maker_driver,
)
from veridex.runtime.orchestrator import Agent
from veridex.runtime.runtime_events import (
    RuntimeEventSink,
    RuntimeEventType,
    runtime_event,
)
from veridex.runtime.window import RunWindow
from veridex_agent.config import AgentRunConfig, build_agent
from veridex_agent.run import standalone_run

if TYPE_CHECKING:
    from pathlib import Path

    from veridex.config import Settings
    from veridex.dust_execution.facade import MMExecutionToolResult
    from veridex.mm_strategy.contracts import StrategyState
    from veridex.mm_strategy.session_factory import MakerReplayTape
    from veridex.runtime.mm_agent_adapter import RunContext, RunDriver
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
        replay_binding: The FROZEN production-replay identity (R-4) a ``replay`` deploy committed to —
            ``{pack_id, fixture_id, content_hash}``, server-derived from the verified catalog. ``None``
            for a live deploy or an injected-tape test. Surfaces WHICH verified pack an auto-resolved
            (unnamed) deploy replays, so an unnamed request never becomes an unidentified run.
    """

    instance_id: str
    config_hash: str
    policy_hash: str
    run_id: str
    owner: str
    replay_binding: dict[str, Any] | None = None


class AgentInstanceDetail(AgentInstance):
    """The owner-scoped instance-detail view: an :class:`AgentInstance` PLUS server-derived labels.

    Every added field is ADDITIVE and OPTIONAL — the inherited :class:`AgentInstance` contract is
    unchanged, so existing readers keep working. The labels only AUGMENT the raw ids the record
    already carries (they never replace them). ``fixture_label`` / ``market_label`` are CURATED
    convenience strings (see :mod:`veridex.api.fixture_labels`), never re-verified at render time.

    Two DISTINCT hash identities are surfaced (never conflated): ``replay_pack_content_hash`` is the
    R-4 :class:`~veridex.ingest.replay_catalog.ReplayCatalog` PACK selection hash, while
    ``maker_tape_content_hash`` is the ``quoteguard-mm`` MakerReplayTape's own content hash (a
    DIFFERENT value the maker run actually verifies). Both are server-derived; the maker-tape fields
    exist ONLY for an MM instance (a directional/non-MM instance has no maker tape → both are ``None``).

    Attributes:
        fixture_id: The pinned fixture id — the frozen ``replay_binding["fixture_id"]`` when present,
            else parsed from the leading ``pmxt:{fixture}:...`` allowlist token, else ``None``.
        fixture_label: The CURATED "Home v Away" label for ``fixture_id`` (``"Fixture {id}"`` for an
            unmapped id), or ``None`` when no fixture id was derivable (the UI then omits the rows).
        market_label: The humanized label for the FIRST pinned ``market_allowlist`` token, or ``None``
            when the allowlist is empty. An unrecognized token passes through unchanged (honest).
        replay_pack_content_hash: The frozen ``replay_binding["content_hash"]`` — the R-4 replay PACK
            selection hash (NOT the maker tape's hash), or ``None`` for a live deploy / no binding.
        replay_pack_id: The frozen ``replay_binding["pack_id"]`` (which verified pack the run
            replays), or ``None`` for a live deploy / a record with no binding.
        maker_tape_ref: The ``quoteguard-mm`` tape catalog key (``mm.tape_ref`` from the effective
            config) for an MM instance, else ``None`` (no maker tape on a directional instance).
        maker_tape_content_hash: The MakerReplayTape's OWN content hash, derived SERVER-SIDE via the
            same :func:`~veridex.mm_strategy.session_factory.default_mm_tape_resolver` reconstruction
            uses. ``None`` for a non-MM instance or when the tape_ref is not banked in the catalog.
    """

    fixture_id: int | None = None
    fixture_label: str | None = None
    market_label: str | None = None
    replay_pack_content_hash: str | None = None
    replay_pack_id: str | None = None
    maker_tape_ref: str | None = None
    maker_tape_content_hash: str | None = None


class InstanceStatusResponse(BaseModel):
    """Owner-scoped run/lease status view for a deployed instance (II-6).

    Attributes:
        instance_id: The instance this status is about.
        run_id: The authoritative Veridex run id (the instance's scalar run).
        run_state: The HEADLINE run/lease state — ``running`` while the hosted run is ACTIVE,
            ``cancelled`` once an owner kill engaged (durable across the run settling, via the
            in-process kill ledger), else the settled adapter phase or the durable instance status
            (``sealed`` / ``failed`` / ``pending``).
        killed: Whether an owner kill engaged the exactly-once cancel for this run.
        status: The DURABLE :class:`~veridex.deploy.instance.DeployStatus` value (persisted record).
        lease_status: The current :class:`~veridex.store.LeaseStatus` value, or ``None`` if no lease.
    """

    instance_id: str
    run_id: str
    run_state: str
    killed: bool
    status: str
    lease_status: str | None = None


class KillResponse(BaseModel):
    """Result of an owner kill (II-6 / AC-16).

    Attributes:
        instance_id: The instance the kill targeted.
        run_id: The run the kill targeted.
        phase: The run's :class:`~veridex.runtime.mm_agent_adapter.RunPhase` value after this call.
        engaged: ``True`` ONLY for the single caller that engaged the exactly-once cancel; a repeat /
            already-settled kill returns ``False`` without re-engaging (no resume, no double-cancel).
    """

    instance_id: str
    run_id: str
    phase: str
    engaged: bool


def _derive_fixture_id(instance: AgentInstance) -> int | None:
    """Resolve the pinned fixture id from the durable record, fail-soft.

    Priority: the FROZEN ``replay_binding["fixture_id"]`` (server-derived at deploy) → the digits of
    the leading ``pmxt:{fixture}:...`` allowlist token → ``None``. Never guesses: a non-``pmxt`` /
    non-numeric token yields ``None`` rather than a fabricated id.
    """
    binding = instance.replay_binding or {}
    bound = binding.get("fixture_id")
    if isinstance(bound, int):
        return bound
    if isinstance(bound, str) and bound.isdigit():
        return int(bound)
    for token in instance.market_allowlist:
        parts = token.split(":")
        if len(parts) >= 2 and parts[0] == "pmxt" and parts[1].isdigit():
            return int(parts[1])
    return None


def _derive_maker_tape_identity(
    instance: AgentInstance,
    tape_resolver: Callable[[str], MakerReplayTape] | None = None,
) -> tuple[str | None, str | None]:
    """Derive ``(maker_tape_ref, maker_tape_content_hash)`` for a ``quoteguard-mm`` instance.

    ONLY an MM-family instance (``submitted_config["strategy"] == "quoteguard-mm"``) has a maker
    tape; a directional/non-MM instance returns ``(None, None)``. The tape_ref is the persisted
    ``mm.tape_ref`` (the effective config IS the ``mm`` block for an MM instance).

    The hash is derived SERVER-SIDE via the EFFECTIVE resolver — the SAME
    ``DeployDeps.mm_tape_resolver`` the maker run itself resolves through (``None`` →
    :func:`~veridex.mm_strategy.session_factory.default_mm_tape_resolver`), so the detail reports the
    tape the run ACTUALLY used, never a hardcoded default that diverges from an injected tape. It is
    NEVER a client-supplied value.

    Self-verifying (mirrors ``reconstruct_mm_session`` at session_factory.py:395-403): the resolved
    tape's events are re-hashed via :func:`~veridex.mm_strategy.session_factory.compute_tape_content_hash`
    and REQUIRED to equal the resolver-returned ``tape.content_hash`` before the hash is surfaced. On a
    MISMATCH (a tampered/broken tape the run would itself reject) it FAILS CLOSED — the hash is
    ``None`` (never a value that doesn't match the events). A tape_ref not banked in the resolver's
    catalog likewise yields a ``None`` hash. The ``tape_ref`` is surfaced regardless (honest).

    NOTE: this identity is recomputed at READ time and is NOT persisted/immutable — it must never be
    labeled "pinned"/"sealed"/"historical" anywhere.
    """
    if instance.submitted_config.get("strategy") != MM_STRATEGY_FAMILY:
        return None, None
    tape_ref = instance.effective_config.get("tape_ref")
    if not isinstance(tape_ref, str):
        return None, None
    resolve = tape_resolver if tape_resolver is not None else default_mm_tape_resolver
    try:
        tape = resolve(tape_ref)
        recomputed = compute_tape_content_hash(tape.events)
    except MMTapeNotFoundError:
        return tape_ref, None
    except Exception:  # noqa: BLE001 - read-only projection must fail closed, never 500 the page
        # This label sits on the PRIMARY (non-best-effort) detail path. A read-only projection must
        # degrade to an omitted hash on ANY resolver/re-hash failure — never propagate a 500 that would
        # take down the whole owner-scoped page (ownership, run_id, status). Same fail-closed outcome as
        # a not-banked tape or a hash mismatch; the tape_ref still names the tape honestly.
        return tape_ref, None
    if recomputed != tape.content_hash:
        # Fail closed: the tape's events don't match its claimed hash — the run would reject it, so
        # the detail must not display a hash. Keep the ref so the surface still names the tape.
        return tape_ref, None
    return tape_ref, recomputed


def build_instance_detail(
    instance: AgentInstance,
    tape_resolver: Callable[[str], MakerReplayTape] | None = None,
) -> AgentInstanceDetail:
    """Project an :class:`AgentInstance` onto the owner-scoped detail view + CURATED labels.

    Purely additive server-side enrichment: derives the fixture id, its CURATED label, the first
    market's humanized label, the frozen replay-PACK identity, and (for an MM instance) the DISTINCT,
    self-verified maker-tape identity. ``tape_resolver`` is threaded from the route's effective
    ``DeployDeps.mm_tape_resolver`` so the maker-tape hash reflects the tape the run actually resolves
    (``None`` → the production default resolver). Never mutates the record; never re-verifies a CURATED
    label against a live source (see :mod:`veridex.api.fixture_labels`). ``replay_pack_content_hash``
    (the R-4 pack hash) and ``maker_tape_content_hash`` (the MakerReplayTape hash) are kept separate —
    they are different identities and one is never presented as the other.
    """
    fixture_id = _derive_fixture_id(instance)
    binding = instance.replay_binding or {}
    first_market = instance.market_allowlist[0] if instance.market_allowlist else None
    maker_tape_ref, maker_tape_content_hash = _derive_maker_tape_identity(instance, tape_resolver)
    return AgentInstanceDetail(
        **instance.model_dump(),
        fixture_id=fixture_id,
        # No derivable fixture id → no label at all (the UI omits the rows rather than showing a
        # truthy "Fixture (unknown)" placeholder).
        fixture_label=fixture_label(fixture_id) if fixture_id is not None else None,
        market_label=market_label(first_market) if first_market is not None else None,
        replay_pack_content_hash=binding.get("content_hash"),
        replay_pack_id=binding.get("pack_id"),
        maker_tape_ref=maker_tape_ref,
        maker_tape_content_hash=maker_tape_content_hash,
    )


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
        mm_run_driver: SERVER-SIDE injectable ``quoteguard-mm`` run driver (the II-6 testability seam,
            mirroring ``mm_proposer`` / ``mm_tape_resolver``). ``None`` → the production driver
            (:func:`~veridex.runtime.mm_agent_adapter.build_market_maker_driver` over the session
            factory). Tests inject a driver that cooperatively parks on the run's ``StopSignal`` so a
            deployed run stays live across an owner kill (exercises the exactly-once cancel). NEVER a
            request field — a client can never choose the run driver.
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
    mm_run_driver: RunDriver | None = None


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

    # R-4: ensure a verified ReplayPack catalog backs the deploy replay path. ``create_app`` sets
    # ``app.state.replay_catalog`` BEFORE mounting these routes; a standalone host (or a test mounting
    # deploy routes on a bare app) may not — so build one from the SAME env-driven curated root
    # ``create_app`` uses, exactly once at registration. A host-provided catalog is NEVER overwritten.
    if getattr(app.state, "replay_catalog", None) is None:
        app.state.replay_catalog = build_catalog(
            os.environ.get("REPLAY_PACK_ROOT", "") or None,
            capture_root=os.environ.get("REPLAY_CAPTURE_ROOT", "") or None,
        )

    # II-6 owner-kill wiring (in-process, like ``background_tasks`` — the durable record stays the
    # store's job). ``mm_run_adapters`` maps a live instance to the hosted ``VeridexAgentAdapter`` so an
    # owner kill can reach the SAME exactly-once ``acancel_run`` (AC-16) the II-4 wrapper uses — there
    # is no new cancel primitive. ``killed_instances`` is the exactly-once kill ledger (only the winner
    # that engaged the cancel records here, so a repeat kill is a durable-in-process no-op and the
    # status view reports ``cancelled`` even after the run settles). ``last_run_phase`` snapshots the
    # adapter's terminal :class:`~veridex.runtime.mm_agent_adapter.RunPhase` at deregistration so the
    # status view keeps a truthful run/lease state after the live adapter is gone.
    mm_run_adapters: dict[str, VeridexAgentAdapter] = {}
    killed_instances: dict[str, str] = {}
    last_run_phase: dict[str, str] = {}
    app.state.deploy_mm_run_adapters = mm_run_adapters
    app.state.deploy_killed_instances = killed_instances

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

    def _resolve_replay_source(config: DeployConfig) -> tuple[ResolvedReplaySource | None, list[MarketState]]:
        """Resolve the REPLAY tape + its FROZEN identity: injected fakes (tests) or the verified pack (R-4).

        The injected ``deps.marketstates`` test seam is unchanged (returns ``(None, injected)`` — an
        injected tape has no catalog identity). The DEFAULT mounted route (no injected deps) selects a
        REAL, hash-verified ReplayPack from the R-2 catalog via :func:`resolve_replay_source` (Option B
        atomic snapshot: a bound ``replay_pack_id`` is looked up exactly; unbound auto-selects the single
        catalogued pack; multiple packs unbound -> fail closed ``pack_id_required``) and loads it through
        the SAME ``load_pack_marketstates`` normalizer live replay uses — NEVER ``build_demo_ticks``.
        ``config.replay_fixture_id`` is presence-aware (``0`` is a valid fixture, never "omitted"). The
        returned :class:`ResolvedReplaySource` is FROZEN onto the instance before launch.

        Raises:
            ReplayResolutionError: Fail-closed selection (empty catalog, pack_id required, unknown
                pack/fixture) — surfaced as a 400 at the deploy boundary, never a silent demo fallback.
        """
        if deps.marketstates is not None:
            return None, list(deps.marketstates)
        catalog = getattr(app.state, "replay_catalog", None)
        resolved = resolve_replay_source(
            catalog, pack_id=config.replay_pack_id, fixture_id=config.replay_fixture_id
        )
        return resolved, load_resolved_marketstates(catalog, resolved)

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

            # The run driver is the injectable II-6 seam (tests park on the StopSignal); production
            # wires the frozen II-2 composition through the session factory (byte-identical default).
            run_driver = deps.mm_run_driver if deps.mm_run_driver is not None else build_market_maker_driver(_session_factory)
            adapter = VeridexAgentAdapter(
                run_driver=run_driver,
                id=f"veridex-mm-{instance.instance_id}",
                event_sink=runtime_event_sink,
            )
            session_id = f"sess_{uuid.uuid4().hex}"

            # Publish the live adapter so an owner kill can reach ``acancel_run`` while the run is
            # ACTIVE; snapshot its terminal phase + deregister on settle (the run is no longer live).
            mm_run_adapters[instance.instance_id] = adapter
            try:
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
            finally:
                settled_phase = adapter.run_phase(instance.run_id)
                if settled_phase is not None:
                    last_run_phase[instance.instance_id] = settled_phase.value
                mm_run_adapters.pop(instance.instance_id, None)
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

        operator_id = principal.did
        config_fingerprint = config.config_hash()

        def _reconcile_existing_deploy(
            existing: AgentInstance, recorded_status: AttemptStatus
        ) -> DeployResponse:
            """Return an already-materialized idempotent instance, re-driving MM provisioning recovery
            if the original saga is still incomplete. The FROZEN ``replay_binding`` is reused verbatim —
            the tape identity is NEVER re-selected on retry (R-4), so an R-0b promotion between the
            original deploy and this retry can never change the run's tape."""
            if (
                config.strategy == MM_STRATEGY_FAMILY
                and recorded_status in _INCOMPLETE_PROVISIONING_STATES
                and require_privy_provisioning(settings) is not None
                and deps.provisioning_provider is not None
            ):
                recovery_task = asyncio.create_task(_recover_mm_provisioning(existing))
                background_tasks.add(recovery_task)
                recovery_task.add_done_callback(_discard_recovery_task)
            return DeployResponse(
                instance_id=existing.instance_id,
                config_hash=existing.config_hash,
                policy_hash=existing.policy_hash,
                run_id=existing.run_id,
                owner=operator_id,
                replay_binding=existing.replay_binding,
            )

        # R-4 (retry availability): reconcile an ALREADY-materialized idempotent instance BEFORE resolving
        # the replay source. A prior successful UNNAMED single-pack replay deploy froze its tape identity
        # onto the instance; if an R-0b promotion later made the catalog multi-pack, a blind re-resolve
        # here would 400 ``pack_id_required`` and break the idempotent retry. The frozen binding is
        # authoritative and reused verbatim. Only the clean already-materialized case short-circuits; a
        # config-fingerprint mismatch or a not-yet-materialized claim falls through to the authoritative
        # attempt claim below (which owns the 409s and the fresh-deploy saga).
        if idempotency_key is not None:
            prior = await store.get_deployment_attempt_by_key(operator_id, idempotency_key)
            if (
                prior is not None
                and prior.instance_id is not None
                and prior.config_fingerprint == config_fingerprint
            ):
                try:
                    existing_instance = await store.get_agent_instance(prior.instance_id)
                except KeyError:
                    existing_instance = None
                if existing_instance is not None:
                    return _reconcile_existing_deploy(existing_instance, prior.status)

        # MODE-AWARE source resolution: a replay deploy resolves its SOURCE (bundled/injected pack)
        # BEFORE preflight, so the named feed_health check verifies the replay source resolves
        # (non-empty) rather than a live feed. Live resolves nothing here — it is gated fail-closed
        # by the feed report (the correct 422 until a live feed is wired).
        replay_marketstates: list[MarketState] = []
        resolved_replay: ResolvedReplaySource | None = None
        source_resolved: bool | None = None
        if config.source_mode == "replay":
            try:
                resolved_replay, replay_marketstates = _resolve_replay_source(config)
            except ReplayResolutionError as exc:
                # Fail-closed: an unresolvable/ambiguous/unverified replay source is a 400 — never a
                # silent fallback to a synthetic tape. No instance is pinned and no run is launched.
                raise HTTPException(
                    status_code=400,
                    detail={"error": "replay_source_unresolved", "reason": exc.reason, "message": str(exc)},
                ) from None
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
        idem_key = idempotency_key if idempotency_key is not None else uuid.uuid4().hex
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
                # crash-recovered), launching NO second run. An MM deploy whose provisioning saga is
                # INCOMPLETE re-drives the owned provisioning recovery in the background; a COMPLETE (or
                # directional non-MM) instance returns unchanged. R-4: the instance's FROZEN
                # replay_binding is reused verbatim — the tape identity is never re-selected on retry.
                return _reconcile_existing_deploy(existing, recorded.status)
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
        # R-4: FREEZE the resolved production-replay identity (pack_id + fixture_id + SERVER-derived
        # content_hash) onto the durable instance BEFORE launch, so the sealed run's tape identity is
        # observable and an idempotent retry that reconciles to this instance REUSES it verbatim (never
        # re-selects). It is carried in a DEDICATED ``AgentInstance.replay_binding`` field — NEVER folded
        # into ``effective_config`` (the MM session factory re-parses that with ``extra="forbid"``, so a
        # rider there would break ``reconstruct_mm_session``). ``None`` for a live/injected-tape deploy.
        replay_binding = resolved_replay.as_binding() if resolved_replay is not None else None
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
            replay_binding=replay_binding,
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
            replay_binding=instance.replay_binding,
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

    @app.get("/agents/instances/{instance_id}", response_model=AgentInstanceDetail)
    async def get_agent_instance(
        instance_id: str,
        principal: PrivyPrincipal = Depends(require_principal),  # noqa: B008
    ) -> AgentInstanceDetail:
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
        # Thread the EFFECTIVE maker-tape resolver (the same one the run resolves through) so the
        # surfaced maker-tape hash reflects the tape actually used — never a hardcoded default.
        return build_instance_detail(instance, tape_resolver=deps.mm_tape_resolver)

    async def _load_owned_instance(instance_id: str, principal: PrivyPrincipal) -> AgentInstance:
        """Owner-gate an instance the SAME way :func:`get_agent_instance` does (fail-closed).

        A missing instance or an UNOWNED / legacy row (``operator_id is None`` — never inherited) is a
        404 (no existence leak); a row owned by another principal is a 403. Ownership resolves from the
        STORED ``operator_id`` only — the caller's supplied identity is never trusted.
        """
        try:
            instance = await store.get_agent_instance(instance_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="agent instance not found") from exc
        if instance.operator_id is None:
            raise HTTPException(status_code=404, detail="agent instance not found")
        if instance.operator_id != principal.did:
            raise HTTPException(status_code=403, detail="principal does not own this agent instance")
        return instance

    #: How a live/settled adapter :class:`RunPhase` maps onto the status view's headline ``run_state``.
    _PHASE_TO_STATE: dict[str, str] = {
        "active": "running",
        "cancelling": "cancelled",
        "cancelled": "cancelled",
        "completed": "sealed",
        "failed": "failed",
    }

    def _derive_run_state(instance: AgentInstance) -> str:
        """Compose the truthful headline run/lease state for an instance's status view.

        Priority: an engaged owner kill (``cancelled``, durable across the run settling) → the LIVE
        adapter phase (``running`` while ACTIVE) → the settled phase snapshot → the durable
        :class:`~veridex.deploy.instance.DeployStatus`. This is a read-only projection — it never
        mutates the durable record (a ``cancelled`` run still seals its window honestly).
        """
        if instance.instance_id in killed_instances:
            return "cancelled"
        adapter = mm_run_adapters.get(instance.instance_id)
        if adapter is not None:
            live = adapter.run_phase(instance.run_id)
            if live is not None:
                return _PHASE_TO_STATE.get(live.value, live.value)
        settled = last_run_phase.get(instance.instance_id)
        if settled is not None:
            return _PHASE_TO_STATE.get(settled, settled)
        return instance.status.value

    def _emit_cancelled_ops(instance: AgentInstance, run_id: str) -> None:
        """Emit the TERMINAL cancelled OPS event for an engaged kill (best-effort; exactly once).

        Routed through the SAME durable OPS sink as the rest of deploy.py. The adapter's own terminal
        STATUS_CHANGED maps a cancelled run to completed/failed, so this is the ONE event that names
        the owner-kill outcome. Called ONLY on the exactly-once winner, so a repeat kill never emits a
        second terminal event. Inert when no sink is wired.
        """
        if runtime_event_sink is None:
            return
        handle = instance.runtime_handle or {}
        runtime_event_sink(
            runtime_event(
                RuntimeEventType.STATUS_CHANGED,
                agent_id=handle.get("runtime_agent_id", instance.agent_id),
                run_id=run_id,
                session_id=handle.get("session_id"),
                status="cancelled",
                reason="owner_kill",
            )
        )

    @app.get("/agents/instances/{instance_id}/status", response_model=InstanceStatusResponse)
    async def get_instance_status(
        instance_id: str,
        principal: PrivyPrincipal = Depends(require_principal),  # noqa: B008
    ) -> InstanceStatusResponse:
        """Owner-scoped run/lease status for a deployed instance (II-6; run-level state view).

        Surfaces the run-level state (``running`` / ``cancelled`` / ``sealed`` / ``failed``) the raw
        durable ``AgentInstance`` record cannot express — reflecting a live owner kill as ``cancelled``
        even though the scalar ``DeployStatus`` has no cancelled value. Read-only; owner-gated exactly
        like :func:`get_agent_instance`.
        """
        instance = await _load_owned_instance(instance_id, principal)
        lease = await store.get_instance_lease(instance_id)
        return InstanceStatusResponse(
            instance_id=instance.instance_id,
            run_id=instance.run_id,
            run_state=_derive_run_state(instance),
            killed=instance.instance_id in killed_instances,
            status=instance.status.value,
            lease_status=lease.status.value if lease is not None else None,
        )

    @app.post("/agents/instances/{instance_id}/kill", response_model=KillResponse)
    async def kill_instance(
        instance_id: str,
        principal: PrivyPrincipal = Depends(require_principal),  # noqa: B008
    ) -> KillResponse:
        """Owner-gated, EXACTLY-ONCE kill for a deployed instance's live run (II-6 / AC-16).

        Owner-gated from server-owned state (never the caller's identity). Acts THROUGH the instance's
        live runtime handle — the hosted :class:`VeridexAgentAdapter`'s inherited
        :meth:`~VeridexAgentAdapter.acancel_run` (owner-first exactly-once ``ACTIVE -> CANCELLING``
        ``StopSignal`` trip) — never a new cancel primitive. The single winner engages the cancel,
        records the kill ledger, and emits the terminal cancelled OPS event; a repeat kill returns
        ``engaged=False`` with NO resume and NO second cancel. There is NO owner-RESTART (a re-arm is
        deferred to Gate P; it stays fail-closed here).

        Raises:
            HTTPException: 404 absent/unowned, 403 owned-by-another (both before any effect); 409 if no
                run was ever minted (``runtime_handle is None``) or the run is not live in this process
                and was never killed (fail-closed, honest — no crash, no resume).
        """
        instance = await _load_owned_instance(instance_id, principal)
        run_id = instance.run_id

        # Fail-closed: a run that was never minted (no runtime_handle) has nothing to kill.
        if instance.runtime_handle is None and instance_id not in killed_instances:
            raise HTTPException(
                status_code=409, detail={"error": "no_active_run", "instance_id": instance_id}
            )

        adapter = mm_run_adapters.get(instance_id)
        if adapter is None:
            # Not live in THIS process. A previously-engaged kill is an idempotent no-op (never a
            # resume); anything else fails closed honestly (the run already settled or lives elsewhere).
            if instance_id in killed_instances:
                return KillResponse(instance_id=instance_id, run_id=run_id, phase="cancelled", engaged=False)
            raise HTTPException(
                status_code=409, detail={"error": "run_not_active", "instance_id": instance_id}
            )

        try:
            result = await adapter.acancel_run(run_id, owner_did=instance.operator_id)
        except KeyError:
            # The adapter no longer tracks this run (it settled between lookup and cancel). A prior
            # engaged kill is idempotent; otherwise fail closed (no crash).
            if instance_id in killed_instances:
                return KillResponse(instance_id=instance_id, run_id=run_id, phase="cancelled", engaged=False)
            raise HTTPException(
                status_code=409, detail={"error": "run_not_active", "instance_id": instance_id}
            ) from None
        except OwnerMismatchError as exc:  # defense in depth — the outer gate already owner-checked.
            raise HTTPException(status_code=403, detail="principal does not own this run") from exc

        if result.engaged:
            # Exactly-once winner: record the kill ledger + emit the ONE terminal cancelled OPS event.
            killed_instances[instance_id] = run_id
            _emit_cancelled_ops(instance, run_id)
        return KillResponse(
            instance_id=instance_id, run_id=run_id, phase=result.phase.value, engaged=result.engaged
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
