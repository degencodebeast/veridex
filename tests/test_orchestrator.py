"""B5 — async run orchestrator + repository persistence (REQ-105 / AC-105, CON-010).

Strict TDD (each test watched RED before the orchestrator/store existed, then GREEN).

Architecture under test (CON-010 — async shell / sync core):
  * ``run_competition`` is ``async``; per tick it decides ALL agents CONCURRENTLY
    (``asyncio.gather``), each wrapped in ``asyncio.timeout`` and fail-closed.
  * The deterministic law / evidence / scoring stay SYNC and are called from the loop.
  * Persistence is via an async ``Store``; the offline suite uses ``InMemoryStore`` only
    (NO network / LLM / DB) — the async LLM agent is a stub, the deterministic agent is real.

The 3 codex carry-forwards are exercised here:
  1. Events are validated/coerced through ``RunEvent`` (int, unique ``sequence_no``) BEFORE hashing.
  2. ``source_mode`` is validated to ∈ {replay, live} at the run boundary.
  3. The SAME closing-horizon snapshot per market is passed to EVERY agent in a run.
"""

from __future__ import annotations

import asyncio
import os

import pytest

import veridex.runtime.orchestrator as orch
from veridex.ingest.marketstate import MarketState
from veridex.runtime.orchestrator import (
    EVENT_DECISION,
    EVENT_ERROR,
    EVENT_TICK,
    Agent,
    RunResult,
    deterministic_agent,
    llm_agent,
    run_competition,
    validate_run_events,
)
from veridex.runtime.schemas import AgentAction, SportsActionType
from veridex.store import InMemoryStore

KEY = "OU_2_5"


# ---------------------------------------------------------------------------
# Fixtures / helpers (dict-form stable_prob_bps — the B1 normalized contract)
# ---------------------------------------------------------------------------


def _market(prob_bps: dict[str, int], *, suspended: bool = False) -> dict:
    return {
        "stable_prob_bps": dict(prob_bps),
        "stable_price": {"over": 1.6, "under": 2.4},
        "suspended": suspended,
    }


def _ms(prob_bps: dict[str, int], *, tick_seq: int) -> MarketState:
    return MarketState(
        fixture_id=1,
        tick_seq=tick_seq,
        ts=1000 + tick_seq,
        phase=2,
        markets={KEY: _market(prob_bps)},
        scores={},
    )


def _marketstates() -> list[MarketState]:
    """Two ticks; OU 'over' drifts 6000 -> 6300 bps (entry-vs-close CLV +300)."""
    return [
        _ms({"over": 6000, "under": 4000}, tick_seq=0),
        _ms({"over": 6300, "under": 3700}, tick_seq=1),
    ]


class _FakeResponse:
    def __init__(self, content: object) -> None:
        self.content = content


class _FakeAsyncAgent:
    """Stub for Agno's Agent — exposes ``arun`` only (used via the injected factory)."""

    def __init__(self, content: object) -> None:
        self._content = content

    async def arun(self, prompt: str, **kwargs: object) -> _FakeResponse:
        return _FakeResponse(self._content)


_SENTINEL_MODEL = object()


def _stub_llm_agent(agent_id: str = "llm-stub") -> Agent:
    """A real ``llm_agent`` whose Agno seam is a fixed, offline async stub."""
    action = AgentAction(
        type=SportsActionType.FLAG_VALUE,
        params={"market_key": KEY, "side": "over", "confidence": 0.7},
    )

    def factory(**kwargs: object) -> _FakeAsyncAgent:
        return _FakeAsyncAgent(action)

    return llm_agent(agent_id, model=_SENTINEL_MODEL, agent_factory=factory)


# ---------------------------------------------------------------------------
# B5-1: a 2-agent run (stub async LLM + real deterministic) → events + score rows
# ---------------------------------------------------------------------------


async def test_two_agent_run_produces_events_and_score_rows() -> None:
    result = await run_competition(_marketstates(), [deterministic_agent(), _stub_llm_agent()], source_mode="replay")

    assert isinstance(result, RunResult)
    assert set(result.agent_ids) == {"deterministic-baseline", "llm-stub"}
    # tick + per-agent decision events exist; 2 ticks * (1 tick + 2 decisions) = 6 events.
    assert len(result.run_events) == 6
    assert any(e["event_type"] == EVENT_TICK for e in result.run_events)
    assert any(e["event_type"] == EVENT_DECISION for e in result.run_events)
    # Both agents score on both ticks (both FLAG 'over').
    assert len(result.score_rows) == 4
    assert len(result.evidence_hash) == 64


# ---------------------------------------------------------------------------
# B5-2: persist + load round-trips via InMemoryStore
# ---------------------------------------------------------------------------


