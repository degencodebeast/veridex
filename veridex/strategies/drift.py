"""S3 — CumulativeDriftAgent: smooth, sustained multi-tick repricing (POST-2D / M4).

Where momentum v2 (:mod:`veridex.strategies.momentum`) catches a SHARP, abrupt sharp-money move,
this agent catches the opposite regime: a SMOOTH, slow, SUSTAINED drift — the kind of multi-day (or
long in-play) repricing where the de-vigged Stable-Price probability grinds monotonically in one
direction. It follows the drifting side, betting the closing consensus confirms the trend
(``clv_bps = closing_bps[side] - entry_bps[side]`` — a side that keeps rising closes higher → +CLV).

Detector (per ``(market_key, side)``, all in logit space — probabilities move additively in
log-odds, linearising the "distance" of a move regardless of its level):

  1. cumulative logit drift — ``logit(latest) - logit(first)`` over the side's observed window; a
     genuine sustained repricing accrues a large same-signed cumulative move.
  2. EWMA-slope trend strength — an EWMA (param ``ewma_slope_alpha``) of the per-tick drift DIRECTION
     (``+1`` up / ``-1`` down / ``0`` flat). A smooth monotone rise smooths to ``+1``; choppy noise
     smooths toward ``0``. This is the "is it actually trending?" gate that separates a real drift
     from a random walk that merely happens to end higher.

A side is flagged only when ALL of: it has been observed at least ``min_tick_count`` times, its
observation horizon spans at least ``min_horizon_s`` seconds, its cumulative logit drift is
``>= cum_drift_logit_min`` (RISING — proposer never fades), AND its EWMA-slope trend strength is
``>= trend_strength_min``. A per-market cooldown then suppresses a refire for ``cooldown_ticks``
ticks. ``close_quality_required`` skips suspended/low-quality markets. The min-tick / min-horizon
gates are the ABSTAIN-ON-THIN-DATA guard: on a short or fast-but-brief tape the agent stays quiet.

Deterministic (same ticks → same actions) and causal (a tick-t decision integrates only ticks
``<= t`` — no lookahead), so a backtest is reproducible and integrity-preserving. Each agent owns
its own detector state (no module-level/shared state), so one agent's future can never perturb
another's prefix decision. PROPOSER ONLY (gate 1): it emits ``FOLLOW_MOMENTUM``; the deterministic
law scores edge/CLV. It imports NO LLM SDK; it is SEPARATE from ``sharp_momentum_agent``.
"""

from __future__ import annotations

from typing import Any

from veridex.ingest.marketstate import MarketState
from veridex.runtime.agent import AGENT_ACTION_SCHEMA_VERSION, agent_config_hash
from veridex.runtime.orchestrator import PROOF_MODE_REPRODUCIBLE, Agent
from veridex.runtime.schemas import AgentAction, SportsActionType
from veridex.strategies.sharp_stats import ewma, logit


