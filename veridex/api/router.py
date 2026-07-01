"""FastAPI surface — B11b (REQ-115 / AC-115) + Phase-2A competition endpoints (Task 6 / P2A-6).

Factory: ``create_app(store)`` returns a configured FastAPI application.  The module-level
``_default_store`` is used when no store is injected (running under Uvicorn).  For testing,
pass an ``InMemoryStore`` to keep everything in-process and offline.

Phase-1 endpoints (unchanged — REQ-222 / AC-212)
-------------------------------------------------
``POST /demo/run``
    Run a demo competition over two deterministic agents (no LLM, no network).
``GET /leaderboard``
    Aggregate scores from all previously stored runs into a ranked board.
``GET /runs/{run_id}``
    Load a persisted run and return its proof card.  404 if unknown.

Phase-2A competition endpoints (additive)
-----------------------------------------
``POST /competitions``
    Create a new DRAFT competition from a ``CompetitionConfig`` body.
``POST /competitions/{competition_id}/agents``
    Register an agent entry on the roster.  Pins ``config_hash`` + normalises ``proof_mode``.
``POST /competitions/{competition_id}/start``
    Run the competition offline/deterministically (2A simplification — see comment in handler)
    and return the finalized state with ``run_id`` set.
``GET /competitions/{competition_id}``
    Return full competition state including leaderboard derived from the canonical event log.
``GET /competitions``
    List all competitions with an optional ``?status=`` filter.
``GET /competitions/{competition_id}/events``
    Return the ordered event log tail (``seq > since_seq``); mirrors WS replay parity.

No auth / Redis / rate-limiting — Phase-2 only (CON-009).
"""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query

from veridex.api.demo_fixtures import (
    build_agents_from_roster,
    build_demo_ticks,
    contrarian_agent,
)
from veridex.api.schemas import (
    AgentRegisterResponse,
    ApprovalResponse,
    CompetitionCreateResponse,
    CompetitionLeaderboardRow,
    CompetitionStartResponse,
    CompetitionStateResponse,
    CompetitionSummaryResponse,
    DemoRunResponse,
    ExplainRequest,
    FeedHealthResponse,
    InspectorRecord,
    KillSwitchResponse,
    LeaderboardResponse,
    LeaderboardRow,
    RuntimeEventsResponse,
    VerifyResponse,
)
from veridex.api.ws import ArenaConnectionManager, register_arena_routes
from veridex.chain.anchor import explorer_tx_url
from veridex.checks.build import (
    build_check_results,
    build_performance_metrics,
    check_results_to_proof_block,
)
from veridex.competition.events import CompetitionEvent, EventType
from veridex.competition.models import (
    AgentEntry,
    Competition,
    CompetitionConfig,
    CompetitionStatus,
    ExecutionMode,
)
from veridex.competition.service import (
    CompetitionConflictError,
    CompetitionIntegrityError,
    CompetitionStateError,
    _default_policy_envelope,
    create_competition,
    register_agent,
    start_competition,
)
from veridex.config import Settings, get_settings
from veridex.execution.models import ExecutionRecord, ExecutionStatus
from veridex.execution.runner import resolve_approval
from veridex.explainer import GLOSSARY_DEFINITIONS, explain_proof
from veridex.ingest.feed_health import feed_health
from veridex.leaderboard import leaderboard as _build_leaderboard
from veridex.runtime.competition import (
    DEFAULT_CLUSTER,
    SCHEMA_VERSIONS,
    _default_checks,
    run_demo_competition,
)
from veridex.runtime.orchestrator import deterministic_agent
from veridex.runtime.runtime_store import RuntimeEventStore
from veridex.scoring import score_run
from veridex.store import InMemoryStore, Store
from veridex.venues.sx_bet import FakeVenueAdapter, SXBetAdapter
from veridex.verifier.proof_card import proof_card_from_run_result
from veridex.verifier.recompute import verify_run as _verify_run_core

# Module-level default store — shared across requests when the app runs under Uvicorn.
# Tests always inject their own store via ``create_app(store=InMemoryStore())``.
_default_store: InMemoryStore = InMemoryStore()


def get_store() -> Store:
    """Dependency provider for the module-level default store.

    Returns:
        The module-level :class:`~veridex.store.InMemoryStore` instance.
    """
    return _default_store


def _derive_leaderboard(events: list[CompetitionEvent]) -> list[CompetitionLeaderboardRow]:
    """Derive a ranked leaderboard from ``SCORE_UPDATE`` events in the canonical log.

    Single source of truth (CON-203): ranking is derived purely from persisted
    ``SCORE_UPDATE`` event payloads — no re-scoring from raw events.  Rows are ranked by
    ``mean_clv_bps`` descending (``None`` treated as ``-inf``), then ``agent_id`` ascending
    as a stable tie-breaker.

    Args:
        events: The full competition event log (any order).

    Returns:
        Ranked :class:`~veridex.api.schemas.CompetitionLeaderboardRow` list, rank 1 first.
    """
    score_updates = [e for e in events if e.event_type == EventType.SCORE_UPDATE]

    def _sort_key(event: CompetitionEvent) -> tuple[float, str]:
        mean_clv = event.payload.get("mean_clv_bps")
        score = -(mean_clv if isinstance(mean_clv, (int, float)) else float("-inf"))
        return (score, str(event.payload.get("agent_id", "")))

    rows: list[CompetitionLeaderboardRow] = []
    for rank, event in enumerate(sorted(score_updates, key=_sort_key), 1):
        p = event.payload
        rows.append(
            CompetitionLeaderboardRow(
                rank=rank,
                agent_id=str(p.get("agent_id", "")),
                total_clv_bps=int(p.get("total_clv_bps", 0)),
                mean_clv_bps=p.get("mean_clv_bps"),
                valid_count=int(p.get("valid_count", 0)),
                proof_mode=p.get("proof_mode"),
            )
        )
    return rows


