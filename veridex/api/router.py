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

from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query

from veridex.api.schemas import (
    AgentRegisterResponse,
    CompetitionCreateResponse,
    CompetitionLeaderboardRow,
    CompetitionStartResponse,
    CompetitionStateResponse,
    CompetitionSummaryResponse,
    DemoRunResponse,
    LeaderboardResponse,
    LeaderboardRow,
)
from veridex.competition.events import CompetitionEvent, EventType
from veridex.competition.models import AgentEntry, CompetitionConfig, CompetitionStatus
from veridex.competition.service import (
    CompetitionConflictError,
    CompetitionIntegrityError,
    CompetitionStateError,
    create_competition,
    register_agent,
    start_competition,
)
from veridex.ingest.marketstate import MarketState
from veridex.leaderboard import leaderboard as _build_leaderboard
from veridex.runtime.competition import (
    DEFAULT_CLUSTER,
    SCHEMA_VERSIONS,
    _default_checks,
    run_demo_competition,
)
from veridex.runtime.orchestrator import (
    PROOF_MODE_REPRODUCIBLE,
    Agent,
    deterministic_agent,
)
from veridex.runtime.schemas import AgentAction, SportsActionType
from veridex.scoring import score_run
from veridex.store import InMemoryStore, Store
from veridex.verifier.proof_card import proof_card_from_run_result

# The OVERUNDER market the demo agents disagree on (see ``_build_demo_ticks``).
_DEMO_MARKET_KEY = "OVERUNDER_PARTICIPANT_GOALS|half=1|line=1"


def _contrarian_agent(agent_id: str = "agent-beta") -> Agent:
    """Build a SECOND, differentiated deterministic agent (no LLM, no network).

    Where :func:`~veridex.runtime.orchestrator.deterministic_agent` picks the
    highest-probability side (the demo's "under"), this agent always FLAG_VALUEs the
    OTHER side ("over") of the same OVERUNDER market.  On the demo ticks "over" drifts
    DOWN while "under" drifts UP, so the contrarian earns a NEGATIVE CLV — giving the
    leaderboard a genuine rank-1 vs rank-2 split instead of a tie.  Fully deterministic
    and §4-scorable (market_key + side present on a non-suspended market).

    Args:
        agent_id: Identifier for this agent (defaults to ``"agent-beta"``).

    Returns:
        An :class:`~veridex.runtime.orchestrator.Agent` whose ``proof_mode`` is
        ``"reproducible"``.
    """

    async def decide(market_state: MarketState) -> AgentAction:
        return AgentAction(
            type=SportsActionType.FLAG_VALUE,
            params={"market_key": _DEMO_MARKET_KEY, "side": "over"},
        )

    return Agent(agent_id=agent_id, proof_mode=PROOF_MODE_REPRODUCIBLE, decide=decide)


# Module-level default store — shared across requests when the app runs under Uvicorn.
# Tests always inject their own store via ``create_app(store=InMemoryStore())``.
_default_store: InMemoryStore = InMemoryStore()


def _build_demo_ticks() -> list[MarketState]:
    """Build two deterministic MarketState ticks for the offline demo fixture.

    Tick 0 reflects the TxLINE fixture (17588404) opening snapshot.  Tick 1 models a
    later update where the "under" probability on the OVERUNDER market drifts up,
    ensuring a positive closing-line CLV (+184 bps) for the deterministic agents'
    tick-0 decisions and a 0-bps CLV for tick-1 decisions.

    Market keys follow the ``market_key()`` format:
    ``{SuperOddsType}|{MarketPeriod or ''}|{MarketParameters or ''}``.

    Returns:
        A two-element list of ``MarketState`` snapshots (tick 0 then tick 1).
    """
    tick0 = MarketState(
        fixture_id=17588404,
        tick_seq=0,
        ts=1782518383,
        phase=0,
        markets={
            # OVERUNDER_PARTICIPANT_GOALS, half=1, line=1 — from txline_native_messages[0]
            "OVERUNDER_PARTICIPANT_GOALS|half=1|line=1": {
                "stable_prob_bps": {"over": 4684, "under": 5316},
                "stable_price": {"over": 2.135, "under": 1.881},
                "suspended": False,
            },
            # 1X2_PARTICIPANT_RESULT — from txline_native_messages[1] (null period/params → "")
            "1X2_PARTICIPANT_RESULT||": {
                "stable_prob_bps": {"home": 4500, "draw": 2500, "away": 3000},
                "stable_price": {"home": 2.222, "draw": 4.000, "away": 3.333},
                "suspended": False,
            },
        },
        scores={},
    )

    # Tick 1: "under" drifts to 5500 (+184 bps vs tick 0) — positive CLV for tick-0 decisions.
    tick1 = MarketState(
        fixture_id=17588404,
        tick_seq=1,
        ts=1782518393,
        phase=0,
        markets={
            "OVERUNDER_PARTICIPANT_GOALS|half=1|line=1": {
                "stable_prob_bps": {"over": 4500, "under": 5500},
                "stable_price": {"over": 2.222, "under": 1.818},
                "suspended": False,
            },
            "1X2_PARTICIPANT_RESULT||": {
                "stable_prob_bps": {"home": 4600, "draw": 2400, "away": 3000},
                "stable_price": {"home": 2.174, "draw": 4.167, "away": 3.333},
                "suspended": False,
            },
        },
        scores={},
    )

    return [tick0, tick1]


