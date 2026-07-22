"""Official Replay League seed — pinned canonical DeployConfigs + a deterministic hash (D1).

This module pins the COMPLETE canonical :class:`~veridex.deploy.preflight.DeployConfig` for the two
OFFICIAL directional agents (``baseline``, ``momentum``) — the verified-runtime-scoreable set on the
shipped ``demo_pack_real`` pack — plus a stable hash that ties the pinned configs to a schema version
so a reproducibility check can detect drift in the seeded DEPLOY CONFIGS. The hash is deliberately
config-only (it equals each deployed instance's ``config_hash``); the public identity binding is
tracked at the store/ledger layer, NOT in this hash (see :func:`seed_definition_hash`).

HONESTY BOUNDARY (Gate-1): exactly TWO official agents. No ``momentum-sharp`` /
``cumulative-drift`` (template-only on the shipped pack) / ``value-vs-venue`` (not runtime-viable) /
``llm``. Each official agent carries BOTH a public identity (``public_agent_id``) and a distinct
runtime id (``agent_id``); the public identity is attached by the seed, NOT by the DeployConfig
(which has no ``public_agent_id`` field).

The canonical config OVERRIDES ``source_mode`` to ``replay`` (off the live default) and pins the
replay SELECTION (``replay_pack_id`` / ``replay_fixture_id``); every other field takes its model
default, so ``config_hash()`` hashes the full default-expanded config exactly as the deploy route
would pin it.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from fastapi import FastAPI
from httpx import ASGITransport

from veridex.api.deploy import _build_agent
from veridex.competition.models import CompetitionStatus
from veridex.deploy.preflight import DeployConfig
from veridex.ingest.replay_catalog import build_catalog
from veridex.ingest.replay_pack import load_pack_marketstates
from veridex.public_agent import OperatorClass, Origin, PublicAgent, Visibility
from veridex.public_agent_backfill import backfill_public_agents
from veridex.public_projection import (
    BoardKind,
    PublicBinding,
    directional_board,
    project_public_rows,
)
from veridex.runtime.evidence import serialize_payload
from veridex.runtime.orchestrator import run_competition
from veridex.scoring import score_run
from veridex.store import Store

#: Schema version of the pinned canonical DeployConfig shape — bumped when the pinned config fields
#: (or their canonical serialization) change, so the seed-definition hash detects drift.
DEPLOYCONFIG_SCHEMA_VERSION = 1

#: The two competitions' fixtures the Official Replay League covers.
LEAGUE_FIXTURES = [18213979, 18222446]

#: The verified R-2 replay pack the official agents replay (the literal catalog id).
_REPLAY_PACK_ID = "demo_pack_real"

#: The pinned replay fixture the canonical config selects (first league fixture); bound to
#: ``LEAGUE_FIXTURES[0]`` so the two constants cannot drift.
_REPLAY_FIXTURE_ID = LEAGUE_FIXTURES[0]


@dataclass(frozen=True)
class OfficialAgentDef:
    """One official league agent's pinned identity + strategy.

    Attributes:
        public_agent_id: The stable PUBLIC identity attached by the seed (not a DeployConfig field).
        template_id: The strategy-archetype template the instance is configured from.
        agent_id: The distinct runtime identifier of the deployed agent.
        strategy: The strategy family (``baseline`` | ``momentum``).
        display_name: Human-facing league display name.
        idempotency_key: The seed idempotency key (stable re-seed dedupe key).
    """

    public_agent_id: str
    template_id: str
    agent_id: str
    strategy: Literal["baseline", "momentum"]
    display_name: str
    idempotency_key: str


#: The exactly-two official, verified-scoreable directional agents.
OFFICIAL_AGENTS: list[OfficialAgentDef] = [
    OfficialAgentDef(
        public_agent_id="agt_official_baseline",
        template_id="official-baseline",
        agent_id="official-baseline-v1",
        strategy="baseline",
        display_name="Official Baseline Control",
        idempotency_key="seed-official-baseline-v1",
    ),
    OfficialAgentDef(
        public_agent_id="agt_official_momentum",
        template_id="official-momentum",
        agent_id="official-momentum-v1",
        strategy="momentum",
        display_name="Official Momentum",
        idempotency_key="seed-official-momentum-v1",
    ),
]


def canonical_deploy_config(defn: OfficialAgentDef) -> DeployConfig:
    """Build the COMPLETE canonical :class:`DeployConfig` for an official agent.

    Overrides ``source_mode`` to ``replay`` (off the live default) and pins the replay selection;
    every other field takes its model default. ``public_agent_id`` is intentionally NOT set — it is
    not a DeployConfig field (identity is attached by the seed).

    Args:
        defn: The official agent definition to pin a config for.

    Returns:
        A valid, default-expanded :class:`DeployConfig` for a ``replay`` deploy.
    """
    return DeployConfig(
        template_id=defn.template_id,
        agent_id=defn.agent_id,
        strategy=defn.strategy,
        source_mode="replay",
        execution_mode="paper",
        replay_pack_id=_REPLAY_PACK_ID,
        replay_fixture_id=_REPLAY_FIXTURE_ID,
        market_allowlist=[],
        venue_allowlist=[],
        min_edge_bps=0,
        max_stake=0.0,
        mm=None,
    )


def seed_definition_hash() -> str:
    """Deterministic fingerprint of the pinned canonical DEPLOY CONFIGS.

    Hashes ``(DEPLOYCONFIG_SCHEMA_VERSION, sorted per-agent config_hash())`` via the ONE canonical
    serializer, so the digest is byte-stable across processes. Deliberately config-only: this equals
    the ``config_hash`` the deploy route pins for each deployed instance, so a reproducibility check
    can compare the shipped seed's configs against the live deployment.

    The public identity binding (``public_agent_id`` ↔ instance) is intentionally NOT hashed here —
    it is tracked in the store instance link + the seed ledger. A change to a pinned config
    (strategy, pack, fixture, knobs) moves this digest; a change to a ``display_name`` /
    ``public_agent_id`` does not (that drift is caught at the store/ledger layer).

    Returns:
        The hex SHA-256 of the canonical serialization of the pinned deploy configs.
    """
    payload = {
        "schema_version": DEPLOYCONFIG_SCHEMA_VERSION,
        "config_hashes": sorted(canonical_deploy_config(a).config_hash() for a in OFFICIAL_AGENTS),
    }
    return hashlib.sha256(serialize_payload(payload).encode("utf-8")).hexdigest()


class ScoreabilityError(RuntimeError):
    """An admitted official agent produced ZERO scored actions on the shipped pack (fail-closed).

    Raised by :func:`assert_scoreable` when the real replay pipeline proves an agent that DEPLOYS
    would silently never act — the honesty failure the guard exists to catch.
    """


async def assert_scoreable(
    agents: Sequence[OfficialAgentDef] = OFFICIAL_AGENTS,
    fixtures: Sequence[int] = LEAGUE_FIXTURES,
) -> None:
    """Run the real build→load→run→score pipeline for each agent on the shipped pack; fail closed.

    Resolves the shipped ``demo_pack_real`` pack the SAME way production does (from the env-driven
    replay catalog, never a hardcoded filesystem path), builds each agent through the ONE canonical
    deploy config, and replays every requested fixture. If any agent's TOTAL ``action_count`` across
    ``fixtures`` is 0, it deploys but silently never acts — a scoreability lie — so the guard raises
    :class:`ScoreabilityError` naming that agent. Returns ``None`` when every agent genuinely acts.

    Args:
        agents: The official agent defs to prove scoreable (defaults to :data:`OFFICIAL_AGENTS`).
        fixtures: The fixture ids to replay (defaults to :data:`LEAGUE_FIXTURES`).

    Raises:
        ScoreabilityError: If the shipped pack is absent, a requested fixture is not in the pack, or
            any admitted agent scores 0 actions across ``fixtures``.
    """
    catalog = build_catalog(os.environ.get("REPLAY_PACK_ROOT", "") or None)
    entry = catalog.get(_REPLAY_PACK_ID)
    if entry is None:
        raise ScoreabilityError(f"pack {_REPLAY_PACK_ID!r} not present in the replay catalog")
    for fid in fixtures:
        if fid not in entry.fixtures:
            raise ScoreabilityError(
                f"fixture {fid} not present in pack {_REPLAY_PACK_ID!r} (has {list(entry.fixtures)})"
            )

    built = [_build_agent(canonical_deploy_config(defn)) for defn in agents]
    totals: dict[str, int] = {defn.agent_id: 0 for defn in agents}
    for fid in fixtures:
        marketstates = load_pack_marketstates(entry.pack_dir, fid)
        run = await run_competition(marketstates, built, source_mode="replay")
        for row in score_run(run):
            totals[row["agent_id"]] += int(row["action_count"])

    for agent_id, total in totals.items():
        if total == 0:
            raise ScoreabilityError(
                f"agent {agent_id!r} scored 0 actions across fixtures {list(fixtures)} — not scoreable"
            )


class SeedError(RuntimeError):
    """A seed phase failed closed: missing pack, an unsealed deploy, or a run with no score rows.

    Raised by :func:`run_seed` at any fail-closed boundary so the seed NEVER publishes a partial /
    unsealed state (an official agent must never surface on the public board before its deployment
    genuinely sealed and produced score rows).
    """


#: The Official Replay League runs are ALL replay provenance — the aggregated board must read
#: ``source_mode == "all-replay"``. The projection asserts each sealed run agrees before publishing.
_SEED_SOURCE_MODE: Literal["replay"] = "replay"


@dataclass(frozen=True)
class SeedResult:
    """The durable outcome of one :func:`run_seed` pass — the created ids + a projection count.

    Every id here is the id of a REAL route-created object (the seed drives the genuine
    deploy→link→await-sealed→create→register→start→project pipeline), so the same fields reconstruct
    on a re-run from the durable seed ledger (deterministic-id idempotency).

    Attributes:
        seed_revision: The seed-run identity the ledger is keyed by.
        public_agent_ids: The two official ``PublicAgent`` ids (deterministic, PUBLIC + OFFICIAL).
        instance_ids: The two deployed instance ids (from the real deploy route), in official order.
        competition_ids: The two finalized competition ids (non-deterministic; ledgered for re-use).
        run_ids: The two sealed run ids the competitions finalized under (projection provenance).
        projected_row_count: The total durable projected rows after this pass (idempotency witness).
    """

    seed_revision: str
    public_agent_ids: list[str]
    instance_ids: list[str]
    competition_ids: list[str]
    run_ids: list[str]
    projected_row_count: int


def _now_iso() -> str:
    """ISO-8601 UTC timestamp for the public-agent ledger writes (caller owns the clock)."""
    return datetime.now(tz=UTC).isoformat()


def _competition_config(fixture_id: int) -> dict[str, Any]:
    """The ``CompetitionConfig`` body for one league competition (replay, roster of 2, bound pack).

    Binds the competition to the verified ``demo_pack_real`` pack + a specific league ``fixture_id``
    at create, so its frozen ``ReplayBinding`` reuses that exact tape at start (never re-selected).
    """
    return {
        "competition_type": "replay_arena",
        "source_mode": "replay",
        "execution_mode": "paper",
        "market_scope": "OfficialReplayLeague",
        "roster_size": 2,
        "pack_id": _REPLAY_PACK_ID,
        "fixture_id": fixture_id,
    }


async def _wait_sealed(
    client: httpx.AsyncClient, instance_id: str, *, timeout_s: float, poll_interval_s: float
) -> None:
    """Poll the REAL status route until ``instance_id`` is ``sealed``; fail closed otherwise (MAJOR 3).

    The deploy route returns PENDING and seals in a BACKGROUND task; the seed MUST await SEALED before
    driving competitions (an unsealed agent must never reach the board). Fails closed — raises
    :class:`SeedError` — on a ``failed`` terminal OR a timeout, never proceeding on an unsealed agent.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        resp = await client.get(f"/agents/instances/{instance_id}/status")
        if resp.status_code != 200:
            raise SeedError(
                f"status poll for {instance_id!r} failed: {resp.status_code} {resp.text}"
            )
        status = resp.json()["status"]
        if status == "sealed":
            return
        if status == "failed":
            raise SeedError(f"deploy for {instance_id!r} reached terminal FAILED (fail closed)")
        if time.monotonic() >= deadline:
            raise SeedError(
                f"deploy for {instance_id!r} did not reach SEALED within {timeout_s}s "
                f"(last status={status!r}) — failing closed rather than publishing an unsealed agent"
            )
        await asyncio.sleep(poll_interval_s)


