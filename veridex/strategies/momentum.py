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
from veridex.strategies.sharp_stats import PageHinkley, logit, robust_z


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


# ============================================================================================
# Momentum v2 — sharp-move detection (REQ-2D-502)
# ============================================================================================
#
# v1 (above) flags any side whose de-vigged prob is a net ``last - first`` above a bps threshold,
# which FALSE-POSITIVES on ordinary volatility that merely happens to end higher than it started.
# v2 supersedes that naive delta with real sharp-move statistics layered on ``sharp_stats``:
#
#   1. logit-space movement — probabilities move additively in log-odds, linearising a move's
#      "distance" regardless of its starting level (a 2%→3% and a 50%→51% move differ).
#   2. EWMA-smoothed level (param ``alpha``) — denoises single-tick spikes; the recurrence is
#      exactly :func:`sharp_stats.ewma` applied incrementally (``s = alpha*x + (1-alpha)*s_prev``).
#   3. robust z-score (median/MAD, ``scale_floor``-floored) of the latest smoothed movement vs its
#      recent window — immune to a lone outlier that would trip a mean/std z; the floor lets a flat
#      market that suddenly reprices still register a finite, large z.
#   4. DIRECTIONAL Page-Hinkley change-point on the smoothed level — confirms the move is SUSTAINED
#      *and in the same direction* as the shock (a downward change-point can never confirm an up move).
#   5. persistence — at least 2 of the last 3 smoothed movements share the shock's direction AND
#      their cumulative logit move clears ``persistence_logit`` (a short-window soccer confirmation).
#
# A side is flagged only when, PAST the warmup window and with enough movement samples, it is RISING
# and ``z >= z_threshold`` AND directional Page-Hinkley confirms "up" AND up-persistence holds (AND,
# not OR — a lone PH trip or lone persistence run is not enough, which keeps single spikes and slow
# drifts quiet). A per-market cooldown then suppresses a refire for ``cooldown_ticks`` ticks.
# Deterministic (same ticks → same actions) and causal (a tick-t decision integrates only ticks
# <= t — no lookahead), so a backtest is reproducible and integrity-preserving. PROPOSER ONLY
# (gate 1 / CON-2D-501): it emits ``FOLLOW_MOMENTUM`` proposals; the deterministic law scores
# edge/CLV. The demo claim is "v2 reduces naive-momentum false positives while still catching
# sustained TxLINE repricing" — NOT "this proves sharp money"; ``reason``/``claimed_edge_bps`` are
# UNTRUSTED UX metadata, never scored.

# Clean market families v2 will act on. Props (player points/assists, anytime-scorer, …) are
# DEFERRED — their price process is noisier and less liquid, so the sharp-move model does not
# apply cleanly yet. Matched by a case-insensitive key prefix. ``OVERUNDER_PARTICIPANT_GOALS`` is
# the REAL normalized TxLINE totals family (deepest liquidity — see
# ``veridex.ingest.txline_normalize.market_key``); the synthetic-demo literal "OU" masked its
# absence, so real totals were silently ineligible until it was added here.
CLEAN_FAMILY_PREFIXES: tuple[str, ...] = ("1X2", "OU", "TOTAL", "OVER_UNDER", "OVERUNDER_PARTICIPANT_GOALS", "WLD")


def is_clean_family(market_key: str) -> bool:
    """Whether ``market_key`` belongs to a clean family (1X2 / totals) v2 is allowed to act on.

    Args:
        market_key: The market identifier (e.g. ``"1X2_HOME"``, ``"OU_2_5"``, ``"PLAYER_PROP_…"``).

    Returns:
        ``True`` for 1X2 / totals families; ``False`` for props and anything else (deferred).
    """
    key = market_key.upper()
    return key.startswith(CLEAN_FAMILY_PREFIXES)