def get_store() -> Store:
    """Dependency provider for the module-level default store.

    Returns:
        The module-level :class:`~veridex.store.InMemoryStore` instance.
    """
    return _default_store


def _build_agents_from_roster(entries: list[AgentEntry]) -> list[Agent]:
    """Build offline Agent objects from registered roster entries.

    2A offline simplification: each entry is mapped to a deterministic or contrarian agent
    (alternating by roster position) so the run is fully reproducible and produces a real
    ≥2-row leaderboard with distinct CLV.  Even-indexed entries → ``deterministic_agent``;
    odd-indexed entries → ``_contrarian_agent``.

    Note: This is a deliberate Phase-2A wiring simplification.  Real BYOA / live agent
    execution (2B) will route each entry to its actual execution environment.

    Args:
        entries: Registered :class:`~veridex.competition.models.AgentEntry` objects in
            roster order.

    Returns:
        A list of :class:`~veridex.runtime.orchestrator.Agent` objects, one per entry.
    """
    agents: list[Agent] = []
    for i, entry in enumerate(entries):
        if i % 2 == 0:
            agents.append(deterministic_agent(entry.agent_id))
        else:
            agents.append(_contrarian_agent(entry.agent_id))
    return agents


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


def create_app(store: Store | None = None) -> FastAPI:
    """Create the Veridex demo FastAPI application.

    Factory pattern: inject ``store`` in tests; omit for the default in-process store.

    Args:
        store: Optional :class:`~veridex.store.Store` override.  Defaults to the
            module-level ``_default_store`` (an :class:`~veridex.store.InMemoryStore`).

    Returns:
        A configured :class:`fastapi.FastAPI` application with nine endpoints: the Phase-1
        demo trio (``POST /demo/run``, ``GET /leaderboard``, ``GET /runs/{run_id}``) plus the
        six Phase-2A competition endpoints (``POST /competitions``,
        ``POST /competitions/{id}/agents``, ``POST /competitions/{id}/start``,
        ``GET /competitions/{id}``, ``GET /competitions``,
        ``GET /competitions/{id}/events``).
    """
    resolved_store: Store = store if store is not None else _default_store

    app = FastAPI(
        title="Veridex Demo API",
        description="TxLINE Agent Proof Arena — Phase 1 demo surface (REQ-115 / AC-115).",
        version="0.1.0",
    )

    # Per-app registry: run_id → {anchor_status, source_mode}.
    # Populated by POST /demo/run; consumed by GET /leaderboard.
    _run_meta: dict[str, dict[str, str]] = {}

    # --- Dependency -------------------------------------------------------

    def _get_store() -> Store:
        """Closure-captured store dependency for this app instance.

        Returns:
            The resolved :class:`~veridex.store.Store` for this application.
        """
        return resolved_store

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
        ticks = _build_demo_ticks()
        agents = [
            deterministic_agent("agent-alpha"),
            _contrarian_agent("agent-beta"),
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
        dep_store: Store = Depends(_get_store),  # noqa: B008
    ) -> CompetitionStartResponse:
        """Run a competition offline/deterministically and return the finalized state.

        2A offline simplification: market data is sourced from the demo ticks
        (``_build_demo_ticks()``), and roster entries are mapped to deterministic Agent
        objects by alternating between ``deterministic_agent`` (even index) and
        ``_contrarian_agent`` (odd index).  This produces a real ≥2-row leaderboard with
        distinct CLV without any LLM or network calls.  Real BYOA / live execution is a
        Phase 2B concern.

        Args:
            competition_id: The competition to start.
            dep_store: Injected store dependency.

        Returns:
            A :class:`~veridex.api.schemas.CompetitionStartResponse` with
            ``status="finalized"`` and ``run_id`` set.

        Raises:
            HTTPException: 404 when the competition is not found; 409 when already
                finalized or already running.
        """
        try:
            competition = await dep_store.get_competition(competition_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"competition {competition_id!r} not found") from None

        ticks = _build_demo_ticks()
        agents = _build_agents_from_roster(competition.entries)

        try:
            finalized = await start_competition(dep_store, competition_id, ticks, agents)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"competition {competition_id!r} not found") from None
        except CompetitionConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except CompetitionIntegrityError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from None
        except CompetitionStateError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
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

        return CompetitionStateResponse(
            competition_id=competition.competition_id,
            status=competition.status.value,
            config=competition.config.model_dump(mode="json"),
            roster=[e.model_dump(mode="json") for e in competition.entries],
            leaderboard=leaderboard,
            latest_seq=latest_seq,
            anchor_status=str(anchor_status),
            run_id=competition.run_id,
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

    return app
