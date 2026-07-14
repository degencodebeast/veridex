"""Pure-tier strategy core (MM-R4-B skeleton).

The deterministic decision function. The skeleton ``decide()`` is a COMPLETE trivial
implementation: it always emits a fixed ``HOLD`` and threads the prior state through
unchanged. Later tasks (E2-T3/E2-T4) replace this body with the real strategy.

Import whitelist (load-bearing): stdlib + pydantic + ``veridex.mm_strategy.contracts`` +
``veridex.runtime.evidence`` ONLY. No network, no I/O, no wall clock, no randomness.
"""

from __future__ import annotations

from veridex.mm_strategy.contracts import (
    StrategyDecision,
    StrategyObservation,
    StrategyState,
)

# The single fixed decision the skeleton ever emits.
_HOLD_DECISION = StrategyDecision()


def decide(
    observation: StrategyObservation,
    state: StrategyState,
    config: object,
) -> tuple[StrategyDecision, StrategyState]:
    """Return a fixed ``HOLD`` and pass the prior state through unchanged (pure, total).

    ``config`` is typed ``object`` on purpose: the pure-tier whitelist forbids ``core`` from
    importing ``veridex.mm_strategy.config``, and the skeleton reads nothing off it.
    """
    return _HOLD_DECISION, state
