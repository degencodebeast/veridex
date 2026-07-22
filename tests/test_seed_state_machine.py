"""D3 — the Official Replay League seed STATE MACHINE driving the REAL app routes (TDD).

``run_seed`` drives the genuine app routes over an in-process ASGI client — deploy → link →
AWAIT-SEALED → create-competition → register → start/finalize → verify → project → aggregate —
to seed the two official agents idempotently. NO startup hook, NO direct app-table row insertion,
NO route duplication. Every test is OFFLINE (InMemoryStore + real ``create_app``), zero-wire; the
deploy/replay path resolves the bundled ``demo_pack_real`` pack the autouse conftest fixture mounts.

The five load-bearing properties (one test each):

1. End-to-end: two PUBLIC official agents, two finalized competitions, a 2-row numeric pooled
   OFFICIAL_BENCHMARK board (no ``avg_clv_bps is None``), all ``source_mode == "all-replay"``.
2. Idempotent (AC-2): a re-run with the SAME seed_revision mints no duplicate public agent,
   instance, competition, or projected row (counts unchanged; created ids reused).
3. Rollout ordering (M6): a pre-existing un-linked instance is linked to a PRIVATE PublicAgent
   BEFORE any board read, so it can NEVER surface on the OFFICIAL_BENCHMARK board.
4. MAJOR 3 fail-closed: a deploy that never seals (its terminal status write parked) makes
   ``run_seed`` BLOCK at wait-sealed and fail closed (raise) on timeout — never publishing an
   unsealed agent onto the board.
5. Binding reconstruction after restart: the projection bindings are reconstructable from
   PERSISTED STORE STATE ALONE (instance pinned agent_id + config_hash + the durable link).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from veridex.api.router import create_app
from veridex.competition.models import CompetitionStatus
from veridex.config import Settings
from veridex.deploy.instance import AgentInstance, DeployStatus
from veridex.public_agent import OperatorClass, Visibility
from veridex.public_projection import BoardKind, PublicBinding, directional_board
from veridex.seed.official_replay_league import (
    OFFICIAL_AGENTS,
    SeedError,
    run_seed,
)
from veridex.store import InMemoryStore

_OFFICIAL_IDS = {defn.public_agent_id for defn in OFFICIAL_AGENTS}


def _dev_settings() -> Settings:
    """Dev-mode settings: every request auto-resolves the fixed principal (no auth header needed)."""
    return Settings(_env_file=None, app_env="development", auth_mode="dev")  # type: ignore[call-arg]


def _build() -> tuple[Any, InMemoryStore]:
    store = InMemoryStore()
    app = create_app(store=store, settings=_dev_settings())
    return app, store


def _officials_on_board(board: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {row["public_agent_id"]: row for row in board if row["public_agent_id"] in _OFFICIAL_IDS}


async def _drain(app: Any) -> None:
    """Cancel any still-pending deploy background tasks (park tests leave one parked)."""
    tasks = list(getattr(app.state, "deploy_background_tasks", set()))
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# =====================================================================================
# RED #1 — end-to-end: two PUBLIC officials, two finalized competitions, a numeric pooled board.
# =====================================================================================


async def test_run_seed_end_to_end_publishes_official_benchmark() -> None:
    app, store = _build()
    try:
        result = await run_seed(app, store, seed_revision="rev1")
    finally:
        await _drain(app)

    # Two PUBLIC + OFFICIAL public agents.
    assert set(result.public_agent_ids) == _OFFICIAL_IDS
    for pid in _OFFICIAL_IDS:
        agent = await store.get_public_agent(pid)
        assert agent is not None
        assert agent.visibility is Visibility.PUBLIC
        assert agent.operator_class is OperatorClass.OFFICIAL

    # Two finalized competitions.
    assert len(result.competition_ids) == 2
    for cid in result.competition_ids:
        competition = await store.get_competition(cid)
        assert competition.status is CompetitionStatus.FINALIZED
        assert competition.run_id is not None

    # Two deployed instances (real deploy route).
    assert len(result.instance_ids) == 2
    for instance_id in result.instance_ids:
        instance = await store.get_agent_instance(instance_id)
        assert instance.status is DeployStatus.SEALED

    # The aggregated OFFICIAL_BENCHMARK board: exactly the two officials, numeric + all-replay.
    board = await directional_board(store, board_kind=BoardKind.OFFICIAL_BENCHMARK)
    mine = _officials_on_board(board)
    assert set(mine) == _OFFICIAL_IDS
    for row in mine.values():
        assert row["avg_clv_bps"] is not None  # numeric pooled CLV, never a None hole
        assert row["source_mode"] == "all-replay"  # replay provenance is load-bearing
        assert row["runs"] == 2  # pooled across BOTH league competitions


# =====================================================================================
# RED #2 — idempotent (AC-2): a re-run with the same seed_revision creates no duplicates.
# =====================================================================================


async def test_run_seed_is_idempotent_no_duplicates() -> None:
    app, store = _build()
    try:
        first = await run_seed(app, store, seed_revision="rev-idem")

        public_before = len(await store.list_public_agents())
        instances_before = len(await store.list_agent_instances())
        competitions_before = len(await store.list_competitions())
        projected_before = len(await store.list_projected_rows())

        second = await run_seed(app, store, seed_revision="rev-idem")
    finally:
        await _drain(app)

    # Created ids are REUSED verbatim (no new instance / competition minted).
    assert second.instance_ids == first.instance_ids
    assert second.competition_ids == first.competition_ids

    # No duplicate rows of any kind after the re-run.
    assert len(await store.list_public_agents()) == public_before
    assert len(await store.list_agent_instances()) == instances_before
    assert len(await store.list_competitions()) == competitions_before
    assert len(await store.list_projected_rows()) == projected_before


# =====================================================================================
# RED #3 — rollout ordering (M6): a pre-existing un-linked instance is made PRIVATE, never on board.
# =====================================================================================


async def test_preexisting_instance_backfilled_private_never_on_board() -> None:
    app, store = _build()
    now = datetime.now(tz=UTC).isoformat()
    # A legacy, UN-LINKED deployed instance present BEFORE the seed runs.
    await store.persist_agent_instance(
        AgentInstance(
            instance_id="inst-legacy",
            template_id="legacy-template",
            agent_id="legacy-agent",
            submitted_config={},
            effective_config={},
            config_hash="l" * 8,
            policy_hash="p" * 8,
            source_mode="replay",
            execution_mode="paper",
            run_id="run-legacy",
            status=DeployStatus.SEALED,
            operator_id="did:privy:legacy",
            created_at=now,
            updated_at=now,
        )
    )

    try:
        await run_seed(app, store, seed_revision="rev-rollout")
    finally:
        await _drain(app)

    # The legacy instance was linked to a PRIVATE, USER-class public agent BEFORE any public read.
    legacy_pid = await store.get_instance_public_agent_id("inst-legacy")
    assert legacy_pid is not None
    legacy_agent = await store.get_public_agent(legacy_pid)
    assert legacy_agent is not None
    assert legacy_agent.visibility is Visibility.PRIVATE
    assert legacy_agent.operator_class is OperatorClass.USER

    # It can NEVER surface on the official benchmark board.
    board = await directional_board(store, board_kind=BoardKind.OFFICIAL_BENCHMARK)
    assert legacy_pid not in {row["public_agent_id"] for row in board}


# =====================================================================================
# RED #4 — MAJOR 3 fail-closed: an unsealed deploy makes run_seed block + raise, never publish.
# =====================================================================================


async def test_run_seed_fails_closed_when_deploy_never_seals(monkeypatch: pytest.MonkeyPatch) -> None:
    app, store = _build()

    # PARK the deploy: drop the durable status transitions so the instance stays PENDING forever
    # (the background seal never advances the observable status the wait-loop polls).
    async def _never_advance(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(store, "update_agent_instance_status", _never_advance)

    try:
        with pytest.raises(SeedError):
            # A SHORT timeout keeps the fail-closed assertion fast.
            await run_seed(app, store, seed_revision="rev-park", wait_timeout_s=0.5, poll_interval_s=0.02)
    finally:
        await _drain(app)

    # Fail-closed: no competition was created and NOTHING was published to the board.
    assert await store.list_competitions() == []
    assert await store.list_projected_rows() == []
    board = await directional_board(store, board_kind=BoardKind.OFFICIAL_BENCHMARK)
    assert _officials_on_board(board) == {}


# =====================================================================================
# RED #5 — the projection bindings are reconstructable from PERSISTED STORE STATE ALONE.
# =====================================================================================


async def test_projection_bindings_reconstructable_from_store_state() -> None:
    app, store = _build()
    try:
        result = await run_seed(app, store, seed_revision="rev-rebuild")
    finally:
        await _drain(app)

    # A fresh process holding ONLY the store rebuilds each PublicBinding from three persisted facts:
    # the instance pinned agent_id (runtime key), the entry pinned config_hash, and the durable link.
    for cid in result.competition_ids:
        competition = await store.get_competition(cid)
        rebuilt: dict[str, PublicBinding] = {}
        for entry in competition.entries:
            assert entry.instance_id is not None
            instance = await store.get_agent_instance(entry.instance_id)
            public_agent_id = await store.get_instance_public_agent_id(entry.instance_id)
            assert public_agent_id in _OFFICIAL_IDS
            assert entry.config_hash == instance.config_hash  # entry pins the deployment hash
            rebuilt[instance.agent_id] = PublicBinding(
                public_agent_id=public_agent_id,
                instance_id=entry.instance_id,
                config_hash=entry.config_hash,
            )
        # Both official contestants are reconstructed, keyed by their runtime agent id.
        assert len(rebuilt) == 2
        assert {b.public_agent_id for b in rebuilt.values()} == _OFFICIAL_IDS