async def run_seed(
    app: FastAPI,
    store: Store,
    *,
    seed_revision: str,
    operator_token: str | None = None,
    wait_timeout_s: float = 30.0,
    poll_interval_s: float = 0.05,
) -> SeedResult:
    """Drive the REAL app routes to seed the two official agents end-to-end, idempotently.

    Runs the genuine pipeline over an in-process ASGI client — assert-pack → backfill-private →
    assert-scoreable → upsert-official-public-agents → deploy → link → AWAIT-SEALED → create-competitions
    → register → start/finalize → verify-score-rows → project → aggregate → publish-gate — with NO
    startup hook, NO direct app-table row insertion, and NO route duplication (it POSTs the actual
    routes). Every phase fails closed.

    Idempotency (deterministic-id): the official ``PublicAgent`` ids are deterministic (UPSERT), and
    the NON-deterministic ids (deployed ``instance_id`` / created ``competition_id``) are written to
    the durable seed ledger keyed by ``seed_revision``. A re-run READS the ledger and REUSES those ids
    (plus the stable deploy ``Idempotency-Key``), and register/start are skipped once a competition is
    finalized — so a re-seed mints NO duplicate public agent, instance, competition, or projected row.

    Args:
        app: The composed FastAPI app (real deploy + competition routes) to drive.
        store: The SAME shared store the app was built with (the ledger + projection durability).
        seed_revision: The stable seed-run identity the ledger is keyed by.
        operator_token: Requests authenticate via ``AUTH_MODE=dev``'s fixed principal when
            ``operator_token`` is None; when provided, ``operator_token`` is sent as a Bearer
            ``Authorization`` header on every in-process client request.
        wait_timeout_s: Max seconds to await BOTH deployments reaching SEALED (fail closed on timeout).
        poll_interval_s: Delay between status polls (also yields so the background seal task progresses).

    Returns:
        The :class:`SeedResult` describing the created ids + the durable projected-row count.

    Raises:
        SeedError: At any fail-closed boundary (absent pack, deploy failure/timeout, empty score rows).
    """
    ledger = await store.get_seed_state(seed_revision) or {}
    instances_ledger: dict[str, str] = dict(ledger.get("instances", {}))
    competition_ledger: list[str] = list(ledger.get("competitions", []))
    now = _now_iso()

    # Phase 1 — assert_pack: the shipped image MUST carry demo_pack_real (catch a misaligned image early).
    catalog = build_catalog(os.environ.get("REPLAY_PACK_ROOT", "") or None)
    if catalog.get(_REPLAY_PACK_ID) is None:
        raise SeedError(f"pack {_REPLAY_PACK_ID!r} absent from the replay catalog (image misaligned)")

    # Phase 2 — backfill_private: BEFORE any public read, map every pre-existing un-linked instance to a
    # PRIVATE PublicAgent (origin=UNKNOWN) so a legacy deployment can NEVER surface on a public board.
    await backfill_public_agents(store, now=now, mint_id=lambda iid: f"agt_bf_{iid}")

    # Phase 3 — assert_scoreable: the D2 guard — fail closed if either official agent is inert.
    await assert_scoreable()

    # Phase 4 — upsert_public_agents: the two official PUBLIC + OFFICIAL identities (deterministic ids).
    for defn in OFFICIAL_AGENTS:
        await store.persist_public_agent(
            PublicAgent(
                public_agent_id=defn.public_agent_id,
                display_name=defn.display_name,
                operator_class=OperatorClass.OFFICIAL,
                origin=Origin.OFFICIAL,
                visibility=Visibility.PUBLIC,
                owner_ref=None,
                created_at=now,
                updated_at=now,
                version=1,
            )
        )

    run_ids: list[str] = []
    auth_headers = {"Authorization": f"Bearer {operator_token}"} if operator_token is not None else None
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=auth_headers
    ) as client:
        # Phase 5 — deploy_instances: POST the real deploy route (stable Idempotency-Key), then link the
        # instance to its official PUBLIC identity. Reuse the ledger id on a re-run (no duplicate deploy).
        for defn in OFFICIAL_AGENTS:
            instance_id = instances_ledger.get(defn.public_agent_id)
            if instance_id is None:
                resp = await client.post(
                    "/agents/deploy",
                    json=canonical_deploy_config(defn).model_dump(mode="json"),
                    headers={"Idempotency-Key": defn.idempotency_key},
                )
                if resp.status_code != 200:
                    raise SeedError(
                        f"deploy failed for {defn.public_agent_id!r}: {resp.status_code} {resp.text}"
                    )
                instance_id = resp.json()["instance_id"]
                instances_ledger[defn.public_agent_id] = instance_id
            await store.link_instance_public_agent(instance_id, defn.public_agent_id)
        await _persist_ledger(store, seed_revision, instances_ledger, competition_ledger)

        # Phase 6 — wait_deployments_terminal (MAJOR 3): both instances MUST reach SEALED before we drive
        # any competition. Fails closed on a failed terminal OR a timeout — never publishes an unsealed agent.
        for instance_id in [instances_ledger[d.public_agent_id] for d in OFFICIAL_AGENTS]:
            await _wait_sealed(
                client, instance_id, timeout_s=wait_timeout_s, poll_interval_s=poll_interval_s
            )

        # Phase 7 — create_competitions: one per league fixture, bound to demo_pack_real. Reuse ledgered
        # ids. Persist the ledger PER-OBJECT (right after each id is appended) so a crash mid-loop leaves
        # at most the single last-created (unledgered) id to be re-created — the same tiny crash window the
        # instance path has via its deploy Idempotency-Key. (Fully closing it needs a deterministic
        # competition id — out of scope here; this makes the crash-recovery window symmetric, not zero.)
        if not competition_ledger:
            for fixture_id in LEAGUE_FIXTURES:
                resp = await client.post("/competitions", json=_competition_config(fixture_id))
                if resp.status_code != 200:
                    raise SeedError(f"create competition failed: {resp.status_code} {resp.text}")
                competition_ledger.append(resp.json()["competition_id"])
                await _persist_ledger(store, seed_revision, instances_ledger, competition_ledger)

        # Phase 8 — register: bind BOTH deployed instances onto EACH competition (instance_id + pinned
        # config_hash) so the arena runs the ACTUAL deployed contestant. Idempotent: skip finalized
        # competitions and already-registered agents; a racing 409 is a benign no-op.
        for cid in competition_ledger:
            competition = await store.get_competition(cid)
            if competition.status is CompetitionStatus.FINALIZED:
                continue
            already = {entry.agent_id for entry in competition.entries}
            for defn in OFFICIAL_AGENTS:
                instance_id = instances_ledger[defn.public_agent_id]
                instance = await store.get_agent_instance(instance_id)
                if instance.agent_id in already:
                    continue
                body = {
                    "agent_id": instance.agent_id,
                    "owner": "veridex-official",
                    "strategy": defn.strategy,
                    "model": None,
                    "proof_mode": "reproducible",
                    "config_hash": instance.config_hash,
                    "instance_id": instance_id,
                }
                resp = await client.post(f"/competitions/{cid}/agents", json=body)
                if resp.status_code not in (200, 409):
                    raise SeedError(
                        f"register {instance.agent_id!r} on {cid!r} failed: {resp.status_code} {resp.text}"
                    )

        # Phase 9 — start_finalize: run each competition offline/deterministically to a finalized state.
        for cid in competition_ledger:
            competition = await store.get_competition(cid)
            if competition.status is CompetitionStatus.FINALIZED:
                if competition.run_id is None:
                    raise SeedError(f"competition {cid!r} finalized without a run_id")
                run_ids.append(competition.run_id)
                continue
            resp = await client.post(f"/competitions/{cid}/start")
            if resp.status_code == 200:
                run_id = resp.json()["run_id"]
            elif resp.status_code == 409:
                # A concurrent/prior start already finalized it — read the authoritative run_id.
                competition = await store.get_competition(cid)
                if competition.run_id is None:
                    raise SeedError(f"competition {cid!r} start 409 but no run_id persisted")
                run_id = competition.run_id
            else:
                raise SeedError(f"start competition {cid!r} failed: {resp.status_code} {resp.text}")
            run_ids.append(run_id)

    # Phase 10 — verify_score_rows: fail closed if a finalized competition sealed with NO score rows.
    for cid, run_id in zip(competition_ledger, run_ids, strict=True):
        run_result = await store.load_run(run_id)
        if not run_result.score_rows:
            raise SeedError(f"competition {cid!r} run {run_id!r} sealed with NO score rows")

    # Phase 11 — project_and_aggregate: reconstruct each projection binding DURABLY FROM THE STORE
    # (instance pinned agent_id + entry config_hash + get_instance_public_agent_id) — no in-memory
    # side channel — then persist projected rows (UPSERT-idempotent, no double-count on a re-run).
    for cid, run_id in zip(competition_ledger, run_ids, strict=True):
        competition = await store.get_competition(cid)
        run_result = await store.load_run(run_id)
        if run_result.source_mode != _SEED_SOURCE_MODE:
            raise SeedError(
                f"competition {cid!r} run {run_id!r} sealed source_mode={run_result.source_mode!r}, "
                f"expected {_SEED_SOURCE_MODE!r} (replay provenance is load-bearing)"
            )
        bindings = await _reconstruct_bindings(store, competition.entries)
        # B1 projects the AGGREGATED per-agent score_run rows (keyed by runtime agent id), NOT the raw
        # per-(tick, agent) sealed rows on RunResult — so aggregate the sealed run here first.
        sealed_rows = score_run(run_result)
        projected = project_public_rows(
            sealed_rows, bindings, run_id=run_id, source_mode=_SEED_SOURCE_MODE
        )
        await store.persist_projected_rows(projected)

    # Phase 12 — publish_gate: the officials are already visibility=PUBLIC (phase 4); now that projected
    # rows exist the board join exposes them. Read it once to prove the publish surface is live.
    await directional_board(store, board_kind=BoardKind.OFFICIAL_BENCHMARK)

    projected_rows = await store.list_projected_rows()
    return SeedResult(
        seed_revision=seed_revision,
        public_agent_ids=[defn.public_agent_id for defn in OFFICIAL_AGENTS],
        instance_ids=[instances_ledger[defn.public_agent_id] for defn in OFFICIAL_AGENTS],
        competition_ids=list(competition_ledger),
        run_ids=run_ids,
        projected_row_count=len(projected_rows),
    )


