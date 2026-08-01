"""Pydantic response models for the Veridex FastAPI surface (B11b — REQ-115 / AC-115; Task 6 — P2A-6).

All models are Google-docstring annotated and carry full type hints so the OpenAPI schema
is accurate and mypy is satisfied without stubs.

Note: ``ProofCardResponse`` is intentionally left as a plain ``dict[str, Any]`` pass-through
(the proof card's nested structure is already validated by ``veridex.verifier.proof_card``);
these models cover the typed response envelopes that the API owns.

Phase-2A adds six competition endpoint response models below the Phase-1 trio.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, StrictInt


class ExplainRequest(BaseModel):
    """Optional focus for the Proof Explainer (``POST /runs/{id}/explain``).

    Both fields are optional: a bare ``{}`` requests a general narration. Neither is scored,
    persisted, or fed to any trust-path code — they only steer the educational narration.

    Attributes:
        question: Free-form question about the already-produced proof.
        target_field: A specific served field to explain.
    """

    question: str | None = None
    target_field: str | None = None


class LeaderboardRow(BaseModel):
    """One ranked agent row from the cross-run leaderboard.

    Mirrors the output of ``veridex.leaderboard.leaderboard()`` for one agent.

    Attributes:
        rank: 1-based rank position (best agent is rank 1).
        agent_id: Stable agent identifier.
        runs: Number of runs this agent participated in.
        avg_clv_bps: Pooled average CLV in basis-points; ``None`` when action_count is 0.
        total_clv_bps: Sum of CLV across all scored actions.
        sim_pnl: Closing-referenced flat-stake PnL proxy (equals ``total_clv_bps`` in Phase 1).
        brier: Mean Brier score when confidence was emitted; ``None`` otherwise.
        max_drawdown: Worst peak-to-trough drop across runs (``<= 0.0``).
        action_count: Total scored actions across all runs.
        valid_pct: Law-acceptance percentage (valid decisions / total decisions × 100).
        proof_mode: Summarised proof mode across runs.
        eligibility_badge: ``"fully-proven"``, ``"partially-proven"``, or ``"unproven"``.
        anchor_status: ``"all-anchored"``, ``"some-pending"``, or ``"none-anchored"``.
        source_mode: ``"all-replay"``, ``"all-live"``, ``"mixed"``, or ``"unknown"``.
        valid_count: Pooled number of law-valid decisions across runs (WD-7 sample size).
        clv_confidence: Sample-size confidence tier — ``"low"``, ``"medium"``, or ``"high"``.
        low_sample: ``True`` when the CLV is backed by a small sample (flag, not a rank input).
    """

    rank: int
    agent_id: str
    runs: int
    avg_clv_bps: float | None
    total_clv_bps: int
    sim_pnl: int
    brier: float | None
    max_drawdown: float
    action_count: int
    valid_pct: float
    proof_mode: str
    eligibility_badge: str
    anchor_status: str
    source_mode: str
    valid_count: int
    clv_confidence: str
    low_sample: bool


class LeaderboardResponse(BaseModel):
    """Response envelope for ``GET /leaderboard``.

    Attributes:
        rows: Ranked leaderboard rows, best agent first.
    """

    rows: list[LeaderboardRow]


class DemoRunResponse(BaseModel):
    """Response envelope for ``POST /demo/run``.

    Bundles every artifact a judge needs to inspect a single demo competition.

    Attributes:
        run_id: Unique run identifier (hex UUID).
        anchor_status: Canonical anchor vocabulary — ``"anchored"`` or ``"not_anchored"``.
        leaderboard: Ranked leaderboard rows for this run (one per agent).
        proof_card: Full proof-card JSON (lineage + checks + anchor + evidence).
    """

    run_id: str
    anchor_status: str
    leaderboard: list[LeaderboardRow]
    proof_card: dict[str, Any]


# ---------------------------------------------------------------------------
# Phase-2A competition endpoint response models (Task 6 — P2A-6)
# ---------------------------------------------------------------------------


class CompetitionCreateResponse(BaseModel):
    """Response envelope for ``POST /competitions``.

    Attributes:
        competition_id: Stable unique identifier for the created competition.
        status: Lifecycle status (always ``"draft"`` on creation).
    """

    competition_id: str
    status: str


class AgentRegisterResponse(BaseModel):
    """Response envelope for ``POST /competitions/{id}/agents``.

    Attributes:
        agent_id: The agent's unique identifier.
        config_hash: Pinned SHA-256 content-hash of the agent config snapshot (CON-207).
        proof_mode: Canonical proof mode (``"reproducible"`` or ``"verified"``).
    """

    agent_id: str
    config_hash: str | None
    proof_mode: str


class CompetitionStartResponse(BaseModel):
    """Response envelope for ``POST /competitions/{id}/start``.

    Attributes:
        competition_id: Stable unique identifier.
        status: Lifecycle status (``"finalized"`` after a successful synchronous run).
        run_id: Sealed Phase-1 run identifier (set after start).
    """

    competition_id: str
    status: str
    run_id: str | None
    replay_binding: dict[str, Any] | None = None
    """The FROZEN production-replay identity the run replayed (R-4): ``{pack_id, fixture_id,
    content_hash}`` — so an auto-resolved (unnamed) run still names WHICH verified pack it ran."""


class CompetitionLeaderboardRow(BaseModel):
    """One row in the competition-scoped leaderboard, derived from ``SCORE_UPDATE`` events.

    Single source of truth: derived from the persisted canonical event log, not recomputed
    from raw scores.  Ranking is by ``mean_clv_bps`` descending (``None`` treated as ``-inf``),
    then ``agent_id`` ascending as a stable tie-breaker.

    Attributes:
        rank: 1-based rank position (best agent is rank 1).
        agent_id: Agent identifier.
        total_clv_bps: Sum of CLV in basis-points across scored actions.
        mean_clv_bps: Mean CLV in basis-points (``None`` if no scored actions).
        valid_count: Number of law-valid decisions.
        proof_mode: Canonical proof mode for this agent (``None`` if absent from log).
    """

    rank: int
    agent_id: str
    total_clv_bps: int
    mean_clv_bps: float | None
    valid_count: int
    proof_mode: str | None


class CompetitionStateResponse(BaseModel):
    """Response envelope for ``GET /competitions/{id}``.

    Attributes:
        competition_id: Stable unique identifier.
        status: Current lifecycle status (``"draft"``, ``"running"``, ``"finalized"``…).
        config: Immutable configuration snapshot (serialized as plain dict).
        roster: Registered agent entries (serialized as plain dicts).
        leaderboard: Ranked rows derived from ``SCORE_UPDATE`` events in the canonical log.
        latest_seq: Maximum ``seq`` in the persisted event log (``0`` when no events yet).
        anchor_status: Anchor status from the ``PROOF_ANCHOR`` event (``"not_anchored"`` if absent).
        run_id: Sealed Phase-1 run identifier (``None`` until start completes).
    """

    competition_id: str
    status: str
    config: dict[str, Any]
    roster: list[dict[str, Any]]
    leaderboard: list[CompetitionLeaderboardRow]
    latest_seq: int
    anchor_status: str
    run_id: str | None
    proof_card: dict[str, Any] | None = None
    execution: dict[str, Any] | None = None
    replay_binding: dict[str, Any] | None = None
    """The FROZEN production-replay identity (R-4): ``{pack_id, fixture_id, content_hash}`` — server-
    derived, so an "unnamed" competition still surfaces WHICH verified pack it ran. ``None`` until bound."""


class KillSwitchResponse(BaseModel):
    """Response envelope for ``POST /competitions/{id}/kill-switch``.

    Attributes:
        competition_id: The competition whose envelope was toggled.
        kill_switch: The new (post-flip) kill-switch state.
        status: Human-readable status (``"kill_switch_on"`` / ``"kill_switch_off"``).
    """

    competition_id: str
    kill_switch: bool
    status: str


class ApprovalResponse(BaseModel):
    """Response envelope for ``POST /executions/{id}/approve``.

    Attributes:
        execution_id: The resolved execution record.
        decision: ``"approved"`` (re-check clean → submitted) or ``"rejected"`` (fail-closed).
        status: The execution record's lifecycle status after resolution.
    """

    execution_id: str
    decision: str
    status: str


class CompetitionSummaryResponse(BaseModel):
    """Summary item for ``GET /competitions`` (list endpoint).

    Attributes:
        competition_id: Stable unique identifier.
        status: Current lifecycle status.
        config: Immutable configuration snapshot (serialized as plain dict).
        run_id: Sealed Phase-1 run identifier (``None`` until start completes).
    """

    competition_id: str
    status: str
    config: dict[str, Any]
    run_id: str | None


# ---------------------------------------------------------------------------
# Phase-2C pinned view-model envelopes (Task 0 — API Surface Contract Freeze)
#
# These are the frozen read/control contracts the frontend (Plans C1/C2/D) binds to.
# The backend returns assembled per-screen view-models, never a raw ``RunResult`` (CON-003).
# SEC-001: the ``checks`` block holds ONLY the 7 CheckId; CLV/performance lives in ``metrics``.
# ---------------------------------------------------------------------------


class ProofArtifactResponse(BaseModel):
    """The proof-card view-model the frontend renders (GET /runs/{run_id}). CLV lives in metrics."""

    verifier_version: str
    run: dict[str, Any]
    lineage: dict[str, Any]
    evidence: dict[str, Any]
    checks: dict[str, Any]
    anchor: dict[str, Any]
    metrics: dict[str, Any] | None = None


class VerifyResponse(BaseModel):
    """WD-1 authoritative recompute (POST /runs/{run_id}/verify). C1's VerifyResult binds to this."""

    run_id: str
    verified: bool
    evidence_hash: str
    recomputed_evidence_hash: str
    manifest_hash: str
    checks: dict[str, Any]
    metrics: dict[str, Any] | None = None
    anchor: dict[str, Any]
    proof_card: dict[str, Any]


