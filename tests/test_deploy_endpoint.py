"""T21 — Studio deploy endpoint: async launch + pinned instance + one flow to proof.

``POST /agents/deploy`` pins the submitted config as an AgentInstance (config_hash + policy_hash
+ template + modes) and launches the run ASYNCHRONOUSLY through the SINGLE runner seam
(``standalone_run``): the response returns ``run_id`` WITHOUT awaiting the window seal, the
background task is tracked + cancellable on shutdown, and the deployed run's sealed window
verifies via the SAME ``/runs/{id}/verify`` path as an arena run.

All offline: an injected fake stream + fetch_updates + ``anchor_fn=None`` — ZERO network.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from veridex.api.deploy import DeployDeps
from veridex.api.router import create_app
from veridex.deploy.instance import DeployFailureReason, DeployStatus, deploy_status_values
from veridex.deploy.preflight import DeployConfig
from veridex.ingest.feed_health import FeedHealthReport
from veridex.ingest.marketstate import MarketState
from veridex.store import InMemoryStore

# --- offline live fixtures (mirror the T20 standalone-run live launch path) ------------------
_LIVE_KEY = "OU|FT|2.5"


def _live_market(over_bps: int) -> dict[str, Any]:
    return {"stable_prob_bps": {"over": over_bps}, "stable_price": {"over": 1.6, "under": 2.4}, "suspended": False}


def _live_ms(over_bps: int, *, tick_seq: int, ts: int, phase: int = 0) -> MarketState:
    return MarketState(
        fixture_id=1, tick_seq=tick_seq, ts=ts, phase=phase, markets={_LIVE_KEY: _live_market(over_bps)}, scores={}
    )


def _finite_ticks() -> list[MarketState]:
    return [
        _live_ms(5000, tick_seq=0, ts=1000, phase=0),
        _live_ms(5200, tick_seq=1, ts=1100, phase=0),
        _live_ms(5300, tick_seq=2, ts=9999, phase=1),  # kickoff → seals a pre_match window
    ]


async def _finite_stream(_config: DeployConfig) -> AsyncIterator[MarketState]:
    for item in _finite_ticks():
        yield item


async def _never_ending_stream(_config: DeployConfig) -> AsyncIterator[MarketState]:
    """Yield one pre-kickoff tick, then block forever — the seal never arrives on its own."""
    yield _live_ms(5000, tick_seq=0, ts=1000, phase=0)
    await asyncio.Event().wait()  # blocks until the task is cancelled on shutdown
    yield _live_ms(5200, tick_seq=1, ts=1100, phase=0)  # unreachable


async def _raising_stream(_config: DeployConfig) -> AsyncIterator[MarketState]:
    """Raise before any tick is fed — the live runner re-raises (nothing to seal)."""
    raise RuntimeError("stream boom pre-seal")
    yield _live_ms(5000, tick_seq=0, ts=1000, phase=0)  # unreachable


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


_VALID: dict[str, Any] = {
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


def _app_and_deps(stream: Any) -> tuple[Any, InMemoryStore]:
    store = InMemoryStore()
    deps = DeployDeps(
        feed_report=_healthy_live_feed(),
        market_resolved=True,
        stream_factory=stream,
        fetch_updates=_close,
        anchor_fn=None,  # offline: no on-chain anchor
    )
    return create_app(store=store, deploy_deps=deps), store


def _transport(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _drain(app: Any) -> None:
    tasks = list(getattr(app.state, "deploy_background_tasks", set()))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# ASYNC — response returns run_id WITHOUT awaiting the seal
# ---------------------------------------------------------------------------


async def test_deploy_returns_run_id_without_awaiting_seal() -> None:
    app, _store = _app_and_deps(_never_ending_stream)
    try:
        async with _transport(app) as client:
            resp = await client.post("/agents/deploy", json=_VALID)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["run_id"]
        assert len(body["config_hash"]) == 64
        assert len(body["policy_hash"]) == 64
        assert body["instance_id"].startswith("inst_")

        # The run was launched but its window is still open (the injected stream never ends),
        # proving the response did NOT block on the seal.
        tasks = getattr(app.state, "deploy_background_tasks", set())
        assert len(tasks) == 1
        assert not next(iter(tasks)).done()
    finally:
        for task in list(getattr(app.state, "deploy_background_tasks", set())):
            task.cancel()
        await _drain(app)


async def test_deploy_background_task_is_tracked_and_cancellable() -> None:
    app, _store = _app_and_deps(_never_ending_stream)
    async with _transport(app) as client:
        await client.post("/agents/deploy", json=_VALID)
    tasks = list(getattr(app.state, "deploy_background_tasks", set()))
    assert len(tasks) == 1
    task = tasks[0]
    task.cancel()
    await _drain(app)
    assert task.cancelled() or task.done()


# ---------------------------------------------------------------------------
# PINNED INSTANCE — config_hash + policy_hash + template + modes
# ---------------------------------------------------------------------------


async def test_deploy_pins_the_instance() -> None:
    app, store = _app_and_deps(_never_ending_stream)
    try:
        async with _transport(app) as client:
            resp = await client.post("/agents/deploy", json=_VALID)
        body = resp.json()
        # The pinned instance is the DURABLE Store record (source of truth) — NOT an app.state entry.
        inst = await store.get_agent_instance(body["instance_id"])
        assert inst.config_hash == body["config_hash"]
        assert inst.policy_hash == body["policy_hash"]
        assert inst.template_id == "sharp-momentum-v2"
        assert inst.source_mode == "live"
        assert inst.execution_mode == "paper"
        assert inst.market_allowlist == [_LIVE_KEY]
        assert inst.venue_allowlist == ["fake"]
        assert inst.run_id == body["run_id"]
        # The pinned config_hash is exactly the submitted config's canonical hash.
        assert inst.config_hash == DeployConfig(**_VALID).config_hash()
    finally:
        for task in list(getattr(app.state, "deploy_background_tasks", set())):
            task.cancel()
        await _drain(app)


# ---------------------------------------------------------------------------
# DURABLE AGENT-INSTANCE — Store/Postgres-backed source of truth (REQ-2D-701A / AC-2D-701A)
# The deployed instance is a PERSISTED record, not an app.state-only entry: it survives an
# app.state clear (loadable from the Store), preflight failure pins NO row + NO run, and the
# background seal/failure durably updates the STORED status.
# ---------------------------------------------------------------------------


async def test_deployed_instance_is_durable_through_the_store_after_app_state_cleared() -> None:
    # The STRONGEST durability test: a successful deploy → the AgentInstance loads through the Store
    # even after every process-local app.state deploy registry is cleared. app.state carries ONLY the
    # live background task handle (cancellation bookkeeping) — never the durable instance record.
    app, store = _app_and_deps(_never_ending_stream)
    try:
        async with _transport(app) as client:
            resp = await client.post("/agents/deploy", json=_VALID)
        body = resp.json()
        instance_id = body["instance_id"]

        # app.state holds NO instance registry — the source of truth is the Store, not memory.
        assert getattr(app.state, "deploy_instances", None) is None
        # Belt-and-braces: even if a future refactor caches instances in app.state, prove the record
        # does not depend on it — wipe any such cache before loading.
        app.state.deploy_instances = {}

        # The record survives in the Store. For Postgres this is a FRESH AsyncConnection to the same
        # DB; for InMemoryStore the store object IS the durable backing (independent of app.state).
        loaded = await store.get_agent_instance(instance_id)
        assert loaded.instance_id == instance_id
        assert loaded.config_hash == body["config_hash"]
        assert loaded.policy_hash == body["policy_hash"]
        assert loaded.run_id == body["run_id"]
        assert loaded.template_id == "sharp-momentum-v2"
        assert loaded.agent_id == "studio-agent"
        assert loaded.market_allowlist == [_LIVE_KEY]
        assert loaded.venue_allowlist == ["fake"]
        # The pinned submitted + effective config snapshots round-trip through the Store.
        assert loaded.submitted_config["agent_id"] == "studio-agent"
        assert loaded.effective_config["strategy"] == "momentum-sharp"
        assert loaded.created_at
        assert loaded.updated_at
    finally:
        for task in list(getattr(app.state, "deploy_background_tasks", set())):
            task.cancel()
        await _drain(app)


async def test_launched_instance_carries_the_named_preflight_check_audit() -> None:
    # Option-1 audit trail: a LAUNCHED instance durably carries the named preflight verdicts that
    # GATED it (each check's name + pass/fail + reason), loadable from a fresh Store and linked to
    # the run_id — durable "why did this agent launch?" traceability beyond just last_failure_reason.
    app, store = _app_and_deps(_never_ending_stream)
    try:
        async with _transport(app) as client:
            resp = await client.post("/agents/deploy", json=_VALID)
        body = resp.json()

        loaded = await store.get_agent_instance(body["instance_id"])
        # The full named preflight set is attached (config + feed_health + market_mapped + policy).
        by_name = {c.name: c for c in loaded.preflight_checks}
        assert set(by_name) == {"config", "feed_health", "market_mapped", "policy_limits"}
        # A launched instance's gating checks are all passing/not-applicable — never a hard fail.
        assert all(c.ok is not False for c in loaded.preflight_checks)
        # Each verdict carries its human-readable reason (the "why"), not just a bare boolean.
        assert all(c.detail for c in loaded.preflight_checks)
        # The audit is linked to the launched run.
        assert loaded.run_id == body["run_id"]
    finally:
        for task in list(getattr(app.state, "deploy_background_tasks", set())):
            task.cancel()
        await _drain(app)


async def test_preflight_failure_persists_no_instance_and_launches_no_run() -> None:
    # Persist-then-launch ordering: a FAILING preflight pins NO AgentInstance row AND starts NO run.
    # (Default app + a LIVE deploy with no live feed wired → the honest feed_health 422.)
    store = InMemoryStore()
    app = create_app(store=store)
    async with _transport(app) as client:
        resp = await client.post("/agents/deploy", json={**_VALID, "source_mode": "live"})
    assert resp.status_code == 422, resp.text
    assert not getattr(app.state, "deploy_background_tasks", set())  # fail-closed: no run launched
    # No AgentInstance row was persisted — query the store → absent (the persist is gated behind
    # preflight success).
    assert store._agent_instances == {}  # noqa: SLF001 (white-box durability assertion)


async def test_background_seal_updates_stored_instance_status() -> None:
    # After the background run seals cleanly, the STORED instance's status is durably ``sealed``.
    app, store = _app_and_deps(_finite_stream)
    async with _transport(app) as client:
        resp = await client.post("/agents/deploy", json=_VALID)
        assert resp.status_code == 200, resp.text
        instance_id = resp.json()["instance_id"]
        await _drain(app)  # drive the background run to its natural seal

    loaded = await store.get_agent_instance(instance_id)
    assert loaded.status == DeployStatus.SEALED
    assert loaded.last_failure_reason is None


async def test_background_failure_updates_stored_instance_status_and_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # After the background run FAILS pre-seal, the STORED instance is durably ``failed`` and carries
    # a bounded, honest ``last_failure_reason`` (never a raw framework trace dump).
    store = InMemoryStore()
    deps = DeployDeps(
        feed_report=_healthy_live_feed(),
        market_resolved=True,
        stream_factory=_raising_stream,
        fetch_updates=_close,
        anchor_fn=None,
    )
    app = create_app(store=store, deploy_deps=deps)
    with caplog.at_level(logging.ERROR, logger="veridex.api.deploy"):
        async with _transport(app) as client:
            resp = await client.post("/agents/deploy", json=_VALID)
            assert resp.status_code == 200, resp.text
            instance_id = resp.json()["instance_id"]
            await _drain(app)
            await asyncio.sleep(0)  # let the done-callback (call_soon) run

    loaded = await store.get_agent_instance(instance_id)
    assert loaded.status == DeployStatus.FAILED
    # The durable reason is a CONTROLLED taxonomy value (seal path failed) — NOT a raw trace.
    assert loaded.last_failure_reason == DeployFailureReason.SEAL_FAILED
    # The raw framework message never leaks into the persisted record...
    assert "stream boom pre-seal" not in (loaded.last_failure_reason.value if loaded.last_failure_reason else "")
    # ...it is surfaced on the SERVER LOG only (raw diagnostics — including the traceback — stay in
    # the logs via exc_info, never in the DB field).
    assert "stream boom pre-seal" in caplog.text


async def test_deploy_response_is_narrow() -> None:
    # The public deploy response exposes ONLY the narrow deployment identity/status fields — no
    # secrets, no private runtime handles, no asyncio.Task object, no raw framework trace shape.
    app, _store = _app_and_deps(_never_ending_stream)
    try:
        async with _transport(app) as client:
            resp = await client.post("/agents/deploy", json=_VALID)
        body = resp.json()
        assert set(body.keys()) == {
            "instance_id",
            "config_hash",
            "policy_hash",
            "run_id",
            "owner",
            "replay_binding",
        }
        # Every exposed value is a plain string identity/hash, except ``replay_binding`` — the R-4
        # frozen tape identity ({pack_id, fixture_id, content_hash}, or None for this live deploy),
        # which is itself a small identity dict, never a secret/handle/task/trace.
        assert body["replay_binding"] is None  # live deploy: no replay tape identity
        assert all(isinstance(v, str) for k, v in body.items() if k != "replay_binding")
        forbidden = ("task", "trace", "secret", "keypair", "adapter", "envelope", "stream", "anchor_fn")
        blob = resp.text.lower()
        assert not any(word in blob for word in forbidden)
    finally:
        for task in list(getattr(app.state, "deploy_background_tasks", set())):
            task.cancel()
        await _drain(app)


def test_deploy_status_values_drift_guard() -> None:
    # The store CHECK-constraint literal tuple stays in lockstep with the DeployStatus enum.
    from veridex.store import _INSTANCE_STATUS_VALUES

    assert deploy_status_values() == _INSTANCE_STATUS_VALUES
    assert deploy_status_values() == ("pending", "running", "sealed", "failed")


# ---------------------------------------------------------------------------
# ONE FLOW TO PROOF — deployed run verifies via the SAME arena /runs/{id}/verify
# ---------------------------------------------------------------------------


async def test_deploy_run_task_exception_is_logged_not_lost(caplog: pytest.LogCaptureFixture) -> None:
    # A deployed run that RAISES pre-seal (not the isolated exec lane) must be SURFACED on the server
    # log — not silently lost to asyncio GC — and the task is still discarded from the registry.
    store = InMemoryStore()
    deps = DeployDeps(
        feed_report=_healthy_live_feed(),
        market_resolved=True,
        stream_factory=_raising_stream,
        fetch_updates=_close,
        anchor_fn=None,
    )
    app = create_app(store=store, deploy_deps=deps)
    with caplog.at_level(logging.ERROR, logger="veridex.api.deploy"):
        async with _transport(app) as client:
            resp = await client.post("/agents/deploy", json=_VALID)
        assert resp.status_code == 200, resp.text
        run_id = resp.json()["run_id"]
        await _drain(app)
        await asyncio.sleep(0)  # let the done-callback (scheduled via call_soon) run

    assert any("stream boom pre-seal" in rec.getMessage() or run_id in rec.getMessage() for rec in caplog.records)
    assert not getattr(app.state, "deploy_background_tasks", set())  # discarded from the registry


async def test_deployed_run_verifies_via_the_same_arena_path() -> None:
    app, _store = _app_and_deps(_finite_stream)
    async with _transport(app) as client:
        resp = await client.post("/agents/deploy", json=_VALID)
        assert resp.status_code == 200, resp.text
        run_id = resp.json()["run_id"]

        # Drive the background run to its natural seal, then verify it exactly like an arena run.
        await _drain(app)

        vresp = await client.post(f"/runs/{run_id}/verify")
        assert vresp.status_code == 200, vresp.text
        verdict = vresp.json()
        assert verdict["run_id"] == run_id
        assert verdict["verified"] is True
        assert verdict["evidence_hash"] == verdict["recomputed_evidence_hash"]


# ---------------------------------------------------------------------------
# DEFAULT MOUNTED APP — honest replay/paper deploy works with ZERO injected deps
# (Codex M6 repro: the headline configure→preflight→deploy→observe→verify flow must be
# demonstrable from the REAL app, not only with test-injected DeployDeps.)
# ---------------------------------------------------------------------------

# The demo-safe Studio-shaped payload: a REPLAY / PAPER deploy (never 'live', never real money).
_REPLAY_STUDIO: dict[str, Any] = {**_VALID, "source_mode": "replay", "execution_mode": "paper"}


async def test_default_app_replay_deploy_runs_and_verifies_without_injected_deps() -> None:
    # The DEFAULT mounted app — create_app(store=InMemoryStore()) with NO deploy_deps: the app
    # sources the in-code demo replay fixture (build_demo_ticks — the SAME zero-I/O source /demo/run
    # uses), so a REPLAY/PAPER deploy runs end-to-end and the deployed run VERIFIES via the SAME
    # /runs/{id}/verify path. This is the M6 gap: previously the default route ran standalone_run([])
    # (empty) and crashed the seal on a live anchor with no keypair — never sealing a loadable run.
    app = create_app(store=InMemoryStore())
    async with _transport(app) as client:
        resp = await client.post("/agents/deploy", json=_REPLAY_STUDIO)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["run_id"]
        assert len(body["config_hash"]) == 64
        assert len(body["policy_hash"]) == 64
        assert body["instance_id"].startswith("inst_")

        # Drive the background replay run to its seal. The seal MUST complete cleanly — a real
        # bundled source + honest (unconfigured → not_anchored) anchoring, NOT a background
        # ValueError from a live-anchor with no keypair. (An empty run + anchor-crash still
        # "verifies" trivially; these assertions forbid that false-green.)
        tasks = list(getattr(app.state, "deploy_background_tasks", set()))
        assert len(tasks) == 1
        await _drain(app)
        assert tasks[0].exception() is None  # clean seal — no pre-seal crash

        # The deployed run actually EXECUTED over the demo replay fixture (non-empty event log),
        # then verifies via the SAME arena /runs/{id}/verify path (evidence_hash == recomputed).
        proof = (await client.get(f"/runs/{body['run_id']}")).json()
        assert proof["evidence"]["run_event_count"] > 0  # ran over real demo replay ticks, not []

        vresp = await client.post(f"/runs/{body['run_id']}/verify")
        assert vresp.status_code == 200, vresp.text
        verdict = vresp.json()
        assert verdict["run_id"] == body["run_id"]
        assert verdict["verified"] is True
        assert verdict["evidence_hash"] == verdict["recomputed_evidence_hash"]


async def test_default_app_live_deploy_stays_fail_closed_without_a_feed() -> None:
    # HONEST fail-closed preserved: a LIVE deploy in the default app (no live feed wired) must STILL
    # 422 on feed_health and start NO run — the live 422 is CORRECT (do not weaken _check_feed).
    app = create_app(store=InMemoryStore())
    async with _transport(app) as client:
        resp = await client.post("/agents/deploy", json={**_VALID, "source_mode": "live"})
    assert resp.status_code == 422, resp.text
    assert "feed_health" in resp.json()["detail"]["failed_checks"]
    assert not getattr(app.state, "deploy_background_tasks", set())  # fail-closed: no run launched


async def test_default_app_replay_preflight_checks_the_source_resolves() -> None:
    # MODE-AWARE preflight: the replay/paper deploy's feed_health check passes because the replay
    # SOURCE (the demo fixture) resolves (not because a live feed is connected). The named check is
    # honest about what it verified for a replay deploy.
    app = create_app(store=InMemoryStore())
    async with _transport(app) as client:
        resp = await client.post("/agents/deploy", json=_REPLAY_STUDIO)
        assert resp.status_code == 200, resp.text
        for task in list(getattr(app.state, "deploy_background_tasks", set())):
            task.cancel()
        await _drain(app)
