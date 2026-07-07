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

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, field_validator

from veridex.maker.markout import assert_native_prob

if TYPE_CHECKING:
    from veridex.maker.mapping import ResolvedMarketRecord

__all__ = [
    "AggressorSide",
    "TradePrint",
    "join_trades_to_fixture",
    "join_trades_to_fixture_with_accounting",
    "load_trade_prints",
]


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

    Malformed lines propagate their natural parse/validation errors (this loader
    does not catch or wrap them): a non-JSON line raises
    :class:`json.JSONDecodeError`, a row missing ``price`` raises ``KeyError`` (the
    explicit ``row["price"]`` bounds-check), and a row missing any other required
    field raises pydantic ``ValidationError`` at :class:`TradePrint` construction.

    Raises:
        MarkoutError: If any row's ``price`` is outside ``[0, 1]``.
        FileNotFoundError: If ``path`` does not exist.
        json.JSONDecodeError: If any non-blank line is not valid JSON.
        KeyError: If any row is missing the ``price`` key.
        ValidationError: If any row is missing another required field (or has a
            field of the wrong type).
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


def join_trades_to_fixture_with_accounting(
    trades: list[TradePrint],
    records: list["ResolvedMarketRecord"],
    fixture_id: int,
) -> tuple[dict[str, list[TradePrint]], int]:
    """Join trade prints to a fixture's markets via the pinned mapping records.

    The join key ``(condition_id, token_id)`` is taken **only** from the committed
    mapping records (never a live lookup), so a trade is grouped under a market
    ``market_ref`` iff a record for this ``fixture_id`` pins that exact pair.

    Every trade is fully accounted (HB-10, no silent drops): each trade is either
    grouped into exactly one ``market_ref`` bucket **xor** counted as ``unmatched``.

    Args:
        trades: Venue trade prints to join.
        records: The pinned mapping records (any fixtures); only those whose
            ``fixture_id`` equals ``fixture_id`` contribute join keys.
        fixture_id: The fixture whose markets we are joining against.

    Returns:
        A ``(joined, unmatched)`` tuple where ``joined`` maps each matched
        ``market_ref`` to its list of trades (file order preserved) and
        ``unmatched`` counts trades matching no record for this fixture.
    """
    index: dict[tuple[str, str], str] = {
        (record.condition_id, record.token_id): record.market_ref
        for record in records
        if record.fixture_id == fixture_id
    }

    joined: dict[str, list[TradePrint]] = {}
    unmatched = 0
    for trade in trades:
        market_ref = index.get((trade.condition_id, trade.token_id))
        if market_ref is None:
            unmatched += 1
            continue
        joined.setdefault(market_ref, []).append(trade)
    return joined, unmatched


def join_trades_to_fixture(
    trades: list[TradePrint],
    records: list["ResolvedMarketRecord"],
    fixture_id: int,
) -> dict[str, list[TradePrint]]:
    """Join trade prints to a fixture's markets, returning only the grouped dict.

    Thin wrapper over :func:`join_trades_to_fixture_with_accounting` that drops the
    unmatched count. See that function for the join-key and accounting semantics.

    Args:
        trades: Venue trade prints to join.
        records: The pinned mapping records providing the join keys.
        fixture_id: The fixture whose markets we are joining against.

    Returns:
        A mapping of each matched ``market_ref`` to its list of trades.
    """
    joined, _ = join_trades_to_fixture_with_accounting(trades, records, fixture_id)
    return joined
