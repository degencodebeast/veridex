"""D-1 — deployment READINESS probe (``/readyz``), distinct from I-5's basic ``/healthz`` liveness.

``/healthz`` (I-5) answers "is the process up?" and never touches the DB. Deployment readiness is
deeper: the stack is only ready to serve real traffic once its three durable dependencies are up —

  * **Postgres** is reachable (``SELECT 1``),
  * the **AgentOS session DB** is initialised — the durable OPS/runtime-events spool table exists
    (i.e. ``init_db`` has run), so the Agent-Ops feed can persist,
  * the **ReplayPack catalog** is loaded — at least one well-formed pack is present under
    ``REPLAY_PACK_ROOT``, so the demo/backtest surfaces have data to replay.

The probe is **FAIL-CLOSED**: any check that returns false OR raises marks the stack not-ready and
``/readyz`` returns 503. It never fails open (a raised probe is treated as down, never as up), so a
half-provisioned deploy is never advertised as ready. The three checks are independent injectable
callables, so each subsystem can be exercised — up and down — in isolation.

This module is additive deployment-readiness wiring only: it reads the durable dependencies, never
mutates them, and touches no auth / competition / instance / attempt authority.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from veridex.ingest.replay_pack import load_pack_marketstates

#: A single readiness check: an async, argument-free callable returning ``True`` when its subsystem
#: is ready. Raising is permitted and is treated as not-ready (fail-closed) by :func:`check_readiness`.
ReadinessProbe = Callable[[], Awaitable[bool]]

#: A supplier of the (possibly ``None``) psycopg connection pool — read lazily so the probe reflects
#: the pool's live state at request time (it is opened in the app lifespan, after app construction).
PoolSupplier = Callable[[], Any]


@dataclass(frozen=True)
class ReadinessReport:
    """Outcome of a readiness evaluation.

    Attributes:
        ready: ``True`` only when EVERY subsystem check passed (fail-closed AND).
        checks: Per-subsystem result, keyed by subsystem name.
    """

    ready: bool
    checks: dict[str, bool]


async def _run_probe(probe: ReadinessProbe) -> bool:
    """Evaluate one probe, converting any exception into a fail-closed ``False``.

    Args:
        probe: The subsystem readiness check.

    Returns:
        The probe's boolean result, or ``False`` if it raised (fail-closed — never fail-open).
    """
    try:
        return bool(await probe())
    except Exception:  # noqa: BLE001 — a readiness probe must never propagate; any error == not-ready.
        return False


async def check_readiness(
    *,
    postgres: ReadinessProbe,
    session_db: ReadinessProbe,
    replay_pack_catalog: ReadinessProbe,
) -> ReadinessReport:
    """Evaluate all three deployment-readiness checks; ready only when ALL pass (fail-closed).

    Args:
        postgres: Probe for Postgres reachability.
        session_db: Probe for the AgentOS session DB (runtime-events spool table initialised).
        replay_pack_catalog: Probe for a loaded ReplayPack catalog.

    Returns:
        A :class:`ReadinessReport` whose ``ready`` is the logical AND of the three checks; a probe
        that raises counts as ``False``.
    """
    checks = {
        "postgres": await _run_probe(postgres),
        "session_db": await _run_probe(session_db),
        "replay_pack_catalog": await _run_probe(replay_pack_catalog),
    }
    return ReadinessReport(ready=all(checks.values()), checks=checks)


# --------------------------------------------------------------------------------------------------
# Real subsystem probes (the deployed wiring)
# --------------------------------------------------------------------------------------------------


def make_postgres_probe(get_pool: PoolSupplier) -> ReadinessProbe:
    """Build a probe that confirms Postgres is reachable via the app's connection pool.

    Args:
        get_pool: Supplier of the psycopg ``AsyncConnectionPool`` (or ``None`` when the app is on the
            InMemory dev path — which is NOT ready for a durable deploy, so the probe returns False).

    Returns:
        An async probe returning ``True`` iff ``SELECT 1`` succeeds.
    """

    async def probe() -> bool:
        pool = get_pool()
        if pool is None:
            return False
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT 1")
            row = await cur.fetchone()
            return bool(row) and row[0] == 1

    return probe


def make_session_db_probe(get_pool: PoolSupplier) -> ReadinessProbe:
    """Build a probe that confirms the AgentOS session DB (runtime-events spool) is initialised.

    Distinct from raw connectivity: this query only succeeds once ``init_db`` has created the durable
    ``runtime_events`` table, so it catches a reachable-but-unprovisioned database (fail-closed).

    Args:
        get_pool: Supplier of the psycopg ``AsyncConnectionPool`` (``None`` → not ready).

    Returns:
        An async probe returning ``True`` iff the ``runtime_events`` table is queryable.
    """

    async def probe() -> bool:
        pool = get_pool()
        if pool is None:
            return False
        async with pool.connection() as conn, conn.cursor() as cur:
            # Existence + queryability of the spool table; LIMIT 0 keeps it O(1) regardless of size.
            await cur.execute("SELECT 1 FROM runtime_events LIMIT 0")
            await cur.fetchall()
            return True

    return probe


def _catalog_has_loadable_pack(root: Path) -> bool:
    """Return ``True`` iff ``root`` holds at least one ReplayPack the replay RUNTIME can load.

    Loadability is proven the way the runtime actually consumes a pack, NOT by mere file existence:
    for at least one declared fixture the records are read through the SAME verified loader the
    replay path uses (:func:`load_pack_marketstates`, ``verify=True`` — the runtime default), and a
    NON-EMPTY MarketState result is required. This is fail-closed — a pack whose ``records`` file is
    unparseable, whose schema is wrong, or whose stored ``content_hash`` no longer matches its data
    all make the loader raise, so such a pack is NOT loadable and ``/readyz`` reports not-ready
    rather than advertising a pack a judge's replay would then fail to start.

    A missing/empty/JSON-unparseable ``pack.json`` (at the root or one level down), or a manifest
    with no fixture that loads to a non-empty result, is likewise not-ready.
    """
    if not root.is_dir():
        return False
    candidates = [root / "pack.json", *sorted(root.glob("*/pack.json"))]
    for manifest in candidates:
        if not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        fixtures = data.get("fixtures") if isinstance(data, dict) else None
        if not isinstance(fixtures, list) or not fixtures:
            continue
        pack_dir = manifest.parent
        for fixture in fixtures:
            fixture_id = fixture.get("fixture_id") if isinstance(fixture, dict) else None
            if not isinstance(fixture_id, int):
                continue
            try:
                # Load ONE declared fixture through the verified runtime loader — this parses,
                # normalizes, and hash-verifies exactly as live replay does. One genuinely-loaded
                # fixture proves loadability; we do not walk every fixture of a large pinned pack.
                marketstates = load_pack_marketstates(pack_dir, fixture_id, verify=True)
            except Exception:  # noqa: BLE001 — any load failure == not-loadable (fail-closed).
                continue
            if marketstates:
                return True
    return False


def make_replay_pack_probe(pack_root: str | Path) -> ReadinessProbe:
    """Build a probe that confirms the ReplayPack catalog under ``pack_root`` is loaded.

    Args:
        pack_root: Directory the runtime resolves from ``REPLAY_PACK_ROOT`` (the curated seed-pack
            mount). An unset/blank value, a missing directory, or an empty/malformed catalog is
            not-ready (fail-closed).

    Returns:
        An async probe returning ``True`` iff a well-formed, loadable pack is present.
    """
    root = Path(pack_root) if pack_root else None

    async def probe() -> bool:
        if root is None:
            return False
        return _catalog_has_loadable_pack(root)

    return probe


# --------------------------------------------------------------------------------------------------
# Router
# --------------------------------------------------------------------------------------------------


def build_readiness_router(
    *,
    get_pool: PoolSupplier | None = None,
    pack_root: str | Path | None = None,
    postgres_probe: ReadinessProbe | None = None,
    session_probe: ReadinessProbe | None = None,
    pack_probe: ReadinessProbe | None = None,
) -> APIRouter:
    """Return an ``APIRouter`` exposing ``GET /readyz`` (200 ready / 503 not-ready, fail-closed).

    Probes may be supplied explicitly (the test seam) or built from ``get_pool`` + ``pack_root`` (the
    deployed wiring). Explicit probes win; otherwise the real Postgres / session-DB / pack-catalog
    probes are constructed.

    Args:
        get_pool: Supplier of the psycopg pool for the real Postgres + session-DB probes.
        pack_root: Directory (``REPLAY_PACK_ROOT``) for the real pack-catalog probe.
        postgres_probe: Explicit Postgres probe override.
        session_probe: Explicit session-DB probe override.
        pack_probe: Explicit pack-catalog probe override.

    Returns:
        A configured :class:`fastapi.APIRouter`.
    """
    supplier: PoolSupplier = get_pool if get_pool is not None else (lambda: None)
    postgres = postgres_probe or make_postgres_probe(supplier)
    session_db = session_probe or make_session_db_probe(supplier)
    packs = pack_probe or make_replay_pack_probe(pack_root or "")

    router = APIRouter()

    @router.get("/readyz")
    async def readyz() -> JSONResponse:
        """Deployment readiness: 200 when Postgres + session DB + pack catalog are all ready, else 503."""
        report = await check_readiness(postgres=postgres, session_db=session_db, replay_pack_catalog=packs)
        return JSONResponse(
            status_code=200 if report.ready else 503,
            content={"ready": report.ready, "checks": report.checks},
        )

    return router
