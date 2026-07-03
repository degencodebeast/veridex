"""T4 — pre-refactor `RunResult` golden baseline generator (REQ-2D-102).

Pins the CURRENT `run_competition` byte-output BEFORE the T5 `feed()/finalize()` refactor, so
T5 can prove it changed exactly zero sealed bytes. Fully offline and deterministic: no network,
no LLM, no real creds, no wall-clock leakage into the golden — fixed `run_id="golden-run-1"`,
deterministic agents only, and the error case's exception strings (`RuntimeError`/`boom`,
`TimeoutError`) are stable across runs.

Run directly to (re)generate the two committed fixtures:

    .venv/bin/python -m tests.golden.generate_golden
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import pathlib
from typing import Any

from veridex.ingest.marketstate import MarketState
from veridex.runtime.orchestrator import Agent, RunResult, deterministic_agent, run_competition
from veridex.runtime.schemas import AgentAction, SportsActionType
from veridex.strategies.momentum import momentum_agent

MARKET_KEY = "OU_2_5"
RUN_ID = "golden-run-1"
GOLDEN_DIR = pathlib.Path(__file__).parent
# Both explicit (symmetric) so the golden is provably env-independent — neither case relies on
# the ambient `get_settings().decision_timeout_s` (env `DECISION_TIMEOUT_S`). HAPPY_DECISION_TIMEOUT_S
# never actually fires (both agents are synchronous and fast); it just removes the env-coupling.
HAPPY_DECISION_TIMEOUT_S = 30.0
ERROR_DECISION_TIMEOUT_S = 0.01


def _market(prob_bps: dict[str, int], *, suspended: bool = False) -> dict[str, Any]:
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
        markets={MARKET_KEY: _market(prob_bps)},
        scores={},
    )


def _marketstates() -> list[MarketState]:
    """4 ticks; OU 'over' rises 6000 -> 6600 bps in steps of 200 (feeds momentum + baseline)."""
    return [
        _ms({"over": 6000, "under": 4000}, tick_seq=0),
        _ms({"over": 6200, "under": 3800}, tick_seq=1),
        _ms({"over": 6400, "under": 3600}, tick_seq=2),
        _ms({"over": 6600, "under": 3400}, tick_seq=3),
    ]


def _failing_agent() -> Agent:
    """Always raises — pins the RuntimeError('boom') error-event path."""

    async def decide(market_state: MarketState) -> AgentAction:
        raise RuntimeError("boom")

    return Agent(agent_id="failing", proof_mode="reproducible", decide=decide)


def _slow_agent() -> Agent:
    """Always exceeds the error case's decision_timeout_s — pins the TimeoutError path."""

    async def decide(market_state: MarketState) -> AgentAction:
        await asyncio.sleep(5)
        return AgentAction(type=SportsActionType.WAIT, params={})

    return Agent(agent_id="slow", proof_mode="reproducible", decide=decide)


def build_inputs(case: str) -> tuple[list[MarketState], list[Agent]]:
    """Build the (marketstates, agents) inputs for `case` ("happy" | "error")."""
    marketstates = _marketstates()
    if case == "happy":
        return marketstates, [deterministic_agent(), momentum_agent()]
    if case == "error":
        return marketstates, [deterministic_agent(), momentum_agent(), _failing_agent(), _slow_agent()]
    raise ValueError(f"unknown case: {case!r}")


def _result_to_dict(result: RunResult) -> dict[str, Any]:
    return dataclasses.asdict(result)


async def _run(case: str) -> dict[str, Any]:
    marketstates, agents = build_inputs(case)
    timeout_s = ERROR_DECISION_TIMEOUT_S if case == "error" else HAPPY_DECISION_TIMEOUT_S
    result = await run_competition(
        marketstates, agents, source_mode="replay", run_id=RUN_ID, decision_timeout_s=timeout_s
    )
    return _result_to_dict(result)


def run_case(case: str) -> dict[str, Any]:
    """Run `case` ("happy" | "error") end-to-end and return the RunResult as a plain dict."""
    return asyncio.run(_run(case))


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, indent=1)


def run_case_dump(case: str) -> str:
    """Run `case` and return the SAME canonical JSON string used to write the fixture.

    The single source of truth for "sealed bytes": both the writer (``main``) and the pinning
    test (``tests/test_orchestrator_golden.py``) go through this + ``_dump`` so the write path
    and the compare path can never diverge.
    """
    return _dump(run_case(case))


def main() -> None:
    for case in ("happy", "error"):
        first_json = run_case_dump(case)
        second_json = run_case_dump(case)
        if first_json != second_json:
            raise SystemExit(f"non-deterministic golden output for case={case!r}: two runs differ")
        path = GOLDEN_DIR / f"run_baseline_{case}.json"
        path.write_text(first_json + "\n")
        print(f"wrote {path} ({len(first_json)} bytes)")


if __name__ == "__main__":
    main()
