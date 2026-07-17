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

``psycopg_pool`` is imported lazily inside the default pool factory so importing this module (and the
offline suite) stays free of the optional ``postgres`` extra.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

from fastapi import FastAPI

from veridex.api.router import create_app
from veridex.config import get_settings
from veridex.store import InMemoryStore, PostgresStore, Store

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


def create_server_app(
    env: Mapping[str, str] | None = None,
    *,
    pool_factory: PoolFactory | None = None,
) -> FastAPI:
    """Build the public-deploy FastAPI app: durable Postgres when configured, else InMemory.

    Args:
        env: Environment mapping (defaults to ``os.environ``). Read for ``DATABASE_URL``,
            ``CORS_ORIGINS`` (required), and optional pool-sizing / connect-timeout keys.
        pool_factory: Injection seam for tests — builds the pool from ``(dsn, env)``. Defaults to a
            real ``psycopg_pool.AsyncConnectionPool``.

    Returns:
        A configured app. ``app.state.store`` exposes the resolved store (for durability assertions);
        ``app.state.db_pool`` is the pool on the Postgres path, else ``None``.

    Raises:
        ValueError: If ``CORS_ORIGINS`` is not configured (fail-closed).
    """
    resolved_env: Mapping[str, str] = os.environ if env is None else env
    _require_cors_origins(resolved_env)  # fail closed BEFORE any store/pool is built
    settings = get_settings()

    database_url = resolved_env.get("DATABASE_URL")
    if database_url:
        pool = (pool_factory or _default_pool_factory)(database_url, resolved_env)
        pg_store = PostgresStore(pool=pool)
        app = create_app(store=pg_store, settings=settings)
        timeout = float(resolved_env.get("DB_CONNECT_TIMEOUT_S", _DEFAULT_CONNECT_TIMEOUT_S))
        _install_pg_lifecycle(app, pool=pool, store=pg_store, timeout=timeout)
        store: Store = pg_store
        app.state.db_pool = pool
    else:
        # Explicit local-dev choice — NOT a fallback for an unreachable Postgres.
        store = InMemoryStore()
        app = create_app(store=store, settings=settings)
        app.state.db_pool = None

    app.state.store = store
    return app


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
