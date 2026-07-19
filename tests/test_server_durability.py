"""I-5 — durable Postgres wiring for the uvicorn entrypoint (``veridex.api.server``) (TDD).

The load-bearing floor this task defends:
  * ``init_db()`` is invoked ONCE at startup so the tables actually exist (nothing did this before).
  * the Postgres path acquires from a psycopg **connection pool**, not a fresh connect-per-call.
  * ``DATABASE_URL`` set-but-unreachable FAILS CLOSED (loud startup error) — it NEVER silently
    substitutes an ``InMemoryStore`` (a demo that runs on InMemory in prod loses state on restart).
  * a required-but-missing env (``CORS_ORIGINS``) fails closed at build, not a silent localhost default.

The fail-closed + init_db + pool assertions are exercised with lightweight fakes so they run
deterministically WITHOUT a live Postgres (no DATABASE_URL required, never SKIPed here).
"""

from __future__ import annotations

import contextlib
from typing import Any
from unittest.mock import AsyncMock

import pytest

from veridex.api import server
from veridex.store import InMemoryStore, PostgresStore

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakePool:
    """Minimal async pool stand-in: records lifecycle calls; connection() yields a dummy conn."""

    def __init__(self, *, wait_error: Exception | None = None) -> None:
        self.opened = False
        self.waited = False
        self.closed = False
        self.getconn_calls = 0
        self.putconn_calls = 0
        self._wait_error = wait_error

    async def open(self) -> None:
        self.opened = True

    async def wait(self, timeout: float = 30.0) -> None:
        self.waited = True
        if self._wait_error is not None:
            raise self._wait_error

    @contextlib.asynccontextmanager
    async def connection(self) -> Any:
        yield object()

    async def getconn(self, timeout: float | None = None) -> Any:
        self.getconn_calls += 1
        return object()

    async def putconn(self, conn: Any) -> None:
        self.putconn_calls += 1

    async def close(self) -> None:
        self.closed = True


_ENV = {"DATABASE_URL": "postgresql://ignored/db", "CORS_ORIGINS": "https://app.example.test"}


# ---------------------------------------------------------------------------
# RED 6 — missing CORS_ORIGINS fails closed at build
# ---------------------------------------------------------------------------


def test_missing_cors_origins_fails_closed() -> None:
    """Building the serving app with no CORS_ORIGINS raises loudly (no silent localhost default)."""
    with pytest.raises(ValueError, match="CORS_ORIGINS"):
        server.create_server_app(env={"DATABASE_URL": "postgresql://ignored/db"})


# ---------------------------------------------------------------------------
# RED 3 — init_db invoked once at startup
# ---------------------------------------------------------------------------


async def test_init_db_invoked_once_at_startup() -> None:
    """Entering the app lifespan invokes PostgresStore.init_db exactly once (tables get created)."""
    pool = _FakePool()
    # II-5f: create_server_app returns the deny-by-default GUARD; the durable Postgres wiring
    # (store + lifecycle + pool) lives on the composed inner FastAPI at ``guard.app``.
    app = server.create_server_app(env=_ENV, pool_factory=lambda dsn, env: pool).app
    assert isinstance(app.state.store, PostgresStore)

    spy = AsyncMock()
    app.state.store.init_db = spy  # type: ignore[method-assign]

    async with app.router.lifespan_context(app):
        pass

    spy.assert_awaited_once()
    assert pool.opened and pool.waited and pool.closed


# ---------------------------------------------------------------------------
# RED 4 — the Postgres path acquires from the pool, not a fresh connect-per-call
# ---------------------------------------------------------------------------


async def test_postgres_store_acquires_from_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """PostgresStore(pool=...)._connect acquires via pool.getconn (never opens a raw connection)."""

    def _boom(*_a: Any, **_k: Any) -> Any:  # a per-call connect would call this — it must not
        raise AssertionError("PostgresStore opened a raw connection instead of using the pool")

    import psycopg

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", staticmethod(_boom))

    pool = _FakePool()
    store = PostgresStore(pool=pool)
    conn = await store._connect()
    assert pool.getconn_calls == 1

    await store._release(conn)
    assert pool.putconn_calls == 1


# ---------------------------------------------------------------------------
# RED 5 — DATABASE_URL set-but-unreachable fails closed (never a silent InMemory fallback)
# ---------------------------------------------------------------------------


async def test_unreachable_db_fails_closed_no_inmemory_fallback() -> None:
    """A pool that can't reach the DB raises at startup; the store is Postgres, never InMemory."""
    pool = _FakePool(wait_error=RuntimeError("pool initialization incomplete"))
    app = server.create_server_app(env=_ENV, pool_factory=lambda dsn, env: pool).app

    # The Postgres path was chosen — NOT silently downgraded to InMemory.
    assert isinstance(app.state.store, PostgresStore)
    assert not isinstance(app.state.store, InMemoryStore)

    with pytest.raises(RuntimeError, match="pool initialization incomplete"):
        async with app.router.lifespan_context(app):
            pass


def test_no_database_url_uses_inmemory() -> None:
    """With no DATABASE_URL the entrypoint uses InMemoryStore (local-dev), still CORS-validated."""
    app = server.create_server_app(env={"CORS_ORIGINS": "https://app.example.test"}).app
    assert isinstance(app.state.store, InMemoryStore)


# ---------------------------------------------------------------------------
# R-2 — the trusted, hash-verified ReplayPack catalog is built at startup + exposed on app.state
# ---------------------------------------------------------------------------