class InspectorRecord(BaseModel):
    """Per-action forensic view-model (frontend adapter over GET /runs + events)."""

    run_id: str
    agent_id: str
    tick_seq: int
    market_state: dict[str, Any]
    agent_action: dict[str, Any]
    recompute: dict[str, Any]
    clv_bps: int | str
    untrusted_llm_metadata: dict[str, Any]


class FeedHealthResponse(BaseModel):
    """Feed-health view-model (Markets / cockpit feed-health strip).

    Read-only OPERATIONAL TELEMETRY — never scored, never in ``evidence_hash``, never a proof
    check or leaderboard input. Carries TWO complementary views of the same feed: A's throughput
    view (``events_per_min`` / ``ws_live`` / ``anchor_status``) and the WD-4 staleness view
    (``txline_configured`` / ``connected`` / ``ticks_seen`` / ``fixture_id`` / ``staleness_s`` /
    ``stale``). ``ws_live`` mirrors ``connected`` so both views agree.

    Attributes:
        source_mode: ``"live"`` or ``"replay"``.
        events_per_min: Tick throughput (``None`` when no live counter is wired).
        ws_live: Whether the live WS stream is up (mirrors ``connected``).
        last_tick_ts: Unix seconds of the most recent tick, or ``None`` when none seen.
        anchor_status: Honest anchor state for the surface (``"not_anchored"`` offline).
        txline_configured: Whether TxLINE credentials are present (never the secret values).
        connected: Whether the stream is currently connected (best-effort).
        ticks_seen: Count of ingested ticks.
        fixture_id: The fixture being followed, or ``None``.
        staleness_s: Seconds since the last tick, or ``None`` when none seen.
        stale: Whether the feed has exceeded its staleness budget.
        feed_state: III-3 honest connection-derived state (the ACTIVE stream's real last-seen, not
            credential presence): one of ``"live"`` / ``"heartbeat_only"`` / ``"stale"`` /
            ``"disconnected"`` / ``"recorded_replay"`` (also ``"connecting"`` transiently). Optional
            + defaulted so the pinned contract fixture stays valid; the live endpoint always sets it.
    """

    source_mode: str
    events_per_min: float | None
    ws_live: bool
    last_tick_ts: int | None
    anchor_status: str
    txline_configured: bool
    connected: bool
    ticks_seen: int
    fixture_id: int | None
    staleness_s: int | None
    stale: bool
    feed_state: str | None = None


