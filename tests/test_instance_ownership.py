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
from veridex.mm_strategy.session_factory import MakerReplayTape, compute_tape_content_hash
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
    market_allowlist: list[str] | None = None,
    replay_binding: dict[str, Any] | None = None,
    submitted_config: dict[str, Any] | None = None,
    effective_config: dict[str, Any] | None = None,
) -> AgentInstance:
    """Build a minimal valid :class:`AgentInstance` for persistence tests."""
    return AgentInstance(
        instance_id=instance_id,
        template_id="sharp-momentum-v2",
        agent_id="studio-agent",
        submitted_config=submitted_config if submitted_config is not None else {"strategy": "momentum-sharp"},
        effective_config=effective_config if effective_config is not None else {"strategy": "momentum-sharp"},
        config_hash="cfg-hash",
        policy_hash="pol-hash",
        source_mode="replay",
        execution_mode="paper",
        run_id=run_id,
        market_allowlist=market_allowlist if market_allowlist is not None else [],
        replay_binding=replay_binding,
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


# ---------------------------------------------------------------------------
# CURATED fixture + market labels on the owner-scoped instance-detail response
# ---------------------------------------------------------------------------


#: The R-4 replay PACK selection hash (curated demo pack) — a DIFFERENT identity than the maker tape.
_PACK_CONTENT_HASH = "f16c3853a80f000000000000000000000000000000000000000000000000abcd"
#: The banked ``pmxt-txline-mm-18209181-v1`` MakerReplayTape's OWN content hash (server-derived).
_MAKER_TAPE_REF = "pmxt-txline-mm-18209181-v1"
_MAKER_TAPE_CONTENT_HASH = "19b314ab0170239bb63fa9b7efd2cf6c459e128f4841a6fd11ffc92c7acd2050"


async def test_instance_detail_surfaces_curated_fixture_and_market_labels() -> None:
    store = InMemoryStore()
    binding = {"pack_id": _MAKER_TAPE_REF, "fixture_id": 18209181, "content_hash": _PACK_CONTENT_HASH}
    await store.persist_agent_instance(
        _make_instance(
            instance_id="inst_maker",
            operator_id=_DID_ALICE,
            market_allowlist=["pmxt:18209181:home_win"],
            replay_binding=binding,
            # An MM (quoteguard-mm) instance: the effective config IS the mm block (carries tape_ref).
            submitted_config={"strategy": "quoteguard-mm"},
            effective_config={"tape_ref": _MAKER_TAPE_REF},
        )
    )
    app = create_app(store=store, settings=_privy_settings())
    try:
        async with _transport(app) as client:
            resp = await client.get("/agents/instances/inst_maker", headers=_bearer(_DID_ALICE))
            assert resp.status_code == 200, resp.text
            body = resp.json()
            # Server-derived, additive label fields — the raw ids/fields remain unchanged alongside.
            assert body["fixture_id"] == 18209181
            assert body["fixture_label"] == "France v Morocco"
            assert body["market_label"] == "Home win"
            assert body["replay_pack_id"] == _MAKER_TAPE_REF
            # MAJOR: the two hash identities are DISTINCT and never conflated. The replay-PACK hash is
            # the R-4 catalog selection; the maker-tape hash is the MakerReplayTape's own hash the run
            # verifies — a DIFFERENT value, derived server-side via the same resolver.
            assert body["replay_pack_content_hash"] == _PACK_CONTENT_HASH
            assert body["maker_tape_ref"] == _MAKER_TAPE_REF
            assert body["maker_tape_content_hash"] == _MAKER_TAPE_CONTENT_HASH
            assert body["maker_tape_content_hash"] != body["replay_pack_content_hash"]
            # The old (misleading) field name is GONE — the pack hash is never labeled "tape".
            assert "tape_content_hash" not in body
            # The existing contract is untouched: raw id + allowlist still present verbatim.
            assert body["instance_id"] == "inst_maker"
            assert body["market_allowlist"] == ["pmxt:18209181:home_win"]
    finally:
        await _drain(app)


async def test_instance_detail_non_mm_instance_has_no_maker_tape_fields() -> None:
    # A directional (non-MM) instance has NO maker tape — the maker-tape fields must be absent/null,
    # while the replay-PACK identity may still be present from its binding.
    store = InMemoryStore()
    binding = {"pack_id": "pack-directional", "fixture_id": 18209181, "content_hash": _PACK_CONTENT_HASH}
    await store.persist_agent_instance(
        _make_instance(
            instance_id="inst_directional",
            operator_id=_DID_ALICE,
            market_allowlist=["pmxt:18209181:home_win"],
            replay_binding=binding,
            submitted_config={"strategy": "momentum-sharp"},
        )
    )
    app = create_app(store=store, settings=_privy_settings())
    try:
        async with _transport(app) as client:
            resp = await client.get("/agents/instances/inst_directional", headers=_bearer(_DID_ALICE))
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["replay_pack_content_hash"] == _PACK_CONTENT_HASH
            assert body["maker_tape_ref"] is None
            assert body["maker_tape_content_hash"] is None
    finally:
        await _drain(app)


def _mm_instance(instance_id: str, tape_ref: str) -> AgentInstance:
    """A ``quoteguard-mm`` instance whose effective config carries ``tape_ref`` (the mm block)."""
    return _make_instance(
        instance_id=instance_id,
        operator_id=_DID_ALICE,
        market_allowlist=["pmxt:18209181:home_win"],
        submitted_config={"strategy": "quoteguard-mm"},
        effective_config={"tape_ref": tape_ref},
    )


async def test_instance_detail_maker_hash_reflects_the_injected_effective_resolver() -> None:
    # MAJOR (a): the detail must report the hash of the tape the RUN would use — i.e. the EFFECTIVE
    # (injected) resolver's tape — NOT a hardcoded default. Inject a DIFFERENT valid tape (different
    # events, self-consistent hash) → the detail surfaces THAT tape's recomputed hash.
    alt_events: tuple[Any, ...] = ("alt-event-1", "alt-event-2", "alt-event-3")
    alt_hash = compute_tape_content_hash(alt_events)
    assert alt_hash != _MAKER_TAPE_CONTENT_HASH  # genuinely a different tape than the default pmxt one
    alt_tape = MakerReplayTape(
        tape_ref=_MAKER_TAPE_REF, identity=None, venue_market_ref="alt", events=alt_events, content_hash=alt_hash
    )

    store = InMemoryStore()
    await store.persist_agent_instance(_mm_instance("inst_injected", _MAKER_TAPE_REF))
    app = create_app(store=store, settings=_privy_settings(), deploy_deps=DeployDeps(mm_tape_resolver=lambda _ref: alt_tape))
    try:
        async with _transport(app) as client:
            resp = await client.get("/agents/instances/inst_injected", headers=_bearer(_DID_ALICE))
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["maker_tape_ref"] == _MAKER_TAPE_REF
            # The surfaced hash is the INJECTED tape's recomputed hash, not the default pmxt hash.
            assert body["maker_tape_content_hash"] == alt_hash
            assert body["maker_tape_content_hash"] != _MAKER_TAPE_CONTENT_HASH
    finally:
        await _drain(app)


async def test_instance_detail_maker_hash_fails_closed_on_unexpected_resolver_error() -> None:
    # MINOR hardening: the maker-tape hash sits on the PRIMARY (non-best-effort) detail path. If the
    # resolver raises anything OTHER than MMTapeNotFoundError (e.g. a banked-but-corrupt tape blowing up
    # during reconstruction), the detail must FAIL CLOSED — surface NO hash but STILL render the page
    # (200 with ownership/run_id/status intact), never 500 the whole owner-scoped instance.
    def _boom(_ref: str) -> MakerReplayTape:
        raise ValueError("corrupt banked tape")

    store = InMemoryStore()
    await store.persist_agent_instance(_mm_instance("inst_boom", _MAKER_TAPE_REF))
    app = create_app(store=store, settings=_privy_settings(), deploy_deps=DeployDeps(mm_tape_resolver=_boom))
    try:
        async with _transport(app) as client:
            resp = await client.get("/agents/instances/inst_boom", headers=_bearer(_DID_ALICE))
            assert resp.status_code == 200, resp.text  # page still loads; the resolver error did NOT 500 it
            body = resp.json()
            assert body["maker_tape_ref"] == _MAKER_TAPE_REF  # ref still named
            assert body["maker_tape_content_hash"] is None  # fail-closed: no hash on an unexpected error
            assert body["run_id"]  # the authoritative identity still renders (page not 500'd)
    finally:
        await _drain(app)


async def test_instance_detail_maker_hash_fails_closed_on_tampered_tape() -> None:
    # MAJOR (b): a tape whose events do NOT match its claimed content_hash (a tampered/broken tape the
    # run itself would REJECT at reconstruction) must FAIL CLOSED — the detail surfaces NO hash
    # (maker_tape_content_hash is None), never a value that doesn't match the events. The ref remains.
    tampered_tape = MakerReplayTape(
        tape_ref=_MAKER_TAPE_REF, identity=None, venue_market_ref="x",
        events=("real-event",), content_hash="0" * 64,  # claimed hash does NOT match the events
    )
    store = InMemoryStore()
    await store.persist_agent_instance(_mm_instance("inst_tampered", _MAKER_TAPE_REF))
    app = create_app(store=store, settings=_privy_settings(), deploy_deps=DeployDeps(mm_tape_resolver=lambda _ref: tampered_tape))
    try:
        async with _transport(app) as client:
            resp = await client.get("/agents/instances/inst_tampered", headers=_bearer(_DID_ALICE))
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["maker_tape_ref"] == _MAKER_TAPE_REF  # ref still named
            assert body["maker_tape_content_hash"] is None  # fail-closed: never a mismatched hash
    finally:
        await _drain(app)


async def test_instance_detail_no_fixture_yields_null_label() -> None:
    # MINOR 1: an instance with no derivable fixture id must project fixture_label=None (so the UI
    # genuinely omits the fixture rows) — never a truthy "Fixture (unknown)" placeholder.
    store = InMemoryStore()
    await store.persist_agent_instance(
        _make_instance(instance_id="inst_nofixture", operator_id=_DID_ALICE, market_allowlist=[])
    )
    app = create_app(store=store, settings=_privy_settings())
    try:
        async with _transport(app) as client:
            resp = await client.get("/agents/instances/inst_nofixture", headers=_bearer(_DID_ALICE))
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["fixture_id"] is None
            assert body["fixture_label"] is None
            assert body["market_label"] is None
    finally:
        await _drain(app)


async def test_instance_detail_unmapped_fixture_falls_back_honestly() -> None:
    store = InMemoryStore()
    # No replay_binding: the fixture id is parsed from the leading pmxt allowlist token, and an
    # UNMAPPED id must fall back to "Fixture {id}" (never a guessed matchup).
    await store.persist_agent_instance(
        _make_instance(
            instance_id="inst_unmapped",
            operator_id=_DID_ALICE,
            market_allowlist=["pmxt:999999:away_win"],
        )
    )
    app = create_app(store=store, settings=_privy_settings())
    try:
        async with _transport(app) as client:
            resp = await client.get("/agents/instances/inst_unmapped", headers=_bearer(_DID_ALICE))
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["fixture_id"] == 999999
            assert body["fixture_label"] == "Fixture 999999"
            assert body["market_label"] == "Away win"
            # No replay binding → the pack identity fields are honestly null.
            assert body["replay_pack_content_hash"] is None
            assert body["replay_pack_id"] is None
    finally:
        await _drain(app)


def test_fixture_label_maps_curated_ids_and_falls_back() -> None:
    from veridex.api.fixture_labels import fixture_label

    assert fixture_label(18209181) == "France v Morocco"
    assert fixture_label(18218149) == "Spain v Belgium"
    assert fixture_label(18213979) == "Norway v England"
    assert fixture_label(18222446) == "Argentina v Switzerland"
    assert fixture_label(999999) == "Fixture 999999"
    assert fixture_label(None) == "Fixture (unknown)"


def test_market_label_humanizes_known_token_forms_and_passes_unknown_through() -> None:
    from veridex.api.fixture_labels import market_label

    # pmxt outcome tokens → the humanized suffix
    assert market_label("pmxt:18209181:home_win") == "Home win"
    assert market_label("pmxt:18209181:draw") == "Draw"
    assert market_label("pmxt:18209181:away_win") == "Away win"
    assert market_label("pmxt:18209181:over") == "Over"
    assert market_label("pmxt:18209181:under") == "Under"
    # directional market keys → their human labels
    assert market_label("1X2_PARTICIPANT_RESULT") == "Match result (1X2)"
    assert market_label("OVERUNDER_PARTICIPANT_GOALS") == "Total goals (O/U)"
    # unknown token → returned VERBATIM (honest, never a guess)
    assert market_label("pmxt:18209181:mystery") == "pmxt:18209181:mystery"
    assert market_label("SOME_UNKNOWN_MARKET") == "SOME_UNKNOWN_MARKET"
    # MINOR 2: only the EXACT pmxt:{numeric}:{suffix} shape is humanized — a bogus string that merely
    # ENDS in a known suffix is NOT a pinned market token and passes through unchanged.
    assert market_label("bogus:home_win") == "bogus:home_win"
    assert market_label("pmxt:notanumber:home_win") == "pmxt:notanumber:home_win"
    assert market_label("home_win") == "home_win"
