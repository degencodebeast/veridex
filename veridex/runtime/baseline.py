"""Deterministic baseline agent (reproducible-proof contestant; no LLM). Test-driven (T4)."""

from __future__ import annotations

from typing import Any

from veridex.runtime.schemas import AgentAction, SportsActionType

# Flag a market as a value pick when the consensus model prob is at/above even money.
FLAG_THRESHOLD_BPS = 5000


def deterministic_baseline_action(market_state: Any) -> AgentAction:
    """Pure-rules strategy: identical input → identical AgentAction (reproducible).

    Reads ``MarketState`` (no LLM, no time, no randomness). Iterates over all
    non-suspended ``(market_key, side)`` pairs whose ``stable_prob_bps[side]`` is
    numeric, picks the highest-conviction one (ties broken deterministically by
    ``(market_key, side)``), and emits:

    - ``FLAG_VALUE`` with ``params={"market_key": key, "side": side,
      "stable_prob_bps": bps}`` when the top prob is ≥ ``FLAG_THRESHOLD_BPS``
      (5 000 bps / even-money).
    - ``WAIT`` with empty params when no candidate clears the threshold, or when
      all markets are suspended.

    The emitted ``market_key`` + ``side`` satisfy the §4 law contract so that
    ``veridex.law.recompute`` can score the action (``valid=True``, numeric
    ``clv_bps``).

    Args:
        market_state: A ``MarketState`` snapshot (or any object whose ``.markets``
            attribute maps market-key → dict with ``stable_prob_bps: dict[str, int]``
            and ``suspended: bool``).

    Returns:
        A deterministic, reproducible ``AgentAction``.
    """
    markets: dict[str, dict[str, Any]] = getattr(market_state, "markets", {}) or {}

    # Collect (bps, market_key, side) for every non-suspended (key, side) pair.
    candidates: list[tuple[int, str, str]] = []
    for key, market in markets.items():
        if market.get("suspended"):
            continue
        prob_bps_map = market.get("stable_prob_bps", {})
        if not isinstance(prob_bps_map, dict):
            continue
        for side, bps in prob_bps_map.items():
            try:
                candidates.append((int(bps), key, side))
            except (TypeError, ValueError):
                continue

    if not candidates:
        return AgentAction(type=SportsActionType.WAIT, params={})

    # Sort by highest prob first; equal probs broken by (market_key, side) ascending
    # so the winner is fully determined by the snapshot alone (no insertion-order risk).
    best_bps, best_key, best_side = sorted(candidates, key=lambda t: (-t[0], t[1], t[2]))[0]

    if best_bps >= FLAG_THRESHOLD_BPS:
        return AgentAction(
            type=SportsActionType.FLAG_VALUE,
            params={
                "market_key": best_key,
                "side": best_side,
                "stable_prob_bps": best_bps,
            },
        )

    return AgentAction(type=SportsActionType.WAIT, params={})
