"""II-5 RED suite — quoteguard-mm deploy family -> AgentOS adapter (backend slice).

OFFLINE, ZERO wire primitives. Every test drives ``POST /agents/deploy`` (and, for the shared-service
proof, the II-4 wrapper route) against `InMemoryStore` + an injected server-side offline tape/proposer
seam (`DeployDeps.mm_tape_resolver` / `.mm_proposer` / `.mm_seed_state`) — never a request field. Each
test maps to one of Codex's II-5 required design items (the requirement number is named in the
docstring).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from tests.mm_strategy_ablation_harness import load_tape
from tests.test_mm_strategy_integration import _warm_seed_state
from veridex.api.deploy import DeployDeps, register_deploy_routes
from veridex.config import Settings
from veridex.deploy.instance import DeployStatus
from veridex.deploy.preflight import DeployConfig, MakerDeployConfig
from veridex.mm_strategy import session_factory as sf
from veridex.mm_strategy.offline_proposer import OfflineRecordingProposer
from veridex.runtime import agentos_service as svc
from veridex.runtime.mm_agent_adapter import VeridexAgentAdapter
from veridex.runtime.runtime_events import RuntimeEvent, RuntimeEventType
from veridex.store import InMemoryStore

# --- offline tape fixture (reuses the II-2-proven pinned "healthy" tape) ----------------------

_TAPE_REF = "healthy-demo"


def _mm_tape() -> sf.MakerReplayTape:
    loaded = load_tape("healthy")
    return sf.MakerReplayTape(
        tape_ref=_TAPE_REF,
        identity=loaded.identity,
        venue_market_ref=loaded.venue_market_ref,
        events=loaded.events,
        content_hash=sf.compute_tape_content_hash(loaded.events),
    )


def _resolver(tape: sf.MakerReplayTape | None = None):
    t = tape if tape is not None else _mm_tape()

    def _resolve(tape_ref: str) -> sf.MakerReplayTape:
        assert tape_ref == _TAPE_REF
        return t

    return _resolve


def _mm_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "template_id": "quoteguard-mm-template",
        "agent_id": "studio-mm-agent",
        "strategy": "quoteguard-mm",
        "source_mode": "replay",
        "execution_mode": "dry_run",
        "market_allowlist": ["0xcondition"],
        "venue_allowlist": ["poly"],
        "min_edge_bps": 10,
        "max_stake": 5.0,
        "mm": {"tape_ref": _TAPE_REF, "guard_enabled": False},
    }
    payload.update(overrides)
    return payload


def _directional_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "template_id": "sharp-momentum-v2",
        "agent_id": "studio-agent",
        "strategy": "momentum-sharp",
        "source_mode": "replay",
        "execution_mode": "paper",
        "market_allowlist": ["OU|FT|2.5"],
        "venue_allowlist": ["fake"],
        "min_edge_bps": 0,
        "max_stake": 0.0,
        "window_id": "w1",
        "fixture_id": 1,
        "end_rule": "pre_match",
    }
    payload.update(overrides)
    return payload


def _app_and_deps(**dep_overrides: Any) -> tuple[FastAPI, InMemoryStore]:
    store = InMemoryStore()
    defaults: dict[str, Any] = {
        "anchor_fn": None,
        "mm_tape_resolver": _resolver(),
        "mm_proposer": OfflineRecordingProposer(),
        "mm_seed_state": _warm_seed_state(),
    }
    defaults.update(dep_overrides)
    deps = DeployDeps(**defaults)
    app = FastAPI()
    register_deploy_routes(app, store=store, settings=Settings(AUTH_MODE="dev"), deploy_deps=deps)
    return app, store


def _transport(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _drain(app: FastAPI) -> None:
    tasks = list(getattr(app.state, "deploy_background_tasks", set()))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _receipt_events(events: list[RuntimeEvent]) -> list[RuntimeEvent]:
    """The dry-run BRIDGED-RECEIPT OPS events (``_receipt_event`` — carries per-leg ``legs``).

    Distinct from a TOOL_CALL-typed synthetic-inventory projection event (which shares the type but
    carries a flat ``{telemetry, inventory_source, synthetic, ...}`` payload, never ``legs``).
    """
    return [e for e in events if e.type == RuntimeEventType.TOOL_CALL and "legs" in e.payload]


# =====================================================================================
# AC-2 — full backend flow: deploy -> attempt -> instance -> adapter run (shared service,
# NOT standalone_run) -> >=1 OPS event AND (dry_run) >=1 dry-run receipt.
# =====================================================================================


async def test_ac2_replay_dry_run_produces_ops_and_dry_run_receipt() -> None:
    events: list[RuntimeEvent] = []
    app, store = _app_and_deps()
    # register a second time isn't needed; use a custom sink via a fresh app registration.
    store2 = InMemoryStore()
    app2 = FastAPI()
    deps = DeployDeps(
        anchor_fn=None,
        mm_tape_resolver=_resolver(),
        mm_proposer=OfflineRecordingProposer(),
        mm_seed_state=_warm_seed_state(),
    )
    register_deploy_routes(app2, store=store2, settings=Settings(AUTH_MODE="dev"), deploy_deps=deps, runtime_event_sink=events.append)
    async with _transport(app2) as client:
        resp = await client.post("/agents/deploy", json=_mm_payload())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    await _drain(app2)

    instance = await store2.get_agent_instance(body["instance_id"])
    assert instance.status == DeployStatus.SEALED, instance.last_failure_reason

    assert events, "expected OPS telemetry"
    receipts = _receipt_events(events)
    assert receipts, "replay+dry_run must produce >= 1 dry-run receipt (TOOL_CALL) event"
    assert any(leg.get("attempted") for e in receipts for leg in e.payload.get("legs", []))
    await _drain(app)  # unused app cleanup (no tasks)


# =====================================================================================
# req 2 — ONE authoritative run_id across instance/lease/runtime_handle/RunContext/OPS/response.
# =====================================================================================


async def test_one_authoritative_run_id_across_every_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_ctx_run_ids: list[str] = []
    real_reconstruct = sf.reconstruct_mm_session

    def _spy(instance: Any, ctx: Any, **kwargs: Any) -> Any:
        captured_ctx_run_ids.append(ctx.run_id)
        return real_reconstruct(instance, ctx, **kwargs)

    import veridex.api.deploy as deploy_mod

    monkeypatch.setattr(deploy_mod, "reconstruct_mm_session", _spy)

    events: list[RuntimeEvent] = []
    store = InMemoryStore()
    app = FastAPI()
    deps = DeployDeps(
        anchor_fn=None, mm_tape_resolver=_resolver(), mm_proposer=OfflineRecordingProposer(), mm_seed_state=_warm_seed_state()
    )
    register_deploy_routes(app, store=store, settings=Settings(AUTH_MODE="dev"), deploy_deps=deps, runtime_event_sink=events.append)
    async with _transport(app) as client:
        resp = await client.post("/agents/deploy", json=_mm_payload())
    body = resp.json()
    await _drain(app)

    instance = await store.get_agent_instance(body["instance_id"])
    lease = await store.get_instance_lease(body["instance_id"])
    assert lease is not None

    response_run_id = body["run_id"]
    assert instance.run_id == response_run_id
    assert lease.run_id == response_run_id
    assert instance.runtime_handle is not None
    assert instance.runtime_handle["run_id"] == response_run_id
    assert captured_ctx_run_ids and captured_ctx_run_ids[0] == response_run_id
    ops_run_ids = {e.run_id for e in events if e.run_id is not None}
    assert ops_run_ids == {response_run_id}, ops_run_ids


# =====================================================================================
# req 4 — the explicit mode matrix.
# =====================================================================================


async def test_replay_paper_produces_ops_no_receipt() -> None:
    events: list[RuntimeEvent] = []
    store = InMemoryStore()
    app = FastAPI()
    deps = DeployDeps(
        anchor_fn=None, mm_tape_resolver=_resolver(), mm_proposer=OfflineRecordingProposer(), mm_seed_state=_warm_seed_state()
    )
    register_deploy_routes(app, store=store, settings=Settings(AUTH_MODE="dev"), deploy_deps=deps, runtime_event_sink=events.append)
    async with _transport(app) as client:
        resp = await client.post("/agents/deploy", json=_mm_payload(execution_mode="paper"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    await _drain(app)

    instance = await store.get_agent_instance(body["instance_id"])
    assert instance.status == DeployStatus.SEALED, instance.last_failure_reason
    assert events, "replay+paper must still emit OPS telemetry"
    assert not _receipt_events(events), "replay+paper (mode='replay') must NOT produce a receipt"


@pytest.mark.parametrize("execution_mode", ["live_guarded"])
async def test_live_guarded_rejected_before_any_side_effect(execution_mode: str) -> None:
    app, store = _app_and_deps()
    async with _transport(app) as client:
        resp = await client.post("/agents/deploy", json=_mm_payload(execution_mode=execution_mode))
    assert resp.status_code == 422, resp.text
    assert (await store.list_agent_instances()) == []
    lease = await store.get_instance_lease("inst-nonexistent")
    assert lease is None


@pytest.mark.parametrize("source_mode,execution_mode", [("live", "paper"), ("live", "dry_run")])
async def test_live_source_rejected_before_any_side_effect(source_mode: str, execution_mode: str) -> None:
    app, store = _app_and_deps()
    async with _transport(app) as client:
        resp = await client.post(
            "/agents/deploy", json=_mm_payload(source_mode=source_mode, execution_mode=execution_mode)
        )
    assert resp.status_code == 422, resp.text
    assert (await store.list_agent_instances()) == []


# =====================================================================================
# req 5 — family-driven, fail-closed dispatch.
# =====================================================================================


async def test_directional_strategy_never_touches_mm_session_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    import veridex.api.deploy as deploy_mod

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("directional deploy must NEVER call reconstruct_mm_session")

    monkeypatch.setattr(deploy_mod, "reconstruct_mm_session", _boom)
    app, store = _app_and_deps()
    async with _transport(app) as client:
        resp = await client.post("/agents/deploy", json=_directional_payload())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    await _drain(app)
    instance = await store.get_agent_instance(body["instance_id"])
    assert instance.status == DeployStatus.SEALED, instance.last_failure_reason


async def test_unknown_strategy_rejected_by_pydantic_422() -> None:
    app, store = _app_and_deps()
    async with _transport(app) as client:
        resp = await client.post("/agents/deploy", json=_mm_payload(strategy="totally-unknown-family"))
    assert resp.status_code == 422, resp.text
    assert (await store.list_agent_instances()) == []


async def test_mm_family_missing_mm_block_fails_preflight_422() -> None:
    app, store = _app_and_deps()
    payload = _mm_payload()
    del payload["mm"]
    async with _transport(app) as client:
        resp = await client.post("/agents/deploy", json=payload)
    assert resp.status_code == 422, resp.text
    assert "mm_family" in resp.json()["detail"]["failed_checks"]
    assert (await store.list_agent_instances()) == []


async def test_mm_construction_failure_marks_failed_not_legacy_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    import veridex.api.deploy as deploy_mod

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("quoteguard-mm must NEVER call build_agent (legacy runner)")

    monkeypatch.setattr(deploy_mod, "build_agent", _boom, raising=False)

    def _bad_resolver(tape_ref: str) -> sf.MakerReplayTape:
        raise sf.MMTapeNotFoundError("no tape banked for the test")

    app, store = _app_and_deps(mm_tape_resolver=_bad_resolver)
    async with _transport(app) as client:
        resp = await client.post("/agents/deploy", json=_mm_payload())
    assert resp.status_code == 200, resp.text  # preflight passes; the failure is a RUNTIME construction failure
    body = resp.json()
    await _drain(app)
    instance = await store.get_agent_instance(body["instance_id"])
    assert instance.status == DeployStatus.FAILED
    assert instance.last_failure_reason is not None


# =====================================================================================
# req 3 — authority reconstruction: a hash mismatch fails CLOSED before the adapter/receipt.
# =====================================================================================


async def test_tape_content_hash_mismatch_fails_closed_before_proposer() -> None:
    tampered = sf.MakerReplayTape(
        tape_ref=_TAPE_REF,
        identity=_mm_tape().identity,
        venue_market_ref=_mm_tape().venue_market_ref,
        events=_mm_tape().events,
        content_hash="0" * 64,  # WRONG — never matches a recomputation of the real events
    )
    proposer = OfflineRecordingProposer()
    app, store = _app_and_deps(mm_tape_resolver=_resolver(tampered), mm_proposer=proposer)
    async with _transport(app) as client:
        resp = await client.post("/agents/deploy", json=_mm_payload())
    body = resp.json()
    await _drain(app)
    instance = await store.get_agent_instance(body["instance_id"])
    assert instance.status == DeployStatus.FAILED
    assert proposer.calls == [], "a tampered tape must fail BEFORE the proposer is ever reached"


def test_directional_envelope_is_not_accepted_as_maker_envelope() -> None:
    mm = MakerDeployConfig(tape_ref=_TAPE_REF, guard_enabled=False, max_orders_per_run=5)
    config = DeployConfig(
        template_id="t", agent_id="a", strategy="quoteguard-mm", source_mode="replay",
        execution_mode="dry_run", market_allowlist=["0xcondition"], venue_allowlist=["poly"],
        min_edge_bps=10, max_stake=5.0, mm=mm,
    )
    directional_envelope = config.to_policy_envelope()
    maker_envelope = sf.build_maker_policy_envelope(config, mm)
    # The directional envelope is a conservative SINGLE-order envelope (max_orders_per_run == 1) —
    # never a valid maker envelope for a >1 order-cap deploy.
    assert directional_envelope.max_orders_per_run == 1
    assert maker_envelope.max_orders_per_run == 5
    assert directional_envelope.policy_hash() != maker_envelope.policy_hash()


def test_manifest_and_envelope_are_bound_by_identity_into_facade_deps() -> None:
    """The II-1 composition assert (`manifest is facade_deps.manifest`) holds through the factory."""
    mm = MakerDeployConfig(tape_ref=_TAPE_REF, guard_enabled=False)
    config = DeployConfig(
        template_id="t", agent_id="a", strategy="quoteguard-mm", source_mode="replay",
        execution_mode="dry_run", market_allowlist=["0xcondition"], venue_allowlist=["poly"],
        min_edge_bps=10, max_stake=5.0, mm=mm,
    )
    envelope = sf.build_maker_policy_envelope(config, mm)
    run_id = "run_identitytest"
    now = datetime.now(tz=UTC).isoformat()
    from veridex.deploy.instance import AgentInstance

    instance = AgentInstance(
        instance_id="inst-x", template_id="t", agent_id="a",
        submitted_config=config.model_dump(mode="json"), effective_config=mm.model_dump(mode="json"),
        config_hash=config.config_hash(), policy_hash=envelope.policy_hash(),
        source_mode="replay", execution_mode="dry_run", market_allowlist=["0xcondition"],
        venue_allowlist=["poly"], run_id=run_id, status=DeployStatus.PENDING,
        operator_id="did:privy:dev", created_at=now, updated_at=now,
    )
    from veridex.runtime.mm_agent_adapter import RunContext

    ctx = RunContext(run_id=run_id, session_id="sess-x", runtime_agent_id="ra-x", owner_did="did:privy:dev")
    instance_cfg, _tape, mode, _guard = sf.reconstruct_mm_session(
        instance, ctx, tape_resolver=_resolver(), seed_state=_warm_seed_state(), session_dir=Path("/tmp")
    )
    assert mode == "replay_dry_run"
    assert instance_cfg.manifest is instance_cfg.facade_deps.manifest
    assert instance_cfg.envelope is instance_cfg.facade_deps.envelope


# =====================================================================================
# Idempotent retry — a retried deploy reconciles to the SAME instance, never a second lease/run.
# =====================================================================================


async def test_idempotent_retry_reconciles_same_instance_never_a_second_run(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    real_reconstruct = sf.reconstruct_mm_session

    def _counting(instance: Any, ctx: Any, **kwargs: Any) -> Any:
        calls.append(ctx.run_id)
        return real_reconstruct(instance, ctx, **kwargs)

    import veridex.api.deploy as deploy_mod

    monkeypatch.setattr(deploy_mod, "reconstruct_mm_session", _counting)

    app, store = _app_and_deps()
    headers = {"Idempotency-Key": "idem-mm-1"}
    async with _transport(app) as client:
        first = await client.post("/agents/deploy", json=_mm_payload(), headers=headers)
        await _drain(app)
        second = await client.post("/agents/deploy", json=_mm_payload(), headers=headers)
        await _drain(app)

    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["instance_id"] == second.json()["instance_id"]
    assert first.json()["run_id"] == second.json()["run_id"]
    assert len(calls) == 1, "the shared session factory (and thus the shared start service) must run exactly once"


# =====================================================================================
# req 1 — the shared start service: BOTH the wrapper route and the deploy dispatch call it;
# the deploy path makes NO in-process HTTP call to the wrapper (the wrapper route does not
# even exist on the deploy-only app).
# =====================================================================================


async def test_deploy_path_has_no_wrapper_route_mounted() -> None:
    """Structural proof: the deploy app never mounts the wrapper route — no in-process HTTP is possible."""
    app, _store = _app_and_deps()
    async with _transport(app) as client:
        resp = await client.post("/agents/instances/inst-x/runs", json={})
    assert resp.status_code == 404


async def test_wrapper_and_deploy_both_call_the_shared_service(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    real_start = svc.start_owned_instance_run

    async def _spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs.get("run_id", args[2] if len(args) > 2 else "?"))
        return await real_start(*args, **kwargs)

    monkeypatch.setattr(svc, "start_owned_instance_run", _spy)

    # (a) drive the deploy path.
    app, store = _app_and_deps()
    async with _transport(app) as client:
        resp = await client.post("/agents/deploy", json=_mm_payload())
    await _drain(app)
    assert resp.status_code == 200, resp.text

    # (b) drive the II-4 wrapper path (a completely separate adapter/app).
    from agno.db.in_memory import InMemoryDb

    from veridex.api.auth_privy import PrivyPrincipal

    def _fake_verifier(token: str, *, app_id: str | None, verification_key: str | None) -> PrivyPrincipal:
        return PrivyPrincipal(did="did:privy:wrapper-owner")

    async def _instant_driver(ctx: Any, stop: Any, sink: Any) -> Any:
        class _S:
            terminal_reason = "completed"

        return _S()

    wrapper_store = InMemoryStore()
    now = datetime.now(tz=UTC).isoformat()
    from veridex.deploy.instance import AgentInstance

    await wrapper_store.persist_agent_instance(
        AgentInstance(
            instance_id="inst-w", template_id="t", agent_id="a", submitted_config={}, effective_config={},
            config_hash="c" * 8, policy_hash="p" * 8, source_mode="replay", execution_mode="paper",
            run_id="run-seed", status=DeployStatus.PENDING, operator_id="did:privy:wrapper-owner",
            created_at=now, updated_at=now,
        )
    )
    adapter = VeridexAgentAdapter(run_driver=_instant_driver)
    guard = svc.build_agentos_app(
        store=wrapper_store, settings=Settings(AUTH_MODE="privy", PRIVY_APP_ID="app", PRIVY_VERIFICATION_KEY="key"),
        adapter=adapter, owner_db=InMemoryDb(), verifier=_fake_verifier, enforce_contract=False,
    )
    async with httpx.AsyncClient(transport=ASGITransport(app=guard), base_url="http://test") as client:
        wrapper_resp = await client.post(
            "/agents/instances/inst-w/runs", headers={"Authorization": "Bearer tok"}, json={}
        )
    assert wrapper_resp.status_code == 200, wrapper_resp.text

    assert len(calls) == 2, "both the deploy dispatch and the wrapper route must call the shared service"