async def test_inmemory_store_round_trips() -> None:
    store = InMemoryStore()
    result = await run_competition(
        _marketstates(), [deterministic_agent(), _stub_llm_agent()], source_mode="replay", store=store
    )

    loaded = await store.load_run(result.run_id)
    assert loaded == result
    assert loaded.source_mode == "replay"
    assert loaded.evidence_hash == result.evidence_hash
    assert loaded.run_events == result.run_events
    assert loaded.score_rows == result.score_rows
    assert loaded.proof_mode_map == result.proof_mode_map


# ---------------------------------------------------------------------------
# B5-3: agents decided CONCURRENTLY via asyncio.gather (per tick)
# ---------------------------------------------------------------------------


async def test_agents_decided_concurrently_per_tick() -> None:
    probe = {"active": 0, "max_active": 0}

    def _probe_agent(agent_id: str) -> Agent:
        async def decide(market_state: MarketState) -> AgentAction:
            probe["active"] += 1
            probe["max_active"] = max(probe["max_active"], probe["active"])
            await asyncio.sleep(0.02)  # hold the slot so a concurrent peer overlaps
            probe["active"] -= 1
            return AgentAction(type=SportsActionType.WAIT, params={})

        return Agent(agent_id=agent_id, proof_mode="reproducible", decide=decide)

    result = await run_competition(_marketstates(), [_probe_agent("a"), _probe_agent("b")], source_mode="replay")

    # If decisions were sequential, max concurrency would be 1. gather → 2 agents overlap.
    assert probe["max_active"] == 2
    # Both agents' decisions are present each tick (2 ticks * 2 agents).
    decisions = [e for e in result.run_events if e["event_type"] == EVENT_DECISION]
    assert len(decisions) == 4


# ---------------------------------------------------------------------------
# B5-4: timeout is fail-closed — slow agent → error event, peer still scored
# ---------------------------------------------------------------------------


async def test_timeout_is_fail_closed() -> None:
    def _slow_agent() -> Agent:
        async def decide(market_state: MarketState) -> AgentAction:
            await asyncio.sleep(5)  # far beyond the per-decide timeout (cancelled fast)
            return AgentAction(type=SportsActionType.WAIT)

        return Agent(agent_id="slow", proof_mode="reproducible", decide=decide)

    result = await run_competition(
        _marketstates(), [deterministic_agent(), _slow_agent()], source_mode="replay", decision_timeout_s=0.01
    )

    # The run completed (never aborted) and recorded an error event for the slow agent.
    error_events = [e for e in result.run_events if e["event_type"] == EVENT_ERROR]
    assert len(error_events) == 2  # one per tick for the slow agent
    assert all("slow" in (e["result_payload_json"] or "") for e in error_events)
    # The slow agent produced NO scored action; the deterministic agent still scored.
    assert all(row["agent_id"] != "slow" for row in result.score_rows)
    assert any(row["agent_id"] == "deterministic-baseline" for row in result.score_rows)


# ---------------------------------------------------------------------------
# B5-5: proof_mode labels correct + carried on rows and proof_mode_map
# ---------------------------------------------------------------------------


async def test_proof_mode_labels_correct_and_carried() -> None:
    det = deterministic_agent()
    llm = _stub_llm_agent()
    assert det.proof_mode == "reproducible"
    assert llm.proof_mode == "LLM/evidence-verified"

    result = await run_competition(_marketstates(), [det, llm], source_mode="replay")

    assert result.proof_mode_map == {
        "deterministic-baseline": "reproducible",
        "llm-stub": "LLM/evidence-verified",
    }
    for row in result.score_rows:
        assert row["proof_mode"] == result.proof_mode_map[row["agent_id"]]


# ---------------------------------------------------------------------------
# B5-6: source_mode carried; arbitrary source_mode → ValueError (carry-forward 2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["replay", "live"])
async def test_source_mode_carried(mode: str) -> None:
    result = await run_competition(_marketstates(), [deterministic_agent()], source_mode=mode)
    assert result.source_mode == mode


async def test_arbitrary_source_mode_rejected() -> None:
    with pytest.raises(ValueError, match="source_mode"):
        await run_competition(_marketstates(), [deterministic_agent()], source_mode="bogus")


# ---------------------------------------------------------------------------
# B5-7: SAME closing-horizon snapshot per market for ALL agents (carry-forward 3)
# ---------------------------------------------------------------------------


async def test_same_closing_snapshot_per_market_for_all_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    marketstates = _marketstates()
    captured: list[object] = []
    real = orch.recompute

    def spy(entry, action, *, closing, source_mode):  # type: ignore[no-untyped-def]
        captured.append(closing)
        return real(entry, action, closing=closing, source_mode=source_mode)

    monkeypatch.setattr(orch, "recompute", spy)

    await run_competition(marketstates, [deterministic_agent(), _stub_llm_agent()], source_mode="replay")

    # Both agents flag OU on both ticks → 4 recompute calls, EVERY one using the SAME
    # closing object: the final tick that contains the market (the closing horizon).
    assert len(captured) == 4
    assert all(c is marketstates[-1] for c in captured)


