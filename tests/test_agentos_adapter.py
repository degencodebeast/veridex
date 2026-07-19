"""II-4 RED suite — the AC-27 deny-by-default AgentOS boundary + lease + owner-first exactly-once cancel.

OFFLINE: ``agno`` is IMPORTED, the AgentOS db is agno's ``InMemoryDb``, the Veridex store is
``InMemoryStore`` — NO network, NO SqliteDb/Postgres (which need the sqlalchemy extra). The
Postgres-backed lease RED SKIPs without ``DATABASE_URL`` (it is never faked).

Each test maps to one of Codex's 9 required RED controls or a plan RED; the control number is named in
the test docstring.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from agno.db.in_memory import InMemoryDb
from fastapi import FastAPI
from fastapi.testclient import TestClient

from veridex.api.auth_privy import PrivyPrincipal
from veridex.config import Settings
from veridex.deploy.instance import AgentInstance, DeployStatus
from veridex.runtime import agentos_service as svc
from veridex.runtime.mm_agent_adapter import (
    CancelResult,
    OwnerMismatchError,
    RunContext,
    RunPhase,
    VeridexAgentAdapter,
    build_market_maker_driver,
)
from veridex.runtime.runtime_events import RuntimeEventType, RuntimeStatus
from veridex.store import (
    DuplicateLeaseError,
    InMemoryStore,
    InstanceLease,
    LeaseStatus,
    lease_status_values,
)

# --- identities ---------------------------------------------------------------

OWNER_A = "did:privy:ownerA"
OWNER_B = "did:privy:ownerB"
_TOKENS = {"tokenA": OWNER_A, "tokenB": OWNER_B}


def _fake_verifier(token: str, *, app_id: str | None, verification_key: str | None) -> PrivyPrincipal:
    """Offline Privy verifier: maps a fixed token to an owner DID; anything else fails closed."""
    did = _TOKENS.get(token)
    if did is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail="invalid token")
    return PrivyPrincipal(did=did)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _settings() -> Settings:
    return Settings(AUTH_MODE="privy", PRIVY_APP_ID="app", PRIVY_VERIFICATION_KEY="key")


def _instance(instance_id: str, operator_id: str | None) -> AgentInstance:
    """Build a minimal persisted AgentInstance owned by ``operator_id`` (None => UNOWNED)."""
    now = _now()
    return AgentInstance(
        instance_id=instance_id,
        template_id="mm-template",
        agent_id="agent-1",
        submitted_config={},
        effective_config={},
        config_hash="c" * 8,
        policy_hash="p" * 8,
        source_mode="replay",
        execution_mode="paper",
        run_id="run-seed",
        status=DeployStatus.PENDING,
        operator_id=operator_id,
        created_at=now,
        updated_at=now,
    )


# --- run drivers (injected seam; offline, no maker machinery) -----------------


class _Summary:
    """A stand-in for the II-2 ``SessionSummary`` carrying only what the adapter inspects."""

    def __init__(self, terminal_reason: str = "completed") -> None:
        self.terminal_reason = terminal_reason


def _instant_driver_factory():
    """A driver that completes immediately; records every (ctx) it was invoked with."""
    calls: list[RunContext] = []

    async def _driver(ctx: RunContext, stop, sink) -> _Summary:
        calls.append(ctx)
        return _Summary("completed")

    return _driver, calls


def _blocking_driver_factory():
    """A driver that blocks until the StopSignal is set (so the run stays ACTIVE for cancel tests)."""
    started = asyncio.Event()

    async def _driver(ctx: RunContext, stop, sink) -> _Summary:
        started.set()
        await stop.wait()
        return _Summary("stopped")

    return _driver, started


# ==============================================================================
# Store lease — AC-25 (crash-safe single-active-run exclusivity)
# ==============================================================================


async def test_lease_status_values_match_enum() -> None:
    """Drift guard: the store CHECK-constraint tuple equals the LeaseStatus enum order."""
    assert lease_status_values() == ("starting", "active", "released", "failed")


async def test_lease_acquire_is_unique_per_instance() -> None:
    """A second acquire for the same instance is the DuplicateLeaseError exclusivity signal."""
    store = InMemoryStore()
    lease = InstanceLease("inst-1", "ra", "s", "r", LeaseStatus.STARTING, OWNER_A, _now(), _now())
    await store.acquire_instance_lease(lease)
    with pytest.raises(DuplicateLeaseError):
        await store.acquire_instance_lease(
            InstanceLease("inst-1", "ra2", "s2", "r2", LeaseStatus.STARTING, OWNER_A, _now(), _now())
        )


async def test_lease_forward_only_and_idempotent() -> None:
    """STARTING->ACTIVE->RELEASED advances; re-RELEASED is idempotent; rewind/terminal-flip rejected."""
    store = InMemoryStore()
    await store.acquire_instance_lease(
        InstanceLease("inst-1", "ra", "s", "r", LeaseStatus.STARTING, OWNER_A, _now(), _now())
    )
    await store.release_instance_lease("inst-1", LeaseStatus.ACTIVE, updated_at=_now())
    await store.release_instance_lease("inst-1", LeaseStatus.RELEASED, updated_at=_now())
    # idempotent re-release (cancellation-safe finally):
    again = await store.release_instance_lease("inst-1", LeaseStatus.RELEASED, updated_at=_now())
    assert again.status is LeaseStatus.RELEASED
    # rewind rejected:
    with pytest.raises(ValueError):
        await store.release_instance_lease("inst-1", LeaseStatus.ACTIVE, updated_at=_now())
    # terminal flip rejected:
    with pytest.raises(ValueError):
        await store.release_instance_lease("inst-1", LeaseStatus.FAILED, updated_at=_now())


async def test_release_missing_lease_raises_keyerror() -> None:
    store = InMemoryStore()
    with pytest.raises(KeyError):
        await store.release_instance_lease("nope", LeaseStatus.ACTIVE, updated_at=_now())


# --- RED#5: two concurrent starts -> EXACTLY ONE lease + ONE run (InMemory) ----


async def test_red5_concurrent_lease_acquire_exactly_one_inmemory() -> None:
    """RED#5 (InMemory): two concurrent acquires for one instance -> exactly one wins, one raises."""
    store = InMemoryStore()

    async def _try(tag: str):
        return await store.acquire_instance_lease(
            InstanceLease("inst-1", f"ra-{tag}", f"s-{tag}", f"r-{tag}", LeaseStatus.STARTING, OWNER_A, _now(), _now())
        )

    results = await asyncio.gather(_try("a"), _try("b"), return_exceptions=True)
    failures = [r for r in results if isinstance(r, DuplicateLeaseError)]
    successes = [r for r in results if r is None]
    assert len(successes) == 1 and len(failures) == 1
    lease = await store.get_instance_lease("inst-1")
    assert lease is not None  # exactly one lease persisted


