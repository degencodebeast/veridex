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
import os
from datetime import UTC, datetime
from typing import Any

import pytest

import veridex.seed.official_replay_league as seed_mod
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


# =====================================================================================
# Crash-recovery symmetry — a crash between the two competition creations must NOT
# double-create on re-run (per-object ledger persist, symmetric with the instance path).
# =====================================================================================


async def test_run_seed_recovers_after_crash_between_competition_creations() -> None:
    """A crash after the first competition is durably ledgered (before the second is) must NOT
    make a re-run re-create BOTH competitions — the ledger persists PER-OBJECT so exactly 2 exist.

    Injection: wrap ``_persist_ledger`` so the ledger write for the state that carries BOTH
    competition ids raises (simulating a crash right when the second competition would be durably
    ledgered, AFTER the first is). Pre-fix (single persist AFTER the whole creation loop) the crash
    leaves ``competitions == []`` durably, so a re-run re-creates BOTH → 4 total (RED). Post-fix
    (per-object persist inside the loop) the first competition is durably ledgered before the crash,
    so a re-run reuses it and does not re-create both → exactly 2 (GREEN).
    """
    import pytest as _pytest

    app, store = _build()
    original_persist = seed_mod._persist_ledger

    async def _crashing_persist(
        store: Any, seed_revision: str, instances: Any, competitions: list[str]
    ) -> None:
        # Crash exactly when the ledger write carries BOTH competition ids — i.e. after the first
        # competition has been (per-object) ledgered and the second has just been created.
        if len(competitions) == 2:
            raise RuntimeError("injected crash before the second competition is durably ledgered")
        await original_persist(store, seed_revision, instances, competitions)

    try:
        seed_mod._persist_ledger = _crashing_persist  # type: ignore[assignment]
        with _pytest.raises(RuntimeError):
            await run_seed(app, store, seed_revision="rev-crash")
    finally:
        seed_mod._persist_ledger = original_persist  # type: ignore[assignment]
        await _drain(app)

    # Re-run with the crash cleared and the SAME seed_revision → must NOT double-create.
    try:
        await run_seed(app, store, seed_revision="rev-crash")
    finally:
        await _drain(app)

    competitions = await store.list_competitions()
    assert len(competitions) == 2  # NOT 3-4: the first was ledgered+reused across the crash.


# =====================================================================================
# operator_token — when provided it is sent as a Bearer Authorization header (FIX 2).
# =====================================================================================


async def test_operator_token_sent_as_bearer_header_and_none_path_unchanged() -> None:
    from httpx import ASGITransport, Request, Response

    captured: dict[str, list[str | None]] = {"auth": []}

    class _RecordingTransport(ASGITransport):
        async def handle_async_request(self, request: Request) -> Response:
            captured["auth"].append(request.headers.get("authorization"))
            return await super().handle_async_request(request)

    # When provided, EVERY outgoing request carries the Bearer header.
    app, store = _build()
    try:
        seed_mod.ASGITransport = _RecordingTransport  # type: ignore[assignment,misc]
        await run_seed(app, store, seed_revision="rev-tok", operator_token="tok-abc")
    finally:
        seed_mod.ASGITransport = ASGITransport  # type: ignore[assignment,misc]
        await _drain(app)
    assert captured["auth"]  # requests were actually observed
    assert all(h == "Bearer tok-abc" for h in captured["auth"])

    # None path unchanged: no Authorization header is attached.
    captured["auth"].clear()
    app2, store2 = _build()
    try:
        seed_mod.ASGITransport = _RecordingTransport  # type: ignore[assignment,misc]
        await run_seed(app2, store2, seed_revision="rev-notok", operator_token=None)
    finally:
        seed_mod.ASGITransport = ASGITransport  # type: ignore[assignment,misc]
        await _drain(app2)
    assert captured["auth"]
    assert all(h is None for h in captured["auth"])


# =====================================================================================
# Postgres parity — the seed-ledger store methods (persist_seed_state / get_seed_state)
# must behave identically over Postgres, not just InMemoryStore (skipped unless
# DATABASE_URL + psycopg present).
# =====================================================================================


def _psycopg_available() -> bool:
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(
    not (os.getenv("DATABASE_URL") and _psycopg_available()),
    reason="Postgres round-trip: set DATABASE_URL and install psycopg",
)
async def test_postgres_seed_state_ledger_round_trip() -> None:
    import psycopg

    from veridex.store import PostgresStore

    dsn = os.environ["DATABASE_URL"]
    store = PostgresStore(dsn=dsn)

    revision = "pg_seed_ledger_rt_v1"

    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        await store.init_db(conn)
        # Clean slate: a prior run of this test leaves the revision behind (UPSERT is
        # idempotent for the round-trip/overwrite assertions below, but the "missing key"
        # assertion needs the row genuinely absent first).
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM seed_state WHERE seed_revision = %s", (revision,))
        await conn.commit()

    # Missing key -> None
    assert await store.get_seed_state(revision) is None

    # Round-trip
    first_state = {"phase": "deploy", "instance_ids": ["i1", "i2"], "n": 1}
    await store.persist_seed_state(revision, first_state)
    assert await store.get_seed_state(revision) == first_state

    # UPSERT last-write-wins (same revision, new snapshot replaces the old one)
    second_state = {"phase": "sealed", "n": 2}
    await store.persist_seed_state(revision, second_state)
    assert await store.get_seed_state(revision) == second_state
