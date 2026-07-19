"""I-2 — Owned :class:`AgentInstance` + runtime-handle fields + owner-scoped instance APIs (TDD).

Four load-bearing trust properties (all offline, zero network):

1. **Owner-scoping (fail-closed):** a non-owner ``GET /agents/instances/{id}`` is refused (403/404);
   the owner gets 200 with their instance. ``GET /agents/instances`` lists ONLY the caller's own
   instances.
2. **Round-trip persistence, ZERO DDL:** ``operator_id`` + ``runtime_handle`` survive a
   ``persist_agent_instance`` round-trip on BOTH stores (InMemory always; Postgres gated) purely via
   the ``record_json`` blob — no new columns.
3. **Legacy compatibility (fail-closed):** a pre-change row (no ``operator_id``) still LOADS, is
   treated as UNOWNED, and is EXCLUDED from every owner-scoped listing — never inherited by any caller.
4. **AC-18 persistence:** an authenticated deploy persists ``operator_id == principal.did``
   (server-derived); a client-forged ``operator_id`` / ``owner`` in the request body is IGNORED.

The Privy ES256 auth boundary (I-1, ``auth-contract@1``) is exercised with locally-signed offline
tokens — the same key setup the deploy-auth suite uses (no JWKS fetch, no network).
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from httpx import ASGITransport

from veridex.api.deploy import DeployDeps
from veridex.api.router import create_app
from veridex.config import Settings
from veridex.deploy.instance import AgentInstance
from veridex.deploy.preflight import DeployConfig
from veridex.ingest.feed_health import FeedHealthReport
from veridex.ingest.marketstate import MarketState
from veridex.store import InMemoryStore

_APP_ID = "test-privy-app-id"
_DID_ALICE = "did:privy:ALICE"
_DID_BOB = "did:privy:BOB"


# --- offline ES256 keys + Privy-format token signer (no network) -----------------------------


def _make_keypair() -> tuple[str, str]:
    """Generate an ES256 (ECDSA P-256) keypair; return ``(private_pem, public_spki_pem)``."""
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
    """Sign a Privy-format ES256 access token whose ``sub`` is the caller's DID."""
    now = int(time.time())
    claims = {"sub": sub, "aud": aud, "iss": iss, "iat": now, "exp": now + exp_delta_s, "sid": "sess-1"}
    return jwt.encode(claims, _PRIV_PEM, algorithm="ES256")


def _bearer(did: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_sign(sub=did)}"}


def _privy_settings(*, auth_mode: str = "privy") -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        app_env="development",
        auth_mode=auth_mode,
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


# --- instance factory (a valid, minimal persisted record) ------------------------------------


