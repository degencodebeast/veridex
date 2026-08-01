"""A3 — backfill every legacy deployed instance to a distinct PRIVATE PublicAgent (TDD).

HONESTY (load-bearing): a legacy instance has no honest public identity, so the backfill mints one
with ``origin=UNKNOWN`` (never a guessed studio/byoa), ``operator_class=USER``,
``visibility=PRIVATE`` — a pre-existing deployment must never leak into the public directory. The
minted id is INJECTED (deterministic), and the pass is IDEMPOTENT: a second run mints nothing.
"""

from __future__ import annotations

from typing import Any

import pytest

from veridex.deploy.instance import AgentInstance
from veridex.public_agent import OperatorClass, Origin, PublicAgent, Visibility
from veridex.public_agent_backfill import backfill_public_agents
from veridex.store import InMemoryStore


def _make_instance(*, instance_id: str, operator_id: str | None) -> AgentInstance:
    """Build a minimal valid :class:`AgentInstance` for persistence tests."""
    cfg: dict[str, Any] = {"strategy": "momentum-sharp"}
    return AgentInstance(
        instance_id=instance_id,
        template_id="sharp-momentum-v2",
        agent_id=f"studio-agent-{instance_id}",
        submitted_config=cfg,
        effective_config=cfg,
        config_hash="cfg-hash",
        policy_hash="pol-hash",
        source_mode="replay",
        execution_mode="paper",
        run_id=f"run-{instance_id}",
        operator_id=operator_id,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


@pytest.fixture
async def store() -> InMemoryStore:
    """An InMemoryStore holding two persisted, un-linked legacy instances."""
    s = InMemoryStore()
    await s.persist_agent_instance(_make_instance(instance_id="inst_a", operator_id="did:privy:ALICE"))
    await s.persist_agent_instance(_make_instance(instance_id="inst_b", operator_id="did:privy:BOB"))
    return s


async def test_backfill_mints_distinct_private_unknown_public_agents(store: InMemoryStore) -> None:
    minted = await backfill_public_agents(
        store, now="2026-07-22T00:00:00Z", mint_id=lambda iid: f"agt_bf_{iid}"
    )

    assert minted == 2

    pid_a = await store.get_instance_public_agent_id("inst_a")
    pid_b = await store.get_instance_public_agent_id("inst_b")
    assert pid_a is not None
    assert pid_b is not None
    assert pid_a != pid_b

    by_instance = {"inst_a": ("did:privy:ALICE", pid_a), "inst_b": ("did:privy:BOB", pid_b)}
    for instance_id, (operator_id, pid) in by_instance.items():
        agent = await store.get_public_agent(pid)
        assert agent is not None
        assert agent.visibility is Visibility.PRIVATE
        assert agent.operator_class is OperatorClass.USER
        assert agent.origin is Origin.UNKNOWN
        assert agent.owner_ref == operator_id
        assert agent.display_name == f"studio-agent-{instance_id}"
        assert agent.created_at == "2026-07-22T00:00:00Z"
        assert agent.updated_at == "2026-07-22T00:00:00Z"
        assert agent.version == 1


async def test_backfill_is_idempotent(store: InMemoryStore) -> None:
    mint = lambda iid: f"agt_bf_{iid}"  # noqa: E731

    first = await backfill_public_agents(store, now="2026-07-22T00:00:00Z", mint_id=mint)
    assert first == 2
    before = {a.public_agent_id for a in await store.list_public_agents()}

    second = await backfill_public_agents(store, now="2026-07-23T00:00:00Z", mint_id=mint)
    assert second == 0
    after = {a.public_agent_id for a in await store.list_public_agents()}
    assert after == before


async def test_backfill_crash_between_persist_and_link_reuses_orphan(store: InMemoryStore) -> None:
    """Fault injection: a crash AFTER persist but BEFORE link must not duplicate on retry.

    Simulate the orphan a crash leaves behind — a PublicAgent persisted for ``inst_a`` under the
    DETERMINISTIC id but NOT yet linked. On retry the deterministic minter re-derives the SAME id, so
    ``persist_public_agent`` UPSERTs the same row (no second agent) and the link is completed. Net
    after crash+retry: exactly ONE PublicAgent + ONE durable link, no orphan, no duplicate.
    """
    mint = lambda iid: f"agt_bf_{iid}"  # noqa: E731
    orphan_pid = "agt_bf_inst_a"

    # The orphan a crash-between-persist-and-link leaves: persisted, NOT linked.
    await store.persist_public_agent(
        PublicAgent(
            public_agent_id=orphan_pid,
            display_name="studio-agent-inst_a",
            operator_class=OperatorClass.USER,
            origin=Origin.UNKNOWN,
            visibility=Visibility.PRIVATE,
            owner_ref="did:privy:ALICE",
            created_at="2026-07-22T00:00:00Z",
            updated_at="2026-07-22T00:00:00Z",
            version=1,
        )
    )
    assert await store.get_instance_public_agent_id("inst_a") is None
    agents_before = await store.list_public_agents()
    assert len(agents_before) == 1

    minted = await backfill_public_agents(store, now="2026-07-22T00:00:00Z", mint_id=mint)
    assert minted == 2

    # (a) the orphan was REUSED via UPSERT, not duplicated: exactly one agent for inst_a.
    agents_after = await store.list_public_agents()
    assert [a.public_agent_id for a in agents_after].count(orphan_pid) == 1
    assert len(agents_after) == 2  # inst_a (reused) + inst_b (fresh), not 3

    # (b) inst_a is now linked to the deterministic id.
    assert await store.get_instance_public_agent_id("inst_a") == orphan_pid

    # (c) a second backfill is idempotent.
    second = await backfill_public_agents(store, now="2026-07-23T00:00:00Z", mint_id=mint)
    assert second == 0