class RuntimeEventsResponse(BaseModel):
    """Agent Ops drawer feed (§4.4 OPS channel). Single-field object wrapper (mirrors
    ``LeaderboardResponse{rows}``) so C2 binds field-name-exact to ``.events``; the ``agent_id``
    is already in the request path, never echoed in the body. ``events`` are RuntimeEvent dicts."""

    events: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# T15 — Backtest endpoints (REQ-2D-302/303/304)
# ---------------------------------------------------------------------------


class BacktestRunRequest(BaseModel):
    """Request body for ``POST /backtests`` — triggers a replay-sourced backtest run.

    The run is driven over a deterministic, offline agent (no LLM, no network). R-3: the browser
    addresses a pack by its ``pack_id`` (the R-2 verified catalog key) — NEVER by a filesystem path.
    The server resolves ``pack_id`` against the authoritative, hash-verified ReplayPack catalog
    (``app.state.replay_catalog``) and binds the catalog-derived ``content_hash`` into the sealed
    report; a client can never point the replay loader at an arbitrary filesystem directory.

    ``extra="forbid"``: a legacy body carrying the removed ``pack_dir`` (or any unknown field) is
    a hard 422 — the client-provided filesystem-path surface is GONE, not merely ignored.

    Attributes:
        pack_id: The R-2 catalog key of the ReplayPack to replay (resolved SERVER-side; unknown -> 404).
        fixture_id: The fixture within the pack to replay (must be catalogued for the pack; else 422).
            STRICT (``StrictInt``): a JSON bool is REJECTED (422), never coerced. A coercive ``int`` would
            normalize ``true``/``false`` to ``1``/``0``, and since ``True == 1`` / ``False == 0`` the
            catalog-membership guard would then admit the bool as the requested fixture identity — the
            exact bool-as-fixture-id alias R-2 excludes from the catalog, reintroduced at the browser
            boundary. Strictness rejects it BEFORE the identity is claimed to be catalog-validated.
        window_id: Stable id for the coverage window (echoed onto the report).
        market_allowlist: Market-key prefixes the window scores (the report's ``market_universe``).
        end_rule: Window close rule — ``"pre_match"`` (default), ``"fixed_duration"``, or ``"manual_stop"``.
        duration_s: Required IFF ``end_rule == "fixed_duration"`` (else must be ``None``).
        min_clv_horizon_s: DEC-2D-2 pending-horizon guard (seconds); defaults to 60.
    """

    model_config = ConfigDict(extra="forbid")

    pack_id: str
    # StrictInt so a JSON bool (``true``/``false``) is a hard 422 at the request boundary — never coerced
    # to 1/0 and aliased into a catalogued fixture id (identity-admission defect; Codex R-3 gate).
    fixture_id: StrictInt
    window_id: str
    market_allowlist: list[str]
    end_rule: str = "pre_match"
    duration_s: int | None = None
    min_clv_horizon_s: int = 60


