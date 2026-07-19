"""D-1 — deployment READINESS probe (``/readyz``), distinct from I-5's basic ``/healthz`` liveness.

``/healthz`` (I-5) answers "is the process up?" and never touches the DB. Deployment readiness is
deeper: the stack is only ready to serve real traffic once the durable dependencies of the DEPLOYED
(surface-only) mode are up. Those — and ONLY those — form the top-level readiness conjunction:

  * **Postgres** is reachable (``SELECT 1``),
  * the **runtime-event/OPS spool** is durable — the Veridex ``runtime_events`` table exists
    (i.e. ``init_db`` has run) so the Agent-Ops feed can persist,
  * the **ReplayPack catalog** is loaded — at least one well-formed pack is present under
    ``REPLAY_PACK_ROOT``, so the demo/backtest surfaces have data to replay.

**The served application mounts the reviewed AgentOS adapter surface behind deny-by-default; execution
and durable authority remain on the Veridex per-instance/Postgres path.** In that surface-only mode the
composed AgentOS owner/session store is an ephemeral in-memory agno DB — non-authoritative, rebuilt at
process start, and losing it cannot change an authoritative result or permit an action. It therefore
does NOT gate readiness. Rather than hide it, ``/readyz`` DISCLOSES it as a non-gating informational
field (``agentos_session_store``: ``backend``/``durable``/``required_for_ready``/``mode``) so no claim
of durable AgentOS sessions, resumable Agno sessions, or functional hosted AgentOS execution is made.

**Fail-closed coupling (Codex Option-A):** this exception holds ONLY while the app is surface-only. If
``surface_only`` is disabled (executor mode / native run+session routes permitted), the served startup
rejects an in-memory AgentOS DB (:func:`veridex.api.server.create_server_app`), and — belt-and-suspenders
— a readiness router built with ``surface_only=False`` promotes the AgentOS store into the gating
conjunction. The temporary exception cannot silently survive a capability flip. (Durable AgentOS storage
— SQLAlchemy/greenlet, a ``postgresql+psycopg://`` DSN, an independent pool + schema/migrations, a real
DB readiness probe, and a restart-persistence test — is a tracked post-hackathon residual.)

The probe is **FAIL-CLOSED**: any gating check that returns false OR raises marks the stack not-ready and
``/readyz`` returns 503. It never fails open (a raised probe is treated as down, never as up), so a
half-provisioned deploy is never advertised as ready. The gating checks are independent injectable
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

#: A supplier of the (possibly ``None``) AgentOS owner/session DB object (an ``agno`` db). Read lazily
#: so the probe reflects the DB actually composed into AgentOS — an in-memory agno DB is ephemeral
#: (process-local, empty after restart) and must NOT be advertised as a durable session DB.
AgentOsDbSupplier = Callable[[], Any]

#: A supplier of the (possibly ``None``) AUTHORITATIVE R-2 :class:`~veridex.ingest.replay_catalog.ReplayCatalog`.
#: Read lazily so the probe reflects the LIVE catalog (it can gain runtime-promoted packs after startup).
CatalogSupplier = Callable[[], Any]


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
    runtime_event_spool: ReadinessProbe,
    replay_pack_catalog: ReadinessProbe,
    agentos_session_store: ReadinessProbe | None = None,
) -> ReadinessReport:
    """Evaluate the deployment-readiness checks; ready only when ALL pass (fail-closed).

    The gating conjunction is the durable Veridex dependencies of the surface-only served mode:
    Postgres, the runtime-event/OPS spool, and the ReplayPack catalog. The AgentOS in-memory session
    store does NOT participate in surface-only mode — it is disclosed separately as non-gating info.

    Args:
        postgres: Probe for Postgres reachability.
        runtime_event_spool: Probe for the durable Veridex ``runtime_events`` OPS spool table.
        replay_pack_catalog: Probe for a loaded ReplayPack catalog.
        agentos_session_store: OPTIONAL gating probe for the AgentOS owner/session DB. Supplied ONLY in
            executor mode (``surface_only=False``) — the fail-closed coupling that promotes the store
            into the conjunction once it becomes authoritative. ``None`` (surface-only) leaves it out.

    Returns:
        A :class:`ReadinessReport` whose ``ready`` is the logical AND of the gating checks; a probe
        that raises counts as ``False``.
    """
    checks = {
        "postgres": await _run_probe(postgres),
        "runtime_event_spool": await _run_probe(runtime_event_spool),
        "replay_pack_catalog": await _run_probe(replay_pack_catalog),
    }
    if agentos_session_store is not None:
        # Executor mode ONLY: the AgentOS store is now authoritative, so it gates (fail-closed coupling).
        checks["agentos_session_store"] = await _run_probe(agentos_session_store)
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


def make_runtime_event_spool_probe(get_pool: PoolSupplier) -> ReadinessProbe:
    """Build a probe that confirms the durable Veridex runtime-event/OPS spool is initialised.

    Accurately named: this queries the Veridex ``runtime_events`` table (the OPS/Agent-Ops spool), NOT
    any AgentOS/agno session DB. Distinct from raw connectivity: this query only succeeds once
    ``init_db`` has created the durable ``runtime_events`` table, so it catches a
    reachable-but-unprovisioned database (fail-closed).

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


