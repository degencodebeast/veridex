"""PUBLIC ``GET /agents/roster`` — the unauthenticated deployed-agent roster (mirrors /replay-packs).

Pins the roster contract:

* ``GET /agents/roster`` is PUBLIC (no auth) and lists EVERY deployed instance across ALL owners —
  a projection of ``store.list_agent_instances()``, NOT owner-filtered (unlike the owner-scoped
  ``/agents/instances``). ``owner`` is exposed intentionally (public "who deployed what" directory).
* The performance columns (``avg_clv_bps`` / ``runs`` / ``valid_pct``) are ALWAYS ``null`` — there is
  no cross-instance scoring aggregation yet, so they are honestly absent, NEVER fabricated.
* An empty store returns an empty roster (honest-empty), never a fabricated row.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from veridex.api.router import create_app
from veridex.deploy.instance import AgentInstance, DeployStatus
from veridex.store import InMemoryStore


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _instance(instance_id: str, operator_id: str | None) -> AgentInstance:
    """Build a minimal persisted, deployed AgentInstance owned by ``operator_id`` (None => UNOWNED)."""
    now = _now()
    return AgentInstance(
        instance_id=instance_id,
        template_id="value_clv",
        agent_id="studio-value_clv",
        submitted_config={},
        effective_config={},
        config_hash="c" * 64,
        policy_hash="p" * 64,
        source_mode="replay",
        execution_mode="paper",
        run_id="run-seed",
        status=DeployStatus.RUNNING,
        operator_id=operator_id,
        created_at=now,
        updated_at=now,
    )


def _store_with(*instances: AgentInstance) -> InMemoryStore:
    store = InMemoryStore()
    for inst in instances:
        asyncio.run(store.persist_agent_instance(inst))
    return store


# --- (a) empty store -> honest-empty roster ---------------------------------


def test_agents_roster_empty_store_is_honest_empty() -> None:
    client = TestClient(create_app(store=InMemoryStore()))

    resp = client.get("/agents/roster")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"agents": []}


# --- (b) a deployed instance surfaces with REAL fields + NULL perf columns ----


def test_agents_roster_lists_deployed_instance_with_null_perf() -> None:
    store = _store_with(_instance("inst_a", "did:privy:owner-a"))
    client = TestClient(create_app(store=store))

    resp = client.get("/agents/roster")

    assert resp.status_code == 200, resp.text
    agents = resp.json()["agents"]
    assert len(agents) == 1
    row = agents[0]
    # Real deployment identity is surfaced from the persisted record …
    assert row["agent_id"] == "studio-value_clv"
    assert row["type"] == "value_clv"
    assert row["owner"] == "did:privy:owner-a"
    assert row["source_mode"] == "replay"
    assert row["execution_mode"] == "paper"
    assert row["status"] == "running"  # DeployStatus value, lowercased
    assert row["config_hash_present"] is True  # a REAL proof indicator (config_hash was pinned)
    # … and the performance columns are HONESTLY null — no scoring aggregation exists (never fabricated).
    assert row["avg_clv_bps"] is None
    assert row["runs"] is None
    assert row["valid_pct"] is None


# --- (c) PUBLIC + unfiltered across ALL owners (incl. UNOWNED) ---------------


def test_agents_roster_is_public_and_lists_all_owners_unfiltered() -> None:
    store = _store_with(
        _instance("inst_a", "did:privy:owner-a"),
        _instance("inst_b", "did:privy:owner-b"),
        _instance("inst_unowned", None),
    )
    client = TestClient(create_app(store=store))

    # No Authorization header — PUBLIC route (mirrors /replay-packs). Every owner surfaces, and an
    # UNOWNED (operator_id is None) row is represented honestly as owner=None (public roster by design).
    resp = client.get("/agents/roster")

    assert resp.status_code == 200, resp.text
    agents = resp.json()["agents"]
    assert len(agents) == 3
    assert {a["owner"] for a in agents} == {"did:privy:owner-a", "did:privy:owner-b", None}
