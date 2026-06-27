"""FastAPI demo surface — B11b (REQ-115 / AC-115).

Factory: ``create_app(store)`` returns a configured FastAPI application.  The module-level
``_default_store`` is used when no store is injected (running under Uvicorn).  For testing,
pass an ``InMemoryStore`` to keep everything in-process and offline.

Endpoints
---------
``POST /demo/run``
    Run a demo competition over two deterministic agents (no LLM, no network).
    Persists the run, returns :class:`~veridex.api.schemas.DemoRunResponse`.
``GET /leaderboard``
    Aggregate scores from all previously stored runs into a ranked board.
    Returns :class:`~veridex.api.schemas.LeaderboardResponse`.
``GET /runs/{run_id}``
    Load a persisted run and return its proof card (with anchor block).
    Returns a plain ``dict`` matching the proof-card schema.  404 if unknown.

No auth / Redis / rate-limiting — Phase-2 only (CON-009).
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, HTTPException

from veridex.api.schemas import DemoRunResponse, LeaderboardResponse, LeaderboardRow
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


def create_app(store: Store | None = None) -> FastAPI:
    """Create the Veridex demo FastAPI application.

    Factory pattern: inject ``store`` in tests; omit for the default in-process store.

    Args:
        store: Optional :class:`~veridex.store.Store` override.  Defaults to the
            module-level ``_default_store`` (an :class:`~veridex.store.InMemoryStore`).

    Returns:
        A configured :class:`fastapi.FastAPI` application with three endpoints.
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

    return app