def _classify_agentos_db(db: Any) -> tuple[str, bool]:
    """Classify the composed AgentOS owner/session DB as ``(backend, durable)`` (honest, no claims).

    Feeds BOTH the non-gating ``agentos_session_store`` disclosure AND the executor-mode gate. An
    in-memory agno DB is ephemeral (process-local, empty after restart) → not durable; ``None`` is
    unwired; anything else is treated as a durable (persistent) backend.
    """
    if db is None:
        return ("unwired", False)
    from agno.db.in_memory import InMemoryDb  # lazy: only on the real serving path

    if isinstance(db, InMemoryDb):
        return ("in_memory", False)
    return ("durable", True)


def make_agentos_session_db_probe(get_agentos_db: AgentOsDbSupplier) -> ReadinessProbe:
    """Build a probe reflecting the ACTUAL AgentOS owner/session DB's durability (fail-closed honest).

    Distinct from :func:`make_runtime_event_spool_probe` (which checks the Veridex ``runtime_events``
    OPS spool): this inspects the ``agno`` DB actually composed into AgentOS. An in-memory AgentOS DB is
    process-local and empty after a restart, so it is reported NOT-ready; a durable (non-in-memory) agno
    DB is reported ready; ``None`` (unwired) is not-ready.

    In the DEPLOYED surface-only mode this probe is NOT part of the readiness conjunction (the store is
    disclosed as non-gating info). It becomes the GATING check only in executor mode
    (``surface_only=False``) — the fail-closed coupling.

    Args:
        get_agentos_db: Supplier of the composed AgentOS db object (``None`` → not ready).

    Returns:
        An async probe returning ``True`` iff the AgentOS DB is a durable (non-in-memory) agno DB.
    """

    async def probe() -> bool:
        _backend, durable = _classify_agentos_db(get_agentos_db())
        return durable

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