class CumulativeDriftStrategy:
    """Stateful, deterministic cumulative-drift detector (one instance per agent per run).

    Per ``(market_key, side)`` it retains the logit series and its first-observation timestamp; per
    market it tracks the last tick it fired (the cooldown clock). State integrates only ticks
    already seen, so decisions are causal (no lookahead) and reproducible. Instances share NO state.
    """

    def __init__(
        self,
        *,
        cum_drift_logit_min: float = 0.15,
        ewma_slope_alpha: float = 0.2,
        trend_strength_min: float = 0.5,
        min_tick_count: int = 20,
        min_horizon_s: int = 600,
        close_quality_required: bool = True,
        cooldown_ticks: int = 5,
    ) -> None:
        """Initialise the detector.

        Args:
            cum_drift_logit_min: Minimum cumulative logit drift (RISING) required to flag a side.
            ewma_slope_alpha: EWMA smoothing factor in ``(0, 1]`` for the drift-direction trend.
            trend_strength_min: Minimum EWMA-slope trend strength (in ``[-1, 1]``) to confirm a trend.
            min_tick_count: Minimum observed ticks for a side before it can fire (thin-data guard).
            min_horizon_s: Minimum observation horizon (seconds) before a side can fire.
            close_quality_required: When ``True``, suspended/low-quality markets are skipped.
            cooldown_ticks: Ticks a market is suppressed after it fires.
        """
        self._cum_drift_logit_min = cum_drift_logit_min
        self._ewma_slope_alpha = ewma_slope_alpha
        self._trend_strength_min = trend_strength_min
        self._min_tick_count = min_tick_count
        self._min_horizon_s = min_horizon_s
        self._close_quality_required = close_quality_required
        self._cooldown_ticks = cooldown_ticks
        self._logits: dict[tuple[str, str], list[float]] = {}
        self._ts_first: dict[tuple[str, str], int] = {}
        self._last_fire_tick: dict[str, int] = {}
        self._tick = -1

    def reset(self) -> None:
        """Clear all accumulated state (reuse the instance for a fresh, independent run)."""
        self._logits.clear()
        self._ts_first.clear()
        self._last_fire_tick.clear()
        self._tick = -1

    def _score_side(self, market_key: str, side: str, prob_bps: int, ts: int) -> float | None:
        """Fold one tick's ``(market, side)`` observation into state; return its firing drift or None.

        State is ALWAYS updated (so history accrues during the thin-data window); a firing cumulative
        drift is returned only when every gate passes: enough observed ticks, enough horizon, RISING
        drift ``>= cum_drift_logit_min``, and EWMA-slope trend strength ``>= trend_strength_min``.
        """
        key = (market_key, side)
        series = self._logits.setdefault(key, [])
        series.append(logit(prob_bps / 10000.0))
        if key not in self._ts_first:
            self._ts_first[key] = ts

        # --- gates, cheap -> expensive ---
        if len(series) < self._min_tick_count:
            return None  # thin-data guard: not enough observations yet
        if ts - self._ts_first[key] < self._min_horizon_s:
            return None  # thin-data guard: observation horizon too short
        cumulative_drift = series[-1] - series[0]
        if cumulative_drift < self._cum_drift_logit_min:
            return None  # proposer follows RISING sides only; drift too small (or falling)
        # EWMA-slope trend strength: EWMA of the per-tick drift DIRECTION (+1 up / -1 down / 0 flat).
        directions = [_direction(series[i] - series[i - 1]) for i in range(1, len(series))]
        if not directions:
            return None
        trend_strength = ewma(directions, self._ewma_slope_alpha)
        if trend_strength < self._trend_strength_min:
            return None  # not a smooth, sustained trend (choppy / random-walk-ish)
        return cumulative_drift

    def _observe_and_rank(self, market_state: MarketState) -> list[tuple[float, str, str]]:
        """Fold this tick into state and return firing ``(drift, market_key, side)`` candidates."""
        markets: dict[str, dict[str, Any]] = getattr(market_state, "markets", {}) or {}
        ts = int(getattr(market_state, "ts", 0))
        candidates: list[tuple[float, str, str]] = []
        for market_key in sorted(markets):
            market = markets[market_key]
            if self._close_quality_required and market.get("suspended"):
                continue
            prob_bps = market.get("stable_prob_bps", {})
            if not isinstance(prob_bps, dict):
                continue
            for side in sorted(prob_bps):
                try:
                    bps = int(prob_bps[side])
                except (TypeError, ValueError):
                    continue
                drift = self._score_side(market_key, side, bps, ts)
                if drift is not None:
                    candidates.append((drift, market_key, side))
        return candidates

    def decide(self, market_state: MarketState) -> AgentAction:
        """Observe this tick, then flag the strongest drifting side (cooldown-permitting) or WAIT.

        Args:
            market_state: The immutable per-tick snapshot (data ``<= t`` only).

        Returns:
            A deterministic :class:`AgentAction` — ``FOLLOW_MOMENTUM`` or ``WAIT``.
        """
        self._tick += 1
        candidates = self._observe_and_rank(market_state)

        eligible = [
            (drift, market_key, side)
            for (drift, market_key, side) in candidates
            if self._tick - self._last_fire_tick.get(market_key, -(10**9)) > self._cooldown_ticks
        ]
        if not eligible:
            return AgentAction(type=SportsActionType.WAIT, params={})

        # Strongest cumulative drift wins; ties broken by (market_key, side) ascending — determined.
        drift, market_key, side = min(eligible, key=lambda c: (-c[0], c[1], c[2]))
        self._last_fire_tick[market_key] = self._tick
        return AgentAction(
            type=SportsActionType.FOLLOW_MOMENTUM,
            params={
                "market_key": market_key,
                "side": side,
                # UNTRUSTED UX metadata (gate 1) — never scored by the law:
                "reason": f"cumulative drift +{drift:.3f} logit, sustained trend",
                "claimed_edge_bps": int(round(drift * 100)),
            },
        )

    async def adecide(self, market_state: MarketState) -> AgentAction:
        """Async wrapper over :meth:`decide` (the orchestrator gathers ``async`` deciders)."""
        return self.decide(market_state)