class ReplayPackInfo(BaseModel):
    """One hash-verified pack of the R-2 catalog, projected for the read-only ``/replay-packs`` API.

    A pure projection of a :class:`~veridex.ingest.replay_catalog.CatalogEntry` — the verified
    ``content_hash``, HONEST ``provenance`` (``is_genuine`` only for a coherent genuine pack), and the
    catalogued ``fixtures``. The internal ``pack_dir`` filesystem path is DELIBERATELY not surfaced:
    the browser addresses packs by ``pack_id`` only, so the server-side path never leaks to a client.

    Attributes:
        pack_id: The stable catalog key the browser addresses (never a filesystem path).
        content_hash: The pack's verified (recompute-matched) ``content_hash``.
        provenance: HONEST provenance label (``genuine-txline`` only for a coherent genuine pack).
        is_genuine: ``True`` only for a hash-verified, coherent genuine TxLINE capture.
        fixtures: The fixture ids the pack manifest declares (the replayable fixtures).
    """

    pack_id: str
    content_hash: str
    provenance: str
    is_genuine: bool
    fixtures: list[int]
    #: ADDITIVE server-derived label join (``fixtures: list[int]`` stays unchanged — raw IDs, wire
    #: type intact). One row per fixture id: ``{fixture_id (raw int), home_team, away_team,
    #: kickoff_ts, label_source: "captured"|"unavailable"}``. A frontend-duplicated label map is
    #: prohibited (authority chain, spec §2/§5.2).
    fixture_metadata: list[dict[str, Any]] = []