# ---------------------------------------------------------------------------
# B5-8: events RunEvent-validated/coerced before hashing (carry-forward 1)
# ---------------------------------------------------------------------------


def test_validate_run_events_coerces_non_int_sequence_no() -> None:
    raw = [
        {"sequence_no": "0", "event_type": EVENT_TICK},
        {"sequence_no": "1", "event_type": EVENT_DECISION},
    ]
    validated = validate_run_events(raw)
    assert [e["sequence_no"] for e in validated] == [0, 1]
    assert all(isinstance(e["sequence_no"], int) for e in validated)


async def test_run_events_are_int_sequence_no() -> None:
    result = await run_competition(_marketstates(), [deterministic_agent(), _stub_llm_agent()], source_mode="replay")
    seqs = [e["sequence_no"] for e in result.run_events]
    assert all(isinstance(s, int) for s in seqs)
    assert len(set(seqs)) == len(seqs)  # unique
    assert seqs == sorted(seqs)  # monotonic


# ---------------------------------------------------------------------------
# B5-9: gate-3 — a raw pre-score record precedes / binds every score row (CON-003)
# ---------------------------------------------------------------------------


async def test_raw_prescore_record_precedes_every_score_row() -> None:
    result = await run_competition(_marketstates(), [deterministic_agent(), _stub_llm_agent()], source_mode="replay")

    assert result.score_rows  # at least one scored action
    for row in result.score_rows:
        prescore = row["raw_prescore"]
        assert prescore["record_kind"] == "raw_prescore"
        # the score derives ONLY from the bound pre-score hash + recomputed values.
        assert row["raw_prescore_hash"] == prescore["raw_prescore_hash"]
        # the pre-score binds the whole-run evidence hash (evidence precedes scoring).
        assert prescore["evidence_hash"] == result.evidence_hash


# ---------------------------------------------------------------------------
# B5-10: evidence_hash is deterministic for identical inputs
# ---------------------------------------------------------------------------


async def test_evidence_hash_deterministic_for_same_inputs() -> None:
    agents1 = [deterministic_agent(), _stub_llm_agent()]
    agents2 = [deterministic_agent(), _stub_llm_agent()]
    r1 = await run_competition(_marketstates(), agents1, source_mode="replay")
    r2 = await run_competition(_marketstates(), agents2, source_mode="replay")
    assert r1.evidence_hash == r2.evidence_hash


# ---------------------------------------------------------------------------
# B5-11: import hygiene — orchestrator/store pull neither agno nor psycopg
# ---------------------------------------------------------------------------


def test_orchestrator_and_store_import_without_agno_or_psycopg() -> None:
    # AST check on the MODULE-LEVEL imports: neither module eagerly imports agno/psycopg
    # (both are lazy, inside functions). sys.modules is unreliable here — a prior test in
    # the same process may have lazily imported psycopg (e.g. the gated Postgres round-trip).
    import ast
    from pathlib import Path

    import veridex.runtime.orchestrator as orch
    import veridex.store as store

    forbidden = {"agno", "psycopg"}
    for mod in (orch, store):
        tree = ast.parse(Path(mod.__file__).read_text())
        for stmt in tree.body:  # module top-level only — not function-body (lazy) imports
            if isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    assert alias.name.split(".")[0] not in forbidden, f"{mod.__name__} eagerly imports {alias.name}"
            elif isinstance(stmt, ast.ImportFrom) and stmt.level == 0 and stmt.module:
                assert stmt.module.split(".")[0] not in forbidden, f"{mod.__name__} eagerly imports from {stmt.module}"


# ---------------------------------------------------------------------------
# B5-12: gated Postgres round-trip (skipped unless DATABASE_URL + psycopg present)
# ---------------------------------------------------------------------------


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
async def test_postgres_store_round_trip() -> None:
    import psycopg

    from veridex.store import PostgresStore

    store = PostgresStore(dsn=os.environ["DATABASE_URL"])
    result = await run_competition(_marketstates(), [deterministic_agent(), _stub_llm_agent()], source_mode="replay")

    async with await psycopg.AsyncConnection.connect(os.environ["DATABASE_URL"]) as conn:
        await store.init_db(conn)

    await store.persist_run(result)
    loaded = await store.load_run(result.run_id)

    assert loaded.run_id == result.run_id
    assert loaded.source_mode == result.source_mode
    assert loaded.evidence_hash == result.evidence_hash
    assert len(loaded.run_events) == len(result.run_events)
    assert len(loaded.score_rows) == len(result.score_rows)
    assert loaded.proof_mode_map == result.proof_mode_map
