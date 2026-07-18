"""II-6 RED suite — owner-gated status + EXACTLY-ONCE kill for a deployed instance (TDD).

Closes the survey-runtime gap: today there is NO reachable kill for a deployed instance (the only
cancel route lives in the UNMOUNTED ``build_agentos_app``). These tests drive the MOUNTED deploy app
(``register_deploy_routes``) directly, acting through the instance's live ``runtime_handle`` adapter
and its inherited ``acancel_run`` (AC-16, owner-first exactly-once) — NEVER a new cancel primitive.

Every test is OFFLINE, zero-wire. The MM run is driven through an INJECTED ``DeployDeps.mm_run_driver``
that cooperatively parks on the run's :class:`~veridex.mm_strategy.orchestration.StopSignal`, so a run
stays ACTIVE across a kill and the ``StopSignal`` trip is observable (the loop actually stops).

The four load-bearing properties (one test each):

1. Non-owner kill → 403 (owner-gating, resolved server-side; the caller's DID is never trusted).
2. Owner kill trips the cancel → the run loop stops → a TERMINAL (cancelled) OPS event is emitted.
3. A second kill is a NO-OP: no resume, no double-cancel, exactly ONE cancelled OPS event (AC-16).
4. The owner status view reflects the run/lease state before vs after a kill (running → cancelled).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import FastAPI
from httpx import ASGITransport

from veridex.api.deploy import DeployDeps, register_deploy_routes
from veridex.config import Settings
from veridex.deploy.instance import AgentInstance, DeployStatus
from veridex.mm_strategy.orchestration import StopSignal
from veridex.runtime.mm_agent_adapter import RunContext
from veridex.runtime.runtime_events import RuntimeEvent, RuntimeEventType
from veridex.store import InMemoryStore

_DEV_OWNER = "did:privy:dev"  # the fixed principal AUTH_MODE=dev yields for every request.


class _ParkingDriver:
    """A :data:`~veridex.runtime.mm_agent_adapter.RunDriver` that parks on the run's ``StopSignal``.

    It records how many times it was entered (a resume would re-enter — it never must), signals when
    the run is live (``entered``), and signals when the loop actually stopped via the ``StopSignal``
    (``stopped``). Returns a ``terminal_reason="stopped"`` result so the adapter classifies a
    cooperatively-cancelled run as CANCELLED (mirrors the II-2 composition's own terminal mapping).
    """

    def __init__(self) -> None:
        self.entries = 0
        self.entered = asyncio.Event()
        self.stopped = asyncio.Event()

    async def __call__(self, ctx: RunContext, stop: StopSignal, sink: Any) -> Any:
        self.entries += 1
        self.entered.set()
        await stop.wait()
        self.stopped.set()

        class _Result:
            terminal_reason = "stopped"

        return _Result()


def _mm_payload(**overrides: Any) -> dict[str, Any]:
    """A valid ``quoteguard-mm`` deploy body (passes preflight; the run is driven by the parking driver)."""
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
        "mm": {"tape_ref": "healthy-demo", "guard_enabled": False},
    }
    payload.update(overrides)
    return payload


def _build_app(
    *, driver: _ParkingDriver | None = None, events: list[RuntimeEvent] | None = None
) -> tuple[FastAPI, InMemoryStore]:
    store = InMemoryStore()
    app = FastAPI()
    deps = DeployDeps(anchor_fn=None, mm_run_driver=driver)
    register_deploy_routes(
        app,
        store=store,
        settings=Settings(AUTH_MODE="dev"),
        deploy_deps=deps,
        runtime_event_sink=(events.append if events is not None else None),
    )
    return app, store


def _transport(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _drain(app: FastAPI) -> None:
    tasks = list(getattr(app.state, "deploy_background_tasks", set()))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _cancelled_ops(events: list[RuntimeEvent]) -> list[RuntimeEvent]:
    """The TERMINAL cancelled OPS events emitted by an owner kill (STATUS_CHANGED / status=cancelled)."""
    return [
        e
        for e in events
        if e.type == RuntimeEventType.STATUS_CHANGED and e.payload.get("status") == "cancelled"
    ]


async def _deploy_and_park(
    client: httpx.AsyncClient, driver: _ParkingDriver, **payload_overrides: Any
) -> dict[str, Any]:
    """Deploy an MM instance and wait until its run is live (parked in the driver)."""
    resp = await client.post("/agents/deploy", json=_mm_payload(**payload_overrides))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    await asyncio.wait_for(driver.entered.wait(), timeout=5.0)
    return body


# =====================================================================================
# RED #1 — a NON-owner kill is refused 403 (owner-gating, resolved from server-owned state).
# =====================================================================================


async def test_non_owner_kill_is_forbidden() -> None:
    app, store = _build_app()
    now = datetime.now(tz=UTC).isoformat()
    # An instance owned by ANOTHER principal, with a minted runtime_handle (i.e. a live-shaped row).
    await store.persist_agent_instance(
        AgentInstance(
            instance_id="inst-other",
            template_id="quoteguard-mm-template",
            agent_id="studio-mm-agent",
            submitted_config={},
            effective_config={},
            config_hash="c" * 8,
            policy_hash="p" * 8,
            source_mode="replay",
            execution_mode="dry_run",
            run_id="run-other",
            status=DeployStatus.RUNNING,
            operator_id="did:privy:attacker",  # NOT the dev caller
            runtime_handle={"runtime_kind": "agentos", "runtime_agent_id": "ra", "run_id": "run-other"},
            created_at=now,
            updated_at=now,
        )
    )
    async with _transport(app) as client:
        resp = await client.post("/agents/instances/inst-other/kill")
    assert resp.status_code == 403, resp.text
    # Fail-closed: the row is untouched (no cancel side effect on a refused caller).
    instance = await store.get_agent_instance("inst-other")
    assert instance.status == DeployStatus.RUNNING


# =====================================================================================
# RED #2 — an owner kill trips the cancel → the loop stops → a terminal OPS event is emitted.
# =====================================================================================


async def test_owner_kill_stops_loop_and_emits_terminal_ops() -> None:
    driver = _ParkingDriver()
    events: list[RuntimeEvent] = []
    app, store = _build_app(driver=driver, events=events)

    async with _transport(app) as client:
        body = await _deploy_and_park(client, driver)
        instance_id = body["instance_id"]
        run_id = body["run_id"]

        kill = await client.post(f"/agents/instances/{instance_id}/kill")
        assert kill.status_code == 200, kill.text
        killed = kill.json()
        assert killed["engaged"] is True
        assert killed["run_id"] == run_id

        # The kill tripped the StopSignal → the parked loop wakes and stops.
        await _drain(app)
        assert driver.stopped.is_set(), "the owner kill must trip the StopSignal and stop the loop"

    cancelled = _cancelled_ops(events)
    assert len(cancelled) == 1, f"expected exactly one terminal cancelled OPS event, got {cancelled}"
    assert cancelled[0].run_id == run_id


# =====================================================================================
# RED #3 — a second kill is a NO-OP: no resume, no double-cancel, exactly ONE cancel (AC-16).
# =====================================================================================


async def test_second_kill_is_idempotent_no_resume() -> None:
    driver = _ParkingDriver()
    events: list[RuntimeEvent] = []
    app, store = _build_app(driver=driver, events=events)

    async with _transport(app) as client:
        body = await _deploy_and_park(client, driver)
        instance_id = body["instance_id"]

        first = await client.post(f"/agents/instances/{instance_id}/kill")
        assert first.status_code == 200, first.text
        assert first.json()["engaged"] is True

        # Second kill BEFORE the run settles: must NOT re-engage the kill (exactly-once winner already
        # engaged) and must NOT resume the run.
        second = await client.post(f"/agents/instances/{instance_id}/kill")
        assert second.status_code == 200, second.text
        assert second.json()["engaged"] is False

        await _drain(app)

        # A THIRD kill AFTER the run has fully settled is still an idempotent no-op (never a resume).
        third = await client.post(f"/agents/instances/{instance_id}/kill")
        assert third.status_code == 200, third.text
        assert third.json()["engaged"] is False

    assert driver.entries == 1, "the run must never be re-entered/resumed by a repeat kill"
    assert len(_cancelled_ops(events)) == 1, "exactly ONE terminal cancelled OPS event across all kills"


# =====================================================================================
# RED #4 — the owner status view reflects the run/lease state before vs after a kill.
# =====================================================================================


async def test_status_reflects_run_state_before_and_after_kill() -> None:
    driver = _ParkingDriver()
    app, store = _build_app(driver=driver)

    async with _transport(app) as client:
        body = await _deploy_and_park(client, driver)
        instance_id = body["instance_id"]

        before = await client.get(f"/agents/instances/{instance_id}/status")
        assert before.status_code == 200, before.text
        assert before.json()["run_state"] == "running"
        assert before.json()["killed"] is False

        kill = await client.post(f"/agents/instances/{instance_id}/kill")
        assert kill.status_code == 200, kill.text
        assert kill.json()["engaged"] is True

        after = await client.get(f"/agents/instances/{instance_id}/status")
        assert after.status_code == 200, after.text
        assert after.json()["run_state"] == "cancelled"
        assert after.json()["killed"] is True

        await _drain(app)

        # The cancelled outcome is durable across the run settling (never flips back to running).
        settled = await client.get(f"/agents/instances/{instance_id}/status")
        assert settled.json()["run_state"] == "cancelled"
        assert settled.json()["killed"] is True


async def test_status_non_owner_is_hidden() -> None:
    """Owner-gating parity: a non-owner status probe is refused (403), never leaked."""
    app, store = _build_app()
    now = datetime.now(tz=UTC).isoformat()
    await store.persist_agent_instance(
        AgentInstance(
            instance_id="inst-guarded",
            template_id="quoteguard-mm-template",
            agent_id="studio-mm-agent",
            submitted_config={},
            effective_config={},
            config_hash="c" * 8,
            policy_hash="p" * 8,
            source_mode="replay",
            execution_mode="dry_run",
            run_id="run-guarded",
            status=DeployStatus.RUNNING,
            operator_id="did:privy:someone-else",
            created_at=now,
            updated_at=now,
        )
    )
    async with _transport(app) as client:
        resp = await client.get("/agents/instances/inst-guarded/status")
    assert resp.status_code == 403, resp.text


async def test_kill_before_run_minted_fails_closed() -> None:
    """If ``runtime_handle`` is None (no run minted), kill fails closed sensibly — no crash, honest 409."""
    app, store = _build_app()
    now = datetime.now(tz=UTC).isoformat()
    await store.persist_agent_instance(
        AgentInstance(
            instance_id="inst-nohandle",
            template_id="quoteguard-mm-template",
            agent_id="studio-mm-agent",
            submitted_config={},
            effective_config={},
            config_hash="c" * 8,
            policy_hash="p" * 8,
            source_mode="replay",
            execution_mode="dry_run",
            run_id="run-nohandle",
            status=DeployStatus.PENDING,
            operator_id=_DEV_OWNER,  # owned by the caller — so this is NOT an owner refusal
            runtime_handle=None,  # no run minted yet
            created_at=now,
            updated_at=now,
        )
    )
    async with _transport(app) as client:
        resp = await client.post("/agents/instances/inst-nohandle/kill")
    assert resp.status_code == 409, resp.text