class ReplayPackListResponse(BaseModel):
    """Envelope for ``GET /replay-packs`` — the verified R-2 catalog listing (single-field wrapper)."""

    packs: list[ReplayPackInfo]


class ReplayMarketRow(BaseModel):
    """One market's LAST-KNOWN projected state, folded across the WHOLE hash-bound replay tape (M11).

    The projection keeps the LAST-SEEN value per ``market_key`` over ALL tape states (not just the
    final tick — which alone carries only the markets present at that instant), plus the ``ts`` and
    per-market ``in_running`` of the state it was last seen in.

    HONESTY (M4): a SUSPENDED market keeps ``stable_prob_bps == {}`` (EMPTY — never back-filled from
    the retained ``stable_price``); the last-known decimal odds in ``stable_price`` stay non-empty so
    the browser can still render odds without fabricating an implied probability.

    Attributes:
        market_key: The stable ``{SuperOddsType}|{MarketPeriod}|{MarketParameters}`` key.
        in_running: The match-phase of the state the market was last seen in. Derived HONESTLY from
            that ``MarketState.phase`` (``1`` ⇒ in-running). At the runtime ``batch_size=1`` each state
            is folded from a SINGLE native record, so its ``phase`` is exactly that record's
            ``InRunning`` flag — the honest per-market source (MarketState carries no per-market flag).
        suspended: ``True`` when the market had no priced (non-NA) probability outcomes at last sight.
        ts: The ``ts`` (epoch seconds) of the state the market was last seen in.
        stable_prob_bps: Outcome-keyed de-vigged probability in basis points. EMPTY ``{}`` for a
            suspended market — PRESERVED empty, never filled.
        stable_price: Outcome-keyed decimal odds (last-known). Non-empty even when suspended.
    """

    market_key: str
    in_running: bool
    suspended: bool
    ts: int
    stable_prob_bps: dict[str, int]
    stable_price: dict[str, float]


class ReplayMarketsResponse(BaseModel):
    """Envelope for ``GET /replay-packs/{pack_id}/fixtures/{fixture_id}/markets`` — the last-known
    per-market projection of one replay fixture (read-only). ``label`` is always ``"CAPTURED REPLAY"``
    (a replay is never dressed up as live). NO ``finished`` / ``closing`` / ``edge`` keys."""

    fixture_id: int
    label: str = "CAPTURED REPLAY"
    markets: list[ReplayMarketRow]


class AgentRosterEntry(BaseModel):
    """One row of the HONEST public agent directory, keyed on the immutable ``public_agent_id``.

    The read-only, UNAUTHENTICATED ``GET /agents/roster`` view (mirrors ``/replay-packs``). Unlike the
    old leaky roster, this directory sources from ``store.list_public_agents()`` joined to deployment
    state and admits an agent ONLY when its ``visibility == PUBLIC`` AND it has a linked instance whose
    deploy status is ``SEALED``; private / pending / failed / running / unlinked agents are excluded.

    TRUST SURFACE: this row NEVER carries a raw ``operator_id`` / ``owner_ref`` (a Privy DID or internal
    id). The only owner rendering is the SAFE ``owner_public_label`` — the legacy raw-shaped ``owner``
    field is GONE.

    The performance columns (``avg_clv_bps`` / ``runs`` / ``valid_pct``) are ``None`` and ``proof_state``
    is ``"unscored"`` until the agent has scored board rows — honestly absent, NEVER fabricated. Once the
    agent appears on the PUBLIC_AGENTS directional board they carry the REAL pooled values.

    Attributes:
        public_agent_id: The immutable public identifier the directory is keyed on.
        display_name: Human-facing name from the public identity.
        owner_public_label: SAFE public owner rendering (brand string / shortened wallet / em-dash) —
            NEVER the raw ``operator_id`` / ``owner_ref``.
        origin: The REAL :class:`~veridex.public_agent.Origin` value (honest ``unknown`` for legacy).
        proof_state: ``"unscored"`` until scored; the REAL proof state once the agent has board rows.
        agent_id: The deployed instance's identifier (informational; the directory keys on the public id).
        type: The strategy-archetype template the instance was configured from (``template_id``).
        source_mode: ``replay`` | ``live`` (the sealed instance's data source).
        execution_mode: ``paper`` | ``dry_run`` | ``live_guarded``.
        status: The :class:`~veridex.deploy.instance.DeployStatus` value, lowercased (always ``sealed``).
        config_hash_present: A REAL proof indicator — ``True`` when the instance pinned a ``config_hash``.
        avg_clv_bps: ``None`` until scored, then the REAL pooled value (never fabricated).
        runs: ``None`` until scored, then the REAL run count (never fabricated).
        valid_pct: ``None`` until scored, then the REAL pooled value (never fabricated).
    """

    public_agent_id: str
    display_name: str
    owner_public_label: str
    origin: str
    proof_state: str
    agent_id: str
    type: str
    source_mode: str
    execution_mode: str
    status: str
    config_hash_present: bool
    avg_clv_bps: float | None = None
    runs: int | None = None
    valid_pct: float | None = None


