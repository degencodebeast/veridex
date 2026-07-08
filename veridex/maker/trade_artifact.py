"""MM-R1.5 trade-artifact provenance layer — the HARD no-fill boundary.

A :class:`NormalizedTradeRow` is a decoded Polymarket ``OrderFilled`` event — a
trade between **other** venue participants, **never a Veridex fill**. It therefore
carries only market-observation fields plus chain-event identity (``block_number,
tx_hash, log_index``) and deliberately has **no** ``fill_price`` /
``real_executable_edge_bps`` / ``pnl`` / ``spread_capture`` field: any of those
would imply the row was our own execution, which it is not.

Prices are native probability / share prices in ``[0, 1]`` (matching the markout
math in :mod:`veridex.maker.markout`); a decimal-priced (``> 1``) row is rejected
at construction via :func:`~veridex.maker.markout.assert_native_prob`, so a
decimal-odds value can never silently reach downstream math.

:func:`recompute_artifact_hash` produces the trust-load-bearing artifact hash over
BOTH the economic fields AND the chain-event identity of every row, under a
deterministic sort; the canonical-dump helper is inlined here (NOT imported from
:mod:`veridex.runtime.evidence`) so the trade trust surface has no cross-module
dependency.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator

from veridex.maker.markout import assert_native_prob
from veridex.maker.trades import AggressorSide

__all__ = [
    "NormalizedTradeRow",
    "recompute_artifact_hash",
]


class NormalizedTradeRow(BaseModel):
    """A single decoded venue trade row with chain-event identity (never our fill).

    Attributes:
        ts: Event timestamp (epoch units as emitted by the source).
        price: Native probability / share price in ``[0, 1]``.
        size: Observed traded size (shares) — observational only, never
            exposure / fill-volume / PnL / rankable.
        aggressor_side: Side that crossed the spread.
        condition_id: Polymarket condition (market) identifier.
        token_id: Outcome-token identifier.
        block_number: Chain block number of the emitting log.
        tx_hash: Transaction hash of the emitting log.
        log_index: Log index within the transaction.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ts: int
    price: float
    size: float
    aggressor_side: AggressorSide
    condition_id: str
    token_id: str
    block_number: int
    tx_hash: str
    log_index: int

    @field_validator("price")
    @classmethod
    def _price_is_native_prob(cls, v: float) -> float:
        """Reject a non-``[0, 1]`` (decimal-odds) price at model construction."""
        return assert_native_prob(v, "price")

    def event_key(self) -> tuple[str, int]:
        """Return the chain-event identity key ``(tx_hash, log_index)``."""
        return (self.tx_hash, self.log_index)