def _make_instance(
    *,
    instance_id: str,
    operator_id: str | None,
    runtime_handle: dict[str, Any] | None = None,
    run_id: str = "run-x",
) -> AgentInstance:
    """Build a minimal valid :class:`AgentInstance` for persistence tests."""
    return AgentInstance(
        instance_id=instance_id,
        template_id="sharp-momentum-v2",
        agent_id="studio-agent",
        submitted_config={"strategy": "momentum-sharp"},
        effective_config={"strategy": "momentum-sharp"},
        config_hash="cfg-hash",
        policy_hash="pol-hash",
        source_mode="replay",
        execution_mode="paper",
        run_id=run_id,
        operator_id=operator_id,
        runtime_handle=runtime_handle,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# RED 1 — owner-scoping (fail-closed) on the owner-scoped instance APIs
# ---------------------------------------------------------------------------


async def test_owner_scoped_instance_apis_isolate_by_owner() -> None:
    store = InMemoryStore()
    await store.persist_agent_instance(_make_instance(instance_id="inst_alice", operator_id=_DID_ALICE))
    await store.persist_agent_instance(_make_instance(instance_id="inst_bob", operator_id=_DID_BOB))
    app = create_app(store=store, settings=_privy_settings())
    try:
        async with _transport(app) as client:
            # Owner reads their own instance → 200 with their record.
            owner_resp = await client.get("/agents/instances/inst_alice", headers=_bearer(_DID_ALICE))
            assert owner_resp.status_code == 200, owner_resp.text
            body = owner_resp.json()
            assert body["instance_id"] == "inst_alice"
            assert body["operator_id"] == _DID_ALICE

            # Non-owner is refused (fail-closed): never 200, never leaks the record.
            other_resp = await client.get("/agents/instances/inst_alice", headers=_bearer(_DID_BOB))
            assert other_resp.status_code in (403, 404), other_resp.text

            # Listing is owner-scoped: Alice sees ONLY her own instance, never Bob's.
            list_resp = await client.get("/agents/instances", headers=_bearer(_DID_ALICE))
            assert list_resp.status_code == 200, list_resp.text
            ids = {row["instance_id"] for row in list_resp.json()}
            assert ids == {"inst_alice"}
    finally:
        await _drain(app)


# ---------------------------------------------------------------------------
# RED 2 — round-trip persistence, ZERO DDL (InMemory always; Postgres gated)
# ---------------------------------------------------------------------------


async def test_operator_id_and_runtime_handle_round_trip_inmemory() -> None:
    store = InMemoryStore()
    handle = {"runtime_kind": "agentos", "runtime_agent_id": "aos-7", "session_id": "s-1", "run_id": "run-x"}
    await store.persist_agent_instance(
        _make_instance(instance_id="inst_rt", operator_id=_DID_ALICE, runtime_handle=handle)
    )
    got = await store.get_agent_instance("inst_rt")
    assert got.operator_id == _DID_ALICE
    assert got.runtime_handle == handle
    # run_id stays the authoritative identity; runtime_handle is replaceable infra carried alongside.
    assert got.run_id == "run-x"


def _psycopg_available() -> bool:
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(
    not (os.getenv("DATABASE_URL") and _psycopg_available()),
    reason="Postgres round-trip: set DATABASE_URL and install psycopg",
)
async def test_operator_id_and_runtime_handle_round_trip_postgres() -> None:
    import psycopg

    from veridex.store import PostgresStore

    dsn = os.environ["DATABASE_URL"]
    store = PostgresStore(dsn=dsn)
    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        await store.init_db(conn)

    handle = {"runtime_kind": "agentos", "runtime_agent_id": "aos-9", "session_id": "s-9", "run_id": "run-pg"}
    await store.persist_agent_instance(
        _make_instance(instance_id="inst_pg_rt", operator_id=_DID_ALICE, runtime_handle=handle, run_id="run-pg")
    )
    got = await store.get_agent_instance("inst_pg_rt")
    # Fields survive purely through record_json — the schema has NO operator_id / runtime_handle column.
    assert got.operator_id == _DID_ALICE
    assert got.runtime_handle == handle


# ---------------------------------------------------------------------------
# RED 3 — legacy compatibility (fail-closed): unowned row loads, never inherited
# ---------------------------------------------------------------------------


async def test_legacy_row_without_operator_id_loads_and_is_never_listed() -> None:
    # A pre-change persisted row: its record_json has NO operator_id key at all.
    legacy_json = _make_instance(instance_id="inst_legacy", operator_id=_DID_ALICE).model_dump(mode="json")
    del legacy_json["operator_id"]
    assert "operator_id" not in legacy_json
    # It STILL loads under the current model (optional field defaults to None → treated as UNOWNED).
    legacy = AgentInstance.model_validate(legacy_json)
    assert legacy.operator_id is None

    store = InMemoryStore()
    await store.persist_agent_instance(legacy)
    app = create_app(store=store, settings=_privy_settings())
    try:
        async with _transport(app) as client:
            # Excluded from EVERY caller's listing — an unowned row is never inherited.
            for did in (_DID_ALICE, _DID_BOB):
                list_resp = await client.get("/agents/instances", headers=_bearer(did))
                assert list_resp.status_code == 200, list_resp.text
                ids = {row["instance_id"] for row in list_resp.json()}
                assert "inst_legacy" not in ids
                # Direct fetch is refused for everyone (fail-closed): no caller may claim it.
                get_resp = await client.get("/agents/instances/inst_legacy", headers=_bearer(did))
                assert get_resp.status_code in (403, 404), get_resp.text
    finally:
        await _drain(app)


# ---------------------------------------------------------------------------
# RED 4 — AC-18: authenticated deploy persists server-derived owner; body owner IGNORED
# ---------------------------------------------------------------------------

_LIVE_KEY = "OU|FT|2.5"

_VALID_DEPLOY: dict[str, Any] = {
    "template_id": "sharp-momentum-v2",
    "agent_id": "studio-agent",
    "strategy": "momentum-sharp",
    "source_mode": "live",
    "execution_mode": "paper",
    "market_allowlist": [_LIVE_KEY],
    "venue_allowlist": ["fake"],
    "min_edge_bps": 0,
    "max_stake": 0.0,
    "window_id": "w1",
    "fixture_id": 1,
    "end_rule": "pre_match",
    "alpha": 0.4,
    "z_threshold": 2.5,
    "ph_delta": 0.01,
    "ph_lambda": 0.15,
    "cooldown_ticks": 3,
    "warmup_ticks": 10,
    "min_movements": 8,
    "lookback": 64,
    "scale_floor": 0.02,
    "persistence_logit": 0.06,
}


def _live_market(over_bps: int) -> dict[str, Any]:
    return {"stable_prob_bps": {"over": over_bps}, "stable_price": {"over": 1.6, "under": 2.4}, "suspended": False}


def _live_ms(over_bps: int, *, tick_seq: int, ts: int) -> MarketState:
    return MarketState(
        fixture_id=1, tick_seq=tick_seq, ts=ts, phase=0, markets={_LIVE_KEY: _live_market(over_bps)}, scores={}
    )


async def _never_ending_stream(_config: DeployConfig) -> AsyncIterator[MarketState]:
    yield _live_ms(5000, tick_seq=0, ts=1000)
    await asyncio.Event().wait()  # blocks until cancelled in cleanup
    yield _live_ms(5200, tick_seq=1, ts=1100)  # unreachable


async def _close(_fixture_id: int) -> list[dict[str, Any]]:
    return [
        {
            "FixtureId": 1,
            "Ts": 1_200_000,
            "InRunning": 0,
            "SuperOddsType": "OU",
            "MarketPeriod": "FT",
            "MarketParameters": "2.5",
            "PriceNames": ["over", "under"],
            "Prices": [1600, 2400],
            "Pct": [66, 34],
        }
    ]


def _healthy_live_feed() -> FeedHealthReport:
    return FeedHealthReport(
        source_mode="live",
        txline_configured=True,
        connected=True,
        last_tick_ts=1000,
        ticks_seen=5,
        fixture_id=1,
        staleness_s=1,
        stale=False,
    )


def _deploy_deps() -> DeployDeps:
    return DeployDeps(
        feed_report=_healthy_live_feed(),
        market_resolved=True,
        stream_factory=_never_ending_stream,
        fetch_updates=_close,
        anchor_fn=None,  # offline: no on-chain anchor
    )


async def test_deploy_persists_server_derived_owner_and_ignores_body_owner() -> None:
    store = InMemoryStore()
    app = create_app(store=store, settings=_privy_settings(), deploy_deps=_deploy_deps())
    # The client forges an owner AND an operator_id in the body — BOTH must be ignored (server-derived).
    forged_body = {**_VALID_DEPLOY, "owner": "did:privy:ATTACKER", "operator_id": "did:privy:ATTACKER"}
    try:
        async with _transport(app) as client:
            resp = await client.post("/agents/deploy", json=forged_body, headers=_bearer(_DID_ALICE))
        assert resp.status_code == 200, resp.text
        instance_id = resp.json()["instance_id"]
        # The PERSISTED record's owner is the token DID, never the forged body value.
        persisted = await store.get_agent_instance(instance_id)
        assert persisted.operator_id == _DID_ALICE
        assert persisted.operator_id != "did:privy:ATTACKER"
    finally:
        await _drain(app)
