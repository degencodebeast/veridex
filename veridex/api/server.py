"""Uvicorn entrypoint (I-5): the PUBLIC-deploy app with a DURABLE, fail-closed Postgres backend.

Run under a container with ``uvicorn veridex.api.server:app`` (or ``python -m veridex.api.server``).

What this module guarantees over the bare ``create_app`` factory:
  * **init_db invoked once at startup** — ``PostgresStore.init_db`` runs a single idempotent
    ``CREATE TABLE IF NOT EXISTS`` pass so the tables EXIST before the first request (nothing else
    invokes it; without this the demo dies on restart).
  * **pooled connections** — a ``psycopg_pool.AsyncConnectionPool`` is wired into the store, so the
    Postgres path acquires/returns instead of opening a fresh connection per call.
  * **fail closed** — ``DATABASE_URL`` set-but-unreachable raises a loud error at startup (via
    ``pool.wait``); it NEVER silently downgrades to an ``InMemoryStore`` that would lose state on
    restart. Only an ABSENT ``DATABASE_URL`` selects InMemory (explicit local-dev, not a fallback).
  * **required env** — ``CORS_ORIGINS`` must be set or the app refuses to build (no localhost default).
  * **AgentOS surface hosted (II-5f)** — the served app is the deny-by-default GUARD hosting the
    AgentOS surface (AC-27/AC-29 enforced on the SERVED app, not just the test harness). Functional
    agent EXECUTION stays authority-bound via the per-instance deploy path (``deploy.py``), NOT the
    hosted wrapper route. The factory RETURNS THE GUARD (the ASGI callable), never the inner FastAPI.

``psycopg_pool`` is imported lazily inside the default pool factory so importing this module (and the
offline suite) stays free of the optional ``postgres`` extra.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator, Callable, Mapping
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI

from veridex.api.readiness import build_readiness_router
from veridex.config import get_settings
from veridex.ingest.replay_catalog import build_catalog
from veridex.store import InMemoryStore, PostgresStore, Store

if TYPE_CHECKING:
    from veridex.api.auth_privy import _Verifier
    from veridex.config import Settings
    from veridex.runtime.agentos_service import DenyByDefaultGuard
    from veridex.runtime.mm_agent_adapter import RunContext

# Container binds all interfaces by default (the process is the isolation boundary).
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000
# Pool sizing / startup-reachability timeout (overridable via env).
_DEFAULT_POOL_MIN_SIZE = 1
_DEFAULT_POOL_MAX_SIZE = 10
_DEFAULT_CONNECT_TIMEOUT_S = 10.0

PoolFactory = Callable[[str, Mapping[str, str]], Any]


def _require_cors_origins(env: Mapping[str, str]) -> list[str]:
    """Return the configured CORS origins, or FAIL CLOSED if none are set.

    The public entrypoint must not guess a localhost default: a missing ``CORS_ORIGINS`` is a
    misconfiguration that should refuse to start rather than silently serve an unreachable UI.
    """
    origins = [o.strip() for o in env.get("CORS_ORIGINS", "").split(",") if o.strip()]
    if not origins:
        raise ValueError("CORS_ORIGINS is required to serve the API (comma-separated web origins)")
    return origins


def _default_pool_factory(dsn: str, env: Mapping[str, str]) -> Any:
    """Create an un-opened ``AsyncConnectionPool`` (opened + reachability-checked in the lifespan)."""
    from psycopg_pool import AsyncConnectionPool  # lazy: only needed on the real serving path

    min_size = int(env.get("DB_POOL_MIN_SIZE", _DEFAULT_POOL_MIN_SIZE))
    max_size = int(env.get("DB_POOL_MAX_SIZE", _DEFAULT_POOL_MAX_SIZE))
    return AsyncConnectionPool(dsn, open=False, min_size=min_size, max_size=max_size)


def _install_pg_lifecycle(app: FastAPI, *, pool: Any, store: PostgresStore, timeout: float) -> None:
    """Compose a Postgres open/init_db/close lifespan AROUND the app's existing lifespan.

    Startup order (fail-closed): open the pool → ``pool.wait`` (raises if the DB is unreachable
    within ``timeout``, closing the pool) → ``init_db`` once. Only if all three succeed does the
    inner (deploy-task) lifespan run and the app serve. On shutdown the pool is closed.
    """
    inner_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def _combined(app_: FastAPI) -> AsyncIterator[None]:
        await pool.open()
        # Reachability gate: PoolTimeout (or any connect error) here means the DB is unreachable —
        # propagate it so the process refuses to start (never a silent InMemory downgrade).
        await pool.wait(timeout=timeout)
        async with pool.connection() as conn:
            await store.init_db(conn)
        try:
            async with inner_lifespan(app_):
                yield
        finally:
            await pool.close()

    app.router.lifespan_context = _combined


def _served_mm_not_executor(_ctx: RunContext) -> Any:
    """Fail-closed MM session factory for the served host adapter (SURFACE hosting; not the executor).

    SYNC by contract: ``build_market_maker_driver`` calls the session factory synchronously
    (``session_factory(ctx)``), so this MUST be a plain function — an ``async def`` would return an
    un-awaited coroutine (a ``coroutine ... never awaited`` warning) instead of failing closed.

    The served AgentOS wrapper route is a HOSTED SURFACE, not the per-instance MM executor: a single
    build-time adapter cannot reconstruct an arbitrary instance from a ``RunContext`` (it carries no
    ``instance_id``, and the wrapper mints a FRESH ``run_id`` that ``reconstruct_mm_session`` rejects,
    since it requires ``ctx.run_id == instance.run_id``). Functional per-instance runs are
    authority-bound via the deploy path (``deploy.py``), which builds a fresh per-instance adapter and
    drives ``start_owned_instance_run`` with ``run_id == instance.run_id``. This fails CLOSED rather
    than fabricating a run — and it is never reached by any public path (every agno-native route is
    denied by the guard; the served wrapper route denies before driving via ``surface_only``).
    """
    raise RuntimeError(
        "served AgentOS hosted wrapper route is not the per-instance MM executor; per-instance runs "
        "are authority-bound via the deploy path (deploy.py)"
    )


def _served_no_tape(_ctx: RunContext) -> Any:
    """Fail-closed tape resolver for the served directional host adapters (surface-only; not driven).

    No server-owned tape is wired for hosted directional runs on the served app — directional
    execution is authority-bound via the deploy path. The directional agno-native routes are all
    denied by the guard, so this is never reached publicly; it fails CLOSED if ever invoked.
    """
    raise RuntimeError(
        "no server-owned tape is wired for hosted directional runs on the served app; directional "
        "execution is authority-bound via the deploy path (deploy.py)"
    )


def _build_served_hosting_adapters() -> tuple[Any, list[Any]]:
    """Construct the SURFACE-HOSTING adapters the served app composes into AgentOS (Approach A).

    Faithful to II-8b's composed set: the primary MM adapter (``veridex-market-maker``) plus the two
    directional contestants (``veridex-cumulative-drift``, ``veridex-llm-drift``). These make the
    agno-native surface REAL behind the deny-by-default guard; their run drivers are NEVER exercised by
    any public path (every agno-native route is denied, and functional per-instance execution stays in
    the deploy path), so they fail CLOSED if ever driven rather than fabricating a run.

    agno-touching imports are LOCAL so this construction only pulls the runtime adapters on the real
    serving path (importing this module adds no NEW agno import beyond the existing router chain).
    """
    from veridex.runtime.directional_agent_adapters import (
        VeridexDeterministicAgentAdapter,
        VeridexLLMAgentAdapter,
    )
    from veridex.runtime.mm_agent_adapter import VeridexAgentAdapter, build_market_maker_driver
    from veridex.strategies.drift import cumulative_drift_agent
    from veridex.strategies.llm_drift import default_model_launcher, llm_drift_agent

    primary = VeridexAgentAdapter(
        run_driver=build_market_maker_driver(_served_mm_not_executor),
        id="veridex-market-maker",
    )
    det = VeridexDeterministicAgentAdapter(
        agent_factory=cumulative_drift_agent,
        tape_resolver=_served_no_tape,
        name="veridex-cumulative-drift",
        id="veridex-cumulative-drift",
    )

    def _served_llm_builder(model: Any, clock: Callable[[], float]) -> Any:
        return llm_drift_agent("veridex-llm-drift", model=model, clock=clock)

    llm = VeridexLLMAgentAdapter(
        agent_builder=_served_llm_builder,
        base_launcher=default_model_launcher(),  # the lazy Agno production launcher (never launched here)
        tape_resolver=_served_no_tape,
        name="veridex-llm-drift",
        id="veridex-llm-drift",
    )
    return primary, [det, llm]


def _require_durable_agentos_db_when_executor(owner_db: Any, *, surface_only: bool) -> None:
    """FAIL CLOSED when the served app would be an executor over an ephemeral AgentOS DB (Option-A coupling).

    In surface-only mode the composed AgentOS store is intentionally non-authoritative, so an in-memory
    agno DB is acceptable (readiness discloses it as non-gating). But if ``surface_only`` is disabled —
    executor mode, or a permitted Agno-native run/session route, or the wrapper becoming an AgentOS
    executor — the store becomes behaviorally/authoritatively relevant, and an in-memory agno DB (lost on
    restart) is no longer acceptable. Reject at startup until a durable backend is configured, so the
    temporary hackathon exception cannot silently survive a capability flip.
    """
    if surface_only:
        return
    from agno.db.in_memory import InMemoryDb  # lazy: only on the real serving path

    if isinstance(owner_db, InMemoryDb):
        raise RuntimeError(
            "executor mode (surface_only=False) requires a DURABLE AgentOS owner/session DB; an in-memory "
            "agno DB is process-local and lost on restart. Configure a durable backend before enabling "
            "native run/session execution (fail-closed coupling)."
        )


def _resolve_verifier(verifier: _Verifier | None) -> _Verifier:
    """Resolve the Privy token verifier, defaulting to the real ``verify_privy_token`` (injectable)."""
    if verifier is not None:
        return verifier
    from veridex.api.auth_privy import verify_privy_token

    return verify_privy_token


def create_server_app(
    env: Mapping[str, str] | None = None,
    *,
    pool_factory: PoolFactory | None = None,
    settings: Settings | None = None,
    verifier: _Verifier | None = None,
    surface_only: bool = True,
) -> DenyByDefaultGuard:
    """Build the public-deploy app: the deny-by-default GUARD hosting the AgentOS surface.

    Composes :func:`~veridex.runtime.agentos_service.build_agentos_app` INTO the served app so the
    AgentOS surface is hosted behind the deny-by-default boundary (AC-27/AC-29 enforced on the SERVED
    app, not just the test harness). The durable Postgres wiring is unchanged (durable when configured,
    else InMemory local-dev); it now operates on ``guard.app`` (the composed FastAPI), and the function
    RETURNS THE GUARD (the ASGI callable) — never the inner FastAPI.

    SURFACE HOSTING (Approach A): the served composition hosts the AgentOS surface; functional agent
    EXECUTION remains authority-bound via the per-instance deploy path (``deploy.py``), NOT the hosted
    wrapper route (the hosted adapters' run drivers fail closed if ever driven — never a fabricated run).

    Args:
        env: Environment mapping (defaults to ``os.environ``). Read for ``DATABASE_URL``,
            ``CORS_ORIGINS`` (required), ``REPLAY_PACK_ROOT`` (the read-only curated catalog root), the
            optional ``REPLAY_CAPTURE_ROOT`` (the separate writable capture root folded in at startup),
            and optional pool / connect-timeout keys.
        pool_factory: Injection seam for tests — builds the pool from ``(dsn, env)``. Defaults to a
            real ``psycopg_pool.AsyncConnectionPool``.
        settings: Injection seam for tests — resolved :class:`~veridex.config.Settings` (auth mode +
            Privy material). Defaults to :func:`~veridex.config.get_settings`.
        verifier: Injection seam for tests — the Privy token verifier. Defaults to the real
            ``verify_privy_token``.
        surface_only: When ``True`` (the deployed default), the served composition mounts the AgentOS
            surface behind deny-by-default and is NOT an executor — so an ephemeral in-memory AgentOS
            owner/session DB is acceptable (non-authoritative; readiness discloses it as non-gating). When
            ``False`` (executor mode / native run+session routes permitted), a DURABLE AgentOS backend is
            REQUIRED and this factory FAILS CLOSED at startup on an in-memory agno DB (Codex Option-A
            fail-closed coupling — the temporary exception cannot survive a capability flip).

    Returns:
        The :class:`~veridex.runtime.agentos_service.DenyByDefaultGuard` ASGI app. Its ``.app`` is the
        composed FastAPI: ``guard.app.state.store`` exposes the resolved store (durability assertions);
        ``guard.app.state.db_pool`` is the pool on the Postgres path, else ``None``.

    Raises:
        ValueError: If ``CORS_ORIGINS`` is not configured (fail-closed).
        RuntimeError: If ``surface_only`` is ``False`` while the composed AgentOS owner/session DB is an
            in-memory agno DB (fail-closed coupling — executor mode requires a durable backend).
    """
    resolved_env: Mapping[str, str] = os.environ if env is None else env
    _require_cors_origins(resolved_env)  # fail closed BEFORE any store/pool/composition is built
    resolved_settings = get_settings() if settings is None else settings

    # Resolve the store/pool (durable Postgres when configured, else explicit InMemory local-dev).
    database_url = resolved_env.get("DATABASE_URL")
    if database_url:
        pool = (pool_factory or _default_pool_factory)(database_url, resolved_env)
        pg_store = PostgresStore(pool=pool)
        store: Store = pg_store
    else:
        # Explicit local-dev choice — NOT a fallback for an unreachable Postgres.
        store = InMemoryStore()
        pool = None

    # Compose AgentOS behind the deny-by-default guard. agno-touching imports are LOCAL (build_agentos_app
    # keeps the agno import lazy internally; the adapters are built via the local helper).
    from agno.db.in_memory import InMemoryDb  # noqa: PLC0415 (lazy: only on the real serving path)

    from veridex.runtime.agentos_service import build_agentos_app  # noqa: PLC0415

    # The AgentOS owner/session DB. Surface-only served mode: the served app mounts the reviewed AgentOS
    # adapter surface behind deny-by-default; execution and durable authority remain on the Veridex
    # per-instance/Postgres path. The composed AgentOS store is therefore a non-authoritative, ephemeral
    # in-memory agno DB (rebuilt at process start; losing it cannot change an authoritative result or
    # permit an action). We do NOT claim durable AgentOS sessions: /readyz discloses this store as
    # NON-GATING info (see build_readiness_router) instead of gating on it.
    #
    # RESIDUAL (tracked, post-hackathon): a DURABLE agno DB (agno's ``PostgresDb``) needs SQLAlchemy/
    # greenlet, ``postgresql+psycopg://`` DSN handling, an independent pool + schema/migrations, a real
    # DB readiness probe, and a restart-persistence test — none are project dependencies today.
    owner_db = InMemoryDb()

    # FAIL-CLOSED COUPLING (Codex Option-A): the ephemeral-AgentOS-DB exception is valid ONLY while the
    # served app is surface-only. If a future capability flip disables surface_only (executor mode /
    # native run+session routes permitted, or the wrapper becomes an AgentOS executor), a durable AgentOS
    # backend is REQUIRED — an in-memory agno DB is process-local and lost on restart. Reject at STARTUP
    # so the temporary exception cannot silently survive the capability change.
    _require_durable_agentos_db_when_executor(owner_db, surface_only=surface_only)

    # D-1 deployment READINESS probe (additive; distinct from I-5's /healthz liveness). Reads the live
    # pool + the composed AgentOS DB LAZILY at request time (the pool is attached to guard.app.state
    # AFTER composition). Registered pre-snapshot as a base router so /readyz is veridex-owned/self-gated
    # (it PASSES the guard, returning 200/503) rather than treated as agno-native and denied. The gate is
    # ONLY the durable Veridex deps; the AgentOS store is disclosed as non-gating info (surface_only).
    pool_holder: dict[str, Any] = {"pool": None}
    pack_root = resolved_env.get("REPLAY_PACK_ROOT", "")

    # R-2 — build the TRUSTED, hash-verified ReplayPack CATALOG at startup (root of replay trust).
    # Scans the READ-ONLY curated REPLAY_PACK_ROOT (and, when configured, the SEPARATE writable capture
    # root so redeploy-surviving captures are folded in), hash-verifies every pack, and allowlists only
    # the verified ones with HONEST provenance — a tampered/unverified pack is fail-closed EXCLUDED. It
    # is exposed on ``app.state`` for the /readyz probe + R-3's serving API; the writable-root register
    # path (``catalog.register_pack``) atomically promotes freshly-captured deployed packs at runtime
    # (no restart), and NEVER writes the read-only curated root. This is additive: it does NOT alter the
    # II-5f served composition (the guard return / deny-by-default / /readyz gate set are unchanged).
    # Built BEFORE the readiness router so /readyz probes the AUTHORITATIVE R-2 catalog (Codex MAJOR-3),
    # not a weaker second filesystem validator.
    replay_catalog = build_catalog(
        pack_root, capture_root=resolved_env.get("REPLAY_CAPTURE_ROOT", "") or None
    )

    readiness_router = build_readiness_router(
        get_pool=lambda: pool_holder["pool"],
        get_catalog=lambda: replay_catalog,  # /readyz gates on the AUTHORITATIVE R-2 catalog (MAJOR-3)
        get_agentos_db=lambda: owner_db,  # /readyz DISCLOSES the ACTUAL (ephemeral) AgentOS DB, honestly
        surface_only=surface_only,
    )

    primary, extra_agents = _build_served_hosting_adapters()
    guard = build_agentos_app(
        store=store,
        settings=resolved_settings,
        adapter=primary,
        extra_agents=extra_agents,
        owner_db=owner_db,
        verifier=_resolve_verifier(verifier),
        enforce_contract=True,  # AC-29: fail-closed on any agno-native surface drift
        base_routers=[readiness_router],  # /readyz registered pre-snapshot -> veridex-owned, public
        surface_only=surface_only,  # SURFACE hosting: wrapper routes deny before mutation (not executor)
    )
    app = guard.app  # the composed FastAPI: durability lifecycle + state live HERE (not on the guard)

    if database_url:
        timeout = float(resolved_env.get("DB_CONNECT_TIMEOUT_S", _DEFAULT_CONNECT_TIMEOUT_S))
        _install_pg_lifecycle(app, pool=pool, store=pg_store, timeout=timeout)
        app.state.db_pool = pool
        pool_holder["pool"] = pool  # readiness now sees the live pool (opened in the lifespan)
    else:
        app.state.db_pool = None

    app.state.store = store
    app.state.replay_catalog = replay_catalog  # R-2: trusted hash-verified catalog for /readyz + R-3
    return guard  # RETURN THE GUARD (the ASGI callable) — never the inner FastAPI


def main() -> None:
    """Run the app under uvicorn, binding ``HOST``/``PORT`` from the environment.

    Uses the ASGI-factory form so the app is built from the environment INSIDE the server process
    (a misconfiguration, e.g. missing ``CORS_ORIGINS``, then fails startup loudly). Importing this
    module never builds the app — keeping the offline test suite free of the required serving env.
    """
    import uvicorn  # lazy: only needed when actually serving

    host = os.environ.get("HOST", DEFAULT_HOST)
    port = int(os.environ.get("PORT", DEFAULT_PORT))
    uvicorn.run("veridex.api.server:create_server_app", factory=True, host=host, port=port)


if __name__ == "__main__":
    main()
