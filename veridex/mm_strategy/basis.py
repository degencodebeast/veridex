"""Pure-tier basis-signal seam (MM-R4-B skeleton).

A single pure pass-through helper standing in for the basis/edge computation later tasks
fill in. Import whitelist (load-bearing): stdlib + pydantic + ``veridex.mm_strategy.contracts``
+ ``veridex.runtime.evidence`` ONLY. The skeleton needs no imports at all — it is a pure,
deterministic function with no side effects.
"""

from __future__ import annotations


def passthrough(value: float) -> float:
    """Identity pass-through placeholder for the basis seam (E2 replaces this)."""
    return value