# UI-facing labels so a leaderboard/inspector can name the two momentum variants distinctly (the
# flagship demo runs v2 explicitly; v1 remains the golden-pinned baseline). Keyed by agent_id.
MOMENTUM_V1_LABEL = "Momentum v1 baseline"
SHARP_MOMENTUM_V2_LABEL = "Sharp Momentum v2"
STRATEGY_LABELS: dict[str, str] = {
    "momentum": MOMENTUM_V1_LABEL,
    "momentum-sharp": SHARP_MOMENTUM_V2_LABEL,
}

# Persistence-confirmation invariants (fixed, NOT tunable knobs): at least ``_PERSISTENCE_COUNT``
# of the last ``_PERSISTENCE_WINDOW`` smoothed movements must share the shock's direction. A short
# window suits in-play soccer, where a genuine repricing shows up as a run of same-signed ticks.
_PERSISTENCE_WINDOW = 3
_PERSISTENCE_COUNT = 2


class SharpMomentumStrategy:
    """Stateful, deterministic sharp-move detector (one instance per agent per run).

    Per ``(market_key, side)`` it maintains an EWMA-smoothed logit level, the recent smoothed
    movements, and a :class:`~veridex.strategies.sharp_stats.PageHinkley` detector; per market it
    tracks the last tick it fired (the cooldown clock). State integrates only ticks already seen,
    so decisions are causal (no lookahead) and reproducible.
    """

    def __init__(
        self,
        *,
        alpha: float = 0.4,
        z_threshold: float = 2.5,
        ph_delta: float = 0.01,
        ph_lambda: float = 0.15,
        cooldown_ticks: int = 3,
        warmup_ticks: int = 10,
        min_movements: int = 8,
        lookback: int = 64,
        scale_floor: float = 0.02,
        persistence_logit: float = 0.06,
    ) -> None:
        """Initialise the detector.

        Args:
            alpha: EWMA smoothing factor in ``(0, 1]`` for the logit level (lower → more denoising).
            z_threshold: Minimum robust z-score of the latest movement required to flag.
            ph_delta: Page-Hinkley per-step magnitude tolerance (absorbs noise).
            ph_lambda: Page-Hinkley alarm threshold (higher → later, more conservative).
            cooldown_ticks: Ticks a market is suppressed after it fires.
            warmup_ticks: No v2 action is emitted before this many ticks have been observed.
            min_movements: Minimum per-side smoothed-movement samples before robust-z can fire.
            lookback: Max smoothed movements retained per side (the robust-z window).
            scale_floor: Minimum robust-z denominator scale (logit units) — lets a flat market's
                sudden repricing register instead of dividing by a ~zero MAD.
            persistence_logit: Minimum cumulative logit move over the persistence window to confirm.
        """
        self._alpha = alpha
        self._z_threshold = z_threshold
        self._ph_delta = ph_delta
        self._ph_lambda = ph_lambda
        self._cooldown_ticks = cooldown_ticks
        self._warmup_ticks = warmup_ticks
        self._min_movements = min_movements
        self._lookback = lookback
        self._scale_floor = scale_floor
        self._persistence_logit = persistence_logit
        self._smoothed: dict[tuple[str, str], float] = {}
        self._movements: dict[tuple[str, str], list[float]] = {}
        self._ph: dict[tuple[str, str], PageHinkley] = {}
        self._last_fire_tick: dict[str, int] = {}
        self._tick = -1

    def reset(self) -> None:
        """Clear all accumulated state (reuse the instance for a fresh, independent run)."""
        self._smoothed.clear()
        self._movements.clear()
        self._ph.clear()
        self._last_fire_tick.clear()
        self._tick = -1

    def _persists_up(self, window: list[float]) -> bool:
        """Whether the recent movements form a sustained UP run (persistence confirmation).

        True iff at least ``_PERSISTENCE_COUNT`` of the last ``_PERSISTENCE_WINDOW`` smoothed
        movements are positive AND their cumulative logit move clears ``persistence_logit``.
        """
        recent = window[-_PERSISTENCE_WINDOW:]
        if len(recent) < _PERSISTENCE_WINDOW:
            return False
        ups = sum(1 for m in recent if m > 0.0)
        return ups >= _PERSISTENCE_COUNT and sum(recent) > self._persistence_logit

    def _score_side(self, market_key: str, side: str, prob_bps: int) -> float | None:
        """Fold one tick's ``(market, side)`` observation into state; return its firing z or None.

        State is ALWAYS updated (so warmup builds history); a firing z is returned only when every
        gate passes: past warmup, enough movement samples, RISING, ``z >= z_threshold``, a
        directional-UP Page-Hinkley change-point, AND up-persistence. The first observation of a
        side only seeds state (no movement yet) and returns ``None``.
        """
        key = (market_key, side)
        raw_level = logit(prob_bps / 10000.0)
        prev = self._smoothed.get(key)
        if prev is None:
            # Seed: first level for this side. Start its Page-Hinkley clock; no movement exists yet.
            self._smoothed[key] = raw_level
            self._ph[key] = PageHinkley(delta=self._ph_delta, lambda_=self._ph_lambda)
            self._ph[key].update(raw_level)
            return None

        # EWMA-smoothed level (identical recurrence to sharp_stats.ewma, applied incrementally).
        smoothed = self._alpha * raw_level + (1.0 - self._alpha) * prev
        movement = smoothed - prev
        self._smoothed[key] = smoothed

        window = self._movements.setdefault(key, [])
        window.append(movement)
        if len(window) > self._lookback:
            del window[: len(window) - self._lookback]

        ph_dir = self._ph[key].update(smoothed)  # "up" / "down" / None — direction, not a bare bool

        # --- gates, cheap → expensive ---
        if self._tick < self._warmup_ticks:
            return None  # global warmup: observe but never act
        if len(window) < self._min_movements:
            return None  # not enough per-side samples for a robust estimate
        if movement <= 0.0:
            return None  # proposer follows RISING sides only (never fades)
        z = robust_z(window, scale_floor=self._scale_floor)
        if z < self._z_threshold:
            return None  # not a statistically significant shock
        # Directional confirmation: BOTH an UP change-point AND up-persistence must agree with the
        # up shock. AND (not OR) keeps single spikes (PH may trip, persistence won't) and slow
        # drifts (persistence may hold, PH won't) quiet — the false-positive reduction is the point.
        if ph_dir != "up":
            return None
        if not self._persists_up(window):
            return None
        return z

    def _observe_and_rank(self, market_state: MarketState) -> list[tuple[float, str, str]]:
        """Fold this tick into state and return firing ``(z, market_key, side)`` candidates."""
        markets: dict[str, dict[str, Any]] = getattr(market_state, "markets", {}) or {}
        candidates: list[tuple[float, str, str]] = []
        for market_key in sorted(markets):
            if not is_clean_family(market_key):
                continue
            market = markets[market_key]
            if market.get("suspended"):
                continue
            prob_bps = market.get("stable_prob_bps", {})
            if not isinstance(prob_bps, dict):
                continue
            for side in sorted(prob_bps):
                try:
                    bps = int(prob_bps[side])
                except (TypeError, ValueError):
                    continue
                z = self._score_side(market_key, side, bps)
                if z is not None:
                    candidates.append((z, market_key, side))
        return candidates

    def decide(self, market_state: MarketState) -> AgentAction:
        """Observe this tick, then flag the strongest sharp riser (cooldown-permitting) or WAIT.

        Args:
            market_state: The immutable per-tick snapshot (data ``<= t`` only).

        Returns:
            A deterministic :class:`AgentAction` — ``FOLLOW_MOMENTUM`` or ``WAIT``.
        """
        self._tick += 1
        candidates = self._observe_and_rank(market_state)

        eligible = [
            (z, market_key, side)
            for (z, market_key, side) in candidates
            if self._tick - self._last_fire_tick.get(market_key, -(10**9)) > self._cooldown_ticks
        ]
        if not eligible:
            return AgentAction(type=SportsActionType.WAIT, params={})

        # Strongest z wins; ties broken by (market_key, side) ascending — fully determined.
        z, market_key, side = min(eligible, key=lambda c: (-c[0], c[1], c[2]))
        self._last_fire_tick[market_key] = self._tick
        return AgentAction(
            type=SportsActionType.FOLLOW_MOMENTUM,
            params={
                "market_key": market_key,
                "side": side,
                # UNTRUSTED UX metadata (gate 1) — never scored by the law:
                "reason": f"sharp move z={z:.2f}, page-hinkley confirmed",
                "claimed_edge_bps": int(round(z * 100)),
            },
        )

    async def adecide(self, market_state: MarketState) -> AgentAction:
        """Async wrapper over :meth:`decide` (the orchestrator gathers ``async`` deciders)."""
        return self.decide(market_state)


