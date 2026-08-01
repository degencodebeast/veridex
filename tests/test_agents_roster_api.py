"""PUBLIC ``GET /agents/roster`` — regression pins for the HONEST public directory (C1).

The route was fixed IN PLACE from a leaky "every deployed instance across all owners" roster into an
honest public directory keyed on the immutable ``public_agent_id``. These pins lock the trust-surface
guarantees the fix introduced (the full admission matrix lives in ``test_agents_directory.py``):

* An empty store returns an honest-empty directory (never a fabricated row).
* A public + SEALED agent surfaces with the SAFE ``owner_public_label`` and NO raw ``operator_id`` /
  ``owner_ref`` anywhere; the legacy raw-shaped ``owner`` key is GONE.
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from veridex.api.router import create_app
from veridex.deploy.instance import AgentInstance, DeployStatus
from veridex.public_agent import OperatorClass, Origin, PublicAgent, Visibility
from veridex.store import InMemoryStore

_RAW_OWNER_REF = "did:privy:secret-owner-should-never-leak"
_RAW_OPERATOR_ID = "operator-secret-should-never-leak"


def _sealed_instance(instance_id: str) -> AgentInstance:
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
        status=DeployStatus.SEALED,
        operator_id=_RAW_OPERATOR_ID,
        created_at="t1",
        updated_at="t1",
    )


def _public_agent(public_agent_id: str) -> PublicAgent:
    return PublicAgent(
        public_agent_id=public_agent_id,
        display_name="Alpha Bot",
        operator_class=OperatorClass.USER,
        origin=Origin.BYOA,
        visibility=Visibility.PUBLIC,
        owner_ref=_RAW_OWNER_REF,
        created_at="t1",
        updated_at="t1",
    )


def _store_with_public_sealed(public_agent_id: str) -> InMemoryStore:
    store = InMemoryStore()

    async def _run() -> None:
        await store.persist_public_agent(_public_agent(public_agent_id))
        inst_id = f"inst_{public_agent_id}"
        await store.persist_agent_instance(_sealed_instance(inst_id))
        await store.link_instance_public_agent(inst_id, public_agent_id)

    asyncio.run(_run())
    return store


# --- (a) empty store -> honest-empty directory ------------------------------


def test_agents_roster_empty_store_is_honest_empty() -> None:
    client = TestClient(create_app(store=InMemoryStore()))

    resp = client.get("/agents/roster")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"agents": []}


# --- (b) a public + SEALED agent surfaces with the SAFE label, no raw leak ----


def test_agents_roster_public_sealed_agent_uses_safe_owner_label() -> None:
    store = _store_with_public_sealed("agent_alpha")
    client = TestClient(create_app(store=store))

    # No Authorization header — PUBLIC route (mirrors /replay-packs).
    resp = client.get("/agents/roster")

    assert resp.status_code == 200, resp.text
    agents = resp.json()["agents"]
    assert len(agents) == 1
    row = agents[0]
    # Keyed on the immutable public id, with the human name + REAL origin surfaced.
    assert row["public_agent_id"] == "agent_alpha"
    assert row["display_name"] == "Alpha Bot"
    assert row["origin"] == "byoa"
    # TRUST SURFACE: only the SAFE label — never the raw operator_id / owner_ref / legacy owner key.
    assert row["owner_public_label"] == "—"  # USER agent, no embeddable wallet -> em-dash
    assert "owner" not in row
    assert _RAW_OWNER_REF not in resp.text
    assert _RAW_OPERATOR_ID not in resp.text
    assert "did:privy" not in resp.text
    # Unscored -> honest nulls + "unscored" proof_state (never fabricated).
    assert row["avg_clv_bps"] is None
    assert row["runs"] is None
    assert row["valid_pct"] is None
    assert row["proof_state"] == "unscored"
