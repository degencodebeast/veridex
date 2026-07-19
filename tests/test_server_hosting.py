"""II-5f — the SERVED app composes AgentOS behind deny-by-default (hosting-composition TDD).

II-8b built the AgentOS adapters + ``build_agentos_app`` but the served ``create_server_app`` did NOT
compose it — hosting was proven ONLY in the test harness. II-5f closes that gap: the SERVED app must
RETURN THE GUARD (a :class:`~veridex.runtime.agentos_service.DenyByDefaultGuard`) hosting the AgentOS
surface, with the native surface byte-identical and ``/readyz`` still public.

Trust boundary (SURFACE HOSTING — Approach A): the served composition hosts the AgentOS surface behind
deny-by-default; every agno-native route stays DENIED (401 anon / 403 authed). Functional agent
EXECUTION remains authority-bound via the per-instance deploy path (``deploy.py``), NOT the hosted
wrapper route — so these tests assert the surface is REAL + auth-gated, never a fabricated run.

OFFLINE: ``agno`` is imported, the AgentOS db is agno's ``InMemoryDb``, the store is ``InMemoryStore``
(the no-``DATABASE_URL`` local-dev path). Auth is exercised with the offline ``_fake_verifier`` seam
(no live Privy creds) and an injected ``settings`` (AUTH_MODE=privy) so the guard fail-closes on anon.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from veridex.api import server
from veridex.api.auth_privy import PrivyPrincipal
from veridex.api.readiness import build_readiness_router
from veridex.config import Settings
from veridex.runtime import agentos_service as svc
from veridex.runtime.mm_agent_adapter import VeridexAgentAdapter
from veridex.store import InMemoryStore

# --- identities / harness auth (mirror II-4/II-8b; offline, no live creds) -----

OWNER_A = "did:privy:ownerA"
_TOKENS = {"tokenA": OWNER_A}

#: The pinned agent ids the served composition hosts (II-8b's composed set: MM + the two directional).
_HOSTED_IDS = {"veridex-market-maker", "veridex-cumulative-drift", "veridex-llm-drift"}

#: The InMemory local-dev serving env (no DATABASE_URL); CORS is required to build (fail-closed).
_ENV = {"CORS_ORIGINS": "https://app.example.test"}


def _fake_verifier(token: str, *, app_id: str | None, verification_key: str | None) -> PrivyPrincipal:
    """Offline Privy verifier: a fixed token maps to an owner DID; anything else fails closed (401)."""
    did = _TOKENS.get(token)
    if did is None:
        raise HTTPException(status_code=401, detail="invalid token")
    return PrivyPrincipal(did=did)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _settings() -> Settings:
    """Auth-enforcing settings so the deny-by-default guard 401s an anonymous caller (not dev-bypass)."""
    return Settings(AUTH_MODE="privy", PRIVY_APP_ID="app", PRIVY_VERIFICATION_KEY="key")


def _served() -> tuple[svc.DenyByDefaultGuard, TestClient]:
    """Build the SERVED app (the guard) + a TestClient, on the offline InMemory path."""
    guard = server.create_server_app(env=_ENV, settings=_settings(), verifier=_fake_verifier)
    return guard, TestClient(guard)


# --- RED#1: deny-by-default preserved on the SERVED app (proves the GUARD is returned) ---

_ANON_NATIVE = [
    ("post", "/agents/veridex-market-maker/runs"),
    ("post", "/agents/veridex-market-maker/runs/r1/cancel"),
    ("get", "/sessions"),
    ("post", "/sessions"),
    ("get", "/sessions/s1"),
]


@pytest.mark.parametrize("method,path", _ANON_NATIVE)
def test_red1_served_anon_native_routes_401(method: str, path: str) -> None:
    """RED#1: an anonymous caller hitting an agno-native route on the SERVED app -> 401.

    Proves ``create_server_app`` returns the ``DenyByDefaultGuard``, not the inner FastAPI (which had
    no such route -> 404). Mirrors ``test_agentos_adapter.py`` RED#1 on the served composition.
    """
    _guard, client = _served()
    assert getattr(client, method)(path).status_code == 401, (method, path)


def test_red1_served_returns_the_guard_not_inner_fastapi() -> None:
    """RED#1: the served app IS the deny-by-default guard (its ``.app`` is the composed FastAPI)."""
    guard, _client = _served()
    assert isinstance(guard, svc.DenyByDefaultGuard)
    from fastapi import FastAPI

    assert isinstance(guard.app, FastAPI)


