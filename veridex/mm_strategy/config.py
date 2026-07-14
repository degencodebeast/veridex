"""Pure-tier strategy config (MM-R4-B skeleton).

A frozen ``StrategyConfig`` stub. Import whitelist (load-bearing): stdlib + pydantic +
``veridex.mm_strategy.contracts`` + ``veridex.runtime.evidence`` ONLY. The skeleton needs
only pydantic; later tasks widen the config surface without ever crossing that whitelist.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class StrategyConfig(BaseModel):
    """Frozen strategy configuration (skeleton). Immutable; unknown fields rejected."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_id: str = "mm-skeleton"
    enabled: bool = True
