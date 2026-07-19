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

    # (a) a CLEAN, EXACT deny — the surface-only host refuses with 409 before any mutation (never 500).
    assert resp.status_code == 409, resp.status_code

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


def test_m2_served_wrapper_cancel_denies_without_corrupting_the_instance() -> None:
    """MAJOR 2 (cancel arm): authed owner POST to the served CANCEL wrapper -> clean 409, ZERO mutation.

    Symmetric to the start-route test: the surface-only served host registers the cancel wrapper (so the
    AC-29 native surface stays byte-identical) but must DENY before any effect — the deny-before-mutation
    contract has to hold on BOTH mutating wrapper routes, not just the start route. Proves a served cancel
    leaves the durable instance identity + lease untouched (no 500, no partial mutation).
    """
    import asyncio
    import warnings

    guard, client = _served()
    store: InMemoryStore = guard.app.state.store
    _persist_owned_instance(store)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        resp = client.post(
            "/agents/instances/inst-A/runs/run-authoritative/cancel", headers=_auth("tokenA"), json={}
        )
        import gc

        gc.collect()

    # (a) a CLEAN, EXACT deny — the surface-only host refuses with 409 before any effect (never 500).
    assert resp.status_code == 409, resp.status_code

    instance = asyncio.run(store.get_agent_instance("inst-A"))
    # (b) runtime_handle + run_id are UNCHANGED (no durable identity corruption).
    assert instance.runtime_handle == {"runtime_kind": "seed", "run_id": "run-authoritative"}
    assert instance.run_id == "run-authoritative"
    # (c) no lease was consumed/created/FAILED by the denied cancel.
    assert asyncio.run(store.get_instance_lease("inst-A")) is None
    # (d) NO coroutine-never-awaited warning (the deny is a plain HTTPException, nothing left un-awaited).
    assert not any(
        issubclass(w.category, RuntimeWarning) and "never awaited" in str(w.message) for w in caught
    ), [str(w.message) for w in caught]


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


# --- MAJOR 3 (Codex ADJUDICATED Option A): /readyz gates ONLY the durable Veridex dependencies of the
#     surface-only served mode (Postgres + runtime-event/OPS spool + verified ReplayPack catalog). The
#     ephemeral AgentOS in-memory store is DISCLOSED as NON-GATING information — never a false durability
#     claim and never a 503 that would break the deploy healthcheck. This REVISES the honest-minimum M3
#     approach (which made the AgentOS store gate readiness and 503 on Postgres). ---


def _always(value: bool):
    """An async readiness probe that always returns ``value`` (test seam)."""

    async def _probe() -> bool:
        return value

    return _probe


#: The exact non-gating disclosure the served (surface-only, in-memory AgentOS) app must publish.
_AGENTOS_INMEMORY_DISCLOSURE = {
    "backend": "in_memory",
    "durable": False,
    "required_for_ready": False,
    "mode": "surface_only",
}

#: The gating conjunction is EXACTLY the three durable Veridex deps — never the AgentOS store.
_DURABLE_GATE_KEYS = {"postgres", "runtime_event_spool", "replay_pack_catalog"}


async def test_m3_agentos_session_db_probe_reflects_true_durability() -> None:
    """The AgentOS durability classifier is False for an ephemeral (in-memory) agno DB, True for durable.

    Retained from the surface-only fix: this classifier feeds the non-gating disclosure AND the
    fail-closed executor-mode gate (surface_only=False) — it is NOT part of the surface-only conjunction.
    """
    from agno.db.in_memory import InMemoryDb

    from veridex.api.readiness import make_agentos_session_db_probe

    assert await make_agentos_session_db_probe(lambda: InMemoryDb())() is False
    assert await make_agentos_session_db_probe(lambda: None)() is False

    class _DurableDb:  # a non-in-memory (durable) AgentOS DB stand-in
        pass

    assert await make_agentos_session_db_probe(lambda: _DurableDb())() is True


def _readyz(**kwargs):
    """Build a standalone /readyz app from ``build_readiness_router(**kwargs)`` and GET it once."""
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(build_readiness_router(**kwargs))
    return TestClient(app).get("/readyz")