def test_replay_catalog_built_from_pack_root_and_exposed(tmp_path: Any) -> None:
    """create_server_app builds the hash-verified catalog from REPLAY_PACK_ROOT onto app.state (R-3 seam)."""
    import shutil
    from pathlib import Path

    from veridex.ingest.replay_catalog import ReplayCatalog

    real_src = Path(__file__).resolve().parents[1] / "scripts" / "fixtures" / "demo_pack_real"
    curated = tmp_path / "curated"
    shutil.copytree(real_src, curated / "real")

    app = server.create_server_app(
        env={"CORS_ORIGINS": "https://app.example.test", "REPLAY_PACK_ROOT": str(curated)}
    ).app

    catalog = app.state.replay_catalog
    assert isinstance(catalog, ReplayCatalog)
    entry = catalog.get("real")
    assert entry is not None and entry.is_genuine is True  # the genuine curated pack is allowlisted


def test_replay_catalog_empty_when_pack_root_unset() -> None:
    """No REPLAY_PACK_ROOT -> an empty (but present) catalog; the app still builds (fail-closed, no crash)."""
    from veridex.ingest.replay_catalog import ReplayCatalog

    app = server.create_server_app(env={"CORS_ORIGINS": "https://app.example.test"}).app
    assert isinstance(app.state.replay_catalog, ReplayCatalog)
    assert len(app.state.replay_catalog) == 0


# ---------------------------------------------------------------------------
# MAJOR 4 (Codex R-2 gate) — the deployed writable capture volume is mounted but
# was never handed to the process via REPLAY_CAPTURE_ROOT, so an in-memory
# promoted R-0b pack vanished after a container restart (the new process never
# rescanned the mounted capture volume). Wire REPLAY_CAPTURE_ROOT = the capture
# mount; a captured pack must be REDISCOVERED by a fresh startup rescan.
# ---------------------------------------------------------------------------

_CAPTURE_MOUNT = "/var/lib/veridex/replay-packs/capture"


def _make_synthetic_pack_at(out_dir: Any, *, fixture_id: int = 42) -> Any:
    """Build a real, hash-valid SYNTHETIC ReplayPack directory at ``out_dir`` (returns the dir)."""
    from pathlib import Path

    from veridex.ingest.capture_chain import synthetic_authority
    from veridex.ingest.recorder import SessionMeta, envelope_line
    from veridex.ingest.replay_pack import pack_from_session

    out_dir = Path(out_dir)
    session = out_dir.parent / f"_session_{out_dir.name}"
    session.mkdir(parents=True, exist_ok=True)
    rec = {
        "FixtureId": fixture_id,
        "Ts": 100_000,
        "InRunning": False,
        "SuperOddsType": "1X2",
        "MarketPeriod": None,
        "MarketParameters": None,
        "PriceNames": ["Home", "Draw", "Away"],
        "Prices": [2500, 3200, 2800],
        "Pct": [35.5, 28.0, 36.5],
    }
    (session / "records.jsonl").write_text(
        envelope_line(rec, 100) + "\n" + envelope_line({**rec, "Ts": 131_000}, 131) + "\n"
    )
    (session / "meta.json").write_text(
        SessionMeta(started_ts=99, endpoints=["/odds/stream"], tool_version="t").model_dump_json()
    )
    pack_from_session(session, out_dir, authority=synthetic_authority())
    return out_dir


def test_m4_captured_pack_rediscovered_at_startup_via_capture_root_env(tmp_path: Any) -> None:
    """A pack persisted on the writable capture volume must be REDISCOVERED by a fresh startup rescan
    when REPLAY_CAPTURE_ROOT points at that volume — restart-durability of promoted R-0b packs."""
    curated = tmp_path / "curated"
    curated.mkdir()
    capture = tmp_path / "capture"
    capture.mkdir()
    # Simulate an R-0b pack that had been promoted and persisted on the writable capture volume.
    _make_synthetic_pack_at(capture / "promoted", fixture_id=77)

    # A FRESH process startup: create_server_app reads REPLAY_CAPTURE_ROOT and folds the capture volume in.
    app = server.create_server_app(
        env={
            "CORS_ORIGINS": "https://app.example.test",
            "REPLAY_PACK_ROOT": str(curated),
            "REPLAY_CAPTURE_ROOT": str(capture),
        }
    ).app

    entry = app.state.replay_catalog.get("promoted")
    assert entry is not None  # survived the "restart" — rediscovered from the mounted capture volume
    assert entry.fixtures == (77,)


def test_m4_compose_and_env_example_wire_replay_capture_root() -> None:
    """The deployed config must SET REPLAY_CAPTURE_ROOT to the writable capture mount (config-wiring RED).

    Without this, the mounted ``replay-capture`` volume is never handed to the process, so a fresh
    startup cannot rescan it and a promoted pack disappears on restart.
    """
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    compose = (repo / "compose.coolify.yml").read_text()
    env_example = (repo / ".env.example").read_text()

    # compose sets REPLAY_CAPTURE_ROOT AND still mounts the writable capture volume at the same path.
    assert "REPLAY_CAPTURE_ROOT" in compose
    assert _CAPTURE_MOUNT in compose
    assert f"replay-capture:{_CAPTURE_MOUNT}" in compose  # the writable mount is unchanged
    # .env.example documents the capture-root var so operators can override it.
    assert "REPLAY_CAPTURE_ROOT" in env_example