@pytest.mark.live_network
async def test_red5_concurrent_lease_acquire_exactly_one_postgres() -> None:
    """RED#5 (Postgres): the UNIQUE(instance_id) claim yields exactly one lease. SKIPs without DATABASE_URL."""
    import os

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("no DATABASE_URL — Postgres lease RED is operator-run, never faked")
    from veridex.store import PostgresStore

    store = PostgresStore(dsn=dsn)
    conn = await store._connect()  # type: ignore[attr-defined]
    try:
        await store.init_db(conn)  # type: ignore[attr-defined]
        await conn.execute("DELETE FROM instance_leases WHERE instance_id = %s", ("inst-pg-1",))
        await conn.commit()
    finally:
        await store._release(conn)  # type: ignore[attr-defined]

    async def _try(tag: str):
        return await store.acquire_instance_lease(
            InstanceLease("inst-pg-1", f"ra-{tag}", f"s-{tag}", f"r-{tag}", LeaseStatus.STARTING, OWNER_A, _now(), _now())
        )

    results = await asyncio.gather(_try("a"), _try("b"), return_exceptions=True)
    assert sum(1 for r in results if r is None) == 1
    assert sum(1 for r in results if isinstance(r, DuplicateLeaseError)) == 1


# --- RED#6: crash after lease insert, before active-handle persist -> no 2nd run