# --- RED#2: /readyz is NOT denied by the guard (public readiness preserved) ---


def test_red2_served_readyz_never_denied() -> None:
    """RED#2: ``/readyz`` on the served app returns 200 or 503, NEVER 401/403.

    Guards recon-risk #1: readiness mounted AFTER the veridex matcher snapshot would be treated as
    agno-native and denied. The fix registers readiness into the base app BEFORE composition, so it is
    veridex-owned/self-gated and stays public (200/503) while everything else stays deny-by-default.
    """
    _guard, client = _served()
    status = client.get("/readyz").status_code
    # 200/503 is the whole allowed set — it already excludes the guard's 401/403 denials.
    assert status in (200, 503), status


# --- RED#3: native surface byte-identical + no template leakage --------------


def test_red3_served_native_surface_matches_known_and_readyz_not_leaked() -> None:
    """RED#3: the served native surface == ``_KNOWN_AGNO_NATIVE_ROUTES``; extras add no templates.

    Reconstructs the veridex-owned template set the way ``build_agentos_app`` snapshots it (base app +
    wrapper routes + the readiness base-router), then subtracts it from the composed surface — the
    remainder must equal the pinned agno-native surface EXACTLY (the two directional extras are
    templated by ``{agent_id}`` and add NO new route templates), and ``/readyz`` must NOT leak into it.
    """
    guard, _client = _served()
    composed = guard.app

    veridex_app = svc.create_app(store=InMemoryStore(), settings=_settings())
    svc._register_wrapper_routes(
        veridex_app,
        store=InMemoryStore(),
        adapter=VeridexAgentAdapter(run_driver=_noop_driver, id="veridex-market-maker"),
        require_principal=lambda **_k: PrivyPrincipal(did="x"),
        event_sink=None,
    )
    veridex_app.include_router(build_readiness_router(get_pool=lambda: None, pack_root=""))
    veridex_templates = set(svc._route_table(veridex_app))

    native = svc.agno_native_routes(composed, veridex_templates)
    assert native == set(svc._KNOWN_AGNO_NATIVE_ROUTES)
    assert ("GET", "/readyz") not in native  # readiness is veridex-owned, never agno-native


async def _noop_driver(ctx, stop, event_sink):  # pragma: no cover - only a template placeholder
    """A no-op driver used ONLY to reconstruct the wrapper-route templates for the surface diff."""
    return None


# ==============================================================================
# Codex SECURITY gate (II-5f) — Majors 1-3 REDs
# ==============================================================================

# --- MAJOR 1: base_routers is a CONSTRAINED public allowlist, not an unchecked bypass ---


def _mm_adapter() -> VeridexAgentAdapter:
    """A hosting adapter for the primary MM slot (its driver is never driven in these tests)."""
    return VeridexAgentAdapter(run_driver=_noop_driver, id="veridex-market-maker")


def test_m1_hostile_base_router_fails_startup() -> None:
    """MAJOR 1 RED: a base_router exposing an unauthenticated non-allowlisted route FAILS STARTUP.

    Codex passed an ``APIRouter`` with an unauthenticated ``GET /private-owner-data`` via
    ``base_routers`` and an anonymous request through the guard got 200 + payload (the route was
    captured in the veridex matcher snapshot, so it bypassed deny-by-default). The fix constrains
    ``base_routers`` to a hardcoded public allowlist and RAISES at composition on anything else — so
    the hostile route is never served (rejected at startup, it can never reach 200).
    """
    from agno.db.in_memory import InMemoryDb
    from fastapi import APIRouter

    hostile = APIRouter()

    @hostile.get("/private-owner-data")
    async def _leak() -> dict[str, str]:  # pragma: no cover - must never be reachable/served
        return {"secret": "owner-only-data"}

    with pytest.raises(svc.AgentOSCompositionError):
        svc.build_agentos_app(
            store=InMemoryStore(),
            settings=_settings(),
            adapter=_mm_adapter(),
            owner_db=InMemoryDb(),
            verifier=_fake_verifier,
            base_routers=[hostile],
        )


