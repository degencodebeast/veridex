"""I-1 — Privy ES256 auth boundary on ``POST /agents/deploy`` (TDD, offline).

The deploy route FAILS CLOSED: an unauthenticated or bad-token request is rejected with 401
**before any persistence or side effect**; a valid Privy access token yields a SERVER-DERIVED
:class:`~veridex.api.auth_privy.PrivyPrincipal` whose ``did`` is exposed on the deploy result; a
client-forged ``owner`` in the POST body is IGNORED. This is the AUTH BOUNDARY only — it does NOT
persist the owner onto ``AgentInstance`` (I-2) and does NOT create ``DeploymentAttempt`` (I-3).

Frozen ``auth-contract@1``: transport is ``Authorization: Bearer <token>``; the backend verifies
ES256 asserting ``iss == "privy.io"``, ``aud == PRIVY_APP_ID``, a valid ``exp``, and ``sub`` starting
``did:privy:``. Tokens are signed OFFLINE with a locally-generated ES256 (ECDSA P-256) keypair — the
same key setup Privy documents for tests (docs/recipes/mock-jwt) — so there is ZERO network here.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException
from httpx import ASGITransport
from pydantic import ValidationError

from veridex.api.auth_privy import PrivyPrincipal, make_require_principal, verify_privy_token
from veridex.api.deploy import DeployDeps
from veridex.api.router import create_app
from veridex.config import Settings
from veridex.deploy.preflight import DeployConfig
from veridex.ingest.feed_health import FeedHealthReport
from veridex.ingest.marketstate import MarketState
from veridex.store import InMemoryStore

_APP_ID = "test-privy-app-id"
_REAL_DID = "did:privy:REAL"

# --- offline ES256 keys + Privy-format token signer (no network) -----------------------------


def _make_keypair() -> tuple[str, str]:
    """Generate an ES256 (ECDSA P-256) keypair; return ``(private_pem, public_spki_pem)`` as str."""
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
_OTHER_PRIV_PEM, _OTHER_PUB_PEM = _make_keypair()  # a DIFFERENT key → forged signatures


def _sign(
    priv_pem: str = _PRIV_PEM,
    *,
    sub: str = _REAL_DID,
    aud: str = _APP_ID,
    iss: str = "privy.io",
    exp_delta_s: int = 3600,
    sid: str = "sess-1",
) -> str:
    """Sign a Privy-format ES256 access token with the given private key + claims."""
    now = int(time.time())
    claims = {"sub": sub, "aud": aud, "iss": iss, "iat": now, "exp": now + exp_delta_s, "sid": sid}
    return jwt.encode(claims, priv_pem, algorithm="ES256")


# --- offline deploy fixtures (mirror the T21 live-launch offline path) ------------------------
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


def _live_ms(over_bps: int, *, tick_seq: int, ts: int, phase: int = 0) -> MarketState:
    return MarketState(
        fixture_id=1, tick_seq=tick_seq, ts=ts, phase=phase, markets={_LIVE_KEY: _live_market(over_bps)}, scores={}
    )


async def _never_ending_stream(_config: DeployConfig) -> AsyncIterator[MarketState]:
    """Yield one pre-kickoff tick, then block forever — the seal never arrives on its own."""
    yield _live_ms(5000, tick_seq=0, ts=1000, phase=0)
    await asyncio.Event().wait()  # blocks until the task is cancelled in cleanup
    yield _live_ms(5200, tick_seq=1, ts=1100, phase=0)  # unreachable


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


class _RecordingStore(InMemoryStore):
    """InMemoryStore that counts ``persist_agent_instance`` — the FIRST deploy side effect."""

    def __init__(self) -> None:
        super().__init__()
        self.persist_calls = 0

    async def persist_agent_instance(self, instance: Any) -> None:  # type: ignore[override]
        self.persist_calls += 1
        await super().persist_agent_instance(instance)


def _privy_settings(*, auth_mode: str = "privy") -> Settings:
    """Offline settings with Privy auth configured against the local test public key."""
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        app_env="development",
        auth_mode=auth_mode,
        privy_app_id=_APP_ID,
        privy_verification_key=_PUB_PEM,
    )


def _deploy_deps() -> DeployDeps:
    return DeployDeps(
        feed_report=_healthy_live_feed(),
        market_resolved=True,
        stream_factory=_never_ending_stream,
        fetch_updates=_close,
        anchor_fn=None,  # offline: no on-chain anchor
    )


def _transport(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _drain(app: Any) -> None:
    tasks = list(getattr(app.state, "deploy_background_tasks", set()))
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# verify_privy_token — the REAL ES256 verifier (offline signed tokens)
# ---------------------------------------------------------------------------


def test_verify_privy_token_valid_returns_principal() -> None:
    principal = verify_privy_token(_sign(), app_id=_APP_ID, verification_key=_PUB_PEM)
    assert isinstance(principal, PrivyPrincipal)
    assert principal.did == _REAL_DID


def test_verify_privy_token_bad_signature_401() -> None:
    forged = _sign(_OTHER_PRIV_PEM)  # signed by a DIFFERENT key
    with pytest.raises(HTTPException) as exc:
        verify_privy_token(forged, app_id=_APP_ID, verification_key=_PUB_PEM)
    assert exc.value.status_code == 401


def test_verify_privy_token_wrong_audience_401() -> None:
    with pytest.raises(HTTPException) as exc:
        verify_privy_token(_sign(aud="some-other-app"), app_id=_APP_ID, verification_key=_PUB_PEM)
    assert exc.value.status_code == 401


def test_verify_privy_token_wrong_issuer_401() -> None:
    with pytest.raises(HTTPException) as exc:
        verify_privy_token(_sign(iss="evil.example"), app_id=_APP_ID, verification_key=_PUB_PEM)
    assert exc.value.status_code == 401


def test_verify_privy_token_expired_401() -> None:
    with pytest.raises(HTTPException) as exc:
        verify_privy_token(_sign(exp_delta_s=-10), app_id=_APP_ID, verification_key=_PUB_PEM)
    assert exc.value.status_code == 401


def test_verify_privy_token_bad_sub_prefix_401() -> None:
    with pytest.raises(HTTPException) as exc:
        verify_privy_token(_sign(sub="attacker-not-a-did"), app_id=_APP_ID, verification_key=_PUB_PEM)
    assert exc.value.status_code == 401


# --- m2: algorithm-pinning regression guards (ES256 pinned; no alg-confusion) -----------------


def test_verify_privy_token_alg_none_401() -> None:
    """An unsigned ``alg=none`` token must be rejected (algorithms pinned to ES256)."""
    now = int(time.time())
    claims = {"sub": _REAL_DID, "aud": _APP_ID, "iss": "privy.io", "iat": now, "exp": now + 3600}
    unsigned = jwt.encode(claims, key="", algorithm="none")
    with pytest.raises(HTTPException) as exc:
        verify_privy_token(unsigned, app_id=_APP_ID, verification_key=_PUB_PEM)
    assert exc.value.status_code == 401


def _forge_hs256(secret: str, claims: dict[str, Any]) -> str:
    """Hand-craft an HS256 JWT that HMAC-signs with ``secret`` (bypasses PyJWT's asymmetric-key guard).

    This is the classic algorithm-confusion attack: the attacker signs with HS256 using the server's
    ES256 *public* PEM as the HMAC secret, hoping the verifier will HMAC-verify with that same public
    key. ``verify_privy_token`` pins ``algorithms=["ES256"]``, so it must reject this outright.
    """

    def _seg(obj: dict[str, Any]) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    signing_input = f"{_seg({'alg': 'HS256', 'typ': 'JWT'})}.{_seg(claims)}".encode()
    sig = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    return f"{signing_input.decode()}.{base64.urlsafe_b64encode(sig).rstrip(b'=').decode()}"


def test_verify_privy_token_hs256_algorithm_confusion_401() -> None:
    """An HS256 token that HMAC-signs with the PEM public key bytes must be rejected (alg confusion)."""
    now = int(time.time())
    claims = {"sub": _REAL_DID, "aud": _APP_ID, "iss": "privy.io", "iat": now, "exp": now + 3600}
    confused = _forge_hs256(_PUB_PEM, claims)
    with pytest.raises(HTTPException) as exc:
        verify_privy_token(confused, app_id=_APP_ID, verification_key=_PUB_PEM)
    assert exc.value.status_code == 401


def test_verify_privy_token_missing_aud_claim_401() -> None:
    """A validly-signed token missing a required claim (``aud``) must be rejected."""
    now = int(time.time())
    claims = {"sub": _REAL_DID, "iss": "privy.io", "iat": now, "exp": now + 3600, "sid": "s"}
    token = jwt.encode(claims, _PRIV_PEM, algorithm="ES256")
    with pytest.raises(HTTPException) as exc:
        verify_privy_token(token, app_id=_APP_ID, verification_key=_PUB_PEM)
    assert exc.value.status_code == 401


def test_verify_privy_token_missing_exp_claim_401() -> None:
    """A validly-signed token missing the required ``exp`` claim must be rejected."""
    now = int(time.time())
    claims = {"sub": _REAL_DID, "aud": _APP_ID, "iss": "privy.io", "iat": now, "sid": "s"}
    token = jwt.encode(claims, _PRIV_PEM, algorithm="ES256")
    with pytest.raises(HTTPException) as exc:
        verify_privy_token(token, app_id=_APP_ID, verification_key=_PUB_PEM)
    assert exc.value.status_code == 401


def test_make_require_principal_accepts_lowercase_bearer_scheme() -> None:
    """m1: the ``Bearer`` scheme is case-insensitive (RFC 6750) — ``bearer <token>`` authenticates."""
    dep = make_require_principal(_privy_settings())
    principal = dep(authorization=f"bearer {_sign()}")
    assert principal.did == _REAL_DID


def test_make_require_principal_accepts_upper_bearer_scheme() -> None:
    """m1: an all-caps ``BEARER <token>`` scheme still authenticates (case-insensitive match)."""
    dep = make_require_principal(_privy_settings())
    principal = dep(authorization=f"BEARER {_sign()}")
    assert principal.did == _REAL_DID


def test_make_require_principal_uses_injected_verifier() -> None:
    """The factory threads an injectable verifier — the seam tests can drive (no network)."""
    calls: list[str] = []

    def _fake_verifier(token: str, *, app_id: str, verification_key: str) -> PrivyPrincipal:
        calls.append(token)
        return PrivyPrincipal(did="did:privy:INJECTED")

    dep = make_require_principal(_privy_settings(), verifier=_fake_verifier)
    principal = dep(authorization="Bearer abc.def.ghi")
    assert principal.did == "did:privy:INJECTED"
    assert calls == ["abc.def.ghi"]


def test_make_require_principal_missing_header_401_without_calling_verifier() -> None:
    """A missing/malformed Bearer 401s BEFORE the verifier is ever consulted (fail-closed)."""
    called = False

    def _fake_verifier(token: str, *, app_id: str, verification_key: str) -> PrivyPrincipal:
        nonlocal called
        called = True
        return PrivyPrincipal(did="did:privy:X")

    dep = make_require_principal(_privy_settings(), verifier=_fake_verifier)
    for header in (None, "token-without-bearer", "Basic xyz"):
        with pytest.raises(HTTPException) as exc:
            dep(authorization=header)
        assert exc.value.status_code == 401
    assert called is False


# ---------------------------------------------------------------------------
# POST /agents/deploy — 401 BEFORE any side effect (AC-18 reject-before half)
# ---------------------------------------------------------------------------


async def test_deploy_no_token_401_before_any_side_effect() -> None:
    store = _RecordingStore()
    app = create_app(store=store, settings=_privy_settings(), deploy_deps=_deploy_deps())
    try:
        async with _transport(app) as client:
            resp = await client.post("/agents/deploy", json=_VALID_DEPLOY)
        assert resp.status_code == 401, resp.text
        # AC-18: rejected BEFORE persistence — the first deploy side effect never fired.
        assert store.persist_calls == 0
        assert len(store._agent_instances) == 0
        # No background run task was launched either (no wallet/runtime side effect).
        assert len(getattr(app.state, "deploy_background_tasks", set())) == 0
    finally:
        await _drain(app)


async def test_deploy_bad_token_401_before_any_side_effect() -> None:
    store = _RecordingStore()
    app = create_app(store=store, settings=_privy_settings(), deploy_deps=_deploy_deps())
    forged = _sign(_OTHER_PRIV_PEM)  # valid shape, WRONG signing key
    try:
        async with _transport(app) as client:
            resp = await client.post(
                "/agents/deploy", json=_VALID_DEPLOY, headers={"Authorization": f"Bearer {forged}"}
            )
        assert resp.status_code == 401, resp.text
        assert store.persist_calls == 0
        assert len(store._agent_instances) == 0
        assert len(getattr(app.state, "deploy_background_tasks", set())) == 0
    finally:
        await _drain(app)


async def test_deploy_valid_principal_owner_derived_client_owner_ignored() -> None:
    store = _RecordingStore()
    app = create_app(store=store, settings=_privy_settings(), deploy_deps=_deploy_deps())
    token = _sign(sub=_REAL_DID)
    # The client ALSO forges an owner in the POST body — it MUST be ignored (server-derived only).
    forged_body = {**_VALID_DEPLOY, "owner": "did:privy:ATTACKER"}
    try:
        async with _transport(app) as client:
            resp = await client.post("/agents/deploy", json=forged_body, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Server-derived owner == the token's DID; the forged body value is IGNORED.
        assert body["owner"] == _REAL_DID
        assert body["owner"] != "did:privy:ATTACKER"
        assert store.persist_calls == 1
    finally:
        await _drain(app)


async def test_deploy_dev_mode_allows_without_token() -> None:
    """AUTH_MODE=dev is the local-dev bypass: deploy proceeds without a Privy token."""
    store = _RecordingStore()
    app = create_app(store=store, settings=_privy_settings(auth_mode="dev"), deploy_deps=_deploy_deps())
    try:
        async with _transport(app) as client:
            resp = await client.post("/agents/deploy", json=_VALID_DEPLOY)
        assert resp.status_code == 200, resp.text
        assert isinstance(resp.json()["owner"], str)
        assert store.persist_calls == 1
    finally:
        await _drain(app)


# ---------------------------------------------------------------------------
# Settings boundary — AUTH_MODE=dev is HARD-REFUSED in production (startup)
# ---------------------------------------------------------------------------


def test_settings_refuses_dev_auth_mode_in_production() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="production", auth_mode="dev")  # type: ignore[call-arg]


def test_settings_refuses_production_missing_privy_vars() -> None:
    with pytest.raises(ValidationError):
        Settings(  # type: ignore[call-arg]
            _env_file=None, app_env="production", auth_mode="privy", privy_app_id=None, privy_verification_key=None
        )


def test_settings_production_privy_configured_ok() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        app_env="production",
        auth_mode="privy",
        privy_app_id=_APP_ID,
        privy_verification_key=_PUB_PEM,
    )
    assert settings.auth_mode == "privy"
    assert settings.app_env == "production"


def test_settings_defaults_construct_offline() -> None:
    """Zero-env construction still works (dev default, no Privy creds required)."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.auth_mode == "dev"
    assert settings.app_env != "production"


# ---------------------------------------------------------------------------
# M1 — app_env normalization is FAIL-CLOSED (unknown/misspelled envs are production)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_env", ["prod", "PRODUCTION", "Production", "production ", "staging"])
def test_settings_refuses_dev_auth_mode_in_nonstandard_production(bad_env: str) -> None:
    """Any env that is not an EXPLICIT non-production value must refuse the dev bypass (fail-closed)."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env=bad_env, auth_mode="dev")  # type: ignore[call-arg]


@pytest.mark.parametrize("dev_env", ["development", "dev", "test", "local"])
def test_settings_dev_auth_mode_allowed_in_explicit_nonproduction(dev_env: str) -> None:
    """Explicit non-production values still permit the dev bypass (positive control)."""
    settings = Settings(_env_file=None, app_env=dev_env, auth_mode="dev")  # type: ignore[call-arg]
    assert settings.auth_mode == "dev"


def test_settings_prod_alias_requires_privy_vars() -> None:
    """A misspelled ``prod`` env is treated as production and still requires the Privy verifier."""
    with pytest.raises(ValidationError):
        Settings(  # type: ignore[call-arg]
            _env_file=None, app_env="prod", auth_mode="privy", privy_app_id=None, privy_verification_key=None
        )