def test_m3_red_a_readyz_ready_with_inmemory_agentos_when_durable_deps_up() -> None:
    """RED (a): PG path + in-memory AgentOS (surface_only) + durable deps up -> /readyz 200 ready:true.

    The ephemeral AgentOS store must NOT drag readiness to 503: the top-level conjunction is ONLY the
    durable Veridex deps. This is the behavior the deploy healthcheck depends on (Postgres up == ready).
    """
    from agno.db.in_memory import InMemoryDb

    resp = _readyz(
        postgres_probe=_always(True),
        runtime_event_spool_probe=_always(True),
        pack_probe=_always(True),
        get_agentos_db=lambda: InMemoryDb(),
        surface_only=True,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ready"] is True
    # gate is EXACTLY the three durable Veridex deps — the agentos store is not in the conjunction.
    assert set(body["checks"]) == _DURABLE_GATE_KEYS


def test_m3_red_b_agentos_session_store_is_nongating_disclosure() -> None:
    """RED (b): /readyz carries the exact non-gating ``agentos_session_store`` disclosure block."""
    from agno.db.in_memory import InMemoryDb

    resp = _readyz(
        postgres_probe=_always(True),
        runtime_event_spool_probe=_always(True),
        pack_probe=_always(True),
        get_agentos_db=lambda: InMemoryDb(),
        surface_only=True,
    )
    body = resp.json()
    assert body["agentos_session_store"] == _AGENTOS_INMEMORY_DISCLOSURE
    # It must NOT participate in the readiness conjunction (never a key in checks).
    assert "agentos_session_store" not in body["checks"]
    assert body["ready"] is True  # its in_memory status did not affect readiness


def test_m3_red_c_durable_dep_down_still_503() -> None:
    """RED (c): a durable Veridex dep DOWN still makes /readyz 503 (the gate still works for real deps)."""
    from agno.db.in_memory import InMemoryDb

    resp = _readyz(
        postgres_probe=_always(False),  # a real durable dep is down
        runtime_event_spool_probe=_always(True),
        pack_probe=_always(True),
        get_agentos_db=lambda: InMemoryDb(),
        surface_only=True,
    )
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["ready"] is False
    assert body["checks"]["postgres"] is False
    # the disclosure is still present and honest even while not-ready for a real reason.
    assert body["agentos_session_store"] == _AGENTOS_INMEMORY_DISCLOSURE


def test_m3_red_d_startup_rejects_inmemory_agentos_when_not_surface_only() -> None:
    """RED (d): surface_only=False + in-memory AgentOS DB -> the served STARTUP rejects (fail-closed).

    The Option-A exception (ephemeral AgentOS store) may exist ONLY while the served app is surface-only.
    If a future capability flip disables surface_only (executor mode / native run+session routes), startup
    MUST reject an in-memory AgentOS DB until a durable backend is configured — proving the exception
    cannot silently survive the capability change.
    """
    with pytest.raises(RuntimeError, match="durable"):
        server.create_server_app(
            env=_ENV, settings=_settings(), verifier=_fake_verifier, surface_only=False
        )
    # the deployed default (surface_only=True) still composes fine on the same in-memory path.
    guard = server.create_server_app(env=_ENV, settings=_settings(), verifier=_fake_verifier)
    assert isinstance(guard, svc.DenyByDefaultGuard)


def test_m3_red_d_readiness_gates_agentos_when_not_surface_only() -> None:
    """RED (d, readiness arm): with surface_only=False the AgentOS DB participates in the gate.

    Belt-and-suspenders to the startup guard: a readiness router built in executor mode over an
    ephemeral store drags /readyz to 503 (never a false ready:true), and the disclosure reports it as
    required_for_ready in ``executor`` mode.
    """
    from agno.db.in_memory import InMemoryDb

    resp = _readyz(
        postgres_probe=_always(True),
        runtime_event_spool_probe=_always(True),
        pack_probe=_always(True),
        get_agentos_db=lambda: InMemoryDb(),
        surface_only=False,
    )
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["ready"] is False
    assert body["checks"]["agentos_session_store"] is False  # now a GATING check
    assert body["agentos_session_store"]["required_for_ready"] is True
    assert body["agentos_session_store"]["mode"] == "executor"


def test_m3_red_e_runtime_event_spool_gates_and_no_stale_session_db_key() -> None:
    """RED (e): the renamed ``runtime_event_spool`` probe still gates; no consumer sees a stale key."""
    from agno.db.in_memory import InMemoryDb

    resp = _readyz(
        postgres_probe=_always(True),
        runtime_event_spool_probe=_always(False),  # the renamed durable spool probe is down
        pack_probe=_always(True),
        get_agentos_db=lambda: InMemoryDb(),
        surface_only=True,
    )
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["checks"]["runtime_event_spool"] is False
    assert "session_db" not in body["checks"]  # the misnomer is gone everywhere


def test_m3_served_app_readyz_discloses_agentos_store() -> None:
    """The served app's /readyz carries the non-gating ``agentos_session_store`` disclosure block.

    The offline served path has no Postgres, so ready is False for the durable deps — but the agentos
    store is disclosed honestly (in-memory, non-gating) rather than dragging the gate as a false claim.
    """
    _guard, client = _served()
    body = client.get("/readyz").json()
    assert body["agentos_session_store"] == _AGENTOS_INMEMORY_DISCLOSURE
    assert "agentos_session_store" not in body["checks"]
    assert set(body["checks"]) == _DURABLE_GATE_KEYS


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


# ==============================================================================
# Codex SECURITY gate (R-2) — MAJOR 3: /readyz must probe the AUTHORITATIVE R-2
# catalog, not the weaker legacy scanner. Codex reproduced a hash-valid manifest
# with ``fixture_id: true`` (bool): the R-2 catalog EXCLUDES it (``type(fid) is
# int``) yet the legacy scanner's ``isinstance(fixture_id, int)`` ADMITS it
# ({'catalog_ids': [], 'ready_probe': True}). Readiness must consult the catalog.
# ==============================================================================


def _bool_fixture_curated_root(tmp_path):
    """Build a curated root holding one hash-VALID pack whose fixture_id is a JSON bool (``true``).

    Re-seals ``content_hash`` after flipping the fixture id to a bool, so the pack VERIFIES — the only
    reason to exclude it is the R-2 honesty gate (``bool`` is not a valid fixture id). This reproduces
    Codex's exact PoC input.
    """
    import json
    from pathlib import Path

    from veridex.ingest.capture_chain import synthetic_authority
    from veridex.ingest.recorder import SessionMeta, envelope_line
    from veridex.ingest.replay_pack import _compute_content_hash, pack_from_session

    curated = tmp_path / "curated"
    curated.mkdir()
    session = tmp_path / "_session_bool"
    session.mkdir()
    rec = {
        "FixtureId": 7,
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
    pack_dir = curated / "boolpack"
    pack_from_session(session, pack_dir, authority=synthetic_authority())

    manifest = json.loads((pack_dir / "pack.json").read_text())
    manifest["fixtures"][0]["fixture_id"] = True  # a JSON bool — bool subclasses int
    manifest["content_hash"] = _compute_content_hash(
        pack_dir,
        manifest["fixtures"],
        pack_version=int(manifest["pack_version"]),
        capture=manifest["capture"],
    )
    (pack_dir / "pack.json").write_text(json.dumps(manifest))
    return Path(curated)


async def _true_probe() -> bool:
    return True


def test_m3_readyz_probes_authoritative_r2_catalog_not_legacy_scanner(tmp_path) -> None:
    """MAJOR 3 RED: a bool-``fixture_id`` pack that the LEGACY scanner would admit but R-2 EXCLUDES must
    make ``/readyz`` report NOT ready (503), because readiness now probes the authoritative R-2 catalog.

    Other gates (postgres, spool) are forced ready so ONLY the catalog gate decides the outcome.
    """
    from fastapi import FastAPI

    from veridex.api.readiness import make_replay_pack_probe
    from veridex.ingest.replay_catalog import build_catalog

    curated = _bool_fixture_curated_root(tmp_path)

    # The authoritative R-2 catalog correctly EXCLUDES the bool-fixture pack -> it is empty.
    catalog = build_catalog(str(curated))
    assert len(catalog) == 0

    # Contrast: the LEGACY filesystem scanner ADMITS it (isinstance(fixture_id, int) admits bool) —
    # this is exactly the weaker validator Codex exploited to get ready_probe: True.
    import asyncio

    legacy_ready = asyncio.run(make_replay_pack_probe(str(curated))())
    assert legacy_ready is True  # the weaker scanner would (wrongly) advertise ready

    # The authoritative wiring: /readyz consults the catalog -> empty -> 503 (fail-closed).
    router = build_readiness_router(
        get_catalog=lambda: catalog,
        postgres_probe=_true_probe,
        runtime_event_spool_probe=_true_probe,
    )
    app = FastAPI()
    app.include_router(router)
    resp = TestClient(app).get("/readyz")
    assert resp.status_code == 503, resp.status_code
    body = resp.json()
    assert body["ready"] is False
    assert body["checks"]["replay_pack_catalog"] is False


def test_m3_readyz_ready_when_catalog_has_a_loadable_admitted_pack(tmp_path) -> None:
    """MAJOR 3 control: a catalog holding a genuinely loadable, admitted-hash pack passes the probe
    (200) once the other gates pass — proving the authoritative probe is not vacuously always-false."""
    import shutil
    from pathlib import Path

    from fastapi import FastAPI

    from veridex.ingest.replay_catalog import build_catalog

    real_src = Path(__file__).resolve().parents[1] / "scripts" / "fixtures" / "demo_pack_real"
    curated = tmp_path / "curated"
    shutil.copytree(real_src, curated / "real")
    catalog = build_catalog(str(curated))
    assert catalog.get("real") is not None

    router = build_readiness_router(
        get_catalog=lambda: catalog,
        postgres_probe=_true_probe,
        runtime_event_spool_probe=_true_probe,
    )
    app = FastAPI()
    app.include_router(router)
    resp = TestClient(app).get("/readyz")
    assert resp.status_code == 200, resp.status_code
    assert resp.json()["checks"]["replay_pack_catalog"] is True


# --- R-3 fold: the ONCE-built catalog is threaded through (no env-built throwaway) ---


def test_build_agentos_app_threads_provided_catalog(monkeypatch) -> None:
    """The served composition builds the R-2 catalog ONCE and threads it through.

    ``build_agentos_app(replay_catalog=X)`` must expose the SAME object on the composed
    ``app.state.replay_catalog`` — NOT a second, env-built catalog. Identity (``is``) proves the
    provided catalog was used verbatim and the ``os.environ`` build was skipped (even with a bogus
    ``REPLAY_PACK_ROOT`` set, which an env-build would have scanned).
    """
    from agno.db.in_memory import InMemoryDb

    from veridex.ingest.replay_catalog import build_catalog

    # A bogus env root: were the served path to env-build, it would scan THIS (and get a different
    # object). The provided catalog must win — env is never consulted.
    monkeypatch.setenv("REPLAY_PACK_ROOT", "/nonexistent/should-never-be-scanned")
    provided = build_catalog("")  # a distinct, empty ReplayCatalog instance (identity sentinel)

    guard = svc.build_agentos_app(
        store=InMemoryStore(),
        settings=_settings(),
        adapter=_mm_adapter(),
        owner_db=InMemoryDb(),
        verifier=_fake_verifier,
        replay_catalog=provided,
    )
    assert guard.app.state.replay_catalog is provided


def test_create_app_uses_provided_catalog_and_skips_env(monkeypatch) -> None:
    """``create_app(replay_catalog=X)`` uses X verbatim and does NOT scan/build from the environment."""
    from veridex.api.router import create_app
    from veridex.ingest.replay_catalog import build_catalog

    monkeypatch.setenv("REPLAY_PACK_ROOT", "/nonexistent/should-never-be-scanned")
    provided = build_catalog("")

    app = create_app(store=InMemoryStore(), replay_catalog=provided)
    assert app.state.replay_catalog is provided
