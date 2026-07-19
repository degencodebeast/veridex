"""I-7b — Owner-scoped, lifecycle-enforced competition writes (TDD, LOAD-BEARING trust boundary).

The competition create -> register -> start flow (F-4) is closed here: an anonymous caller cannot
create a competition, graft an agent onto another owner's roster, or start someone else's
competition; and the roster is frozen once the competition has started/finalized.

Trust properties (all offline, zero network — the same locally-signed Privy ES256 tokens the I-1/I-2
suites use, no JWKS fetch):

1. **Anonymous is refused (fail-closed):** no principal -> no write of any kind (401).
2. **Owner-scoping (fail-closed):** a non-owner register/start is refused (403); the ``owner_id`` is
   SERVER-DERIVED (== ``principal.did``) at create time and a client-forged owner in the body is
   IGNORED. A legacy competition with no ``owner_id`` is UNOWNED and never silently inherited.
3. **Lifecycle enforcement:** registering after start/finalize is refused (409); a duplicate agent is
   refused (409); registering beyond the roster cap is refused (409).
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from httpx import ASGITransport

from veridex.api.router import create_app
from veridex.competition.models import (
    Competition,
    CompetitionConfig,
    CompetitionStatus,
    CompetitionType,
)
from veridex.config import Settings
from veridex.store import InMemoryStore

_APP_ID = "test-privy-app-id"
_DID_ALICE = "did:privy:ALICE"
_DID_BOB = "did:privy:BOB"
_DID_ATTACKER = "did:privy:ATTACKER"


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


# --- request bodies --------------------------------------------------------------------------

_COMPETITION_CONFIG: dict[str, Any] = {
    "competition_type": "replay_arena",
    "source_mode": "replay",
    "execution_mode": "paper",
    "market_scope": "WC:TEST",
    "roster_size": 2,
}


def _entry(agent_id: str) -> dict[str, Any]:
    """A minimal valid agent-registration body."""
    return {
        "agent_id": agent_id,
        "owner": "team",
        "strategy": "baseline",
        "model": None,
        "proof_mode": "reproducible",
    }


async def _create(client: httpx.AsyncClient, did: str, *, config: dict[str, Any] | None = None) -> str:
    """Create a competition as ``did`` and return its id (asserting 200)."""
    resp = await client.post("/competitions", json=config or _COMPETITION_CONFIG, headers=_bearer(did))
    assert resp.status_code == 200, resp.text
    return resp.json()["competition_id"]


# ---------------------------------------------------------------------------
# RED 1 — anonymous create is refused (fail-closed): no principal -> no write.
# ---------------------------------------------------------------------------


async def test_anonymous_create_is_refused_401() -> None:
    app = create_app(store=InMemoryStore(), settings=_privy_settings())
    try:
        async with _transport(app) as client:
            resp = await client.post("/competitions", json=_COMPETITION_CONFIG)  # no Authorization header
            assert resp.status_code == 401, resp.text
    finally:
        await _drain(app)


# ---------------------------------------------------------------------------
# RED 2 — registering onto ANOTHER owner's competition is refused (403).
# ---------------------------------------------------------------------------


async def test_register_onto_another_owners_competition_is_refused_403() -> None:
    app = create_app(store=InMemoryStore(), settings=_privy_settings())
    try:
        async with _transport(app) as client:
            comp_id = await _create(client, _DID_ALICE)  # owned by Alice
            resp = await client.post(
                f"/competitions/{comp_id}/agents", json=_entry("agent-x"), headers=_bearer(_DID_BOB)
            )
            assert resp.status_code == 403, resp.text
    finally:
        await _drain(app)


# ---------------------------------------------------------------------------
# RED 3 — starting ANOTHER owner's competition is refused (403).
# ---------------------------------------------------------------------------


async def test_start_another_owners_competition_is_refused_403() -> None:
    app = create_app(store=InMemoryStore(), settings=_privy_settings())
    try:
        async with _transport(app) as client:
            comp_id = await _create(client, _DID_ALICE)  # owned by Alice
            resp = await client.post(f"/competitions/{comp_id}/start", headers=_bearer(_DID_BOB))
            assert resp.status_code == 403, resp.text
    finally:
        await _drain(app)


# ---------------------------------------------------------------------------
# RED 4 — authenticated owner: all three succeed; owner_id is SERVER-DERIVED
#         (== principal.did) and a client-forged owner in the body is IGNORED.
# ---------------------------------------------------------------------------


async def test_owner_flow_succeeds_and_owner_id_is_server_derived() -> None:
    store = InMemoryStore()
    app = create_app(store=store, settings=_privy_settings())
    try:
        async with _transport(app) as client:
            # Alice creates while FORGING an owner_id in the body — it must NOT land.
            forged = {**_COMPETITION_CONFIG, "owner_id": _DID_ATTACKER, "owner": _DID_ATTACKER}
            create_resp = await client.post("/competitions", json=forged, headers=_bearer(_DID_ALICE))
            assert create_resp.status_code == 200, create_resp.text
            comp_id = create_resp.json()["competition_id"]

            # The PERSISTED owner is the token DID, never the forged body value.
            persisted = await store.get_competition(comp_id)
            assert persisted.owner_id == _DID_ALICE
            assert persisted.owner_id != _DID_ATTACKER

            # Register two agents + start — all succeed for the owner.
            reg_a = await client.post(
                f"/competitions/{comp_id}/agents", json=_entry("agent-alpha"), headers=_bearer(_DID_ALICE)
            )
            assert reg_a.status_code == 200, reg_a.text
            reg_b = await client.post(
                f"/competitions/{comp_id}/agents", json=_entry("agent-beta"), headers=_bearer(_DID_ALICE)
            )
            assert reg_b.status_code == 200, reg_b.text
            start = await client.post(f"/competitions/{comp_id}/start", headers=_bearer(_DID_ALICE))
            assert start.status_code == 200, start.text
            assert start.json()["status"] == "finalized"
    finally:
        await _drain(app)


# ---------------------------------------------------------------------------
# RED 5 — registering AFTER the competition has started/finalized is refused (409).
# ---------------------------------------------------------------------------


async def test_register_after_start_is_refused_409() -> None:
    app = create_app(store=InMemoryStore(), settings=_privy_settings())
    try:
        async with _transport(app) as client:
            comp_id = await _create(client, _DID_ALICE)
            for agent_id in ("agent-alpha", "agent-beta"):
                reg = await client.post(
                    f"/competitions/{comp_id}/agents", json=_entry(agent_id), headers=_bearer(_DID_ALICE)
                )
                assert reg.status_code == 200, reg.text
            start = await client.post(f"/competitions/{comp_id}/start", headers=_bearer(_DID_ALICE))
            assert start.status_code == 200, start.text

            # The roster is frozen post-start: even the owner cannot graft another agent on.
            late = await client.post(
                f"/competitions/{comp_id}/agents", json=_entry("agent-late"), headers=_bearer(_DID_ALICE)
            )
            assert late.status_code == 409, late.text
    finally:
        await _drain(app)


# ---------------------------------------------------------------------------
# RED 6 — a duplicate agent registration is refused (409).
# ---------------------------------------------------------------------------


async def test_duplicate_agent_registration_is_refused_409() -> None:
    app = create_app(store=InMemoryStore(), settings=_privy_settings())
    try:
        async with _transport(app) as client:
            comp_id = await _create(client, _DID_ALICE)
            first = await client.post(
                f"/competitions/{comp_id}/agents", json=_entry("agent-alpha"), headers=_bearer(_DID_ALICE)
            )
            assert first.status_code == 200, first.text
            dup = await client.post(
                f"/competitions/{comp_id}/agents", json=_entry("agent-alpha"), headers=_bearer(_DID_ALICE)
            )
            assert dup.status_code == 409, dup.text
    finally:
        await _drain(app)


# ---------------------------------------------------------------------------
# RED 7 — registering beyond the roster cap is refused (409).
# ---------------------------------------------------------------------------


async def test_registration_beyond_roster_cap_is_refused_409() -> None:
    app = create_app(store=InMemoryStore(), settings=_privy_settings())
    try:
        async with _transport(app) as client:
            comp_id = await _create(client, _DID_ALICE)  # roster_size == 2
            for agent_id in ("agent-alpha", "agent-beta"):
                reg = await client.post(
                    f"/competitions/{comp_id}/agents", json=_entry(agent_id), headers=_bearer(_DID_ALICE)
                )
                assert reg.status_code == 200, reg.text
            # The third registration exceeds the cap of 2.
            over = await client.post(
                f"/competitions/{comp_id}/agents", json=_entry("agent-gamma"), headers=_bearer(_DID_ALICE)
            )
            assert over.status_code == 409, over.text
    finally:
        await _drain(app)


# ---------------------------------------------------------------------------
# Persistence — owner_id survives a round-trip with ZERO DDL (InMemory always; Postgres gated).
# ---------------------------------------------------------------------------


async def test_owner_id_round_trips_inmemory() -> None:
    store = InMemoryStore()
    comp = Competition(
        competition_id="c_rt",
        config=CompetitionConfig(
            competition_type=CompetitionType.REPLAY_ARENA,
            source_mode="replay",
            market_scope="WC:TEST",
            roster_size=2,
        ),
        status=CompetitionStatus.DRAFT,
        entries=[],
        run_id=None,
        owner_id=_DID_ALICE,
    )
    await store.create_competition(comp)
    got = await store.get_competition("c_rt")
    assert got.owner_id == _DID_ALICE


async def test_legacy_competition_without_owner_id_loads_as_unowned() -> None:
    # A pre-change persisted competition: its serialized form has NO owner_id key at all.
    legacy_json = Competition(
        competition_id="c_legacy",
        config=CompetitionConfig(
            competition_type=CompetitionType.REPLAY_ARENA,
            source_mode="replay",
            market_scope="WC:TEST",
            roster_size=2,
        ),
        status=CompetitionStatus.DRAFT,
        entries=[],
        run_id=None,
        owner_id=_DID_ALICE,
    ).model_dump(mode="json")
    del legacy_json["owner_id"]
    assert "owner_id" not in legacy_json
    # It STILL loads under the current model (optional field defaults to None -> UNOWNED).
    legacy = Competition.model_validate(legacy_json)
    assert legacy.owner_id is None


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
async def test_owner_id_round_trips_postgres() -> None:
    import psycopg

    from veridex.store import PostgresStore

    dsn = os.environ["DATABASE_URL"]
    store = PostgresStore(dsn=dsn)
    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        await store.init_db(conn)

    comp = Competition(
        competition_id="c_pg_owner",
        config=CompetitionConfig(
            competition_type=CompetitionType.REPLAY_ARENA,
            source_mode="replay",
            market_scope="WC:TEST",
            roster_size=2,
        ),
        status=CompetitionStatus.DRAFT,
        entries=[],
        run_id=None,
        owner_id=_DID_ALICE,
    )
    await store.create_competition(comp)
    got = await store.get_competition("c_pg_owner")
    # owner_id survives purely through the config_json blob — the schema has NO owner_id column.
    assert got.owner_id == _DID_ALICE
