"""M5 (S4) — deterministic baselines for the S6 evaluation (Task 15).

TRUST INVARIANT: none of these are agents-with-edge. Every callable in :data:`BASELINES` carries
``is_baseline = True`` — that flag is what the S6 evaluation checks to keep a baseline from ever
being mislabeled an alpha-earning agent. Each baseline is a pure, deterministic decision rule
returning an :class:`~veridex.runtime.schemas.AgentAction`; ``seeded_random`` uses its own
:class:`random.Random` instance (never module-level ``random.random()``) so the same seed always
reproduces the same decision.
"""

from __future__ import annotations

import random
from collections.abc import Callable

from veridex.backtest.benchmark import translate_threshold
from veridex.runtime.schemas import AgentAction, SportsActionType

#: Label attached to baseline results wherever a report distinguishes baseline from agent rows.
BASELINE_LABEL = "baseline"


def no_trade(prices: list[float], horizon_s: int) -> AgentAction:
    """Never trade — the zero-edge floor every real strategy must clear."""
    return AgentAction(type=SportsActionType.WAIT, params={})


no_trade.is_baseline = True  # type: ignore[attr-defined]


def favorite(fair_probs: dict[str, float], horizon_s: int) -> AgentAction:
    """Back the side with the highest fair probability (ties broken by side name, ascending)."""
    if not fair_probs:
        return AgentAction(type=SportsActionType.WAIT, params={})
    best_side = max(sorted(fair_probs), key=lambda side: fair_probs[side])
    return AgentAction(
        type=SportsActionType.FOLLOW_MOMENTUM,
        params={"side": best_side, "reason": "baseline: highest fair-prob favorite"},
    )


favorite.is_baseline = True  # type: ignore[attr-defined]


def threshold_move(
    prices: list[float], horizon_s: int, *, move_threshold_pct: float = 2.0
) -> AgentAction:
    """Fire on a flat percent-move threshold — sports-workbench's detector (`translate_threshold`).

    Reuses `translate_threshold`'s config shape so the threshold is expressed in the same terms
    as the M2 competitor-replication benchmark, not a bespoke number invented here.
    """
    cfg = translate_threshold({"moveThreshold": move_threshold_pct})
    threshold = cfg.translated_params["move_threshold_pct"]
    if len(prices) < 2 or prices[0] == 0:
        return AgentAction(type=SportsActionType.WAIT, params={})
    pct_move = abs(prices[-1] - prices[0]) / prices[0] * 100.0
    if pct_move >= threshold:
        return AgentAction(
            type=SportsActionType.FOLLOW_MOMENTUM,
            params={"reason": f"baseline: {pct_move:.2f}pct move >= {threshold}pct threshold"},
        )
    return AgentAction(type=SportsActionType.WAIT, params={})


threshold_move.is_baseline = True  # type: ignore[attr-defined]


def seeded_random(prices: list[float], horizon_s: int, seed: int) -> AgentAction:
    """Coin-flip decision from a SEEDED `random.Random` instance — same seed, same decision."""
    rng = random.Random(seed)
    if not prices or rng.random() < 0.5:
        return AgentAction(type=SportsActionType.WAIT, params={})
    return AgentAction(
        type=SportsActionType.FOLLOW_MOMENTUM,
        params={"reason": "baseline: seeded random", "seed": seed},
    )


seeded_random.is_baseline = True  # type: ignore[attr-defined]

#: The four named baselines the S6 evaluation compares agents against — never alpha (see module docstring).
BASELINES: dict[str, Callable] = {
    "no_trade": no_trade,
    "favorite": favorite,
    "threshold_move": threshold_move,
    "seeded_random": seeded_random,
}
