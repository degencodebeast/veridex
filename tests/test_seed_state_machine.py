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
import dataclasses
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
    LEAGUE_FIXTURES,
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


def _app_on(store: InMemoryStore) -> Any:
    """A FRESH app over the SAME store — the way a re-run/restart drives the durable seed ledger."""
    return create_app(store=store, settings=_dev_settings())


async def _assert_two_fixture_topology(
    store: InMemoryStore, seed_revision: str, result: Any
) -> None:
    """The resumed league is EXACTLY the two fixture-bound, ledgered+finalized competitions."""
    ledger = await store.get_seed_state(seed_revision)
    assert ledger is not None
    comps = ledger["competitions"]
    assert set(comps) == {str(f) for f in LEAGUE_FIXTURES}  # fixture-keyed ledger, both slots present
    assert result.competition_ids == [comps[str(f)] for f in LEAGUE_FIXTURES]  # fixture-ordered
    assert len(result.competition_ids) == 2
    for cid in comps.values():
        competition = await store.get_competition(cid)
        assert competition.status is CompetitionStatus.FINALIZED


async def _assert_board_two_runs(store: InMemoryStore) -> None:
    """Both official rows pool BOTH league runs (runs == 2) on the resumed board."""
    board = await directional_board(store, board_kind=BoardKind.OFFICIAL_BENCHMARK)
    mine = _officials_on_board(board)
    assert set(mine) == _OFFICIAL_IDS
    for row in mine.values():
        assert row["runs"] == 2


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
# MAJOR 1 — fixture-keyed competition ledger: a crash between the two competition creations
# resumes to EXACTLY the two fixture-bound finalized competitions (board runs == 2), never the
# old all-or-nothing skip that finalized ONE. Two boundaries, each RED on the flat-list ledger.
# =====================================================================================