async def test_red6_crash_after_starting_no_second_run() -> None:
    """RED#6: a lease stuck in STARTING (crash) blocks a second start — NEVER a second run."""
    store = InMemoryStore()
    # Simulate a crash: lease acquired in STARTING, but ACTIVE + runtime_handle never persisted.
    await store.acquire_instance_lease(
        InstanceLease("inst-1", "ra", "s", "r", LeaseStatus.STARTING, OWNER_A, _now(), _now())
    )
    lease = await store.get_instance_lease("inst-1")
    assert lease is not None and lease.status is LeaseStatus.STARTING
    # A recovery/duplicate starter cannot mint a second lease/run under the same instance.
    with pytest.raises(DuplicateLeaseError):
        await store.acquire_instance_lease(
            InstanceLease("inst-1", "ra2", "s2", "r2", LeaseStatus.STARTING, OWNER_A, _now(), _now())
        )


# ==============================================================================
# Adapter — AC-26 (zero construct side effects) + AC-16 (owner-first exactly-once cancel)
# ==============================================================================


def test_ac26_construct_has_zero_side_effects() -> None:
    """AC-26 / plan RED: constructing the adapter starts NO run and touches NO external resource."""
    driver, calls = _instant_driver_factory()
    adapter = VeridexAgentAdapter(run_driver=driver)
    assert adapter.db is None  # AgentOS injects the owner db later
    assert adapter._runs == {}  # no run registered
    assert calls == []  # driver never invoked at construct
    assert adapter.get_id() == "veridex-market-maker"


async def test_start_run_emits_terminal_status_changed() -> None:
    """Plan RED (fu-ii2-minors M3): start_run drives the run + emits a terminal STATUS_CHANGED(completed)."""
    driver, calls = _instant_driver_factory()
    adapter = VeridexAgentAdapter(run_driver=driver)
    events: list = []
    result = await adapter.start_run(
        run_id="run-1", session_id="sess-1", runtime_agent_id="ra-1", owner_did=OWNER_A, event_sink=events.append
    )
    assert isinstance(result, _Summary)
    assert len(calls) == 1 and calls[0].run_id == "run-1"
    status_events = [e for e in events if e.type == RuntimeEventType.STATUS_CHANGED.value]
    assert status_events, "expected a terminal STATUS_CHANGED event"
    assert status_events[-1].payload["status"] == RuntimeStatus.COMPLETED.value