def _direction(delta: float) -> float:
    """Sign of a per-tick logit move: ``+1`` up, ``-1`` down, ``0`` flat (the drift-trend indicator)."""
    if delta > 0.0:
        return 1.0
    if delta < 0.0:
        return -1.0
    return 0.0


def cumulative_drift_agent(
    *,
    cum_drift_logit_min: float = 0.15,
    ewma_slope_alpha: float = 0.2,
    trend_strength_min: float = 0.5,
    min_tick_count: int = 20,
    min_horizon_s: int = 600,
    close_quality_required: bool = True,
    cooldown_ticks: int = 5,
    agent_id: str = "cumulative-drift",
) -> Agent:
    """Build a reproducible-proof cumulative-drift contestant for the orchestrator.

    EVERY behavioural parameter enters the agent ``config_hash`` so a backtest is reproducible
    purely from its config. Proposer-only: the law scores edge/CLV; this agent self-certifies
    nothing. SEPARATE from :func:`~veridex.strategies.momentum.sharp_momentum_agent`.

    Args:
        cum_drift_logit_min: Minimum cumulative logit drift (RISING) required to flag a side.
        ewma_slope_alpha: EWMA smoothing factor for the drift-direction trend strength.
        trend_strength_min: Minimum EWMA-slope trend strength to confirm a smooth, sustained trend.
        min_tick_count: Minimum observed ticks for a side before it can fire (thin-data guard).
        min_horizon_s: Minimum observation horizon (seconds) before a side can fire.
        close_quality_required: When ``True``, suspended/low-quality markets are skipped.
        cooldown_ticks: Ticks a market is suppressed after it fires.
        agent_id: Identifier for this agent.

    Returns:
        An :class:`~veridex.runtime.orchestrator.Agent` whose ``proof_mode`` is ``"reproducible"``.
    """
    strategy = CumulativeDriftStrategy(
        cum_drift_logit_min=cum_drift_logit_min,
        ewma_slope_alpha=ewma_slope_alpha,
        trend_strength_min=trend_strength_min,
        min_tick_count=min_tick_count,
        min_horizon_s=min_horizon_s,
        close_quality_required=close_quality_required,
        cooldown_ticks=cooldown_ticks,
    )

    async def decide(market_state: MarketState) -> AgentAction:
        return await strategy.adecide(market_state)

    def config_hash(market_state: MarketState) -> str:
        # ALL behavioural params enter the hash → same config ⇒ same sealed identity ⇒ reproducible.
        return agent_config_hash(
            agent_id,
            (
                f"cumulative_drift:cum_drift_logit_min={cum_drift_logit_min}:"
                f"ewma_slope_alpha={ewma_slope_alpha}:trend_strength_min={trend_strength_min}:"
                f"min_tick_count={min_tick_count}:min_horizon_s={min_horizon_s}:"
                f"close_quality_required={close_quality_required}:cooldown_ticks={cooldown_ticks}"
            ),
            AGENT_ACTION_SCHEMA_VERSION,
        )

    return Agent(agent_id=agent_id, proof_mode=PROOF_MODE_REPRODUCIBLE, decide=decide, config_hash=config_hash)