def _entry_loadable_with_admitted_hash(entry: Any) -> bool:
    """Return ``True`` iff a catalogued entry is still loadable AT ITS EXACT ADMITTED HASH (fail-closed).

    Two gates, both fail-closed: (1) the pack's CURRENT stored ``content_hash`` must equal the hash the
    catalog ADMITTED (``entry.content_hash``) — a drift means the served bytes are no longer the admitted
    ones; (2) at least one declared fixture must load through the SAME verified runtime loader live
    replay uses (:func:`load_pack_marketstates`, ``verify=True``) to a NON-EMPTY result. Any error /
    mismatch counts as not-loadable.
    """
    pack_dir = Path(entry.pack_dir)
    try:
        manifest = json.loads((pack_dir / "pack.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(manifest, dict) or manifest.get("content_hash") != entry.content_hash:
        return False  # the served bytes drifted from the exact admitted hash
    for fixture_id in entry.fixtures:
        try:
            marketstates = load_pack_marketstates(pack_dir, fixture_id, verify=True)
        except Exception:  # noqa: BLE001 — any load failure == not-loadable (fail-closed).
            continue
        if marketstates:
            return True
    return False


def make_replay_catalog_probe(get_catalog: CatalogSupplier) -> ReadinessProbe:
    """Build the AUTHORITATIVE ReplayPack-catalog readiness probe (consults the R-2 catalog directly).

    Distinct from :func:`make_replay_pack_probe` (a weaker, independent filesystem scanner that admits a
    ``bool`` fixture id via ``isinstance(fixture_id, int)``): this probe consults the SAME allowlisted,
    hash-verified R-2 :class:`~veridex.ingest.replay_catalog.ReplayCatalog` the serving path uses, and
    proves at least ONE catalogued entry remains loadable at its EXACT admitted hash
    (:func:`_entry_loadable_with_admitted_hash`). An empty catalog (or none loadable) is not-ready —
    fail-closed, so a deploy whose authoritative catalog has nothing servable is never advertised ready.

    Args:
        get_catalog: Supplier of the live R-2 catalog (``None`` → not ready).

    Returns:
        An async probe returning ``True`` iff a catalogued entry is loadable at its admitted hash.
    """

    async def probe() -> bool:
        catalog = get_catalog()
        if catalog is None:
            return False
        return any(_entry_loadable_with_admitted_hash(e) for e in catalog.snapshot().values())

    return probe


# --------------------------------------------------------------------------------------------------
# Router
# --------------------------------------------------------------------------------------------------


def build_readiness_router(
    *,
    get_pool: PoolSupplier | None = None,
    pack_root: str | Path | None = None,
    get_catalog: CatalogSupplier | None = None,
    get_agentos_db: AgentOsDbSupplier | None = None,
    surface_only: bool = True,
    postgres_probe: ReadinessProbe | None = None,
    runtime_event_spool_probe: ReadinessProbe | None = None,
    pack_probe: ReadinessProbe | None = None,
) -> APIRouter:
    """Return an ``APIRouter`` exposing ``GET /readyz`` (200 ready / 503 not-ready, fail-closed).

    The top-level readiness conjunction is the durable Veridex dependencies of the surface-only served
    mode: Postgres, the runtime-event/OPS spool, and the ReplayPack catalog. Probes may be supplied
    explicitly (the test seam) or built from ``get_pool`` + ``pack_root`` (the deployed wiring); explicit
    probes win.

    When ``get_agentos_db`` is provided (the served composition), ``/readyz`` additionally publishes a
    NON-GATING ``agentos_session_store`` disclosure describing the composed AgentOS owner/session DB
    (``backend`` / ``durable`` / ``required_for_ready`` / ``mode``) — honest about the ephemeral
    in-memory store without claiming durable AgentOS sessions. The store gates readiness ONLY in executor
    mode (``surface_only=False``): the fail-closed coupling that promotes it into the conjunction once it
    becomes authoritative.

    Args:
        get_pool: Supplier of the psycopg pool for the real Postgres + runtime-event-spool probes.
        pack_root: Directory (``REPLAY_PACK_ROOT``) for the LEGACY filesystem pack scanner — used only as
            a fallback when neither ``pack_probe`` nor ``get_catalog`` is supplied (never the deployed path).
        get_catalog: Supplier of the AUTHORITATIVE R-2 ReplayPack catalog. When provided (the deployed
            served path), the ``replay_pack_catalog`` gate consults the catalog directly and proves an
            entry is loadable at its exact admitted hash — not the weaker filesystem scanner.
        get_agentos_db: Supplier of the composed AgentOS owner/session DB. When provided, ``/readyz``
            emits the non-gating ``agentos_session_store`` disclosure; in executor mode it also gates.
        surface_only: ``True`` (the deployed served mode) → the AgentOS store is non-gating info. ``False``
            (executor mode) → the store is a GATING check (durable required) and ``required_for_ready``
            is reported true.
        postgres_probe: Explicit Postgres probe override.
        runtime_event_spool_probe: Explicit runtime-event/OPS-spool probe override.
        pack_probe: Explicit pack-catalog probe override.

    Returns:
        A configured :class:`fastapi.APIRouter`.
    """
    supplier: PoolSupplier = get_pool if get_pool is not None else (lambda: None)
    postgres = postgres_probe or make_postgres_probe(supplier)
    runtime_event_spool = runtime_event_spool_probe or make_runtime_event_spool_probe(supplier)
    # The replay_pack_catalog gate must be AUTHORITATIVE (MAJOR-3): when a catalog supplier is wired (the
    # deployed served path) the probe consults the R-2 catalog directly. An explicit ``pack_probe`` still
    # wins (test seam); ``make_replay_pack_probe`` (the weaker legacy filesystem scanner) is only the
    # fallback for callers that supply neither — never the deployed path.
    if pack_probe is not None:
        packs = pack_probe
    elif get_catalog is not None:
        packs = make_replay_catalog_probe(get_catalog)
    else:
        packs = make_replay_pack_probe(pack_root or "")

    # Executor-mode ONLY (surface_only=False): the AgentOS store becomes authoritative, so it gates the
    # conjunction (fail-closed coupling). In surface-only mode it is disclosed but NEVER gates.
    agentos_gate: ReadinessProbe | None = (
        make_agentos_session_db_probe(get_agentos_db)
        if (get_agentos_db is not None and not surface_only)
        else None
    )

    def _agentos_disclosure() -> dict[str, Any]:
        backend, durable = _classify_agentos_db(get_agentos_db() if get_agentos_db is not None else None)
        return {
            "backend": backend,
            "durable": durable,
            # In surface-only mode the store is intentionally non-authoritative -> not required for ready.
            "required_for_ready": not surface_only,
            "mode": "surface_only" if surface_only else "executor",
        }

    router = APIRouter()

    @router.get("/readyz")
    async def readyz() -> JSONResponse:
        """Deployment readiness: 200 when the durable Veridex deps are all ready, else 503 (fail-closed).

        In executor mode the AgentOS store also gates. The composed AgentOS store is always DISCLOSED as
        non-gating ``agentos_session_store`` info when wired — never a durable-session claim.
        """
        report = await check_readiness(
            postgres=postgres,
            runtime_event_spool=runtime_event_spool,
            replay_pack_catalog=packs,
            agentos_session_store=agentos_gate,
        )
        content: dict[str, Any] = {"ready": report.ready, "checks": report.checks}
        if get_agentos_db is not None:
            content["agentos_session_store"] = _agentos_disclosure()
        return JSONResponse(status_code=200 if report.ready else 503, content=content)

    return router