async def test_cancel_owner_check_before_any_effect() -> None:
    """AC-16 / RED#2/#3: a non-owner (or None) cancel is refused BEFORE any effect (no stop.set)."""
    driver, started = _blocking_driver_factory()
    adapter = VeridexAgentAdapter(run_driver=driver)
    task = asyncio.create_task(
        adapter.start_run(run_id="run-1", session_id="s", runtime_agent_id="ra", owner_did=OWNER_A)
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    # Non-owner: refused, no effect (run still ACTIVE, not cancelling).
    with pytest.raises(OwnerMismatchError):
        await adapter.acancel_run("run-1", owner_did=OWNER_B)
    # None owner (agno-native path carries no principal): refused.
    with pytest.raises(OwnerMismatchError):
        await adapter.acancel_run("run-1", owner_did=None)
    assert adapter.run_phase("run-1") is RunPhase.ACTIVE  # NO effect from the refused cancels
    # Owner: engages the kill; the blocked run unblocks and settles.
    res = await adapter.acancel_run("run-1", owner_did=OWNER_A)
    assert res.engaged is True
    await asyncio.wait_for(task, timeout=1)
    assert adapter.run_phase("run-1") is RunPhase.CANCELLED


async def test_red7_concurrent_cancels_engage_exactly_once() -> None:
    """RED#7: two concurrent owner cancels engage the kill EXACTLY ONCE; the other returns not-engaged."""
    driver, started = _blocking_driver_factory()
    adapter = VeridexAgentAdapter(run_driver=driver)
    task = asyncio.create_task(
        adapter.start_run(run_id="run-1", session_id="s", runtime_agent_id="ra", owner_did=OWNER_A)
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    results = await asyncio.gather(
        adapter.acancel_run("run-1", owner_did=OWNER_A),
        adapter.acancel_run("run-1", owner_did=OWNER_A),
    )
    engaged = [r for r in results if isinstance(r, CancelResult) and r.engaged]
    not_engaged = [r for r in results if isinstance(r, CancelResult) and not r.engaged]
    assert len(engaged) == 1, "cancel-all must engage exactly once"
    assert len(not_engaged) == 1
    await asyncio.wait_for(task, timeout=1)
    # A repeat cancel after terminal returns the terminal state without re-engaging.
    late = await adapter.acancel_run("run-1", owner_did=OWNER_A)
    assert late.engaged is False and late.phase is RunPhase.CANCELLED


async def test_cancel_unknown_run_raises_keyerror() -> None:
    """An unknown run id fails closed (KeyError -> wrapper 404); no existence leak."""
    driver, _ = _instant_driver_factory()
    adapter = VeridexAgentAdapter(run_driver=driver)
    with pytest.raises(KeyError):
        await adapter.acancel_run("ghost", owner_did=OWNER_A)


async def test_build_market_maker_driver_threads_stop_and_ids() -> None:
    """The production driver factory threads the adapter StopSignal + server ids into run_market_maker."""
    seen: dict = {}

    def _session_factory(ctx: RunContext):
        seen["ctx"] = ctx
        # A fake (instance_cfg, tape, mode, guard_enabled) tuple is fine — run_market_maker is patched.
        return object(), object(), "replay", False

    driver = build_market_maker_driver(_session_factory)
    captured: dict = {}

    async def _fake_rmm(instance_cfg, tape, *, mode, guard_enabled, event_sink, stop):
        captured.update(mode=mode, guard_enabled=guard_enabled, stop=stop)
        return _Summary("completed")

    import veridex.mm_strategy.composition as comp

    orig = comp.run_market_maker
    comp.run_market_maker = _fake_rmm  # type: ignore[assignment]
    try:
        adapter = VeridexAgentAdapter(run_driver=driver)
        await adapter.start_run(run_id="run-1", session_id="s", runtime_agent_id="ra", owner_did=OWNER_A)
    finally:
        comp.run_market_maker = orig  # type: ignore[assignment]
    assert seen["ctx"].run_id == "run-1"
    assert captured["mode"] == "replay" and captured["guard_enabled"] is False
    assert captured["stop"] is not None  # the adapter-owned StopSignal was threaded through


# ==============================================================================
# Composition + AC-27 boundary (the CRITICAL) — via the guarded ASGI app
# ==============================================================================


def _build(store: InMemoryStore, adapter: VeridexAgentAdapter, *, enforce: bool = True, sink=None):
    return svc.build_agentos_app(
        store=store,
        settings=_settings(),
        adapter=adapter,
        owner_db=InMemoryDb(),
        verifier=_fake_verifier,
        event_sink=sink,
        enforce_contract=enforce,
    )


@pytest.fixture
def owned_setup():
    """A store with instance ``inst-A`` owned by OWNER_A, a guarded app, and a TestClient."""
    store = InMemoryStore()
    driver, calls = _instant_driver_factory()
    adapter = VeridexAgentAdapter(run_driver=driver)
    guard = _build(store, adapter)
    client = TestClient(guard)
    return store, adapter, calls, client


async def _persist(store: InMemoryStore, instance_id: str, operator_id: str | None) -> None:
    await store.persist_agent_instance(_instance(instance_id, operator_id))


# --- RED#1: anonymous -> fail closed (401) on every native + wrapper surface ---

_ANON_NATIVE = [
    ("post", "/agents/veridex-market-maker/runs"),
    ("post", "/agents/veridex-market-maker/runs/r1/cancel"),
    ("post", "/agents/veridex-market-maker/runs/r1/continue"),
    ("post", "/agents/veridex-market-maker/sessions/s1/fork"),
    ("get", "/sessions"),
    ("get", "/sessions/s1"),
    ("post", "/sessions"),
    ("delete", "/sessions/s1"),
    ("patch", "/sessions/s1"),
    ("post", "/sessions/s1/rename"),
    ("get", "/agents/veridex-market-maker/runs/r1"),
]


@pytest.mark.parametrize("method,path", _ANON_NATIVE)
def test_red1_anonymous_native_routes_401(owned_setup, method: str, path: str) -> None:
    """RED#1: an anonymous caller hitting ANY agno-native run/cancel/continue/session route -> 401."""
    _store, _adapter, _calls, client = owned_setup
    resp = getattr(client, method)(path)
    assert resp.status_code == 401, (method, path, resp.status_code)


def test_red1_anonymous_wrapper_run_and_cancel_401(owned_setup) -> None:
    """RED#1: anonymous run-start AND cancel on the Veridex wrapper routes -> 401 (II-11 smoke)."""
    _store, _adapter, _calls, client = owned_setup
    assert client.post("/agents/instances/inst-A/runs", json={}).status_code == 401
    assert client.post("/agents/instances/inst-A/runs/r1/cancel").status_code == 401


def test_red1_anonymous_websocket_denied(owned_setup) -> None:
    """RED#1/#9: an anonymous WebSocket to agno's /workflows/ws cannot connect/execute."""
    _store, _adapter, _calls, client = owned_setup
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises((WebSocketDisconnect, Exception)), client.websocket_connect("/workflows/ws"):
        pass


# --- RED#2: authenticated NON-OWNER -> no data + no side effect ---------------


def test_red2_authed_nonowner_native_routes_403(owned_setup) -> None:
    """RED#2: an authenticated non-owner on agno-native routes gets 403 (deny-by-default, no data)."""
    _store, _adapter, _calls, client = owned_setup
    assert client.get("/sessions", headers=_auth("tokenA")).status_code == 403
    assert client.get("/sessions/s1", headers=_auth("tokenA")).status_code == 403
    assert client.post("/agents/veridex-market-maker/runs", headers=_auth("tokenA"), data={"message": "x"}).status_code == 403


def test_red2_authed_nonowner_wrapper_run_hidden(owned_setup) -> None:
    """RED#2/#3: OWNER_B cannot run OWNER_A's instance — 403 (owned by another), no run started.

    Mirrors ``deploy.py:534``: an existing instance owned by ANOTHER principal is 403 (not 404), while
    the no-side-effect guarantee holds — the non-owner drives NO run.
    """
    store, _adapter, calls, client = owned_setup
    asyncio.run(_persist(store, "inst-A", OWNER_A))
    resp = client.post("/agents/instances/inst-A/runs", headers=_auth("tokenB"), json={})
    assert resp.status_code == 403  # owned by another principal -> forbidden (deploy.py:534)
    assert calls == []  # NO run started for the non-owner


def test_red2_unowned_instance_hidden(owned_setup) -> None:
    """RED#2: an UNOWNED (operator_id=None) instance is never inherited — 404 for any caller."""
    store, _adapter, _calls, client = owned_setup
    asyncio.run(_persist(store, "inst-legacy", None))
    assert client.post("/agents/instances/inst-legacy/runs", headers=_auth("tokenA"), json={}).status_code == 404


# --- RED#3: caller-forged ids cannot cross-bind ownership --------------------


def test_red3_owner_from_server_state_not_request_body(owned_setup) -> None:
    """RED#3: the OWNER runs the instance; a forged body payload never changes ownership resolution."""
    store, _adapter, calls, client = owned_setup
    asyncio.run(_persist(store, "inst-A", OWNER_A))
    # A caller-supplied body with forged session/user metadata is ignored for ownership.
    resp = client.post(
        "/agents/instances/inst-A/runs",
        headers=_auth("tokenA"),
        json={"input": {"user_id": OWNER_B, "session_id": "forged", "instance_id": "other"}},
    )
    assert resp.status_code == 200
    body = resp.json()
    # SERVER-pre-allocated ids — never the caller's forged values.
    assert body["run_id"].startswith("run_") and body["session_id"].startswith("sess_")
    assert len(calls) == 1
    # The lease + run were bound to OWNER_A's instance from server state, not the forged body.
    lease = asyncio.run(store.get_instance_lease("inst-A"))
    assert lease is not None and lease.operator_id == OWNER_A


# --- RED#4: trailing-slash + route-shadow cannot bypass the guard ------------


def test_red4_trailing_slash_variants_still_denied(owned_setup) -> None:
    """RED#4: trailing-slash forms of native routes are denied identically (401 anon / 403 authed)."""
    _store, _adapter, _calls, client = owned_setup
    assert client.get("/sessions/").status_code == 401
    assert client.get("/sessions/", headers=_auth("tokenA")).status_code == 403
    assert client.post("/agents/veridex-market-maker/runs/").status_code == 401


def test_red4_wrapper_not_shadowed_by_native_run(owned_setup) -> None:
    """RED#4: POST /agents/instances/{id}/runs resolves to the OWNER-GATED veridex route, not agno."""
    store, _adapter, calls, client = owned_setup
    asyncio.run(_persist(store, "inst-A", OWNER_A))
    # If agno's /agents/{agent_id}/runs shadowed this, "instances" would be treated as agent_id and the
    # owner gate would be bypassed. Instead the veridex handler owner-gates -> 200 for the owner.
    assert client.post("/agents/instances/inst-A/runs", headers=_auth("tokenA"), json={}).status_code == 200
    assert len(calls) == 1


# --- RED#5 (integration): the wrapper never starts a second run for one instance


def test_red5_wrapper_second_start_is_409(owned_setup) -> None:
    """RED#5: with an active lease held, a second wrapper start -> 409, and NO second run is driven."""
    store, _adapter, calls, client = owned_setup
    asyncio.run(_persist(store, "inst-A", OWNER_A))
    # Pre-hold the lease (as if a run is in flight / mid-STARTING after a crash).
    asyncio.run(
        store.acquire_instance_lease(
            InstanceLease("inst-A", "ra", "s", "r", LeaseStatus.STARTING, OWNER_A, _now(), _now())
        )
    )
    resp = client.post("/agents/instances/inst-A/runs", headers=_auth("tokenA"), json={})
    assert resp.status_code == 409
    assert calls == []  # the wrapper never drove a second run


def test_wrapper_persists_runtime_handle(owned_setup) -> None:
    """Plan RED#5: a successful wrapper start persists the instance runtime_handle (agentos ids)."""
    store, _adapter, _calls, client = owned_setup
    asyncio.run(_persist(store, "inst-A", OWNER_A))
    resp = client.post("/agents/instances/inst-A/runs", headers=_auth("tokenA"), json={})
    assert resp.status_code == 200
    instance = asyncio.run(store.get_agent_instance("inst-A"))
    handle = instance.runtime_handle
    assert handle is not None and handle["runtime_kind"] == "agentos"
    assert handle["run_id"] == resp.json()["run_id"]
    # lease settled to RELEASED after the inline run.
    lease = asyncio.run(store.get_instance_lease("inst-A"))
    assert lease is not None and lease.status is LeaseStatus.RELEASED


def test_wrapper_cancel_owner_flow(owned_setup) -> None:
    """RED#1/#2 cancel: anon -> 401, non-owner -> 404, and a bad run_id for the owner -> 404."""
    store, adapter, _calls, client = owned_setup
    asyncio.run(_persist(store, "inst-A", OWNER_A))
    # No lease/run yet -> owner cancel of an unknown run is 404 (coherence check).
    assert client.post("/agents/instances/inst-A/runs/ghost/cancel", headers=_auth("tokenA")).status_code == 404
    # Non-owner cancel of OWNER_A's instance -> 403 (owned by another; no effect).
    assert client.post("/agents/instances/inst-A/runs/ghost/cancel", headers=_auth("tokenB")).status_code == 403
    # Anonymous cancel -> 401.
    assert client.post("/agents/instances/inst-A/runs/ghost/cancel").status_code == 401


# ==============================================================================
# AC-29 — deployed-surface contract (fail-closed on drift)
# ==============================================================================


def test_ac29_adapter_and_agentos_signature_contracts() -> None:
    """AC-29: the adapter hooks + AgentOS('preserve_base_app') signature contracts hold."""
    driver, _ = _instant_driver_factory()
    adapter = VeridexAgentAdapter(run_driver=driver)
    svc.assert_adapter_contract(adapter)  # no raise
    svc.assert_agentos_contract()  # no raise


def test_ac29_full_composition_passes_contract() -> None:
    """AC-29: composing the real deployed surface passes the drift contract (enforce_contract=True)."""
    store = InMemoryStore()
    driver, _ = _instant_driver_factory()
    adapter = VeridexAgentAdapter(run_driver=driver)
    guard = _build(store, adapter, enforce=True)  # would raise on drift
    assert isinstance(guard, svc.DenyByDefaultGuard)  # the guard is the OUTERMOST app


def test_red8_synthetic_unknown_agno_route_fails_contract() -> None:
    """RED#8: an unclassified NEW agno-native route in the composed table FAILS the AC-29 contract."""
    app = FastAPI()
    app.add_api_route("/agents/{agent_id}/runs", lambda: None, methods=["POST"])  # a known native route
    app.add_api_route("/agents/instances/{instance_id}/runs", lambda: None, methods=["POST"])  # veridex wrapper
    veridex_templates = {("POST", "/agents/instances/{instance_id}/runs")}
    # Add every REQUIRED native route so only the synthetic one is the drift.
    for method, path in svc.REQUIRED_AGNO_NATIVE_ROUTES:
        if method == "WEBSOCKET":
            continue
        if (method, path) != ("POST", "/agents/{agent_id}/runs"):
            app.add_api_route(path, lambda: None, methods=[method])
    app.add_api_websocket_route("/workflows/ws", lambda ws: None)
    # Baseline: without the synthetic route the known set is a superset -> passes only if all known.
    # Now inject a synthetic UNKNOWN agno-native route:
    app.add_api_route("/agents/{agent_id}/evil-backdoor", lambda: None, methods=["POST"])
    with pytest.raises(svc.AgentOSCompositionError):
        svc.assert_agno_surface(app, veridex_templates)


def test_red8_missing_required_route_fails_contract() -> None:
    """AC-29: if a REQUIRED agno-native route disappears, the contract fails deploy."""
    app = FastAPI()
    app.add_api_route("/agents/instances/{instance_id}/runs", lambda: None, methods=["POST"])
    veridex_templates = {("POST", "/agents/instances/{instance_id}/runs")}
    # No agno-native routes at all -> required routes missing.
    with pytest.raises(svc.AgentOSCompositionError):
        svc.assert_agno_surface(app, veridex_templates)


def test_composed_inventory_covers_known_native_surface() -> None:
    """AC-29: the live composed agno-native surface equals the pinned reviewed inventory (no drift)."""
    store = InMemoryStore()
    driver, _ = _instant_driver_factory()
    adapter = VeridexAgentAdapter(run_driver=driver)
    guard = _build(store, adapter, enforce=False)
    composed = guard.app
    # Rebuild veridex_templates the same way build_agentos_app does.
    veridex_app = svc.create_app(store=InMemoryStore(), settings=_settings())
    svc._register_wrapper_routes(
        veridex_app, store=InMemoryStore(), adapter=adapter, require_principal=lambda **k: PrivyPrincipal(did="x"), event_sink=None
    )
    veridex_templates = set(svc._route_table(veridex_app))
    native = svc.agno_native_routes(composed, veridex_templates)
    assert native == set(svc._KNOWN_AGNO_NATIVE_ROUTES)
    assert native >= svc.REQUIRED_AGNO_NATIVE_ROUTES
