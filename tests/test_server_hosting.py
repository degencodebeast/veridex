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


def _served() -> tuple[object, TestClient]:
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
    assert status in (200, 503), status
    assert status not in (401, 403), status


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


def test_red4_served_native_run_route_reachable_but_gated() -> None:
    """RED#4: the agno-native agent-run route EXISTS + an authenticated request REACHES it (403, not 404).

    Reachable-but-gated (403) — NOT absent (404) — proves the run surface is genuinely hosted behind
    the boundary, without driving a run. (Authority-bound execution stays in the deploy path.)
    """
    _guard, client = _served()
    resp = client.post("/agents/veridex-market-maker/runs", headers=_auth("tokenA"))
    assert resp.status_code == 403, resp.status_code  # exists + gated (hosted), not 404 (absent)
