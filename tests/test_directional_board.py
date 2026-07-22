"""B2 — durable projected rows + visibility/scope-joined directional_board (TDD, memory + Postgres).

Exercises the two projected-row persistence methods added to the Store protocol and BOTH
implementations, plus :func:`veridex.public_projection.directional_board` which joins CURRENT
public-agent visibility (and, for the official benchmark, ``operator_class``) at read time before
handing the surviving rows to the UNCHANGED pure aggregator :func:`veridex.leaderboard.leaderboard`.

The ``store`` fixture is parametrized over ``InMemoryStore`` AND a ``PostgresStore`` wired to the
local dev Postgres — the Postgres parametrization MUST RUN (the DB is up), never skip, so the
durable UPSERT / no-double-count contract is proven, not assumed. Every public_agent_id carries a
per-test uuid suffix so Postgres rows never collide and each test can filter the board down to its
OWN agents (the durable board accumulates rows from earlier tests in the same session).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

import psycopg
import pytest

from veridex.leaderboard import leaderboard
from veridex.public_agent import OperatorClass, Origin, PublicAgent, Visibility
from veridex.public_projection import BoardKind, directional_board
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
    operator_class: OperatorClass,
    visibility: Visibility = Visibility.PUBLIC,
    display_name: str = "Alpha",
) -> PublicAgent:
    return PublicAgent(
        public_agent_id=public_agent_id,
        display_name=display_name,
        operator_class=operator_class,
        origin=Origin.OFFICIAL if operator_class is OperatorClass.OFFICIAL else Origin.STUDIO,
        visibility=visibility,
        owner_ref=None if operator_class is OperatorClass.OFFICIAL else "did:privy:abc",
        created_at="t1",
        updated_at="t1",
    )


def _make_projected_row(
    public_agent_id: str,
    *,
    run_id: str,
    action_count: int,
    total_clv_bps: int = 120,
) -> dict[str, Any]:
    """A realistic score_run-shaped PUBLIC row (agent_id == public id, as B1 emits)."""
    return {
        "agent_id": public_agent_id,  # B1 replaces agent_id with the public id
        "public_agent_id": public_agent_id,
        "run_id": run_id,
        "source_mode": "replay",
        "total_clv_bps": total_clv_bps,
        "action_count": action_count,
        "avg_clv_bps": (total_clv_bps / action_count) if action_count else None,
        "sim_pnl": 500,
        "brier": 0.21,
        "max_drawdown": -30.0,
        "valid_pct": 90.0,
        "proof_mode": "attested",
    }


def _board_by_agent(board: list[dict[str, Any]], ids: set[str]) -> dict[str, dict[str, Any]]:
    """Filter a board to this test's own agents (the durable board carries earlier tests' rows)."""
    return {row["agent_id"]: row for row in board if row["agent_id"] in ids}


async def test_official_benchmark_pools_across_runs(store: Store) -> None:
    suffix = _suffix()
    pid_a = f"off_a_{suffix}"
    pid_b = f"off_b_{suffix}"
    await store.persist_public_agent(_make_public_agent(pid_a, operator_class=OperatorClass.OFFICIAL))
    await store.persist_public_agent(_make_public_agent(pid_b, operator_class=OperatorClass.OFFICIAL))

    await store.persist_projected_rows(
        [
            _make_projected_row(pid_a, run_id="r1", action_count=5),
            _make_projected_row(pid_a, run_id="r2", action_count=7),
            _make_projected_row(pid_b, run_id="r1", action_count=3),
            _make_projected_row(pid_b, run_id="r2", action_count=4),
        ]
    )

    board = await directional_board(store, board_kind=BoardKind.OFFICIAL_BENCHMARK)
    mine = _board_by_agent(board, {pid_a, pid_b})

    assert set(mine) == {pid_a, pid_b}
    # Pooled across the two runs (Σ action_count per agent).
    assert mine[pid_a]["action_count"] == 12
    assert mine[pid_b]["action_count"] == 7
    # It really is the unchanged aggregator's output.
    assert mine[pid_a]["runs"] == 2


async def test_visibility_flip_drops_from_board_but_keeps_stored_rows(store: Store) -> None:
    suffix = _suffix()
    pid = f"off_{suffix}"
    await store.persist_public_agent(_make_public_agent(pid, operator_class=OperatorClass.OFFICIAL))
    await store.persist_projected_rows(
        [
            _make_projected_row(pid, run_id="r1", action_count=5),
            _make_projected_row(pid, run_id="r2", action_count=7),
        ]
    )

    board = await directional_board(store, board_kind=BoardKind.OFFICIAL_BENCHMARK)
    assert pid in _board_by_agent(board, {pid})

    # Flip to PRIVATE — current visibility is joined at read time, so it DISAPPEARS.
    await store.set_public_agent_visibility(pid, Visibility.PRIVATE, updated_at="t2")

    board_after = await directional_board(store, board_kind=BoardKind.OFFICIAL_BENCHMARK)
    assert pid not in _board_by_agent(board_after, {pid})

    # The projected rows themselves are NOT deleted — visibility is a read-time join, not a purge.
    stored = await store.list_projected_rows()
    assert sum(1 for row in stored if row["public_agent_id"] == pid) == 2


async def test_re_persist_same_run_does_not_double_count(store: Store) -> None:
    suffix = _suffix()
    pid = f"off_{suffix}"
    await store.persist_public_agent(_make_public_agent(pid, operator_class=OperatorClass.OFFICIAL))
    rows = [
        _make_projected_row(pid, run_id="r1", action_count=5),
        _make_projected_row(pid, run_id="r2", action_count=7),
    ]
    await store.persist_projected_rows(rows)

    board = await directional_board(store, board_kind=BoardKind.OFFICIAL_BENCHMARK)
    assert _board_by_agent(board, {pid})[pid]["action_count"] == 12

    # Re-persist the SAME (run_id, public_agent_id) rows — UPSERT overwrites, never duplicates.
    await store.persist_projected_rows(rows)

    board_again = await directional_board(store, board_kind=BoardKind.OFFICIAL_BENCHMARK)
    assert _board_by_agent(board_again, {pid})[pid]["action_count"] == 12

    stored = await store.list_projected_rows()
    assert sum(1 for row in stored if row["public_agent_id"] == pid) == 2


async def test_user_agent_scoped_to_public_agents_board_only(store: Store) -> None:
    suffix = _suffix()
    pid = f"user_{suffix}"
    await store.persist_public_agent(
        _make_public_agent(pid, operator_class=OperatorClass.USER, visibility=Visibility.PUBLIC)
    )
    await store.persist_projected_rows([_make_projected_row(pid, run_id="r1", action_count=6)])

    public_board = await directional_board(store, board_kind=BoardKind.PUBLIC_AGENTS)
    assert pid in _board_by_agent(public_board, {pid})

    official_board = await directional_board(store, board_kind=BoardKind.OFFICIAL_BENCHMARK)
    assert pid not in _board_by_agent(official_board, {pid})


async def test_directional_board_equals_leaderboard_of_kept_rows(store: Store) -> None:
    """directional_board is exactly leaderboard(kept_rows) — a thin visibility/scope join."""
    suffix = _suffix()
    pid = f"off_{suffix}"
    await store.persist_public_agent(_make_public_agent(pid, operator_class=OperatorClass.OFFICIAL))
    r1 = _make_projected_row(pid, run_id="r1", action_count=5)
    r2 = _make_projected_row(pid, run_id="r2", action_count=7)
    await store.persist_projected_rows([r1, r2])

    board = await directional_board(store, board_kind=BoardKind.OFFICIAL_BENCHMARK)
    mine = _board_by_agent(board, {pid})[pid]
    expected = {r["agent_id"]: r for r in leaderboard([r1, r2])}[pid]
    # ``rank`` is a whole-board property (the durable Postgres board pools other tests' official
    # agents too), so compare every aggregated field EXCEPT rank — the aggregation must be identical.
    assert {k: v for k, v in mine.items() if k != "rank"} == {
        k: v for k, v in expected.items() if k != "rank"
    }
