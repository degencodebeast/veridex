"""MAJOR 1 (Codex narrow re-gate) — the II-9 arena comparison must be REACHABLE and OBSERVABLE over
the AUTHENTICATED, owner-scoped competition surface, not merely a Python function seam.

Codex ruled: do NOT retrofit the legacy unauthenticated ``POST /demo/run`` (offline
deterministic/contrarian demo — the wrong roster and authority surface). Instead the checkpointed
det-Drift vs LLM-Drift comparison must run through the authenticated, owner-scoped lifecycle and be
observable from an HTTP response. This suite pins a dedicated OWNER-SCOPED arena endpoint
(``POST /competitions/{id}/arena``):

  1. ANONYMOUS is refused (fail-closed): no principal -> no arena run (401).
  2. A NON-OWNER is refused (403): the competition is owner-scoped (AC-27/AC-29 preserved).
  3. The OWNER gets the honest ``ArenaComparisonReport`` payload over HTTP (never ``None``) with the
     eligible-checkpoints / actions-vs-WAITs / scoreable-decisions / fixture-count / clustered-
     uncertainty fields — and NEVER a bare average CLV headline (addendum §3).

Fully offline: the same locally-signed Privy ES256 tokens the I-1/I-7b suites use (no JWKS fetch),
and an INJECTED model launcher (no real LLM, no network) via ``create_app(arena_model_launcher=...)``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from httpx import ASGITransport

from veridex.api.router import create_app
from veridex.config import Settings
from veridex.runtime.schemas import AgentAction, SportsActionType
from veridex.store import InMemoryStore

_APP_ID = "test-privy-app-id"
_DID_ALICE = "did:privy:ALICE"
_DID_BOB = "did:privy:BOB"


# --- offline ES256 keys + Privy-format token signer (no network) -----------------------------


def _make_keypair() -> tuple[str, str]:
    priv = ec.generate_private_key(ec.SECP256R1())
    priv_pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub_pem = (
        priv.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return priv_pem, pub_pem


_PRIV_PEM, _PUB_PEM = _make_keypair()


def _sign(*, sub: str, aud: str = _APP_ID, iss: str = "privy.io", exp_delta_s: int = 3600) -> str:
    now = int(time.time())
    claims = {"sub": sub, "aud": aud, "iss": iss, "iat": now, "exp": now + exp_delta_s, "sid": "sess-1"}
    return jwt.encode(claims, _PRIV_PEM, algorithm="ES256")


def _bearer(did: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_sign(sub=did)}"}


def _privy_settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        app_env="development",
        auth_mode="privy",
        privy_app_id=_APP_ID,
        privy_verification_key=_PUB_PEM,
    )


def _transport(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _drain(app: Any) -> None:
    tasks = list(getattr(app.state, "deploy_background_tasks", set()))
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# --- injected offline model launcher (no LLM, no network) ------------------------------------


class _DoneHandle:
    """A handle already done, returning one raw model output on ``result()`` (completes fresh)."""

    def __init__(self, raw: object) -> None:
        self._raw = raw

    def done(self) -> bool:
        return True

    def cancel(self) -> None:
        pass

    def cancelled(self) -> bool:
        return False

    def exception(self) -> BaseException | None:
        return None

    def result(self) -> object:
        return self._raw


class _OfflineArenaLauncher:
    """Injected arena model seam — every launch completes fresh with a WAIT (no network)."""

    def launch(self, prompt: str) -> _DoneHandle:
        return _DoneHandle(AgentAction(type=SportsActionType.WAIT))


# --- request bodies --------------------------------------------------------------------------

_COMPETITION_CONFIG: dict[str, Any] = {
    "competition_type": "replay_arena",
    "source_mode": "replay",
    "execution_mode": "paper",
    "market_scope": "WC:TEST",
    "roster_size": 2,
}


def _entry(agent_id: str, strategy: str) -> dict[str, Any]:
    return {
        "agent_id": agent_id,
        "owner": "team",
        "strategy": strategy,
        "model": None,
        "proof_mode": "reproducible",
    }


async def _create(client: httpx.AsyncClient, did: str) -> str:
    resp = await client.post("/competitions", json=_COMPETITION_CONFIG, headers=_bearer(did))
    assert resp.status_code == 200, resp.text
    return resp.json()["competition_id"]


async def _seed_drift_roster(store: InMemoryStore, app: Any, did: str) -> str:
    """Create an owned competition declaring the det-Drift + LLM-Drift roster; return its id."""
    async with _transport(app) as client:
        comp_id = await _create(client, did)
        # The DECLARED det-Drift + LLM-Drift roster (registration is a pure offline config-hash pin).
        for agent_id, strategy in (("det-drift", "cumulative-drift"), ("llm-drift", "llm")):
            reg = await client.post(
                f"/competitions/{comp_id}/agents", json=_entry(agent_id, strategy), headers=_bearer(did)
            )
            assert reg.status_code == 200, reg.text
    return comp_id


# ---------------------------------------------------------------------------
# RED 1 — anonymous arena run is refused (fail-closed): no principal -> no run.
# ---------------------------------------------------------------------------


async def test_anonymous_arena_run_is_refused_401() -> None:
    store = InMemoryStore()
    app = create_app(store=store, settings=_privy_settings(), arena_model_launcher=_OfflineArenaLauncher())
    try:
        comp_id = await _seed_drift_roster(store, app, _DID_ALICE)
        async with _transport(app) as client:
            resp = await client.post(f"/competitions/{comp_id}/arena")  # no Authorization header
            assert resp.status_code == 401, resp.text
    finally:
        await _drain(app)


# ---------------------------------------------------------------------------
# RED 2 — a non-owner arena run is refused (403): the surface is owner-scoped.
# ---------------------------------------------------------------------------


async def test_non_owner_arena_run_is_refused_403() -> None:
    store = InMemoryStore()
    app = create_app(store=store, settings=_privy_settings(), arena_model_launcher=_OfflineArenaLauncher())
    try:
        comp_id = await _seed_drift_roster(store, app, _DID_ALICE)  # owned by Alice
        async with _transport(app) as client:
            resp = await client.post(f"/competitions/{comp_id}/arena", headers=_bearer(_DID_BOB))
            assert resp.status_code == 403, resp.text
    finally:
        await _drain(app)


# ---------------------------------------------------------------------------
# RED 3 — the OWNER gets the honest arena comparison payload over HTTP (not None).
# ---------------------------------------------------------------------------


async def test_owner_arena_run_returns_honest_comparison_over_http() -> None:
    store = InMemoryStore()
    app = create_app(store=store, settings=_privy_settings(), arena_model_launcher=_OfflineArenaLauncher())
    try:
        comp_id = await _seed_drift_roster(store, app, _DID_ALICE)
        async with _transport(app) as client:
            resp = await client.post(f"/competitions/{comp_id}/arena", headers=_bearer(_DID_ALICE))
            assert resp.status_code == 200, resp.text
            body = resp.json()

        # The comparison is ACTUALLY produced and observable over HTTP — never left None.
        assert body["competition_id"] == comp_id
        payload = body["arena_comparison"]
        assert payload is not None, "the authenticated arena run must EXPOSE the comparison over HTTP"

        # The honest headline fields the addendum requires.
        for f in (
            "eligible_checkpoints",
            "identical_opportunities",
            "scoreable_decisions",
            "fixture_count",
            "contestants",
            "clustered_uncertainty",
        ):
            assert f in payload, f"missing honest arena field: {f}"
        for cid in ("det-drift", "llm-drift"):
            row = payload["contestants"][cid]
            assert "actions" in row and "waits" in row and "scoreable_decisions" in row

        # NEVER a bare average CLV headline (addendum §3 honesty).
        banned = {"average_clv", "avg_clv", "mean_clv", "clv", "average_clv_bps"}
        assert banned.isdisjoint(payload.keys())
        assert banned.isdisjoint(payload["clustered_uncertainty"].keys())
    finally:
        await _drain(app)


# ---------------------------------------------------------------------------
# RED 4 — an unknown competition is 404 (owner-scoped surface, no existence leak beyond 404).
# ---------------------------------------------------------------------------


async def test_arena_run_unknown_competition_is_404() -> None:
    app = create_app(store=InMemoryStore(), settings=_privy_settings(), arena_model_launcher=_OfflineArenaLauncher())
    try:
        async with _transport(app) as client:
            resp = await client.post("/competitions/does-not-exist/arena", headers=_bearer(_DID_ALICE))
            assert resp.status_code == 404, resp.text
    finally:
        await _drain(app)
