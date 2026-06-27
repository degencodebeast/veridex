"""Phase 1 demo CLI — judge-inspectable artifact in ~30 s (B11b, REQ-115 / AC-115).

Usage::

    python scripts/demo_phase1.py          # offline: fixture + mocked anchor (not_anchored)
    python scripts/demo_phase1.py --live   # live: real Solana anchor (requires creds in env)

Prints the proof-card JSON then the leaderboard JSON to stdout, separated by labelled
headers so a judge can pipe or redirect each block independently.

The script is importable (``from scripts.demo_phase1 import main``) so the test suite can
invoke it in-process and capture stdout without spawning a subprocess.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from veridex.ingest.marketstate import MarketState
from veridex.runtime.competition import AnchorFn, run_demo_competition
from veridex.runtime.orchestrator import (
    PROOF_MODE_REPRODUCIBLE,
    Agent,
    deterministic_agent,
)
from veridex.runtime.schemas import AgentAction, SportsActionType

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


def _build_demo_ticks() -> list[MarketState]:
    """Return two deterministic ``MarketState`` ticks for the offline demo.

    Tick 0 reflects the TxLINE fixture (17588404) opening snapshot.
    Tick 1 models a later update where the "under" probability increases by 184 bps,
    ensuring a positive closing-line CLV for tick-0 decisions.

    Returns:
        Two-element list ``[tick0, tick1]``.
    """
    tick0 = MarketState(
        fixture_id=17588404,
        tick_seq=0,
        ts=1782518383,
        phase=0,
        markets={
            "OVERUNDER_PARTICIPANT_GOALS|half=1|line=1": {
                "stable_prob_bps": {"over": 4684, "under": 5316},
                "stable_price": {"over": 2.135, "under": 1.881},
                "suspended": False,
            },
            "1X2_PARTICIPANT_RESULT||": {
                "stable_prob_bps": {"home": 4500, "draw": 2500, "away": 3000},
                "stable_price": {"home": 2.222, "draw": 4.000, "away": 3.333},
                "suspended": False,
            },
        },
        scores={},
    )
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


async def _run(*, live: bool) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run the demo competition and return ``(proof_card, leaderboard)``.

    Args:
        live: When ``True``, uses the real :func:`~veridex.chain.anchor.anchor_memo`
            (requires Solana creds in env).  When ``False``, skips anchoring
            (``anchor_fn=None`` → ``"not_anchored"``).

    Returns:
        A ``(proof_card, leaderboard)`` tuple.
    """
    anchor_fn: AnchorFn | None
    if live:
        from veridex.chain.anchor import anchor_memo

        anchor_fn = anchor_memo
    else:
        anchor_fn = None

    ticks = _build_demo_ticks()
    agents = [
        deterministic_agent("agent-alpha"),
        _contrarian_agent("agent-beta"),
    ]

    result = await run_demo_competition(
        ticks,
        agents,
        source_mode="replay",
        anchor_fn=anchor_fn,
    )
    return result.proof_card, result.leaderboard


def main() -> None:
    """Entry point: run the demo and print proof-card + leaderboard JSON to stdout.

    Reads ``--live`` from ``sys.argv`` to switch between the offline (mock anchor)
    and live (real Solana anchor) paths.  Always exits cleanly — anchor errors in
    live mode are not swallowed; they propagate as exceptions so CI catches them.
    """
    live = "--live" in sys.argv
    proof_card, leaderboard = asyncio.run(_run(live=live))

    print("=== PROOF CARD ===")
    print(json.dumps(proof_card, indent=2, default=str))
    print("\n=== LEADERBOARD ===")
    print(json.dumps(leaderboard, indent=2, default=str))


if __name__ == "__main__":
    main()