async def test_crash_recovery_boundary_a_resumes_two_fixture_topology(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boundary A — crash AFTER the first fixture's competition is durably ledgered, BEFORE the
    second is created.

    Injection: wrap ``_persist_ledger`` so the FIRST fixture's write commits, THEN it raises — the
    exact "first ledgered, second not yet created" window. Pre-fix (flat ``list`` ledger guarded by
    ``if not competition_ledger``) the resume sees a NON-empty list, skips ALL creation, and finalizes
    ONLY the first competition → board runs [1, 1] (RED). Post-fix (fixture-keyed) the resume creates
    ONLY the missing second fixture → exactly 2 finalized fixture-bound competitions, board runs == 2.
    """
    app, store = _build()
    original = seed_mod._persist_ledger

    async def _commit_then_crash(
        store_: Any, seed_revision: str, instances: Any, competitions: Any
    ) -> None:
        await original(store_, seed_revision, instances, competitions)
        if len(competitions) == 1:  # first fixture durably ledgered — crash before the second create
            raise RuntimeError("injected crash after the first fixture is ledgered")

    monkeypatch.setattr(seed_mod, "_persist_ledger", _commit_then_crash)
    try:
        with pytest.raises(RuntimeError):
            await run_seed(app, store, seed_revision="rev-a")
    finally:
        monkeypatch.setattr(seed_mod, "_persist_ledger", original)
        await _drain(app)

    app2 = _app_on(store)
    try:
        result = await run_seed(app2, store, seed_revision="rev-a")
    finally:
        await _drain(app2)

    await _assert_two_fixture_topology(store, "rev-a", result)
    await _assert_board_two_runs(store)


async def test_crash_recovery_boundary_b_second_created_but_unledgered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boundary B — crash AFTER the second fixture's competition is CREATED (the create POST returned)
    but BEFORE its ledger write.

    Injection: wrap ``_persist_ledger`` so the two-fixture write raises BEFORE committing — the second
    competition exists in the store but is never ledgered (an orphan DRAFT). Pre-fix the resume off the
    one-entry flat list finalizes ONLY the first competition (RED). Post-fix the resume creates the
    correct missing second fixture; the LEDGERED+FINALIZED set is EXACTLY the two fixtures (an orphan
    DRAFT may remain, and is harmless), board runs == 2.
    """
    app, store = _build()
    original = seed_mod._persist_ledger

    async def _crash_before_second_write(
        store_: Any, seed_revision: str, instances: Any, competitions: Any
    ) -> None:
        if len(competitions) == 2:  # second created, its ledger write about to happen — crash first
            raise RuntimeError("injected crash before the second fixture's ledger write")
        await original(store_, seed_revision, instances, competitions)

    monkeypatch.setattr(seed_mod, "_persist_ledger", _crash_before_second_write)
    try:
        with pytest.raises(RuntimeError):
            await run_seed(app, store, seed_revision="rev-b")
    finally:
        monkeypatch.setattr(seed_mod, "_persist_ledger", original)
        await _drain(app)

    app2 = _app_on(store)
    try:
        result = await run_seed(app2, store, seed_revision="rev-b")
    finally:
        await _drain(app2)

    # The ledgered+finalized topology is EXACTLY the two fixture slots (an unledgered orphan DRAFT
    # competition may also exist in the store — the orphan policy — but it is never in the ledger).
    await _assert_two_fixture_topology(store, "rev-b", result)
    await _assert_board_two_runs(store)
    ledger = await store.get_seed_state("rev-b")
    assert ledger is not None
    assert set(ledger["competitions"]) == {str(f) for f in LEAGUE_FIXTURES}


# =====================================================================================
# MAJOR 2 — reproducibility/identity drift is ENFORCED on resume via a persisted manifest.
# A same-seed_revision re-run whose pinned config or official identity set changed FAILS CLOSED.
# =====================================================================================


async def test_run_seed_fails_closed_on_config_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    """A resume whose pinned canonical config drifted (config_hash changes) must FAIL CLOSED.

    Pre-fix ``run_seed`` never persisted/validated a manifest, so it silently reused the old deployed
    config (RED — no raise). Post-fix the ledgered manifest no longer matches the freshly-computed one
    → :class:`SeedError` (require a NEW --seed-revision).
    """
    app, store = _build()
    try:
        await run_seed(app, store, seed_revision="drift-cfg")
    finally:
        await _drain(app)

    # Mutate the pinned replay fixture the canonical config selects → both agents' config_hash (and
    # the seed_definition_hash) change → the persisted manifest no longer matches the current one.
    monkeypatch.setattr(seed_mod, "_REPLAY_FIXTURE_ID", 90000001)

    app2 = _app_on(store)
    try:
        with pytest.raises(SeedError):
            await run_seed(app2, store, seed_revision="drift-cfg")
    finally:
        await _drain(app2)


async def test_run_seed_fails_closed_on_identity_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    """A resume whose official identity set changed must FAIL CLOSED and NOT mint a third identity.

    Pre-fix ``run_seed`` relinked the old instance to the renamed public id while retaining the old
    public/projected rows → a THIRD official board identity (Codex repro), no raise (RED). Post-fix the
    ledgered identity manifest no longer matches → :class:`SeedError` BEFORE any public-agent upsert, so
    the board never gains a third identity.
    """
    app, store = _build()
    try:
        await run_seed(app, store, seed_revision="drift-id")
    finally:
        await _drain(app)

    # Rename one official public identity so the manifest's identity set differs.
    drifted = list(OFFICIAL_AGENTS)
    drifted[0] = dataclasses.replace(drifted[0], public_agent_id="agt_official_baseline_v2")
    monkeypatch.setattr(seed_mod, "OFFICIAL_AGENTS", drifted)

    app2 = _app_on(store)
    try:
        with pytest.raises(SeedError):
            await run_seed(app2, store, seed_revision="drift-id")
    finally:
        await _drain(app2)

    # Fail-closed BEFORE phase-4 upsert: the renamed identity never reaches the official board.
    board = await directional_board(store, board_kind=BoardKind.OFFICIAL_BENCHMARK)
    assert {row["public_agent_id"] for row in board} == _OFFICIAL_IDS


# =====================================================================================
# MAJOR 3 — publish_gate is a REAL fail-closed predicate: a broken publish surface RAISES.
# =====================================================================================


async def test_publish_gate_rejects_partial_projection() -> None:
    """Dropping one official's projected rows (board loses that identity) makes the publish gate RAISE.

    Pre-fix phase 12 discarded the board read, so ``run_seed`` returned success with a one-agent board
    (RED — no predicate existed). Post-fix the predicate requires the exact official id set → raises.
    """
    app, store = _build()
    try:
        await run_seed(app, store, seed_revision="pg-partial")
    finally:
        await _drain(app)

    ledger = await store.get_seed_state("pg-partial")
    assert ledger is not None
    competitions = ledger["competitions"]

    baseline_pid = OFFICIAL_AGENTS[0].public_agent_id
    # Drop ALL of one official's projected rows → the official board loses that identity entirely.
    store._projected_rows = {
        key: row for key, row in store._projected_rows.items() if key[1] != baseline_pid
    }

    with pytest.raises(SeedError):
        await seed_mod._assert_publish_gate(store, competitions)


async def test_publish_gate_rejects_missing_run() -> None:
    """An official present on the board but with only ONE pooled run (runs != league fixtures) RAISES.

    Post-fix the predicate requires ``runs == len(LEAGUE_FIXTURES)`` for every row; dropping one of an
    agent's two per-run projected rows leaves it at runs == 1 → raise (a partial league must not ship).
    """
    app, store = _build()
    try:
        await run_seed(app, store, seed_revision="pg-missing")
    finally:
        await _drain(app)

    ledger = await store.get_seed_state("pg-missing")
    assert ledger is not None
    competitions = ledger["competitions"]

    baseline_pid = OFFICIAL_AGENTS[0].public_agent_id
    # Drop exactly ONE of the two per-run rows for one official → runs == 1 (!= the 2 league fixtures).
    drop_key = next(key for key in store._projected_rows if key[1] == baseline_pid)
    del store._projected_rows[drop_key]

    with pytest.raises(SeedError):
        await seed_mod._assert_publish_gate(store, competitions)


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
