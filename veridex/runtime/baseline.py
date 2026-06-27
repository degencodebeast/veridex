"""Deterministic baseline agent (reproducible-proof contestant; no LLM). Test-driven (T4)."""
from __future__ import annotations

from typing import Any

from veridex.runtime.schemas import AgentAction, SportsActionType

# Flag a market as a value pick when the consensus model prob is at/above even money.
FLAG_THRESHOLD_BPS = 5000


def deterministic_baseline_action(market_state: Any) -> AgentAction:
    """Pure-rules strategy: identical input → identical AgentAction (reproducible).

    Reads `MarketState` (no LLM, no time, no randomness). Iteration is key-sorted so the
    output is a deterministic function of the snapshot alone:
      1. any suspended market  → WIDEN_OR_SUSPEND
      2. highest-conviction market at/over the flag threshold → FLAG_VALUE
      3. otherwise              → WAIT
    """
    markets = getattr(market_state, "markets", {}) or {}

    for key in sorted(markets):
        if markets[key].get("suspended"):
            return AgentAction(type=SportsActionType.WIDEN_OR_SUSPEND, params={"market": key})

    # Deterministic pick: highest stable_prob_bps, ties broken by market key.
    ranked = sorted(
        markets.items(), key=lambda kv: (-int(kv[1].get("stable_prob_bps", 0)), kv[0])
    )
    if ranked:
        key, m = ranked[0]
        prob = int(m.get("stable_prob_bps", 0))
        if prob >= FLAG_THRESHOLD_BPS:
            return AgentAction(
                type=SportsActionType.FLAG_VALUE, params={"market": key, "stable_prob_bps": prob}
            )

    return AgentAction(type=SportsActionType.WAIT, params={})
