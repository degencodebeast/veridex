"""On-chain venue trade prints and their JSONL loader (MM-R1.5, no-fill boundary).

A :class:`TradePrint` records a Polymarket ``OrderFilled`` event — a trade between
**other** venue participants, **never a Veridex fill**. It therefore carries only
market-observation fields (``ts, price, size, aggressor_side, condition_id,
token_id``) and deliberately has **no** ``fill_price`` / ``real_executable_edge_bps``
/ ``pnl`` / ``spread_capture`` field: any of those would imply the print was our own
execution, which it is not.

Prices are native probability / share prices in ``[0, 1]`` (matching the markout
math in :mod:`veridex.maker.markout`). A decimal-priced (``> 1``) trade file is
**rejected** at load time via :func:`assert_native_prob`, so a decimal-odds value
can never silently reach downstream math (the "Run-002 longshot-scale" bug class).
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator

from veridex.maker.markout import assert_native_prob

__all__ = ["AggressorSide", "TradePrint", "load_trade_prints"]


class AggressorSide(str, Enum):
    """Which side crossed the spread to initiate the venue trade."""

    BUY = "buy"
    SELL = "sell"


class TradePrint(BaseModel):
    """A single on-chain venue trade observation (never our own fill).

    Attributes:
        ts: Event timestamp (epoch units as emitted by the source).
        price: Native probability / share price in ``[0, 1]``.
        size: Traded size (shares).
        aggressor_side: Side that crossed the spread.
        condition_id: Polymarket condition (market) identifier.
        token_id: Outcome-token identifier.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ts: int
    price: float
    size: float
    aggressor_side: AggressorSide
    condition_id: str
    token_id: str

    @field_validator("price")
    @classmethod
    def _price_is_native_prob(cls, v: float) -> float:
        """Defense-in-depth: reject a non-``[0, 1]`` price at model construction.

        Note:
            A :class:`~veridex.maker.markout.MarkoutError` raised here surfaces to a
            direct ``TradePrint(...)`` caller as a pydantic ``ValidationError``
            (pydantic wraps ``ValueError`` subclasses). The actionable file-load
            error is produced by :func:`load_trade_prints`, which calls
            :func:`assert_native_prob` explicitly *before* construction.
        """
        return assert_native_prob(v, "price")


def load_trade_prints(path: str | Path) -> list[TradePrint]:
    """Load venue trade prints from a JSONL file (one trade object per line).

    Blank lines are skipped. For each row the price is bounds-checked via
    :func:`assert_native_prob` **before** constructing the model, so an
    out-of-range (decimal-odds) price raises :class:`MarkoutError` directly
    rather than a pydantic ``ValidationError``.

    Args:
        path: Path to the JSONL trade file.

    Returns:
        The parsed trade prints in file order.

    Raises:
        MarkoutError: If any row's ``price`` is outside ``[0, 1]``.
        FileNotFoundError: If ``path`` does not exist.
    """
    trades: list[TradePrint] = []
    with Path(path).open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            assert_native_prob(row["price"], "price")
            trades.append(TradePrint(**row))
    return trades