def sharp_momentum_agent(
    agent_id: str = "momentum-sharp",
    *,
    alpha: float = 0.4,
    z_threshold: float = 2.5,
    ph_delta: float = 0.01,
    ph_lambda: float = 0.15,
    cooldown_ticks: int = 3,
    warmup_ticks: int = 10,
    min_movements: int = 8,
    lookback: int = 64,
    scale_floor: float = 0.02,
    persistence_logit: float = 0.06,
) -> Agent:
    """Build a reproducible-proof sharp-move (momentum v2) contestant for the orchestrator.

    EVERY behavioural parameter enters the agent ``config_hash`` so a backtest is reproducible
    purely from its config. Proposer-only: the law scores edge/CLV; this agent self-certifies
    nothing.

    Args:
        agent_id: Identifier for this agent.
        alpha: EWMA smoothing factor for the logit level.
        z_threshold: Minimum robust z-score to flag a side.
        ph_delta: Page-Hinkley magnitude tolerance.
        ph_lambda: Page-Hinkley alarm threshold.
        cooldown_ticks: Ticks a market is suppressed after firing.
        warmup_ticks: No action before this many observed ticks.
        min_movements: Minimum per-side movement samples before robust-z can fire.
        lookback: Robust-z movement window size.
        scale_floor: Minimum robust-z scale (logit units).
        persistence_logit: Minimum cumulative logit move over the persistence window to confirm.

    Returns:
        An :class:`~veridex.runtime.orchestrator.Agent` whose ``proof_mode`` is ``"reproducible"``.
    """
    strategy = SharpMomentumStrategy(
        alpha=alpha,
        z_threshold=z_threshold,
        ph_delta=ph_delta,
        ph_lambda=ph_lambda,
        cooldown_ticks=cooldown_ticks,
        warmup_ticks=warmup_ticks,
        min_movements=min_movements,
        lookback=lookback,
        scale_floor=scale_floor,
        persistence_logit=persistence_logit,
    )

    async def decide(market_state: MarketState) -> AgentAction:
        return await strategy.adecide(market_state)

    def config_hash(market_state: MarketState) -> str:
        # ALL behavioural params enter the hash → same config ⇒ same sealed identity ⇒ reproducible.
        return agent_config_hash(
            agent_id,
            (
                f"sharp_momentum:alpha={alpha}:z_threshold={z_threshold}:"
                f"ph_delta={ph_delta}:ph_lambda={ph_lambda}:cooldown_ticks={cooldown_ticks}:"
                f"warmup_ticks={warmup_ticks}:min_movements={min_movements}:lookback={lookback}:"
                f"scale_floor={scale_floor}:persistence_logit={persistence_logit}"
            ),
            AGENT_ACTION_SCHEMA_VERSION,
        )

    return Agent(agent_id=agent_id, proof_mode=PROOF_MODE_REPRODUCIBLE, decide=decide, config_hash=config_hash)
