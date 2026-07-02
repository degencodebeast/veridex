"""Polymarket venue adapter — READ path (REQ-2D-203/204, AC-2D-202/203/405, §4.3).

TRUST-ADJACENT: this adapter feeds the edge/execution path, so its one load-bearing job is the
PRICE-UNIT DOCTRINE (§4.3). Polymarket's order book is denominated in NATIVE share prices
``q ∈ (0, 1)`` — a probability-like cost for a share that pays $1. The Veridex trust core
(:mod:`veridex.law.edge`) computes ``p * price - 1`` and that ``price`` MUST be DECIMAL ODDS.
The conversion is a single inversion, ``decimal = 1 / q``, applied ONCE at this venue boundary:

  * :attr:`~veridex.venues.base.Quote.price` — DECIMAL ODDS (``1 / avg_q``), the depth-aware
    cost-to-fill for ``for_size`` shares (NOT the midpoint).
  * :attr:`~veridex.venues.base.Quote.native_price` — the native ``avg_q`` it derived from
    (AUDIT ONLY).
  * :attr:`~veridex.venues.base.QuoteLevel.native_price` — raw book prices ``q`` (NATIVE units).

A native ``q`` leaking into ``.price`` silently corrupts every downstream edge/slippage/policy
number, so that must never happen.

TWO CLIENTS (see :mod:`veridex.venues.polymarket_resolver`): resolution uses the Gamma client;
the ORDER BOOK uses the CLOB client. This adapter is constructed with a resolved market plus an
INJECTED book client (a duck type ``async def get_book(token_id) -> {"bids": [...], "asks": [...]}``
matching the vendored ``get_book`` shape). Tests inject a fake book client — no network.

OFFLINE-SAFE IMPORT (CON-010): the vendored CLOB ``LOB`` (and ``numpy``) are lazy-imported inside
the method that needs them, so ``import veridex.venues.polymarket`` never pulls the vendored
signing stack. The vendored ``LOB`` is consumed (not re-implemented) only to SORT the ladder; the
fill-to-size VWAP walk is done here over the sorted array, so the latent ``IndexError`` hazards in
``LOB.get_mid`` / ``LOB.get_cumulative_size`` (empty/one-sided books, over-sweep) are never hit.

WRITE PATH DISABLED (AC-2D-203): Polymarket CLOB is MAINNET real money. :meth:`submit_order`,
:meth:`cancel_order`, and :meth:`get_order_status` raise :class:`PolymarketWriteDisabled` unless
``settings.polymarket_write_enabled`` is explicitly true (default ``False``). T17 wires the live
write path behind the same gate.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Protocol

from veridex.execution.models import ExecutionReceipt
from veridex.venues.base import (
    CancelAck,
    Order,
    OrderStatus,
    Quote,
    QuoteLevel,
    SubmitAck,
    build_receipt,
)
from veridex.venues.polymarket_resolver import ResolvedMarket, side_to_token

if TYPE_CHECKING:
    from veridex.config import Settings

# Timestamps above this bound are Unix milliseconds (Polymarket CLOB) rather than seconds; used to
# normalise the book timestamp to the seconds unit :attr:`Quote.ts` documents. ~Sat Mar 2033 in ms.
_MS_EPOCH_BOUND: float = 1e11


class PolymarketWriteDisabled(Exception):
    """Raised when a write is attempted while the Polymarket write path is disabled.

    Polymarket CLOB is MAINNET real money, so the write path fails closed by default: it opens only
    when ``settings.polymarket_write_enabled`` is explicitly true (AC-2D-203).
    """


class BookClient(Protocol):
    """Structural protocol for the CLOB-shaped book client the adapter reads from.

    Matches the vendored ``Polymarket.get_book`` shape. Tests inject a fake returning a synthetic
    book; the live path (T17) injects the real CLOB client.
    """

    async def get_book(self, token_id: str) -> dict[str, Any]:
        """Return the raw order book ``{"bids": [...], "asks": [...], "timestamp": ...}``."""
        ...


# ---------------------------------------------------------------------------
# Native (share price q) <-> decimal odds conversion (consumed by T17 write path)
# ---------------------------------------------------------------------------


def native_to_decimal(q: float) -> float:
    """Convert a Polymarket native share price ``q`` (probability-like) to DECIMAL ODDS.

    Args:
        q: Native share price in ``(0, 1)``.

    Returns:
        Decimal odds ``1 / q``.

    Raises:
        ValueError: If ``q <= 0`` (no valid decimal odds; never fabricate a price).
    """
    if q <= 0.0:
        raise ValueError(f"native share price must be > 0, got {q!r}")
    return 1.0 / q


def decimal_to_native(price: float) -> float:
    """Convert DECIMAL ODDS to a Polymarket native share price ``q``.

    Args:
        price: Decimal odds (``> 1`` for a real market, but any positive value is invertible).

    Returns:
        The native share price ``1 / price``.

    Raises:
        ValueError: If ``price <= 0`` (no valid native price).
    """
    if price <= 0.0:
        raise ValueError(f"decimal odds must be > 0, got {price!r}")
    return 1.0 / price


def round_to_tick(q: float, tick: float) -> float:
    """Round a native share price ``q`` to the market's tick size.

    Args:
        q: Native share price to round.
        tick: The market's minimum price increment (``ResolvedMarket.tick_size``).

    Returns:
        ``q`` rounded to the nearest multiple of ``tick``.

    Raises:
        ValueError: If ``tick <= 0``.
    """
    if tick <= 0.0:
        raise ValueError(f"tick size must be > 0, got {tick!r}")
    return round(q / tick) * tick


# ---------------------------------------------------------------------------
# Book parsing / depth-aware fill (pure, offline)
# ---------------------------------------------------------------------------


def _parse_levels(raw_levels: Any) -> list[tuple[float, float]]:
    """Parse raw book levels into ``(price, size)`` float tuples.

    Tolerant of both shapes the CLOB book can present a level in: the canonical vendored dict
    ``{"price": ..., "size": ...}`` and a ``[price, size]`` pair. Malformed/zero-size levels are
    dropped rather than crashing (fail-safe read path).
    """
    levels: list[tuple[float, float]] = []
    for raw in raw_levels or []:
        try:
            if isinstance(raw, dict):
                price = float(raw["price"])
                size = float(raw["size"])
            else:
                price = float(raw[0])
                size = float(raw[1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if price > 0.0 and size > 0.0:
            levels.append((price, size))
    return levels


def _fill_to_size(
    ladder: list[tuple[float, float]], for_size: float
) -> tuple[float, float] | None:
    """Walk a best-first ``ladder`` accumulating fills up to ``for_size`` shares.

    Args:
        ladder: ``(price, size)`` levels sorted best-first (asks ascending by price).
        for_size: Target number of shares to fill.

    Returns:
        ``(avg_native_price, filled_size)`` where ``avg_native_price`` is the size-weighted average
        native fill price (the cost-to-fill, NOT a midpoint) and ``filled_size`` is what the book
        could actually supply (``<= for_size`` on a thin book). ``None`` when nothing is fillable
        (empty ladder or non-positive target) — the caller then degrades honestly.
    """
    if not ladder or for_size <= 0.0:
        return None
    remaining = for_size
    notional = 0.0
    filled = 0.0
    for price, size in ladder:
        take = size if size < remaining else remaining
        notional += price * take
        filled += take
        remaining -= take
        if remaining <= 1e-12:
            break
    if filled <= 0.0:
        return None
    return notional / filled, filled


def _book_ts_seconds(raw_ts: Any) -> int:
    """Normalise a raw book timestamp to Unix SECONDS (:attr:`Quote.ts` unit).

    Polymarket CLOB timestamps are Unix milliseconds; tests may pass seconds. Values above
    :data:`_MS_EPOCH_BOUND` are treated as milliseconds. Missing/unparseable → capture time now.
    """
    if raw_ts is None:
        return int(time.time())
    try:
        value = float(raw_ts)
    except (TypeError, ValueError):
        return int(time.time())
    return int(value / 1000.0) if value > _MS_EPOCH_BOUND else int(value)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class PolymarketAdapter:
    """Polymarket venue adapter — depth-aware decimal quotes; write path disabled by default.

    One adapter instance is bound to a resolved market, a side, and a target fill size, so
    :meth:`quote_market` (whose Protocol signature takes only ``market_ref``) has the side/token and
    ``for_size`` it needs to price the depth-aware cost-to-fill.

    Attributes:
        _resolved: The :class:`~veridex.venues.polymarket_resolver.ResolvedMarket` (token IDs, tick).
        _book_client: Injected CLOB-shaped book client (no network in tests).
        _side: Which side of the market to price (mapped to a token via ``side_to_token``).
        _for_size: Shares the quote's cost-to-fill is computed for.
        _venue: Venue slug for receipts.
        _settings: Optional injected settings; ``None`` resolves lazily via ``get_settings``.
    """

    def __init__(
        self,
        resolved: ResolvedMarket,
        book_client: BookClient,
        *,
        side: str = "yes",
        for_size: float = 100.0,
        venue: str = "polymarket",
        settings: Settings | None = None,
    ) -> None:
        """Initialise the adapter.

        Args:
            resolved: Resolved market with token IDs and tick size.
            book_client: Injected CLOB-shaped book client (``async get_book(token_id)``).
            side: Side to price (``"yes"``/``"over"``/``"home"`` or ``"no"``/``"under"``/``"away"``).
            for_size: Shares the quote's cost-to-fill is computed for.
            venue: Venue slug used on execution receipts.
            settings: Optional settings for the write gate; ``None`` → lazy ``get_settings()``.
        """
        self._resolved = resolved
        self._book_client = book_client
        self._side = side
        self._for_size = for_size
        self._venue = venue
        self._settings = settings

    # -- read path ----------------------------------------------------------

    async def quote_market(self, market_ref: str) -> Quote:
        """Fetch a depth-aware DECIMAL-ODDS quote for the configured side.

        Fetches the book for the side's token, walks the ask ladder to compute the size-weighted
        average NATIVE fill price for ``for_size`` shares, and converts that ONCE to decimal odds.
        An empty/one-sided/unfillable book degrades honestly (``size=0``, non-executable ``price``,
        ``native_price=None``) — never a fabricated or midpoint price, never an ``IndexError``.

        Args:
            market_ref: Venue-specific market identifier (carried onto the returned quote).

        Returns:
            A v2 :class:`~veridex.venues.base.Quote`: ``price`` decimal odds, ``native_price`` the
            native ``avg_q`` (audit), ``levels`` in NATIVE units, ``size`` the fillable liquidity.
        """
        token_id = side_to_token(self._resolved, self._side)
        book = await self._book_client.get_book(token_id)
        ts = _book_ts_seconds(book.get("timestamp") if isinstance(book, dict) else None)

        asks_raw = book.get("asks") if isinstance(book, dict) else None
        ladder = self._sorted_ask_ladder(_parse_levels(asks_raw))
        levels = [QuoteLevel(native_price=price, size=size) for price, size in ladder]

        fill = _fill_to_size(ladder, self._for_size)
        if fill is None:
            # Honest degrade: no executable price. price=0.0 is the "no price" sentinel the edge law
            # (veridex.law.edge) already treats as no-edge; native_price stays None (nothing to audit).
            return Quote(
                market_ref=market_ref,
                price=0.0,
                native_price=None,
                size=0.0,
                for_size=self._for_size,
                levels=levels,
                ts=ts,
            )

        avg_q, filled = fill
        return Quote(
            market_ref=market_ref,
            price=native_to_decimal(avg_q),  # decimal odds — the ONE conversion
            native_price=avg_q,  # native q — audit only
            size=filled,
            for_size=self._for_size,
            levels=levels,
            ts=ts,
        )

    @staticmethod
    def _sorted_ask_ladder(levels: list[tuple[float, float]]) -> list[tuple[float, float]]:
        """Sort ask levels best-first (ascending price) via the vendored ``LOB``.

        The vendored ``LOB`` (and ``numpy``) are lazy-imported here so importing this module stays
        offline-safe. Only ``LOB.update`` (snapshot sort) is consumed — the hazardous ``get_mid`` /
        ``get_cumulative_size`` are avoided; the empty ladder is short-circuited before any indexing.
        """
        if not levels:
            return []
        import numpy as np

        from veridex.venues._vendor.polymarket_clob.client import LOB

        asks = np.array(levels, dtype=np.float64)
        lob = LOB(depth=len(levels))
        lob.update(
            timestamp=0,
            bids=np.empty((0, 2), dtype=np.float64),
            asks=asks,
            is_snapshot=True,
        )
        return [(float(price), float(size)) for price, size in lob.get_asks()]

    # -- write path (DISABLED by default) -----------------------------------

    def _require_write_enabled(self) -> Settings:
        """Return settings if the write path is enabled, else raise :class:`PolymarketWriteDisabled`.

        Settings are resolved lazily (``get_settings()``) unless injected, keeping module import
        offline-safe and the offline suite credential-free.
        """
        settings = self._settings
        if settings is None:
            from veridex.config import get_settings  # lazy: keep import offline-safe

            settings = get_settings()
        if not settings.polymarket_write_enabled:
            raise PolymarketWriteDisabled(
                "Polymarket write path disabled (mainnet real money): "
                "set POLYMARKET_WRITE_ENABLED=true to enable"
            )
        return settings

    async def submit_order(self, order: Order) -> SubmitAck:
        """Submit an order — DISABLED by default.

        Raises:
            PolymarketWriteDisabled: Unless ``settings.polymarket_write_enabled`` is true. The live
                submit path is wired in T17 behind this same gate.
        """
        self._require_write_enabled()
        raise PolymarketWriteDisabled("Polymarket live submit path not yet wired (T17)")

    async def get_order_status(self, venue_order_id: str) -> OrderStatus:
        """Query an order's status — DISABLED by default.

        In read-only mode there are no live orders to query, so this fails closed behind the write
        gate. T17 wires the live status path.

        Raises:
            PolymarketWriteDisabled: Unless ``settings.polymarket_write_enabled`` is true.
        """
        self._require_write_enabled()
        raise PolymarketWriteDisabled("Polymarket live order-status path not yet wired (T17)")

    async def cancel_order(self, venue_order_id: str) -> CancelAck:
        """Cancel an order — DISABLED by default.

        Raises:
            PolymarketWriteDisabled: Unless ``settings.polymarket_write_enabled`` is true. The live
                cancel path is wired in T17 behind this same gate.
        """
        self._require_write_enabled()
        raise PolymarketWriteDisabled("Polymarket live cancel path not yet wired (T17)")

    def normalize_receipt(
        self,
        execution_id: str,
        order: Order,
        status: OrderStatus,
        *,
        mode: str,
    ) -> ExecutionReceipt:
        """Convert a venue :class:`~veridex.venues.base.OrderStatus` into an
        :class:`~veridex.execution.models.ExecutionReceipt`.

        Delegates to :func:`~veridex.venues.base.build_receipt` — pure, synchronous, no credentials.

        Args:
            execution_id: Stable identifier shared with the parent execution record.
            order: The original order that was submitted.
            status: Current order status from the venue.
            mode: Execution mode label (``"dry_run"``, ``"paper"``, ``"live_guarded"``).

        Returns:
            An :class:`~veridex.execution.models.ExecutionReceipt` with no credentials attached.
        """
        return build_receipt(execution_id, order, status, mode=mode)
