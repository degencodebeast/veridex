"""WD-2 — deterministic trend-continuation (momentum) strategy (REQ-051).

The real-edge agent: it earns positive CLV by taking the side whose de-vigged Stable-Price
probability is *rising*, betting the closing consensus confirms the move
(``clv_bps = closing_bps[side] - entry_bps[side]`` — a rising side closes higher → +CLV). This is
a timing/momentum edge over the baseline's favorite-flagging — NOT de-vigging (TxLINE already
de-margins the consensus).

NO SELF-CERTIFICATION (gate 1): the strategy only PROPOSES a side; the deterministic law
(``veridex.law.recompute``) computes the CLV. Any ``reason``/``confidence``/``claimed_edge_bps``
placed in ``params`` is UNTRUSTED UX metadata and is never scored.

The strategy is a reproducible-proof agent: stateful only over the ticks it has already been
given (all ``<= t`` — never future rows) and fully deterministic (same tick sequence → same
actions). It imports NO LLM SDK; it lives in the agent shell (``strategies/``), alongside
``value.py``.
"""

from __future__ import annotations

from typing import Any

from veridex.ingest.marketstate import MarketState
from veridex.runtime.agent import AGENT_ACTION_SCHEMA_VERSION, agent_config_hash
from veridex.runtime.orchestrator import PROOF_MODE_REPRODUCIBLE, Agent
from veridex.runtime.schemas import AgentAction, SportsActionType


def prob_momentum(history: list[dict[str, int]], side: str) -> int:
    """Net change in ``side``'s ``stable_prob_bps`` across the observed window (last − first).

    Reads only observations that carry the side. With fewer than two such observations there is
    no trend, so the momentum is ``0``.

    Args:
        history: Per-tick ``{side -> stable_prob_bps}`` observations, oldest first.
        side: The market side to measure.

    Returns:
        ``last_bps - first_bps`` over the side's observations, or ``0`` when < 2 observations.
    """
    series = [obs[side] for obs in history if side in obs]
    if len(series) < 2:
        return 0
    return int(series[-1] - series[0])


def select_momentum_action(
    history_by_market: dict[str, list[dict[str, int]]],
    *,
    min_momentum_bps: int,
) -> AgentAction:
    """Pick the strongest-rising ``(market_key, side)`` clearing ``min_momentum_bps``, else WAIT.

    Pure and deterministic: ties (equal momentum) are broken by ``(market_key, side)`` ascending
    so the choice is fully determined by the inputs.

    Args:
        history_by_market: ``market_key -> [ {side -> stable_prob_bps}, ... ]`` (oldest first).
        min_momentum_bps: Minimum positive momentum (bps) a side must show to be flagged.

    Returns:
        A ``FOLLOW_MOMENTUM`` :class:`AgentAction` on the strongest riser, or ``WAIT``.
    """
    best: tuple[int, str, str] | None = None
    for market_key in sorted(history_by_market):
        history = history_by_market[market_key]
        sides = {side for obs in history for side in obs}
        for side in sorted(sides):
            momentum = prob_momentum(history, side)
            if momentum < min_momentum_bps:
                continue
            candidate = (momentum, market_key, side)
            # Highest momentum wins; ties broken by (market_key, side) ascending (already sorted).
            if best is None or momentum > best[0]:
                best = candidate

    if best is None:
        return AgentAction(type=SportsActionType.WAIT, params={})

    momentum, market_key, side = best
    return AgentAction(
        type=SportsActionType.FOLLOW_MOMENTUM,
        params={
            "market_key": market_key,
            "side": side,
            # UNTRUSTED UX metadata (gate 1) — never scored by the law:
            "reason": f"prob momentum +{momentum}bps",
            "claimed_edge_bps": momentum,
        },
    )


class MomentumStrategy:
    """Stateful, deterministic momentum decision-maker (one instance per agent per run).

    Accumulates per-market ``stable_prob_bps`` observations as ticks arrive and, each tick,
    flags the strongest-rising side. Only the last ``lookback`` observations per market are kept.
    """

    def __init__(self, *, lookback: int = 8, min_momentum_bps: int = 50) -> None:
        """Initialise the strategy.

        Args:
            lookback: Max observations retained per market (the momentum window).
            min_momentum_bps: Minimum positive momentum (bps) required to flag a side.
        """
        self._lookback = lookback
        self._min_momentum_bps = min_momentum_bps
        self._history_by_market: dict[str, list[dict[str, int]]] = {}

    def reset(self) -> None:
        """Clear all accumulated observations (reuse the instance for a fresh run)."""
        self._history_by_market.clear()

    def _observe(self, market_state: MarketState) -> None:
        """Append the current tick's non-suspended numeric ``stable_prob_bps`` per market."""
        markets: dict[str, dict[str, Any]] = getattr(market_state, "markets", {}) or {}
        for market_key, market in markets.items():
            if market.get("suspended"):
                continue
            prob_bps = market.get("stable_prob_bps", {})
            if not isinstance(prob_bps, dict):
                continue
            obs: dict[str, int] = {}
            for side, bps in prob_bps.items():
                try:
                    obs[side] = int(bps)
                except (TypeError, ValueError):
                    continue
            if not obs:
                continue
            window = self._history_by_market.setdefault(market_key, [])
            window.append(obs)
            if len(window) > self._lookback:
                del window[: len(window) - self._lookback]

    def decide(self, market_state: MarketState) -> AgentAction:
        """Observe this tick, then flag the strongest-rising side (or WAIT).

        Args:
            market_state: The immutable per-tick snapshot (data ``<= t`` only).

        Returns:
            A deterministic :class:`AgentAction`.
        """
        self._observe(market_state)
        return select_momentum_action(self._history_by_market, min_momentum_bps=self._min_momentum_bps)

    async def adecide(self, market_state: MarketState) -> AgentAction:
        """Async wrapper over :meth:`decide` (the orchestrator gathers ``async`` deciders)."""
        return self.decide(market_state)


def momentum_agent(agent_id: str = "momentum", *, lookback: int = 8, min_momentum_bps: int = 50) -> Agent:
    """Build a reproducible-proof momentum contestant for the orchestrator.

    Args:
        agent_id: Identifier for this agent.
        lookback: Momentum window (observations per market).
        min_momentum_bps: Minimum positive momentum (bps) required to flag a side.

    Returns:
        An :class:`~veridex.runtime.orchestrator.Agent` whose ``proof_mode`` is ``"reproducible"``.
    """
    strategy = MomentumStrategy(lookback=lookback, min_momentum_bps=min_momentum_bps)

    async def decide(market_state: MarketState) -> AgentAction:
        return await strategy.adecide(market_state)

    def config_hash(market_state: MarketState) -> str:
        return agent_config_hash(
            agent_id,
            f"momentum:lookback={lookback}:min_momentum_bps={min_momentum_bps}",
            AGENT_ACTION_SCHEMA_VERSION,
        )

    return Agent(agent_id=agent_id, proof_mode=PROOF_MODE_REPRODUCIBLE, decide=decide, config_hash=config_hash)