def test_m1_readyz_only_base_router_composes_and_is_public() -> None:
    """MAJOR 1 RED: a base_router carrying ONLY the allowlisted ``GET /readyz`` composes fine + public."""
    from agno.db.in_memory import InMemoryDb

    guard = svc.build_agentos_app(
        store=InMemoryStore(),
        settings=_settings(),
        adapter=_mm_adapter(),
        owner_db=InMemoryDb(),
        verifier=_fake_verifier,
        base_routers=[build_readiness_router(get_pool=lambda: None, pack_root="")],
    )
    assert TestClient(guard).get("/readyz").status_code in (200, 503)


def test_m1_public_base_route_allowlist_is_exactly_readyz() -> None:
    """MAJOR 1 RED: the enforced allowlist is exactly the no-auth ``GET /readyz`` (hardcoded)."""
    assert frozenset({("GET", "/readyz")}) == svc._ALLOWED_PUBLIC_BASE_ROUTES


# --- MAJOR 2: the SERVED wrapper denies BEFORE any durable mutation (no instance corruption) ---


def _persist_owned_instance(store: InMemoryStore) -> None:
    """Persist an owned instance whose authoritative run identity is ``run-authoritative``."""
    import asyncio
    from datetime import UTC, datetime

    from veridex.deploy.instance import AgentInstance, DeployStatus

    now = datetime.now(tz=UTC).isoformat()
    instance = AgentInstance(
        instance_id="inst-A",
        template_id="mm-template",
        agent_id="agent-1",
        submitted_config={},
        effective_config={},
        config_hash="c" * 8,
        policy_hash="p" * 8,
        source_mode="replay",
        execution_mode="paper",
        run_id="run-authoritative",
        status=DeployStatus.PENDING,
        operator_id=OWNER_A,
        runtime_handle={"runtime_kind": "seed", "run_id": "run-authoritative"},
        created_at=now,
        updated_at=now,
    )
    asyncio.run(store.persist_agent_instance(instance))


def test_m2_served_wrapper_denies_without_corrupting_the_instance() -> None:
    """MAJOR 2 RED: authed owner POST to the served wrapper -> clean deny, ZERO durable mutation.

    Codex reproduced: the served surface-only composition exposed a mutating owner run wrapper; an
    authed owner's ``POST /agents/instances/inst-A/runs`` minted a fresh run_id, acquired the lease
    and overwrote ``runtime_handle`` BEFORE the surface-only driver failed -> 500, corrupted
    ``runtime_handle.run_id``, lease FAILED under the new id, retry 409. The fix denies at the served
    wrapper BEFORE any lease/runtime_handle mutation, so the instance is untouched and a subsequent
    legitimate run via the real path still works.
    """
    import asyncio
    import warnings

    guard, client = _served()
    store: InMemoryStore = guard.app.state.store
    _persist_owned_instance(store)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        resp = client.post("/agents/instances/inst-A/runs", headers=_auth("tokenA"), json={})
        import gc

        gc.collect()

    # (a) a CLEAN deny (never a 500-with-corruption).
    assert resp.status_code != 500, resp.status_code
    assert resp.status_code in (403, 409), resp.status_code

    instance = asyncio.run(store.get_agent_instance("inst-A"))
    # (b) runtime_handle is UNCHANGED (no durable identity corruption).
    assert instance.runtime_handle == {"runtime_kind": "seed", "run_id": "run-authoritative"}
    assert instance.run_id == "run-authoritative"
    # (c) the one-run lease was NOT consumed/created.
    assert asyncio.run(store.get_instance_lease("inst-A")) is None
    # (d) NO coroutine-never-awaited warning.
    assert not any(
        issubclass(w.category, RuntimeWarning) and "never awaited" in str(w.message) for w in caught
    ), [str(w.message) for w in caught]

    # (e) a subsequent LEGITIMATE run via the real shared service still works.
    result_ok = asyncio.run(
        svc.start_owned_instance_run(
            store,
            _mm_adapter(),
            instance=instance,
            run_id="run-legit",
            session_id="sess-legit",
        )
    )
    assert result_ok is None  # the noop driver completed
    lease = asyncio.run(store.get_instance_lease("inst-A"))
    assert lease is not None and lease.run_id == "run-legit"
    refreshed = asyncio.run(store.get_agent_instance("inst-A"))
    assert refreshed.runtime_handle["run_id"] == "run-legit"