def _build_execution_attachment(records: list[ExecutionRecord]) -> dict[str, Any] | None:
    """Build the NON-SCORING execution attachment from execution records (REQ-2B-20 / AC-2B-12).

    The attachment is explicitly labelled ``non_scoring`` + ``derived`` and is an OFF-CHAIN venue
    artifact (``venue_artifact=True``) — distinct from the Phase-1 Memo anchor and excluded from
    every evidence/scoring hash. Receipts are surfaced ONLY here, never in the skill-block proof
    card.

    Args:
        records: The competition's execution records (any order).

    Returns:
        ``None`` when there are no records; otherwise a dict with the records, their receipts, and
        the pinned ``policy_hash`` set (REQ-2B-03).
    """
    if not records:
        return None
    receipts = [r.receipt.model_dump(mode="json") for r in records if r.receipt is not None]
    policy_hashes = sorted({r.policy_hash for r in records})
    return {
        "non_scoring": True,
        "derived": True,
        "venue_artifact": True,
        "off_chain": True,
        "records": [r.model_dump(mode="json") for r in records],
        "receipts": receipts,
        "policy_hashes": policy_hashes,
    }


def create_app(
    store: Store | None = None,
    settings: Settings | None = None,
    runtime_event_store: RuntimeEventStore | None = None,
) -> FastAPI:
    """Create the Veridex demo FastAPI application.

    Factory pattern: inject ``store`` / ``settings`` in tests; omit for the default in-process
    store and env-backed settings.

    Args:
        store: Optional :class:`~veridex.store.Store` override.  Defaults to the
            module-level ``_default_store`` (an :class:`~veridex.store.InMemoryStore`).
        settings: Optional :class:`~veridex.config.Settings` override carrying the operator
            control-plane credentials.  Defaults to :func:`~veridex.config.get_settings`.
        runtime_event_store: Optional :class:`~veridex.runtime.runtime_store.RuntimeEventStore`
            override — the OPS-channel buffer the Agent Ops drawer reads (SEC-003). Defaults to a
            fresh per-app store. Distinct from the evidence/competition log.

    Returns:
        A configured :class:`fastapi.FastAPI` application: the Phase-1 demo trio, the six
        Phase-2A competition endpoints, plus the Phase-2B control-plane endpoints
        (``GET /competitions/{id}/executions``, ``GET /executions/{id}``,
        ``POST /competitions/{id}/kill-switch``, ``POST /executions/{id}/approve``).
    """
    resolved_store: Store = store if store is not None else _default_store
    resolved_settings: Settings = settings if settings is not None else get_settings()
    resolved_runtime_store: RuntimeEventStore = (
        runtime_event_store if runtime_event_store is not None else RuntimeEventStore()
    )

    app = FastAPI(
        title="Veridex Demo API",
        description="TxLINE Agent Proof Arena — Phase 1 demo surface (REQ-115 / AC-115).",
        version="0.1.0",
    )

    # Per-app registry: run_id → {anchor_status, source_mode}.
    # Populated by POST /demo/run; consumed by GET /leaderboard.
    _run_meta: dict[str, dict[str, str]] = {}

    # Per-app live-fanout manager (owns per-client bounded broadcast queues). The live producer
    # (start_competition's broadcast callback) persists each event BEFORE broadcasting it.
    arena_manager = ArenaConnectionManager()

    # --- Dependency -------------------------------------------------------

    def _get_store() -> Store:
        """Closure-captured store dependency for this app instance.

        Returns:
            The resolved :class:`~veridex.store.Store` for this application.
        """
        return resolved_store

    # --- Control-plane auth (REQ-2B-18/19; fail-closed) -------------------

    def _authenticate(authorization: str | None) -> str | None:
        """Validate a ``Bearer`` operator token; return the principal ``operator_id``.

        Fail-closed: a missing/malformed header, or a token that does not match the configured
        ``operator_token`` (or no token configured at all), raises 401.

        Args:
            authorization: The raw ``Authorization`` header value, if present.

        Returns:
            The authenticated principal's ``operator_id`` (``settings.operator_id``).

        Raises:
            HTTPException: 401 if authentication fails.
        """
        token = resolved_settings.operator_token
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing or malformed operator bearer token")
        presented = authorization.removeprefix("Bearer ")
        if token is None or presented != token:
            raise HTTPException(status_code=401, detail="invalid operator token")
        return resolved_settings.operator_id

    def _require_operator(authorization: str | None = Header(default=None)) -> str | None:  # noqa: B008
        """FastAPI dependency: require a valid operator bearer token; return the principal id."""
        return _authenticate(authorization)

    def _check_owner(competition: Competition, principal_operator_id: str | None) -> None:
        """Enforce per-competition ownership: 403 if the competition is owned by another operator.

        A competition with ``operator_id is None`` is owner-less, so ANY authenticated operator
        may act on it (the single-operator-token model — authentication is already enforced by
        ``_require_operator`` / ``_authenticate`` upstream).

        Args:
            competition: The loaded competition.
            principal_operator_id: The authenticated principal's ``operator_id``.

        Raises:
            HTTPException: 403 if ``competition.config.operator_id`` is set and differs from the
                principal.
        """
        owner = competition.config.operator_id
        if owner is not None and owner != principal_operator_id:
            raise HTTPException(status_code=403, detail="operator does not own this competition")

    # --- POST /demo/run ---------------------------------------------------

    @app.post("/demo/run", response_model=DemoRunResponse)
    async def demo_run(dep_store: Store = Depends(_get_store)) -> DemoRunResponse:  # noqa: B008
        """Run a demo competition and return the full artifact bundle.

        Drives two deterministic agents (no LLM, no network) over the bundled
        fixture ticks.  Anchoring is skipped (``anchor_fn=None``) so the call
        completes offline in < 1 s.

        Args:
            dep_store: Injected store dependency.

        Returns:
            A :class:`~veridex.api.schemas.DemoRunResponse` with ``run_id``,
            ``anchor_status``, ``leaderboard``, and ``proof_card``.
        """
        ticks = build_demo_ticks()
        agents = [
            deterministic_agent("agent-alpha"),
            contrarian_agent("agent-beta"),
        ]
        result = await run_demo_competition(
            ticks,
            agents,
            source_mode="replay",
            store=dep_store,
            anchor_fn=None,  # offline: not_anchored (no Solana creds needed)
        )

        _run_meta[result.run.run_id] = {
            "anchor_status": result.anchor_status,
            "source_mode": "replay",
        }

        rows = [LeaderboardRow(**row) for row in result.leaderboard]
        return DemoRunResponse(
            run_id=result.run.run_id,
            anchor_status=result.anchor_status,
            leaderboard=rows,
            proof_card=result.proof_card,
        )

    # --- GET /leaderboard -------------------------------------------------

    @app.get("/leaderboard", response_model=LeaderboardResponse)
    async def get_leaderboard(dep_store: Store = Depends(_get_store)) -> LeaderboardResponse:  # noqa: B008
        """Return the cross-run leaderboard aggregated from all stored runs.

        Iterates over every run registered by ``POST /demo/run``, re-scores each
        run from persisted events, tags rows with anchor/source metadata, and
        delegates aggregation + ranking to :func:`veridex.leaderboard.leaderboard`.

        Args:
            dep_store: Injected store dependency.

        Returns:
            A :class:`~veridex.api.schemas.LeaderboardResponse` with ranked rows.
        """
        all_score_rows: list[dict[str, Any]] = []

        for run_id, meta in _run_meta.items():
            try:
                run_result = await dep_store.load_run(run_id)
            except KeyError:
                continue
            for row in score_run(run_result):
                all_score_rows.append(
                    {
                        **row,
                        "anchor_status": meta["anchor_status"],
                        "source_mode": meta["source_mode"],
                    }
                )

        rows_data = _build_leaderboard(all_score_rows) if all_score_rows else []
        return LeaderboardResponse(rows=[LeaderboardRow(**r) for r in rows_data])

    # --- GET /feed/health (WD-4: live-feed shown + judge-testable) --------

    @app.get("/feed/health", response_model=FeedHealthResponse)
    async def feed_health_endpoint(source_mode: str = Query(default="replay")) -> FeedHealthResponse:  # noqa: B008
        """Report live/replay TxLINE feed health (WD-4 / REQ-053; offline-honest).

        Read-only OPERATIONAL TELEMETRY: nothing here enters ``evidence_hash``, the proof checks,
        scoring, or the leaderboard. Surfaces ``txline_configured`` from credential PRESENCE only
        (never the secret values — COM-001). Offline (no creds) the report is honest:
        ``txline_configured=False``, ``connected=False``, no ticks, ``stale=False``. A judge can
        curl this against the deployed devnet API to confirm the live path is wired.

        The :func:`~veridex.ingest.feed_health.feed_health` core drives the WD-4 staleness view;
        A's throughput view rides alongside (``ws_live`` mirrors ``connected``; ``events_per_min``
        is ``None`` until a live counter is wired; ``anchor_status`` is the honest default).

        Args:
            source_mode: ``"replay"`` (default) or ``"live"``.

        Returns:
            A :class:`~veridex.api.schemas.FeedHealthResponse`.
        """
        configured = resolved_settings.txline_jwt is not None and resolved_settings.txline_api_token is not None
        report = feed_health(
            source_mode=source_mode,
            txline_configured=configured,
            connected=configured and source_mode == "live",
            last_tick_ts=None,
            now_ts=int(time.time()),
            ticks_seen=0,
            fixture_id=None,
        )
        return FeedHealthResponse(
            source_mode=report.source_mode,
            events_per_min=None,  # A's throughput view — no live counter wired yet (honest None)
            ws_live=report.connected,  # both views agree: ws live == connected
            last_tick_ts=report.last_tick_ts,
            anchor_status="not_anchored",  # honest default for the offline/health surface
            txline_configured=report.txline_configured,
            connected=report.connected,
            ticks_seen=report.ticks_seen,
            fixture_id=report.fixture_id,
            staleness_s=report.staleness_s,
            stale=report.stale,
        )

    # --- GET /runs/{run_id} -----------------------------------------------

    @app.get("/runs/{run_id}")
    async def get_run(run_id: str, dep_store: Store = Depends(_get_store)) -> dict[str, Any]:  # noqa: B008
        """Return the proof card for a previously persisted run.

        Loads the run from the store, recomputes checks from the score rows,
        and assembles a full proof card with an anchor block.

        Args:
            run_id: The run identifier returned by ``POST /demo/run``.
            dep_store: Injected store dependency.

        Returns:
            A proof-card dict with ``verifier_version``, ``run``, ``lineage``,
            ``evidence``, ``checks``, and ``anchor``.

        Raises:
            HTTPException: 404 when no run with ``run_id`` is found in the store.
        """
        try:
            run_result = await dep_store.load_run(run_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"run {run_id!r} not found") from None

        scores = score_run(run_result)
        checks = _default_checks(scores, run_result)
        metrics = build_performance_metrics(scores)  # SEC-001: CLV lives here, not in checks
        # Recover anchor_status from the registry; fall back to not_anchored for
        # runs loaded from an externally supplied store.
        meta = _run_meta.get(run_id, {})
        anchor_status = meta.get("anchor_status", "not_anchored")
        anchor_block: dict[str, Any] = {
            "status": anchor_status,
            "signature": None,
            "cluster": DEFAULT_CLUSTER,
        }
        return proof_card_from_run_result(
            run_result,
            checks=checks,
            anchor=anchor_block,
            schema_versions=dict(SCHEMA_VERSIONS),
            metrics=metrics,
        )

    # --- GET /runs/{run_id}/actions/{seq} (C1 Inspector — per-action forensic view) -------

    @app.get("/runs/{run_id}/actions/{seq}", response_model=InspectorRecord)
    async def get_inspector_record(
        run_id: str,
        seq: int,
        dep_store: Store = Depends(_get_store),  # noqa: B008
    ) -> InspectorRecord:
        """Per-action forensic record for one sealed decision (C1 Inspector — killer-flow step).

        Read-only over the sealed run. ``seq`` is the decision event's ``sequence_no`` in the run's
        event log (the canonical-stream seq the cockpit links from). Serializes from SEALED data:
        the :class:`AgentAction` + its entry ``market_state`` from ``run_events``, and the
        law-recomputed ``clv_bps`` from the matching ``score_rows`` entry. The action's
        ``reason``/``confidence``/``claimed_edge_bps`` are surfaced as ``untrusted_llm_metadata`` —
        recorded-but-NEVER-scored (SEC-003/007); the frontend fences them. Never mutates the seal,
        never recomputes the evidence hash, never calls an LLM (it reads recorded params).

        Args:
            run_id: The sealed run identifier.
            seq: The decision event's ``sequence_no`` in the run's event log.
            dep_store: Injected store dependency.

        Returns:
            A :class:`~veridex.api.schemas.InspectorRecord`.

        Raises:
            HTTPException: 404 when the run is unknown, or ``seq`` is not a decision/action event.
        """
        try:
            run_result = await dep_store.load_run(run_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"run {run_id!r} not found") from None

        # Walk the sealed log in sequence order; track the most recent tick snapshot (the action's
        # entry market state) and stop at the requested decision event.
        entry_snapshot: dict[str, Any] | None = None
        decision: dict[str, Any] | None = None
        for event in sorted(run_result.run_events, key=lambda e: e["sequence_no"]):
            if event.get("event_type") == "tick" and event.get("state_snapshot_json"):
                entry_snapshot = json.loads(event["state_snapshot_json"])
            if event.get("sequence_no") == seq:
                decision = event
                break

        if decision is None or decision.get("event_type") != "decision" or not decision.get("action_payload_json"):
            raise HTTPException(status_code=404, detail=f"no action at seq {seq} in run {run_id!r}")

        agent_action = json.loads(decision["action_payload_json"])
        result_payload = json.loads(decision["result_payload_json"]) if decision.get("result_payload_json") else {}
        agent_id = str(result_payload.get("agent_id", ""))
        tick_seq = int(entry_snapshot["tick_seq"]) if entry_snapshot and "tick_seq" in entry_snapshot else -1

        # SCORED metric: the deterministic law-recomputed clv from the matching sealed score row —
        # NEVER the agent's claim. "pending" (non-numeric) for a valid WAIT/abstention.
        score_row = next(
            (r for r in run_result.score_rows if r.get("tick_seq") == tick_seq and r.get("agent_id") == agent_id),
            {},
        )
        clv_bps = score_row.get("clv_bps", "pending")
        recompute = {
            "recomputed_edge_bps": score_row.get("recomputed_edge_bps"),
            "clv_bps": clv_bps,
            "valid": score_row.get("valid"),
        }

        # UNTRUSTED (SEC-003/007): reason/confidence/claimed_edge_bps are recorded-but-NEVER-scored.
        params = agent_action.get("params", {}) or {}
        untrusted = {key: params[key] for key in ("reason", "confidence", "claimed_edge_bps") if key in params}

        return InspectorRecord(
            run_id=run_id,
            agent_id=agent_id,
            tick_seq=tick_seq,
            market_state=entry_snapshot or {},
            agent_action=agent_action,
            recompute=recompute,
            clv_bps=clv_bps,
            untrusted_llm_metadata=untrusted,
        )

    # --- POST /runs/{run_id}/verify (WD-1 authoritative recompute, REQ-050 / AC-020) ------

    @app.post("/runs/{run_id}/verify", response_model=VerifyResponse)
    async def verify_run_endpoint(run_id: str, dep_store: Store = Depends(_get_store)) -> VerifyResponse:  # noqa: B008
        """Authoritatively recompute a sealed run: confirm the evidence hash + rebuild the proof.

        The frontend NEVER reimplements the law (CON-003): it calls this, renders the returned
        recompute + hash-confirmation, and opens the Solana tx. ``verified`` is ``True`` iff the
        recomputed evidence hash matches the sealed one (fail-closed on any recompute error).

        Delegates the deterministic recompute to the WD-1 trust-path core
        (:func:`veridex.verifier.recompute.verify_run`): it recomputes the evidence hash over the
        sealed ``run_events`` prefix, re-derives the score root, and rebuilds the run manifest
        (base + ``root_forest``) so ``manifest_hash`` is byte-identical to the anchored Memo
        payload — receipt-independent (SEC-004) and read-only over the seal. The Proof-Checks block
        (7 ``CheckId``, no clv — SEC-001) + the separate Performance-Metrics block (clv lives here)
        are composed via the SAME builders the Proof-Card GET uses, so both endpoints return one
        consistent shape to the Proof Card (Plan C1).
        """
        try:
            run_result = await dep_store.load_run(run_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"run {run_id!r} not found") from None

        report = _verify_run_core(run_result)
        metrics = build_performance_metrics(report.score_rows)  # SEC-001: CLV lives here, not in checks

        meta = _run_meta.get(run_id, {})
        signature = meta.get("signature")  # present only after a real on-chain anchor
        anchor_block: dict[str, Any] = {
            "status": meta.get("anchor_status", "not_anchored"),
            "signature": signature,
            "cluster": DEFAULT_CLUSTER,
            "explorer_url": explorer_tx_url(signature, cluster=DEFAULT_CLUSTER),
        }
        # Bind the FULL verdict: pass the reconstructed manifest/manifest_hash + anchor so
        # MANIFEST_BOUND confirms the seal — the 2-arg _default_checks omits them, leaving
        # manifest_bound=not_applicable, which false-reds the manifest hash on an honest run.
        check_results = build_check_results(
            scores=report.score_rows,
            run=run_result,
            manifest=report.manifest,
            manifest_hash=report.manifest_hash,
            anchor=anchor_block,
            source_mode=report.source_mode,
        )
        checks = check_results_to_proof_block(check_results)
        proof_card = proof_card_from_run_result(
            run_result,
            checks=checks,
            anchor=anchor_block,
            schema_versions=dict(SCHEMA_VERSIONS),
            metrics=metrics,
        )
        return VerifyResponse(
            run_id=report.run_id,
            verified=report.verified,
            evidence_hash=report.evidence_hash_sealed,
            recomputed_evidence_hash=(
                report.evidence_hash_recomputed
                if report.evidence_hash_recomputed is not None
                else f"recompute_error:{report.evidence_error}"
            ),
            manifest_hash=report.manifest_hash,
            checks=checks,
            metrics=metrics,
            anchor=anchor_block,
            proof_card=proof_card,
        )

    # --- POST /runs/{run_id}/explain (Proof Explainer — educational LLM narration) --------

    @app.post("/runs/{run_id}/explain")
    async def explain_run_endpoint(
        run_id: str,
        body: ExplainRequest | None = None,
        dep_store: Store = Depends(_get_store),  # noqa: B008
    ) -> dict[str, str]:
        """Narrate an already-produced proof in plain language — the Proof Explainer (NOT a verifier).

        READ-ONLY and NON-scoring: this endpoint recomputes the served proof artifact + verify
        view-models exactly as ``GET /runs/{id}`` and ``POST /runs/{id}/verify`` do, assembles a
        SANITIZED read-model (ONLY the served ``ProofArtifactResponse`` + ``VerifyResponse`` fields
        + the pinned glossary — never the raw ``RunResult``, unsealed/live state, or the store
        handle), and hands that dict to the LLM explainer. It performs NO writes, NO mutation, and
        NO control. The deterministic Verify result remains the source of truth; the explanation is
        educational only and carries that disclaimer.

        Args:
            run_id: The sealed run identifier.
            body: Optional ``{"question": str, "target_field": str}`` to focus the narration.
            dep_store: Injected store dependency.

        Returns:
            ``{"explanation": ..., "disclaimer": ..., "footer": ...}``.

        Raises:
            HTTPException: 404 when no run with ``run_id`` is found.
        """
        try:
            run_result = await dep_store.load_run(run_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"run {run_id!r} not found") from None

        # Rebuild the SERVED verify view-model via the SAME trust-path core the verify route uses.
        report = _verify_run_core(run_result)
        metrics = build_performance_metrics(report.score_rows)  # SEC-001: CLV lives here, not checks
        meta = _run_meta.get(run_id, {})
        signature = meta.get("signature")
        anchor_block: dict[str, Any] = {
            "status": meta.get("anchor_status", "not_anchored"),
            "signature": signature,
            "cluster": DEFAULT_CLUSTER,
            "explorer_url": explorer_tx_url(signature, cluster=DEFAULT_CLUSTER),
        }
        check_results = build_check_results(
            scores=report.score_rows,
            run=run_result,
            manifest=report.manifest,
            manifest_hash=report.manifest_hash,
            anchor=anchor_block,
            source_mode=report.source_mode,
        )
        checks = check_results_to_proof_block(check_results)
        proof_artifact = proof_card_from_run_result(
            run_result,
            checks=checks,
            anchor=anchor_block,
            schema_versions=dict(SCHEMA_VERSIONS),
            metrics=metrics,
        )

        # SANITIZED read-model: ONLY served ProofArtifactResponse + VerifyResponse fields + glossary.
        # No raw RunResult, no run_events, no unsealed/live state, no store handle crosses this line.
        read_model: dict[str, Any] = {
            "proof_artifact": proof_artifact,
            "verify": {
                "run_id": report.run_id,
                "verified": report.verified,
                "evidence_hash": report.evidence_hash_sealed,
                "recomputed_evidence_hash": (
                    report.evidence_hash_recomputed
                    if report.evidence_hash_recomputed is not None
                    else f"recompute_error:{report.evidence_error}"
                ),
                "manifest_hash": report.manifest_hash,
                "checks": checks,
                "metrics": metrics,
                "anchor": anchor_block,
            },
            "glossary": GLOSSARY_DEFINITIONS,
        }

        req = body or ExplainRequest()
        return await explain_proof(
            read_model,
            question=req.question,
            target_field=req.target_field,
            settings=resolved_settings,
        )

    # --- POST /competitions -----------------------------------------------

    @app.post("/competitions", response_model=CompetitionCreateResponse)
    async def create_competition_endpoint(
        config: CompetitionConfig,
        dep_store: Store = Depends(_get_store),  # noqa: B008
    ) -> CompetitionCreateResponse:
        """Create a new DRAFT competition from the supplied configuration.

        Args:
            config: Immutable :class:`~veridex.competition.models.CompetitionConfig`.
            dep_store: Injected store dependency.

        Returns:
            A :class:`~veridex.api.schemas.CompetitionCreateResponse` with ``competition_id``
            and ``status="draft"``.
        """
        comp = await create_competition(dep_store, config)
        return CompetitionCreateResponse(
            competition_id=comp.competition_id,
            status=comp.status.value,
        )

    # --- POST /competitions/{competition_id}/agents -----------------------

    @app.post("/competitions/{competition_id}/agents", response_model=AgentRegisterResponse)
    async def register_agent_endpoint(
        competition_id: str,
        entry: AgentEntry,
        dep_store: Store = Depends(_get_store),  # noqa: B008
    ) -> AgentRegisterResponse:
        """Register an agent on a competition's roster.

        Pins ``config_hash`` (CON-207) and normalises ``proof_mode`` to the two canonical
        Phase-2A values via :func:`~veridex.competition.service.register_agent`.

        Args:
            competition_id: The owning competition.
            entry: Raw agent entry from the wire boundary.
            dep_store: Injected store dependency.

        Returns:
            A :class:`~veridex.api.schemas.AgentRegisterResponse` with ``agent_id``,
            ``config_hash``, and the normalised ``proof_mode``.

        Raises:
            HTTPException: 404 when ``competition_id`` is not found.
        """
        try:
            finalized = await register_agent(dep_store, competition_id, entry)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"competition {competition_id!r} not found") from None
        return AgentRegisterResponse(
            agent_id=finalized.agent_id,
            config_hash=finalized.config_hash,
            proof_mode=finalized.proof_mode,
        )

    # --- POST /competitions/{competition_id}/start ------------------------

    @app.post("/competitions/{competition_id}/start", response_model=CompetitionStartResponse)
    async def start_competition_endpoint(
        competition_id: str,
        authorization: str | None = Header(default=None),  # noqa: B008
        dep_store: Store = Depends(_get_store),  # noqa: B008
    ) -> CompetitionStartResponse:
        """Run a competition offline/deterministically and return the finalized state.

        Offline simplification: market data is sourced from the demo ticks
        (:func:`~veridex.api.demo_fixtures.build_demo_ticks`), and roster entries are mapped to
        deterministic Agent objects (alternating deterministic/contrarian).  This produces a real
        ≥2-row leaderboard with distinct CLV without any LLM or network calls.

        Control-plane auth (fail-closed): a ``paper`` start is OPEN/public; a ``dry_run`` /
        ``live_guarded`` start REQUIRES a valid operator bearer token (401) AND competition
        ownership (403).  The live executor lane runs DOWNSTREAM of the seal as a separate derived
        block and is broadcast live (persist-before-broadcast).

        Args:
            competition_id: The competition to start.
            authorization: Operator bearer header (required only for non-paper modes).
            dep_store: Injected store dependency.

        Returns:
            A :class:`~veridex.api.schemas.CompetitionStartResponse` with
            ``status="finalized"`` and ``run_id`` set.

        Raises:
            HTTPException: 404 (unknown competition), 409 (already finalized/running),
                401/403 (control-plane auth for non-paper), 501 (live venue not enabled),
                500 (evidence-prefix integrity breach).
        """
        try:
            competition = await dep_store.get_competition(competition_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"competition {competition_id!r} not found") from None

        # Fail-closed auth gate for non-paper modes (paper stays public).
        if competition.config.execution_mode != ExecutionMode.PAPER:
            principal = _authenticate(authorization)
            _check_owner(competition, principal)

        ticks = build_demo_ticks()
        agents = build_agents_from_roster(competition.entries)

        async def _broadcast(event: CompetitionEvent) -> None:
            await arena_manager.broadcast(competition_id, event)

        try:
            finalized = await start_competition(dep_store, competition_id, ticks, agents, broadcast=_broadcast)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"competition {competition_id!r} not found") from None
        except CompetitionConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except CompetitionIntegrityError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from None
        except CompetitionStateError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except NotImplementedError as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

        return CompetitionStartResponse(
            competition_id=finalized.competition_id,
            status=finalized.status.value,
            run_id=finalized.run_id,
        )

    # --- GET /competitions/{competition_id} -------------------------------

    @app.get("/competitions/{competition_id}", response_model=CompetitionStateResponse)
    async def get_competition_state_endpoint(
        competition_id: str,
        dep_store: Store = Depends(_get_store),  # noqa: B008
    ) -> CompetitionStateResponse:
        """Return full competition state with a leaderboard derived from the canonical event log.

        Leaderboard derivation (single source of truth — CON-203): reads ``SCORE_UPDATE``
        events from the persisted log, ranks them by ``mean_clv_bps`` descending (``None``
        treated as ``-inf``), then ``agent_id`` ascending as a stable tie-breaker.

        ``anchor_status`` comes from the ``PROOF_ANCHOR`` event payload; defaults to
        ``"not_anchored"`` when the event is absent (e.g. competition not yet started).

        Args:
            competition_id: The competition to inspect.
            dep_store: Injected store dependency.

        Returns:
            A :class:`~veridex.api.schemas.CompetitionStateResponse`.

        Raises:
            HTTPException: 404 when ``competition_id`` is not found.
        """
        try:
            competition = await dep_store.get_competition(competition_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"competition {competition_id!r} not found") from None

        # Read entire log (since_seq=-1 includes seq=0).
        events = await dep_store.list_competition_events(competition_id, since_seq=-1)

        leaderboard = _derive_leaderboard(events)
        latest_seq = max((e.seq for e in events), default=0)

        anchor_event = next((e for e in events if e.event_type == EventType.PROOF_ANCHOR), None)
        anchor_status = anchor_event.payload.get("anchor_status", "not_anchored") if anchor_event else "not_anchored"

        # Proof-card SKILL/scoring block (REQ-2B-20): byte-identical with or without fills, because
        # it is built purely from the SEALED run. Execution receipts live ONLY under the separate
        # ``execution`` attachment below (non-scoring, derived, off-chain) — never in this block.
        proof_card: dict[str, Any] | None = None
        if competition.run_id is not None:
            try:
                run_result = await dep_store.load_run(competition.run_id)
            except KeyError:
                run_result = None
            if run_result is not None:
                scores = score_run(run_result)
                checks = _default_checks(scores, run_result)
                metrics = build_performance_metrics(scores)  # SEC-001: CLV lives here, not in checks
                anchor_block: dict[str, Any] = {
                    "status": str(anchor_status),
                    "signature": None,
                    "cluster": DEFAULT_CLUSTER,
                }
                proof_card = proof_card_from_run_result(
                    run_result,
                    checks=checks,
                    anchor=anchor_block,
                    schema_versions=dict(SCHEMA_VERSIONS),
                    metrics=metrics,
                )

        execution_records = await dep_store.list_executions(competition_id)
        execution = _build_execution_attachment(execution_records)

        return CompetitionStateResponse(
            competition_id=competition.competition_id,
            status=competition.status.value,
            config=competition.config.model_dump(mode="json"),
            roster=[e.model_dump(mode="json") for e in competition.entries],
            leaderboard=leaderboard,
            latest_seq=latest_seq,
            anchor_status=str(anchor_status),
            run_id=competition.run_id,
            proof_card=proof_card,
            execution=execution,
        )

    # --- GET /competitions ------------------------------------------------

    @app.get("/competitions", response_model=list[CompetitionSummaryResponse])
    async def list_competitions_endpoint(
        status: CompetitionStatus | None = Query(default=None),  # noqa: B008
        dep_store: Store = Depends(_get_store),  # noqa: B008
    ) -> list[CompetitionSummaryResponse]:
        """List all competitions with an optional status filter.

        Args:
            status: When provided, only competitions with this lifecycle status are returned.
                FastAPI validates the value against :class:`~veridex.competition.models.CompetitionStatus`
                and returns 422 for unknown values.
            dep_store: Injected store dependency.

        Returns:
            A list of :class:`~veridex.api.schemas.CompetitionSummaryResponse` items.
        """
        competitions = await dep_store.list_competitions(status=status)
        return [
            CompetitionSummaryResponse(
                competition_id=c.competition_id,
                status=c.status.value,
                config=c.config.model_dump(mode="json"),
                run_id=c.run_id,
            )
            for c in competitions
        ]

    # --- GET /competitions/{competition_id}/events ------------------------

    @app.get("/competitions/{competition_id}/events")
    async def get_competition_events_endpoint(
        competition_id: str,
        since_seq: int = Query(default=0),  # noqa: B008
        dep_store: Store = Depends(_get_store),  # noqa: B008
    ) -> list[dict[str, Any]]:
        """Return the ordered event log tail (``seq > since_seq``).

        Mirrors :func:`~veridex.competition.events.replay_from` semantics: strict-greater
        bound, ascending ``seq`` order.  The default ``since_seq=0`` returns all events with
        ``seq >= 1`` (excluding the ``COMPETITION_STARTED`` event at ``seq=0``).

        Args:
            competition_id: The owning competition.
            since_seq: Exclusive lower bound on ``seq`` (default ``0`` → seq ≥ 1).
            dep_store: Injected store dependency.

        Returns:
            JSON-serialized :class:`~veridex.competition.events.CompetitionEvent` list,
            ordered ascending by ``seq``.

        Raises:
            HTTPException: 404 when ``competition_id`` is not found.
        """
        try:
            await dep_store.get_competition(competition_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"competition {competition_id!r} not found") from None

        events = await dep_store.list_competition_events(competition_id, since_seq=since_seq)
        return [e.model_dump(mode="json") for e in events]

    # --- GET /competitions/{competition_id}/executions --------------------

    @app.get("/competitions/{competition_id}/executions", response_model=list[ExecutionRecord])
    async def list_executions_endpoint(
        competition_id: str,
        dep_store: Store = Depends(_get_store),  # noqa: B008
    ) -> list[ExecutionRecord]:
        """Return all execution records for a competition (read-only, public).

        Args:
            competition_id: The owning competition.
            dep_store: Injected store dependency.

        Returns:
            The competition's :class:`~veridex.execution.models.ExecutionRecord` list, sorted by
            ``execution_id``.
        """
        return await dep_store.list_executions(competition_id)

    # --- GET /executions/{execution_id} -----------------------------------

    @app.get("/executions/{execution_id}", response_model=ExecutionRecord)
    async def get_execution_endpoint(
        execution_id: str,
        dep_store: Store = Depends(_get_store),  # noqa: B008
    ) -> ExecutionRecord:
        """Return a single execution record (with its receipt, if any) — read-only, public.

        Args:
            execution_id: The execution record id.
            dep_store: Injected store dependency.

        Returns:
            The :class:`~veridex.execution.models.ExecutionRecord`.

        Raises:
            HTTPException: 404 when no record with ``execution_id`` exists.
        """
        try:
            return await dep_store.get_execution_record(execution_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"execution {execution_id!r} not found") from None

    # --- GET /agents/{agent_id}/runtime-events (OPS channel; read-only) ----

    @app.get("/agents/{agent_id}/runtime-events", response_model=RuntimeEventsResponse)
    async def get_runtime_events(  # noqa: B008
        agent_id: str,
        since: int = Query(default=0),  # noqa: B008
        limit: int | None = Query(default=None),  # noqa: B008
    ) -> RuntimeEventsResponse:
        """Serve an agent's OPS-channel RuntimeEvents (Agent Ops drawer feed — SEC-003 / REQ-030).

        Read-only, runtime-neutral (SEC-010). Unknown agents return an empty list, never 404, so a
        minimal/just-started runtime renders a clean empty drawer (REQ-031).

        Args:
            agent_id: The agent whose OPS-channel telemetry to read.
            since: ``ts`` lower bound (ms); ``0`` returns everything retained.
            limit: When set, return only the most-recent ``limit`` matching events.

        Returns:
            A :class:`~veridex.api.schemas.RuntimeEventsResponse` wrapping the ``events`` list.
        """
        events = resolved_runtime_store.list_for_agent(agent_id, since=since, limit=limit)
        return RuntimeEventsResponse(events=[e.model_dump(mode="json") for e in events])

    # --- POST /competitions/{competition_id}/kill-switch (auth) -----------

    @app.post("/competitions/{competition_id}/kill-switch", response_model=KillSwitchResponse)
    async def kill_switch_endpoint(
        competition_id: str,
        principal: str | None = Depends(_require_operator),  # noqa: B008
        dep_store: Store = Depends(_get_store),  # noqa: B008
    ) -> KillSwitchResponse:
        """Flip the competition's policy-envelope kill-switch (control-plane write; fail-closed).

        Args:
            competition_id: The competition to update.
            principal: The authenticated operator principal (injected by ``_require_operator``).
            dep_store: Injected store dependency.

        Returns:
            A :class:`~veridex.api.schemas.KillSwitchResponse` with the new kill-switch state.

        Raises:
            HTTPException: 401 (unauthenticated), 403 (wrong owner), 404 (unknown competition).
        """
        try:
            competition = await dep_store.get_competition(competition_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"competition {competition_id!r} not found") from None
        _check_owner(competition, principal)

        envelope = competition.config.policy_envelope or _default_policy_envelope()
        flipped = envelope.model_copy(update={"kill_switch": not envelope.kill_switch})
        new_config = competition.config.model_copy(update={"policy_envelope": flipped})
        await dep_store.update_competition_config(competition_id, new_config)

        return KillSwitchResponse(
            competition_id=competition_id,
            kill_switch=flipped.kill_switch,
            status="kill_switch_on" if flipped.kill_switch else "kill_switch_off",
        )

    # --- POST /executions/{execution_id}/approve (auth) -------------------

    @app.post("/executions/{execution_id}/approve", response_model=ApprovalResponse)
    async def approve_execution_endpoint(
        execution_id: str,
        body: dict[str, Any] | None = None,
        principal: str | None = Depends(_require_operator),  # noqa: B008
        dep_store: Store = Depends(_get_store),  # noqa: B008
    ) -> ApprovalResponse:
        """Resolve an ``awaiting_human`` execution: re-check law+policy+eligibility, then submit-or-reject.

        Control-plane write (fail-closed): requires a valid operator token (401) and competition
        ownership (403). The resolution emits a NON-SCORING approval audit event, independently
        re-derives the proposal from the SEALED run, re-evaluates the CURRENT envelope (kill-switch
        included) + eligibility, and either advances to submission or rejects (no submit).

        Args:
            execution_id: The ``awaiting_human`` execution record to resolve.
            body: Optional JSON body with an operator ``note``.
            principal: The authenticated operator principal.
            dep_store: Injected store dependency.

        Returns:
            An :class:`~veridex.api.schemas.ApprovalResponse` with the decision + resulting status.

        Raises:
            HTTPException: 401/403 (auth/owner), 404 (unknown execution/run), 409 (not awaiting).
        """
        try:
            record = await dep_store.get_execution_record(execution_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"execution {execution_id!r} not found") from None

        try:
            competition = await dep_store.get_competition(record.competition_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"competition {record.competition_id!r} not found") from None
        _check_owner(competition, principal)

        if record.status != ExecutionStatus.AWAITING_HUMAN:
            raise HTTPException(
                status_code=409,
                detail=f"execution {execution_id!r} is not awaiting_human (status={record.status.value})",
            )

        try:
            run_result = await dep_store.load_run(record.run_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"run {record.run_id!r} not found") from None

        envelope = competition.config.policy_envelope or _default_policy_envelope()
        execution_mode = competition.config.execution_mode
        adapter = FakeVenueAdapter() if execution_mode == ExecutionMode.DRY_RUN else SXBetAdapter()
        entry = next((e for e in competition.entries if e.agent_id == record.agent_id), None)
        note = (body or {}).get("note")

        # Append the resolution block AFTER the current log tail (contiguous seqs).
        existing = await dep_store.list_competition_events(record.competition_id, since_seq=-1)
        next_seq = max((e.seq for e in existing), default=-1) + 1

        updated, events, decision = await resolve_approval(
            dep_store,
            record=record,
            run_result=run_result,
            envelope=envelope,
            adapter=adapter,
            entry=entry,
            execution_mode=execution_mode.value,
            base_seq=next_seq,
            event_ts=0,
            approver_id=principal,
            note=note,
        )
        await dep_store.append_competition_events(record.competition_id, events)
        for event in events:
            await arena_manager.broadcast(record.competition_id, event)

        return ApprovalResponse(execution_id=execution_id, decision=decision, status=updated.status.value)

    # --- WS /competitions/{competition_id}/arena --------------------------
    # Read-only spectator projection (P2A-7). The per-app ``arena_manager`` (created above) owns
    # per-client bounded broadcast queues; the route closes over ``resolved_store`` for replay.
    # The Phase-2B live producer (start_competition / approve) PERSISTS each event BEFORE calling
    # ``arena_manager.broadcast`` for it (persist-before-broadcast), so spectators see a gapless
    # projection of the sealed log. ``broadcast`` never blocks the run loop (REQ-2B-30).
    register_arena_routes(app, store=resolved_store, manager=arena_manager)

    return app
