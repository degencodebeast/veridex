"""Security matrix for the OWNER-SCOPED async guard OFF/ON A/B route.

``GET /maker/live-ab/{instance_id}`` is TRUST-CRITICAL: ownership is derived SERVER-SIDE from the
persisted :class:`AgentInstance` (the client supplies NO run_id / tape / config / hash / decisions),
the A/B is reconstructed ONLY from server-owned state and run OFFLINE (``run_guard_ablation`` on the
production catalog tape with an isolated no-op OPS sink — never the proposer/wallet/R4-A/venue), and
the read is a pure BEHAVIOR projection that never enters a leaderboard, emits a rank/toxicity/PnL/
fill/edge, produces an OPS receipt, or touches the sealed maker arena result.

Written RED-first (TDD). The matrix: owner 200 (+ frozen-input reproducibility), anonymous 401,
non-owner 403, unknown 404, directional (non-MM) 404 (fail closed, never reconstructs), no
rank/PnL/fill/venue-order field + no new OPS rows, and per-key single-flight under concurrency.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from httpx import ASGITransport

from veridex.api.auth_privy import make_require_principal
from veridex.api.deploy import DeployDeps
from veridex.api.maker_router import register_maker_routes
from veridex.api.router import create_app
from veridex.config import Settings
from veridex.deploy.instance import AgentInstance, DeployStatus
from veridex.mm_strategy import pmxt_tape
from veridex.mm_strategy import session_factory as sf
from veridex.mm_strategy.composition import run_guard_ablation
from veridex.mm_strategy.offline_proposer import OfflineRecordingProposer
from veridex.runtime.mm_agent_adapter import RunContext
from veridex.store import InMemoryStore

# --- offline Privy ES256 token helpers (mirror tests/test_runtime_events_durable.py) -------------

_APP_ID = "test-privy-app-id"
_DID_ALICE = "did:privy:ALICE"
_DID_BOB = "did:privy:BOB"


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


def _sign(*, sub: str) -> str:
    now = int(time.time())
    claims = {"sub": sub, "aud": _APP_ID, "iss": "privy.io", "iat": now, "exp": now + 3600, "sid": "s"}
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


def _offline_deps() -> DeployDeps:
    """Offline MM deploy deps: resolve THROUGH the production catalog, dry-run proposer, no wire/anchor."""
    return DeployDeps(
        anchor_fn=None,
        mm_tape_resolver=None,
        mm_proposer=OfflineRecordingProposer(),
        mm_seed_state=None,
    )


_CANONICAL_MM_PAYLOAD_PATH = (
    Path(pmxt_tape.__file__).resolve().parents[2]
    / "contracts"
    / "fixtures"
    / "studio_mm_deploy_payload.json"
)


def _mm_payload() -> dict[str, Any]:
    """The canonical Studio MM deploy payload the real UI emits (shared committed fixture)."""
    payload: dict[str, Any] = json.loads(_CANONICAL_MM_PAYLOAD_PATH.read_text())
    assert payload["mm"]["tape_ref"] == pmxt_tape.TAPE_REF
    return payload


async def _drain(app: FastAPI) -> None:
    tasks = list(getattr(app.state, "deploy_background_tasks", set()))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _deploy_sealed_mm(store: InMemoryStore, app: FastAPI, client: httpx.AsyncClient, did: str) -> str:
    """Deploy the canonical maker instance owned by ``did`` and drain it to SEALED; return instance_id."""
    resp = await client.post("/agents/deploy", json=_mm_payload(), headers=_bearer(did))
    assert resp.status_code == 200, resp.text
    instance_id = resp.json()["instance_id"]
    await _drain(app)
    inst = await store.get_agent_instance(instance_id)
    assert inst.status == DeployStatus.SEALED, inst.last_failure_reason
    return instance_id


def _all_keys(value: Any) -> set[str]:
    """Every (nested) mapping key in a JSON structure — for the no-rank/no-PnL honesty scan."""
    keys: set[str] = set()
    if isinstance(value, dict):
        for k, v in value.items():
            keys.add(k)
            keys |= _all_keys(v)
    elif isinstance(value, list):
        for v in value:
            keys |= _all_keys(v)
    return keys


# --- 1) owner gets 200 with a reproducible same-strategy/same-tape ablation ----------------------


async def test_owner_gets_200_reproducible_ablation() -> None:
    store = InMemoryStore()
    app = create_app(store=store, settings=_privy_settings(), deploy_deps=_offline_deps())
    async with _transport(app) as client:
        instance_id = await _deploy_sealed_mm(store, app, client, _DID_ALICE)

        r1 = await client.get(f"/maker/live-ab/{instance_id}", headers=_bearer(_DID_ALICE))
        assert r1.status_code == 200, r1.text
        b1 = r1.json()
        assert b1["is_ablation"] is True
        assert b1["panel"] == "guard_on_off_ablation"
        # both arms fold the genuine 18209181 tape -> 300 decisions each; the guard diverges.
        assert len(b1["guard_off"]["decisions"]) == 300
        assert len(b1["guard_on"]["decisions"]) == 300
        assert b1["diverges"] is True
        assert b1["divergent_frame_indices"], "expected a non-empty guard divergence on the real tape"

        # Frozen inputs -> a second read returns the byte-identical projection (memoized, deterministic).
        r2 = await client.get(f"/maker/live-ab/{instance_id}", headers=_bearer(_DID_ALICE))
        assert r2.status_code == 200, r2.text
        assert r2.json() == b1


# --- 2) anonymous -> 401 -------------------------------------------------------------------------


async def test_anonymous_401() -> None:
    store = InMemoryStore()
    app = create_app(store=store, settings=_privy_settings(), deploy_deps=_offline_deps())
    async with _transport(app) as client:
        instance_id = await _deploy_sealed_mm(store, app, client, _DID_ALICE)
        resp = await client.get(f"/maker/live-ab/{instance_id}")  # no Authorization header
        assert resp.status_code == 401, resp.text


# --- 3) authenticated non-owner -> 403 -----------------------------------------------------------


async def test_non_owner_403() -> None:
    store = InMemoryStore()
    app = create_app(store=store, settings=_privy_settings(), deploy_deps=_offline_deps())
    async with _transport(app) as client:
        instance_id = await _deploy_sealed_mm(store, app, client, _DID_ALICE)
        resp = await client.get(f"/maker/live-ab/{instance_id}", headers=_bearer(_DID_BOB))
        assert resp.status_code == 403, resp.text
        assert instance_id not in resp.text  # no leak of the owned identity


# --- 4) unknown instance -> 404 ------------------------------------------------------------------


async def test_unknown_instance_404() -> None:
    store = InMemoryStore()
    app = create_app(store=store, settings=_privy_settings())
    async with _transport(app) as client:
        resp = await client.get("/maker/live-ab/inst_nope", headers=_bearer(_DID_ALICE))
        assert resp.status_code == 404, resp.text


# --- 5) directional (non-MM) instance -> 404, fail closed (never reconstructs) -------------------


async def test_directional_instance_404_never_reconstructs() -> None:
    store = InMemoryStore()
    # A directional instance owned by ALICE with an EMPTY effective_config: if the route ever tried to
    # reconstruct it the MM reconstruct would raise (409); a 404 proves it fails closed on the strategy
    # gate BEFORE any reconstruction.
    inst = AgentInstance(
        instance_id="inst_dir",
        template_id="det-drift",
        agent_id="det-agent",
        submitted_config={"strategy": "cumulative-drift"},
        effective_config={},
        config_hash="cfg-dir",
        policy_hash="pol-dir",
        source_mode="replay",
        execution_mode="paper",
        run_id="run-dir",
        operator_id=_DID_ALICE,
        status=DeployStatus.SEALED,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    await store.persist_agent_instance(inst)
    app = create_app(store=store, settings=_privy_settings())
    async with _transport(app) as client:
        resp = await client.get("/maker/live-ab/inst_dir", headers=_bearer(_DID_ALICE))
        assert resp.status_code == 404, resp.text


# --- 6) no rank/PnL/fill/venue-order field; the ablation produces NO durable OPS rows -------------


async def test_no_rank_or_pnl_fields_and_no_new_ops_rows() -> None:
    store = InMemoryStore()
    app = create_app(store=store, settings=_privy_settings(), deploy_deps=_offline_deps())
    async with _transport(app) as client:
        instance_id = await _deploy_sealed_mm(store, app, client, _DID_ALICE)
        inst = await store.get_agent_instance(instance_id)

        before = await store.list_runtime_events_for_run(inst.run_id)
        resp = await client.get(f"/maker/live-ab/{instance_id}", headers=_bearer(_DID_ALICE))
        assert resp.status_code == 200, resp.text
        after = await store.list_runtime_events_for_run(inst.run_id)
        # The read-only A/B runs on an isolated no-op sink: it emits NO durable OPS receipt row.
        assert len(after) == len(before)

        banned = ("rank", "toxicity", "pnl", "fill", "edge", "order_id", "venue")
        for key in _all_keys(resp.json()):
            assert not any(tok in key.lower() for tok in banned), key


# --- 7) concurrent duplicate reads single-flight (compute once, identical result) ----------------


async def test_concurrent_reads_single_flight(tmp_path: Path) -> None:
    store = InMemoryStore()
    app1 = create_app(store=store, settings=_privy_settings(), deploy_deps=_offline_deps())
    async with _transport(app1) as c1:
        instance_id = await _deploy_sealed_mm(store, app1, c1, _DID_ALICE)

    # Pre-compute ONE real ablation result from server-owned state to hand back from the stub.
    inst = await store.get_agent_instance(instance_id)
    ctx = RunContext(
        run_id=inst.run_id, session_id="t", runtime_agent_id="t", owner_did=inst.operator_id or ""
    )
    cfg, tape, mode, _guard = sf.reconstruct_mm_session(
        inst, ctx, tape_resolver=None, proposer=OfflineRecordingProposer(), session_dir=tmp_path
    )
    precomputed = await run_guard_ablation(cfg, tape, mode=mode, event_sink=lambda _e: None)

    calls = {"n": 0}

    async def _slow_compute(_instance: AgentInstance) -> Any:
        calls["n"] += 1
        await asyncio.sleep(0.05)  # widen the window so a second read overlaps the first
        return precomputed

    app2 = FastAPI()
    register_maker_routes(
        app2,
        store=store,
        require_principal=make_require_principal(_privy_settings()),
        ab_compute=_slow_compute,
    )
    async with _transport(app2) as c2:
        r1, r2 = await asyncio.gather(
            c2.get(f"/maker/live-ab/{instance_id}", headers=_bearer(_DID_ALICE)),
            c2.get(f"/maker/live-ab/{instance_id}", headers=_bearer(_DID_ALICE)),
        )
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.json() == r2.json()
    # Single-flight: the two overlapping duplicate reads launched exactly ONE computation.
    assert calls["n"] == 1


# --- MAJOR 1 (Codex) — the memo must NEVER launder fail-closed reconstruction-authority drift ------
# reconstruct_mm_session (the authority gate: config_hash / policy_hash / tape-content-hash re-verify)
# MUST run on EVERY request BEFORE the result cache, so a warm cache can never mask a later mutation.


async def test_warm_cache_then_policy_hash_drift_still_409() -> None:
    store = InMemoryStore()
    app = create_app(store=store, settings=_privy_settings(), deploy_deps=_offline_deps())
    async with _transport(app) as client:
        instance_id = await _deploy_sealed_mm(store, app, client, _DID_ALICE)
        # Warm the result cache with a valid read.
        warm = await client.get(f"/maker/live-ab/{instance_id}", headers=_bearer(_DID_ALICE))
        assert warm.status_code == 200, warm.text

        # Overwrite the persisted policy_hash -> reconstruction authority drift (policy_hash mismatch).
        inst = await store.get_agent_instance(instance_id)
        await store.persist_agent_instance(inst.model_copy(update={"policy_hash": "0" * 64}))

        # The warm cache must NOT launder the drift: the fresh reconstruct fails closed -> 409.
        resp = await client.get(f"/maker/live-ab/{instance_id}", headers=_bearer(_DID_ALICE))
        assert resp.status_code == 409, resp.text


async def test_warm_cache_then_tape_ref_drift_still_409() -> None:
    store = InMemoryStore()
    app = create_app(store=store, settings=_privy_settings(), deploy_deps=_offline_deps())
    async with _transport(app) as client:
        instance_id = await _deploy_sealed_mm(store, app, client, _DID_ALICE)
        warm = await client.get(f"/maker/live-ab/{instance_id}", headers=_bearer(_DID_ALICE))
        assert warm.status_code == 200, warm.text

        # Overwrite the persisted effective_config tape_ref with a no-such-tape ref.
        inst = await store.get_agent_instance(instance_id)
        bad_eff = {**inst.effective_config, "tape_ref": "no-such-tape-ref-xyz"}
        await store.persist_agent_instance(inst.model_copy(update={"effective_config": bad_eff}))

        # Fresh tape resolution fails closed (MMTapeNotFoundError) -> 409, never a cached 200.
        resp = await client.get(f"/maker/live-ab/{instance_id}", headers=_bearer(_DID_ALICE))
        assert resp.status_code == 409, resp.text


# --- MAJOR 2 (Codex) — the read is a NON-EXECUTING replay: the R4-A dry-run bridge is never driven --


async def test_ab_is_non_executing_replay_no_r4a_bridge(monkeypatch: Any) -> None:
    import veridex.mm_strategy.composition as comp

    calls: list[int] = []
    real = comp.execute_plan_bridged

    async def _spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return await real(*args, **kwargs)

    monkeypatch.setattr(comp, "execute_plan_bridged", _spy)

    store = InMemoryStore()
    app = create_app(store=store, settings=_privy_settings(), deploy_deps=_offline_deps())
    async with _transport(app) as client:
        instance_id = await _deploy_sealed_mm(store, app, client, _DID_ALICE)
        # Ignore the deploy's own sealed run — we only care about the A/B read below.
        calls.clear()

        resp = await client.get(f"/maker/live-ab/{instance_id}", headers=_bearer(_DID_ALICE))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # FORCED non-executing replay: the response mode is "replay", NOT the reconstructed
        # "replay_dry_run" — the guard decisions/divergence are preserved without any R4-A bridge.
        assert body["mode"] == "replay"
        assert len(body["guard_off"]["decisions"]) == 300
        assert len(body["guard_on"]["decisions"]) == 300
        assert body["diverges"] is True
        assert body["divergent_frame_indices"]
        # The behavior-only A/B NEVER drives the R4-A dry-run execution bridge.
        assert calls == [], "the read-only A/B must not invoke execute_plan_bridged (no R4-A)"
