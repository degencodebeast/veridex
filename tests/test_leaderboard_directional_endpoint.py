"""B3 — ``GET /leaderboard/directional`` HTTP surface over :func:`directional_board`.

Pins the endpoint contract:

* The route exposes the closed :class:`~veridex.public_projection.BoardKind` enum as a required
  ``board_kind`` query param — an unknown value is a 422 (FastAPI enum validation), never a silent
  fallback to some default board.
* The body is ``{"board_kind": <the value>, "rows": [...]}`` where ``rows`` is EXACTLY the aggregated
  output of :func:`directional_board` (the route does NOT rank/aggregate — it is a thin HTTP shell).
* No scored public rows -> 200 with ``rows: []`` (honest-empty), never a fabricated row.

Seeds are wired through the injected store BEFORE the request: public agents via
``persist_public_agent`` + scored rows via ``persist_projected_rows`` (mirrors B2's setup), so the
board is joined from durable state exactly as production reads it.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi.testclient import TestClient

from veridex.api.router import create_app
from veridex.public_agent import OperatorClass, Origin, PublicAgent, Visibility
from veridex.store import InMemoryStore


def _make_public_agent(
    public_agent_id: str,
    *,
    operator_class: OperatorClass = OperatorClass.OFFICIAL,
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
        "agent_id": public_agent_id,
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


def _seed(store: InMemoryStore, agents: list[PublicAgent], rows: list[dict[str, Any]]) -> None:
    async def _run() -> None:
        for agent in agents:
            await store.persist_public_agent(agent)
        if rows:
            await store.persist_projected_rows(rows)

    asyncio.run(_run())


def test_directional_official_benchmark_returns_two_scored_agents() -> None:
    store = InMemoryStore()
    _seed(
        store,
        [
            _make_public_agent("off_a"),
            _make_public_agent("off_b"),
        ],
        [
            _make_projected_row("off_a", run_id="r1", action_count=5),
            _make_projected_row("off_b", run_id="r1", action_count=3),
        ],
    )
    client = TestClient(create_app(store=store))

    resp = client.get("/leaderboard/directional", params={"board_kind": "official_benchmark"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["board_kind"] == "official_benchmark"
    assert len(body["rows"]) == 2
    assert {row["agent_id"] for row in body["rows"]} == {"off_a", "off_b"}


def test_directional_no_scored_rows_is_honest_empty() -> None:
    store = InMemoryStore()
    client = TestClient(create_app(store=store))

    resp = client.get("/leaderboard/directional", params={"board_kind": "official_benchmark"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["board_kind"] == "official_benchmark"
    assert body["rows"] == []


def test_directional_unknown_board_kind_is_422() -> None:
    client = TestClient(create_app(store=InMemoryStore()))

    resp = client.get("/leaderboard/directional", params={"board_kind": "bogus"})

    assert resp.status_code == 422, resp.text