def test_m2_served_mm_session_factory_is_sync_fail_closed() -> None:
    """MAJOR 2 RED: the served MM session factory is SYNC (matches ``build_market_maker_driver``).

    ``build_market_maker_driver`` calls ``session_factory(ctx)`` synchronously; an ``async`` factory
    returns an un-awaited coroutine (the ``coroutine ... never awaited`` warning Codex saw). The
    served factory must be a plain sync callable that fails CLOSED.
    """
    import inspect

    assert not inspect.iscoroutinefunction(server._served_mm_not_executor)
    with pytest.raises(RuntimeError):
        server._served_mm_not_executor(object())


# --- MAJOR 3: /readyz honestly reflects the AgentOS DB (no false session_db:true for ephemeral) ---


def _always(value: bool):
    """An async readiness probe that always returns ``value`` (test seam)."""

    async def _probe() -> bool:
        return value

    return _probe


async def test_m3_agentos_session_db_probe_reflects_true_durability() -> None:
    """MAJOR 3 RED: the AgentOS session-DB probe is False for an ephemeral (in-memory) agno DB."""
    from agno.db.in_memory import InMemoryDb

    from veridex.api.readiness import make_agentos_session_db_probe

    assert await make_agentos_session_db_probe(lambda: InMemoryDb())() is False
    assert await make_agentos_session_db_probe(lambda: None)() is False

    class _DurableDb:  # a non-in-memory (durable) AgentOS DB stand-in
        pass

    assert await make_agentos_session_db_probe(lambda: _DurableDb())() is True


def test_m3_readyz_not_falsely_healthy_for_ephemeral_agentos_db() -> None:
    """MAJOR 3 RED: with Postgres+packs up but an in-memory AgentOS DB, /readyz is NOT healthy.

    Simulates a restart/empty-agno-DB: even when the other durable deps are up, an ephemeral AgentOS
    session DB must make ``session_db`` False and ``/readyz`` 503 — never a false ``session_db: true``.
    """
    from agno.db.in_memory import InMemoryDb
    from fastapi import FastAPI

    router = build_readiness_router(
        postgres_probe=_always(True),
        pack_probe=_always(True),
        get_agentos_db=lambda: InMemoryDb(),
    )
    app = FastAPI()
    app.include_router(router)
    resp = TestClient(app).get("/readyz")
    assert resp.status_code == 503, resp.status_code
    assert resp.json()["checks"]["session_db"] is False


def test_m3_served_app_wires_agentos_db_into_readiness() -> None:
    """MAJOR 3 RED: the served app's /readyz reflects its (ephemeral) AgentOS DB, never falsely true."""
    _guard, client = _served()
    body = client.get("/readyz").json()
    # The served composition uses an in-memory AgentOS DB (durable agno DB is a tracked residual);
    # /readyz must NOT advertise session_db durable for it.
    assert body["checks"]["session_db"] is False


# --- RED#4: AgentOS surface actually hosted (authenticated; NO fabricated run) ---


def test_red4_served_hosts_the_registered_agents() -> None:
    """RED#4: the composed served app actually hosts the REGISTERED adapters (not an empty surface).

    Read agno's own ``GET /agents`` listing on the composed app DIRECTLY (bypassing the guard) — it
    must list the pinned hosted ids, proving agno composed the REAL adapters. This is the "hosting is
    real" proof, distinct from RED#1's anon-deny; it fabricates NO run.
    """
    guard, _client = _served()
    inner = TestClient(guard.app)  # bypass the deny guard to read agno's registry
    resp = inner.get("/agents")
    assert resp.status_code == 200, resp.status_code
    ids = {entry.get("id") for entry in resp.json()}
    assert ids >= _HOSTED_IDS, ids


def test_red4_served_native_run_route_hosted_and_gated() -> None:
    """RED#4: the agno-native agent-run route is genuinely HOSTED on the composed app, and gated.

    Route existence is proven DIRECTLY against the composed inner app's route table — the guard's 403
    alone does NOT prove existence (it denies ANY authenticated non-veridex path before dispatch, so a
    nonexistent id would 403 too). The through-guard 403 then shows the hosted run surface is auth-gated
    (deny-by-default), never public. No run is driven.
    """
    guard, client = _served()
    # (a) the agno-native run route is genuinely hosted on the composed app (not an empty surface).
    assert ("POST", "/agents/{agent_id}/runs") in set(svc._route_table(guard.app))
    # (b) reaching it THROUGH the guard is auth-gated (deny-by-default), never public.
    resp = client.post("/agents/veridex-market-maker/runs", headers=_auth("tokenA"))
    assert resp.status_code == 403, resp.status_code