class AgentRosterResponse(BaseModel):
    """Envelope for ``GET /agents/roster`` — the PUBLIC deployed-agent roster (single-field wrapper)."""

    agents: list[AgentRosterEntry]


class BacktestRunResponse(BaseModel):
    """Response envelope for ``POST /backtests``.

    Attributes:
        backtest_id: Deterministic id for the produced report (fetch it via ``GET /backtests/{id}``).
        mode_label: The honest mode-ladder label (always ``"Backtest"`` on this path — REQ-2D-304).
        run_id: The sealed ``RunResult`` id backing the report.
    """

    backtest_id: str
    mode_label: str
    run_id: str


# ---------------------------------------------------------------------------
# Maker arena lane — read-only sealed-result envelope (separate lane, SEC-005)
# ---------------------------------------------------------------------------


class MakerArenaResultResponse(BaseModel):
    """Response envelope for ``GET /maker/arena-result`` (read-only maker-UI bridge).

    Serves the SEALED :class:`~veridex.maker.result.MakerArenaResult` artifact over HTTP for the
    frontend. This is a SEPARATE lane from the directional leaderboard (SEC-005): the maker rank
    axis is ``avg_toxicity_loss_bps`` (ascending — lower quote toxicity is better), which is NOT a
    CLV/PnL/edge/return metric and is never relabelled as one. ``real_executable_edge_bps`` is
    pinned ``None`` throughout — the maker lane makes no fill or PnL claim.

    Attributes:
        schema_version: Frozen envelope version tag (``"maker_arena_result.v1"``).
        lane: Always ``"maker"`` — distinguishes this from the directional lane.
        source_mode: Always ``"replay"`` — the sealed artifact is a replay over the cp1 tape.
        rank_axis: The maker ranking metric name (``"avg_toxicity_loss_bps"``).
        rank_axis_direction: ``"asc"`` — lower toxicity loss ranks better.
        result: The sealed ``MakerArenaResult`` JSON (fields preserved unchanged).
        proof_card: ``render_proof_card(result)`` JSON — the honest one-glance summary.
        diagnostics: Axis-honesty labels distinguishing the rank axis from diagnostics.
    """

    schema_version: str = "maker_arena_result.v1"
    lane: str = "maker"
    source_mode: str = "replay"
    rank_axis: str = "avg_toxicity_loss_bps"
    rank_axis_direction: str = "asc"
    result: dict[str, Any]
    proof_card: dict[str, Any]
    diagnostics: dict[str, Any]
    #: ADDITIVE label join (never mutates the sealed ``result``). One row per raw ID in
    #: ``result.fixtures`` order: ``{fixture_id (raw int, always), home_team, away_team,
    #: kickoff_ts, label_source: "captured"|"unavailable"}``. Labels are captured/curated, never
    #: "verified"; a missing/malformed source yields all-"unavailable" rows (raw IDs preserved).
    fixture_metadata: list[dict[str, Any]] = []
