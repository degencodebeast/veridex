"""F1 — Official Replay League SEEDED ACCEPTANCE over the SHARED durable Postgres (TDD).

This is the in-process twin of the running-container proof in
``scripts/test_maker_arena_image.sh``: it builds the SAME app shape the F1 container serves —
:func:`~veridex.api.server.create_server_app` over the shared dev Postgres (durable
``PostgresStore`` + real pool + ``init_db`` via the app lifespan) — drives the D3
:func:`~veridex.seed.official_replay_league.run_seed` state machine over it, then HTTP-asserts the
four Official-Replay-League acceptance surfaces over an in-process ASGI client:

* ``GET /agents/roster`` — EXACTLY 2 PUBLIC official agents, each with a NUMERIC ``avg_clv_bps`` and a
  human ``display_name`` (not the opaque id); a pre-existing PRIVATE backfilled instance is ABSENT.
* ``GET /leaderboard/directional?board_kind=official_benchmark`` — EXACTLY 2 pooled rows, ``source_mode``
  uniformly all-replay. Asserts the OFFICIAL board TOPOLOGY (2 officials), never a global competition
  count (a crash-window orphan DRAFT can inflate the global count but never reaches the official board).
* ``GET /replay-packs/demo_pack_real/fixtures/18213979/markets`` — 30 markets, 13 suspended, each
  suspended market keeping an EMPTY ``stable_prob_bps`` (never back-filled) + a retained ``stable_price``,
  under the honest ``"CAPTURED REPLAY"`` label.
* ``GET /maker/arena-result`` — the sealed Maker ``result.fixtures`` is byte-UNCHANGED across the seed
  (AC-11: seeding the directional league must not perturb the sealed Maker evidence).

Determinism: the shared Postgres public schema is DROPPED + recreated before the app builds (the
lifespan's ``init_db`` rebuilds the tables), so the 2-row assertions are exact regardless of prior runs.
Requires the dev Postgres (up at ``postgresql://postgres:dev@localhost:5433/postgres``); the autouse
``_replay_pack_root`` conftest fixture points ``REPLAY_PACK_ROOT`` at the bundled ``demo_pack_real`` pack
(leaf ``demo_pack_real`` -> ``pack_id="demo_pack_real"``, matching the seed's pinned pack).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

import httpx
import psycopg
import pytest
from httpx import ASGITransport

from veridex.api.server import create_server_app
from veridex.config import Settings
from veridex.deploy.instance import AgentInstance, DeployStatus
from veridex.seed.official_replay_league import OFFICIAL_AGENTS, run_seed
from veridex.store import Store

_PG_DSN = os.getenv("DATABASE_URL", "postgresql://postgres:dev@localhost:5433/postgres")
_OFFICIAL_IDS = {defn.public_agent_id for defn in OFFICIAL_AGENTS}
_LEGACY_INSTANCE_ID = "inst-legacy-accept"
_REPLAY_FIXTURE_ID = 18213979


def _dev_settings() -> Settings:
    """Dev-mode settings: every request auto-resolves the fixed principal (no auth header needed)."""
    return Settings(_env_file=None, app_env="development", auth_mode="dev")  # type: ignore[call-arg]


async def _reset_public_schema() -> None:
    """DROP + recreate the shared Postgres ``public`` schema so the seed lands on a clean slate.

    The app lifespan's ``init_db`` rebuilds every table afterwards, so the 2-row acceptance assertions
    are exact and independent of any rows a prior test/seed left behind.
    """
    async with await psycopg.AsyncConnection.connect(_PG_DSN) as conn:
        async with conn.cursor() as cur:
            await cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await conn.commit()


def _legacy_unlinked_instance() -> AgentInstance:
    """A legacy, UN-LINKED SEALED instance present BEFORE the seed runs (backfilled PRIVATE by the seed)."""
    now = datetime.now(tz=UTC).isoformat()
    return AgentInstance(
        instance_id=_LEGACY_INSTANCE_ID,
        template_id="legacy-template",
        agent_id="legacy-agent",
        submitted_config={},
        effective_config={},
        config_hash="l" * 8,
        policy_hash="p" * 8,
        source_mode="replay",
        execution_mode="paper",
        run_id="run-legacy-accept",
        status=DeployStatus.SEALED,
        operator_id="did:privy:legacy",
        created_at=now,
        updated_at=now,
    )


async def test_seeded_official_league_acceptance_over_shared_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seed the Official Replay League over the shared durable Postgres and HTTP-assert all four surfaces."""
    if not os.getenv("DATABASE_URL"):
        monkeypatch.setenv("DATABASE_URL", _PG_DSN)
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost")

    await _reset_public_schema()

    guard = create_server_app(env=os.environ, settings=_dev_settings())
    app = guard.app
    store: Store = app.state.store

    lifespan = app.router.lifespan_context
    async with lifespan(app):
        # A pre-existing legacy instance the seed backfills to a PRIVATE user agent (never on the board).
        await store.persist_agent_instance(_legacy_unlinked_instance())

        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # AC-11 pre-seed capture: the sealed Maker envelope BEFORE the directional seed runs.
            maker_before = await client.get("/maker/arena-result")
            assert maker_before.status_code == 200, maker_before.text
            fixtures_before = json.dumps(maker_before.json()["result"]["fixtures"], sort_keys=True)

            result = await run_seed(app, store, seed_revision="accept_r1")
            assert set(result.public_agent_ids) == _OFFICIAL_IDS
            assert len(result.competition_ids) == 2

            # --- GET /agents/roster: exactly 2 PUBLIC officials, numeric CLV + human display_name --------
            roster = await client.get("/agents/roster")
            assert roster.status_code == 200, roster.text
            agents = roster.json()["agents"]
            assert len(agents) == 2, agents
            assert {a["public_agent_id"] for a in agents} == _OFFICIAL_IDS
            for a in agents:
                assert isinstance(a["avg_clv_bps"], (int, float)), a  # NUMERIC pooled CLV, never a None hole
                assert a["display_name"] and a["display_name"] != a["public_agent_id"]  # human name, not id
            # The pre-existing PRIVATE backfilled instance is ABSENT from the public roster.
            assert _LEGACY_INSTANCE_ID not in {a["agent_id"] for a in agents}
            legacy_pid = await store.get_instance_public_agent_id(_LEGACY_INSTANCE_ID)
            assert legacy_pid is not None
            assert legacy_pid not in {a["public_agent_id"] for a in agents}

            # --- GET /leaderboard/directional?board_kind=official_benchmark: 2 pooled all-replay rows ----
            board = await client.get(
                "/leaderboard/directional", params={"board_kind": "official_benchmark"}
            )
            assert board.status_code == 200, board.text
            board_body = board.json()
            assert board_body["board_kind"] == "official_benchmark"
            rows = board_body["rows"]
            # ASSERT THE OFFICIAL BOARD TOPOLOGY (exactly 2 officials), not a global competition count.
            assert len(rows) == 2, rows
            assert {row["public_agent_id"] for row in rows} == _OFFICIAL_IDS
            for row in rows:
                assert row["avg_clv_bps"] is not None
                assert row["source_mode"] == "all-replay"

            # --- GET /replay-packs/demo_pack_real/fixtures/18213979/markets: 30 markets, 13 suspended ----
            markets_resp = await client.get(
                f"/replay-packs/demo_pack_real/fixtures/{_REPLAY_FIXTURE_ID}/markets"
            )
            assert markets_resp.status_code == 200, markets_resp.text
            markets_body = markets_resp.json()
            assert markets_body["label"] == "CAPTURED REPLAY"
            markets = markets_body["markets"]
            assert len(markets) == 30, len(markets)
            suspended = [m for m in markets if m["suspended"]]
            assert len(suspended) == 13, len(suspended)
            for m in suspended:
                # HONESTY (M4): a suspended market keeps its EMPTY prob map + retained last-known price.
                assert m["stable_prob_bps"] == {}
                assert m["stable_price"]

            # --- GET /maker/arena-result: sealed result.fixtures byte-UNCHANGED across the seed (AC-11) --
            maker_after = await client.get("/maker/arena-result")
            assert maker_after.status_code == 200, maker_after.text
            fixtures_after = json.dumps(maker_after.json()["result"]["fixtures"], sort_keys=True)
            assert fixtures_after == fixtures_before
