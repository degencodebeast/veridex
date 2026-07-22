"""A2 — PublicAgent persistence + instance→public-agent FK link (TDD, memory + Postgres parity).

Exercises the seven PublicAgent persistence methods added to the Store protocol and BOTH
implementations. The ``store`` fixture is parametrized over ``InMemoryStore`` AND a
``PostgresStore`` wired to the local dev Postgres — the Postgres parametrization MUST RUN (the DB
is up), never skip, so the durable path is proven, not assumed.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import psycopg
import pytest

from veridex.deploy.instance import AgentInstance
from veridex.public_agent import OperatorClass, Origin, PublicAgent, Visibility
from veridex.store import InMemoryStore, PostgresStore, Store

# The dev Postgres is RUNNING at this DSN; default to it so the Postgres parametrization RUNS
# under the plain test command (no DATABASE_URL export required) instead of skipping.
_PG_DSN = os.getenv("DATABASE_URL", "postgresql://postgres:dev@localhost:5433/postgres")


async def _init_pg(store: PostgresStore) -> None:
    async with await psycopg.AsyncConnection.connect(_PG_DSN) as conn:
        await store.init_db(conn)


@pytest.fixture(params=["memory", "postgres"])
def store(request: pytest.FixtureRequest) -> Store:
    """Yield each store implementation in turn; Postgres gets a one-shot idempotent DDL init."""
    if request.param == "memory":
        return InMemoryStore()
    pg = PostgresStore(dsn=_PG_DSN)
    asyncio.run(_init_pg(pg))
    return pg


def _suffix() -> str:
    """A per-test unique suffix so Postgres rows never collide across tests / reruns."""
    return uuid.uuid4().hex[:8]


def _make_public_agent(
    public_agent_id: str,
    *,
    display_name: str = "Alpha",
    visibility: Visibility = Visibility.PRIVATE,
    operator_class: OperatorClass = OperatorClass.USER,
    origin: Origin = Origin.STUDIO,
    owner_ref: str | None = "did:privy:abc",
    version: int = 1,
) -> PublicAgent:
    return PublicAgent(
        public_agent_id=public_agent_id,
        display_name=display_name,
        operator_class=operator_class,
        origin=origin,
        visibility=visibility,
        owner_ref=owner_ref,
        created_at="t1",
        updated_at="t1",
        version=version,
    )


def _make_agent_instance(instance_id: str) -> AgentInstance:
    return AgentInstance(
        instance_id=instance_id,
        template_id="tmpl_1",
        agent_id="agent_1",
        submitted_config={},
        effective_config={},
        config_hash="cfg_hash",
        policy_hash="pol_hash",
        source_mode="replay",
        execution_mode="paper",
        run_id="run_1",
        created_at="t1",
        updated_at="t1",
    )


async def test_persist_get_roundtrip(store: Store) -> None:
    pid = f"pa_{_suffix()}"
    agent = _make_public_agent(pid)
    await store.persist_public_agent(agent)

    got = await store.get_public_agent(pid)
    assert got == agent


async def test_get_missing_returns_none(store: Store) -> None:
    assert await store.get_public_agent(f"missing_{_suffix()}") is None


async def test_list_public_agents_includes_persisted(store: Store) -> None:
    pid = f"pa_{_suffix()}"
    agent = _make_public_agent(pid)
    await store.persist_public_agent(agent)

    listed = await store.list_public_agents()
    assert any(a.public_agent_id == pid for a in listed)


async def test_set_visibility_updates_bumps_version(store: Store) -> None:
    pid = f"pa_{_suffix()}"
    await store.persist_public_agent(_make_public_agent(pid, visibility=Visibility.PRIVATE))

    await store.set_public_agent_visibility(pid, Visibility.PUBLIC, updated_at="t2")

    got = await store.get_public_agent(pid)
    assert got is not None
    assert got.visibility is Visibility.PUBLIC
    assert got.updated_at == "t2"
    assert got.version == 2


async def test_set_display_name_updates_bumps_version(store: Store) -> None:
    pid = f"pa_{_suffix()}"
    await store.persist_public_agent(_make_public_agent(pid, display_name="Alpha"))

    await store.set_public_agent_display_name(pid, "Renamed", updated_at="t3")

    got = await store.get_public_agent(pid)
    assert got is not None
    assert got.display_name == "Renamed"
    assert got.updated_at == "t3"
    assert got.version == 2


async def test_link_instance_public_agent_roundtrip(store: Store) -> None:
    suffix = _suffix()
    pid = f"pa_{suffix}"
    instance_id = f"inst_{suffix}"
    await store.persist_public_agent(_make_public_agent(pid))
    await store.persist_agent_instance(_make_agent_instance(instance_id))

    assert await store.get_instance_public_agent_id(instance_id) is None

    await store.link_instance_public_agent(instance_id, pid)

    assert await store.get_instance_public_agent_id(instance_id) == pid