async def _persist_ledger(
    store: Store, seed_revision: str, instances: dict[str, str], competitions: list[str]
) -> None:
    """Write the seed ledger (created ids) durably, keyed by ``seed_revision`` (re-run reuse source)."""
    state: dict[str, Any] = {"instances": dict(instances), "competitions": list(competitions)}
    await store.persist_seed_state(seed_revision, state)


async def _reconstruct_bindings(
    store: Store, entries: Sequence[Any]
) -> dict[str, PublicBinding]:
    """Rebuild the runtime-agent-id → :class:`PublicBinding` map from DURABLE STORE STATE ALONE.

    For each instance-bound roster entry, the binding is reconstructed from three persisted facts:
    the instance's pinned ``agent_id`` (the runtime key), the entry's pinned deployment ``config_hash``,
    and the durable instance→public-agent link. A fresh process holding only the store can therefore
    rebuild the exact projection bindings — no in-memory side channel is required.

    Raises:
        SeedError: An instance-bound entry has no durable public-agent link (fail closed).
    """
    bindings: dict[str, PublicBinding] = {}
    for entry in entries:
        if entry.instance_id is None:
            continue
        instance = await store.get_agent_instance(entry.instance_id)
        public_agent_id = await store.get_instance_public_agent_id(entry.instance_id)
        if public_agent_id is None:
            raise SeedError(
                f"instance {entry.instance_id!r} has no public-agent link (cannot project — fail closed)"
            )
        bindings[instance.agent_id] = PublicBinding(
            public_agent_id=public_agent_id,
            instance_id=entry.instance_id,
            config_hash=entry.config_hash or instance.config_hash,
        )
    return bindings
