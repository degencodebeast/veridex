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

WRITE PATH (AC-2D-203, REQ-2D-403, AC-2D-405): Polymarket CLOB is MAINNET real money, so writes
fail closed. :meth:`get_order_status` needs ``settings.polymarket_write_enabled`` (default
``False``); a real :meth:`submit_order` / :meth:`cancel_order` additionally needs ``dry_run=False``
(the safe default is ``True``) and an injected ``write_client``. When armed, ``submit_order``
converts DECIMAL ODDS to a NATIVE tick-rounded share price so the wire NEVER carries decimal odds
(§4.3), and ``get_order_status`` reports the REAL matched fill from ``get_order`` — never the
request (SEC-004). The two-phase precondition gate lives in
:mod:`veridex.venues.polymarket_preflight`; the 1-share operator smoke is ``scripts/polymarket_smoke.py``.
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
    from veridex.dust_execution.resting_order import RestingOrder

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


class WriteClient(Protocol):
    """Structural protocol for the CLOB-shaped WRITE client the adapter submits/cancels through.

    Matches the vendored ``Polymarket`` write surface (``limit_order`` / ``get_order`` /
    ``cancel_all_orders``). Tests inject a fake that CAPTURES the payload — no network, no signing.
    The live path (operator, real money) injects the vendored client after ``init_client``.
    """

    async def limit_order(
        self,
        ticker: str,
        amount: float,
        price: float,
        tif: str = ...,
        round_price: bool = ...,
        tick_size: str | None = ...,
    ) -> dict[str, Any]:
        """Sign+POST an order; ``price`` is the NATIVE share price (tick units), ``amount`` signed."""
        ...

    async def get_order(self, order_id: str) -> dict[str, Any]:
        """Return the raw order record (carries ``size_matched`` and the matched native ``price``)."""
        ...

    # -- ADDITIVE resting-maker WRITE surface (E3-T3, REQ-016, §6 group 16) ---------------------
    # DISTINCT from the taker FAK/FOK ``limit_order`` path: a resting order (GTC/GTD, post-only) RESTS
    # on the book and later appears in ``get_orders``. Adding it leaves the sealed FAK/FOK write
    # behavior unchanged. Mirrors :class:`~veridex.venues.base.RestingOrderVenue`.
    async def submit_resting_order(
        self,
        *,
        token_id: str,
        amount: float,
        native_price: float,
        order_type: str,
        post_only: bool,
        expiration: int,
        tick_size: str | None = ...,
    ) -> dict[str, Any]:
        """Rest a GTC/GTD post-only order (§6 wire); ``native_price`` is the NATIVE tick-unit price."""
        ...

    async def cancel_all_orders(self) -> dict[str, Any]:
        """Cancel resting orders (FAK orders never rest, so this is a defensive cleanup)."""
        ...

    # -- ADDITIVE single-order cancel (E3-T4, REQ-007/008, §6 group 4) --------------------------
    # NET-NEW (G5): the vendored V1 client has ONLY cancel-all; the single-order authenticated
    # ``DELETE /order`` (``{"orderID": ...}`` -> ``canceled``/``not_canceled``, E3-T0 §4) is added
    # here. PHYSICALLY DISTINCT from ``cancel_all_orders`` (the sweep): it cancels ONE named order.
    async def cancel_single_order(self, order_id: str) -> dict[str, Any]:
        """Cancel ONE named order via authenticated ``DELETE /order`` (``{"orderID": ...}``) →
        ``{"canceled": [...], "not_canceled": {...}}`` (E3-T0 §4). NOT a ``/cancel`` route."""
        ...

    # -- ADDITIVE read / reconciliation surface (E3-T2, IDM-005/DAT-004) -----------------------
    # These expose the vendored CLOB reads upward for E4 own-fill reconciliation + fee lookup. They
    # are read-only and non-fund-touching; adding them does not change the sealed write behavior.

    async def get_orders(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Return the paginated list of open orders (E3-T0 §5; vendored ``get_orders(**kwargs)``)."""
        ...

    async def get_market(self, condition_id: str) -> dict[str, Any]:
        """Return per-market info incl. the ``fd`` fee descriptor (E3-T0 §8; vendored ``get_market``)."""
        ...

    async def get_fill_history(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Return own-fill / trade history (E3-T0 §3 ``get_trades`` shape). NET-NEW surface (G9)."""
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
# Write-path reconciliation (pure, offline): venue response -> Veridex value types
# ---------------------------------------------------------------------------

# Polymarket order-status strings (from ``get_order``) that are terminal WITHOUT any fill.
_POLY_DEAD_STATUSES: frozenset[str] = frozenset({"canceled", "cancelled"})
_POLY_UNFILLED_STATUSES: frozenset[str] = frozenset({"unmatched", "rejected", "matched"})
# Statuses that are still live/resting (non-terminal); a FAK order should never rest, but we keep
# these transient so a stray poll degrades to a non-fill rather than a fabricated fill.
_POLY_LIVE_STATUSES: frozenset[str] = frozenset({"live", "delayed", "open", "pending"})


def _submit_ack_from_response(response: Any) -> SubmitAck:
    """Map a vendored ``post_order`` response into a :class:`~veridex.venues.base.SubmitAck`.

    Tolerant of the CLOB response key variants; ``accepted`` follows the venue's ``success`` flag
    (defaulting to "accepted if an order id came back"). Never fabricates an id.
    """
    if not isinstance(response, dict):
        return SubmitAck(venue_order_id="", accepted=False)
    order_id = (
        response.get("orderID")
        or response.get("orderId")
        or response.get("orderHash")
        or response.get("id")
        or ""
    )
    accepted = bool(response.get("success", bool(order_id)))
    return SubmitAck(venue_order_id=str(order_id), accepted=accepted)


def _reconcile_status(raw_status: str, size_matched: float, original_size: float) -> str:
    """Reconcile a venue-native status from the MATCHED SIZE first — never trust a label over the number.

    The honest fill (SEC-004) is the size the book actually matched, so a positive ``size_matched``
    is a fill (``filled`` when it reaches the original size, else ``partial``) regardless of the
    venue's status string. Only when nothing matched do we fall back to the label to distinguish a
    killed/rejected FAK from a (transient) resting order.
    """
    if size_matched > 0.0:
        if original_size > 0.0 and size_matched + 1e-9 >= original_size:
            return "filled"
        return "partial"
    if raw_status in _POLY_LIVE_STATUSES:
        return "open"  # non-terminal: poll_order_terminal keeps polling / times out to UNRESOLVED
    if raw_status in _POLY_DEAD_STATUSES:
        return "cancelled"
    if raw_status in _POLY_UNFILLED_STATUSES:
        return "rejected"
    return raw_status or "unresolved"


def _order_status_from_raw(venue_order_id: str, raw: Any) -> OrderStatus:
    """Build an honest :class:`~veridex.venues.base.OrderStatus` from a vendored ``get_order`` record.

    Reads the REAL matched fill: ``filled_size`` is ``size_matched`` and ``native_price`` is the
    matched native share price ``q`` (audit); ``price`` is its decimal inverse. The request size is
    NEVER used — the receipt reflects only what the venue matched.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"get_order returned a non-dict record: {raw!r}")
    size_matched = float(raw.get("size_matched", 0.0) or 0.0)
    original_size = float(raw.get("original_size", 0.0) or 0.0)

    raw_price = raw.get("price")
    native_price: float | None = None
    price = 0.0
    if raw_price is not None and raw_price != "":
        native_price = float(raw_price)
        price = native_to_decimal(native_price) if native_price > 0.0 else 0.0

    status = _reconcile_status(str(raw.get("status", "")).strip().lower(), size_matched, original_size)
    return OrderStatus(
        venue_order_id=venue_order_id,
        status=status,
        filled_size=size_matched,
        price=price,
        native_price=native_price,
    )


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
        _for_size: Default shares the quote's cost-to-fill is computed for (overridable per call).
        _venue: Venue slug for receipts.
        _settings: Optional injected settings; ``None`` resolves lazily via ``get_settings``.
        _write_client: Injected CLOB-shaped WRITE client; ``None`` until the write path is wired.
        _dry_run: Safety default — when true, ``submit_order`` / ``cancel_order`` NEVER touch the
            wire (a real submit needs write-enabled AND ``dry_run=False``).
    """

    # DISPLAY-HONESTY marker (REQ-2D-701 gate 4): declares that this adapter's ``quote_market`` is a
    # GENUINE real-venue quote (depth-aware, from the real CLOB book). The execution lane keys the
    # POLICY_RESULT ``real_venue_quote`` flag on this — so the flagship edge renders ONLY on a real
    # venue quote. Fail-closed: Fake / SX-skeleton adapters do not set it, so they stay False.
    PROVIDES_REAL_VENUE_QUOTE: bool = True

    def __init__(
        self,
        resolved: ResolvedMarket,
        book_client: BookClient,
        *,
        side: str = "yes",
        for_size: float = 100.0,
        venue: str = "polymarket",
        settings: Settings | None = None,
        write_client: WriteClient | None = None,
        dry_run: bool = True,
    ) -> None:
        """Initialise the adapter.

        Args:
            resolved: Resolved market with token IDs and tick size.
            book_client: Injected CLOB-shaped book client (``async get_book(token_id)``).
            side: Side to price (``"yes"``/``"over"``/``"home"`` or ``"no"``/``"under"``/``"away"``).
            for_size: Default shares the quote's cost-to-fill is computed for.
            venue: Venue slug used on execution receipts.
            settings: Optional settings for the write gate; ``None`` → lazy ``get_settings()``.
            write_client: Injected CLOB-shaped write client (``limit_order`` / ``get_order`` /
                ``cancel_all_orders``); required to arm the live write path.
            dry_run: When ``True`` (the SAFE default) the write methods never reach the wire.
        """
        self._resolved = resolved
        self._book_client = book_client
        self._side = side
        self._for_size = for_size
        self._venue = venue
        self._settings = settings
        self._write_client = write_client
        self._dry_run = dry_run

    # -- read path ----------------------------------------------------------

    async def quote_market(self, market_ref: str, for_size: float | None = None) -> Quote:
        """Fetch a depth-aware DECIMAL-ODDS quote for the configured side.

        Fetches the book for the side's token, walks the ask ladder to compute the size-weighted
        average NATIVE fill price for ``for_size`` shares, and converts that ONCE to decimal odds.
        An empty/one-sided/unfillable book degrades honestly (``size=0``, non-executable ``price``,
        ``native_price=None``) — never a fabricated or midpoint price, never an ``IndexError``.

        QUOTE-SIZE COUPLING (gate B): pass ``for_size`` to price the depth-aware cost-to-fill for the
        SIZE the order will actually submit, so slippage / ``executable_edge_bps`` are evaluated on
        the right depth. When ``None`` the adapter's default ``for_size`` is used.

        Args:
            market_ref: Venue-specific market identifier (carried onto the returned quote).
            for_size: Shares to price the cost-to-fill for; ``None`` → the adapter's default.

        Returns:
            A v2 :class:`~veridex.venues.base.Quote`: ``price`` decimal odds, ``native_price`` the
            native ``avg_q`` (audit), ``levels`` in NATIVE units, ``size`` the fillable liquidity.
        """
        effective_for_size = self._for_size if for_size is None else for_size
        token_id = side_to_token(self._resolved, self._side)
        book = await self._book_client.get_book(token_id)
        ts = _book_ts_seconds(book.get("timestamp") if isinstance(book, dict) else None)

        asks_raw = book.get("asks") if isinstance(book, dict) else None
        ladder = self._sorted_ask_ladder(_parse_levels(asks_raw))
        levels = [QuoteLevel(native_price=price, size=size) for price, size in ladder]

        fill = _fill_to_size(ladder, effective_for_size)
        if fill is None:
            # Honest degrade: no executable price. price=0.0 is the "no price" sentinel the edge law
            # (veridex.law.edge) already treats as no-edge; native_price stays None (nothing to audit).
            return Quote(
                market_ref=market_ref,
                price=0.0,
                native_price=None,
                size=0.0,
                for_size=effective_for_size,
                levels=levels,
                ts=ts,
            )

        avg_q, filled = fill
        return Quote(
            market_ref=market_ref,
            price=native_to_decimal(avg_q),  # decimal odds — the ONE conversion
            native_price=avg_q,  # native q — audit only
            size=filled,
            for_size=effective_for_size,
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

    def _require_write_client(self) -> WriteClient:
        """Return the injected write client or fail closed if the live path is not wired."""
        if self._write_client is None:
            raise PolymarketWriteDisabled(
                "Polymarket write client not injected: cannot reach the live CLOB write path"
            )
        return self._write_client

    def _require_armed(self, action: str) -> WriteClient:
        """Gate a real-money action: write-enabled AND not DRY_RUN AND a write client present.

        Returns the write client only when every safety condition holds; otherwise raises
        :class:`PolymarketWriteDisabled` WITHOUT touching the wire. ``dry_run`` is the safe default,
        so an armed real submit needs ``polymarket_write_enabled`` true AND ``dry_run=False``.
        """
        self._require_write_enabled()
        if self._dry_run:
            raise PolymarketWriteDisabled(
                f"Polymarket {action} refused: DRY_RUN active (the safe default). "
                "Arm a real order with polymarket_write_enabled=true AND dry_run=False."
            )
        return self._require_write_client()

    async def submit_order(self, order: Order) -> SubmitAck:
        """Submit a FAK order — converts DECIMAL ODDS to a NATIVE tick-rounded share price on the wire.

        Fails closed by default (see :meth:`_require_armed`): a real submit needs
        ``polymarket_write_enabled`` true AND ``dry_run=False``. The wire carries the NATIVE price
        ``round_to_tick(1/order.price)`` — a decimal-odds value NEVER reaches the venue (§4.3).

        Args:
            order: The order to submit; ``order.price`` is DECIMAL ODDS, ``order.size`` the shares.

        Returns:
            A :class:`~veridex.venues.base.SubmitAck` parsed from the venue response.

        Raises:
            PolymarketWriteDisabled: Unless armed (write-enabled AND not DRY_RUN AND client present).
        """
        client = self._require_armed("submit")
        token_id = side_to_token(self._resolved, order.side)
        # DECIMAL ODDS -> native share price q -> tick-rounded to the market's tick. NATIVE on the wire.
        native_price = round_to_tick(decimal_to_native(order.price), self._resolved.tick_size)
        # Defense-in-depth (real money): a valid share price is strictly inside (0, 1). Fail CLOSED
        # BEFORE the wire so a pathological decimal price (e.g. odds <= 1) can never reach a mainnet
        # order — the vendored client would also reject it, but we never rely on the wire to catch it.
        if not 0.0 < native_price < 1.0:
            raise PolymarketWriteDisabled(
                f"refusing to submit: native price {native_price!r} outside (0, 1) for decimal odds "
                f"{order.price!r} at tick {self._resolved.tick_size!r} — fail-closed on the money path"
            )
        response = await client.limit_order(
            ticker=token_id,
            amount=order.size,  # positive => BUY the side's token
            price=native_price,  # NATIVE tick-rounded price — never decimal odds
            tif=order.tif,  # FAK (fill-and-kill) by default; GTC is unrepresentable for this lane
            round_price=False,  # already tick-rounded here; the client must not re-round
            tick_size=str(self._resolved.tick_size),
        )
        return _submit_ack_from_response(response)

    async def get_order_status(self, venue_order_id: str) -> OrderStatus:
        """Query an order's HONEST fill — reads the REAL matched size/price from ``get_order``.

        The receipt reflects only what the venue matched (SEC-004): ``filled_size`` is the venue's
        ``size_matched`` and ``native_price`` the matched native ``q`` (``price`` its decimal
        inverse) — the request size is NEVER echoed as a fill. Gated behind the write flag (in
        read-only mode there are no live orders to query).

        Args:
            venue_order_id: Opaque order reference to query.

        Returns:
            An honest :class:`~veridex.venues.base.OrderStatus` built from the matched fill.

        Raises:
            PolymarketWriteDisabled: Unless ``settings.polymarket_write_enabled`` is true.
        """
        self._require_write_enabled()
        client = self._require_write_client()
        raw = await client.get_order(venue_order_id)
        return _order_status_from_raw(venue_order_id, raw)

    async def cancel_order(self, venue_order_id: str) -> CancelAck:
        """The cancel-all SWEEP (``cancel_all_orders``) — gated identically to a real submit.

        This is EXPLICITLY the cancel-all primitive, NOT a single-order cancel: it fires the
        vendored ``cancel_all_orders`` sweep (``DELETE /cancel-all``) which removes EVERY resting
        order. FAK orders are fill-and-kill (they never rest), so a post-submit cancel is a defensive
        cleanup. The cancel-all lifecycle is modelled by the cause-only
        :class:`~veridex.dust_execution.contracts.CancelAllTriggeredEvent` /
        :class:`~veridex.dust_execution.contracts.CancelAllAck` (emitted by the sealed
        ``SafetyController`` primitive) which carry only a ``trigger_cause`` + swept count and NEVER a
        single order id (SAF-003). ``venue_order_id`` is echoed on the returned ack ONLY to satisfy
        the sealed :class:`~veridex.venues.base.VenueAdapter` contract; it is NOT a claim that this
        one order (and only it) was cancelled — the sweep is all-or-nothing.

        To cancel EXACTLY one named order, use :meth:`cancel_single_order` (the ``DELETE /order``
        route). ``cancel_replace`` is modelled as cancel-all-then-repost until ``DELETE /order``
        passes the E3-T5/REQ-017 gate — NO atomic replace is implemented here.

        Args:
            venue_order_id: Opaque order reference (echoed on the ack; see caveat above).

        Returns:
            A :class:`~veridex.venues.base.CancelAck`.

        Raises:
            PolymarketWriteDisabled: Unless armed (write-enabled AND not DRY_RUN AND client present).
        """
        client = self._require_armed("cancel")
        response = await client.cancel_all_orders()
        cancelled = bool(response.get("success", True)) if isinstance(response, dict) else False
        return CancelAck(venue_order_id=venue_order_id, cancelled=cancelled)

    async def cancel_single_order(self, venue_order_id: str) -> dict[str, Any]:
        """Cancel EXACTLY one named order via authenticated ``DELETE /order`` (E3-T4, REQ-007/008).

        PHYSICALLY DISTINCT from :meth:`cancel_order` (the cancel-all SWEEP): this hits the REAL
        ``DELETE /order`` route with body ``{"orderID": ...}`` (E3-T0 §4, CONFIRMED against
        ``py-clob-client-v2`` — NOT a nonexistent ``/cancel`` route) for the ONE named order and
        returns the venue ``{"canceled": [...], "not_canceled": {...}}`` shape verbatim
        (``not_canceled`` maps ``orderId -> reason``; an unknown/already-gone id is reported there,
        never as a phantom cancel — fail-closed). NET-NEW (G5): the vendored V1 client exposes only
        ``cancel_all_orders``; the single-order ``DELETE /order`` is added for R4-A/CLOB-V2.

        NON-TERMINAL ACK: a ``canceled`` ACK does NOT mark the order definitively absent — only E4
        reconciliation against complete venue truth may resolve that. The caller must not treat the
        bare cancel ACK as terminal.

        Gated identically to a real submit (armed = write-enabled AND not DRY_RUN AND client present):
        cancelling a live order is fund-touching.

        Args:
            venue_order_id: The venue order hash/id to cancel (the ``{"orderID": ...}`` body).

        Returns:
            The venue ``{"canceled": [...], "not_canceled": {...}}`` response verbatim.

        Raises:
            PolymarketWriteDisabled: Unless armed (write-enabled AND not DRY_RUN AND client present).
        """
        client = self._require_armed("cancel_single_order")
        return await client.cancel_single_order(venue_order_id)

    async def submit_resting_order(self, resting_order: RestingOrder) -> SubmitAck:
        """Submit a DISTINCT :class:`~veridex.dust_execution.resting_order.RestingOrder` — GTC/GTD,
        post-only, RESTS on the book (E3-T3, REQ-016, §6 group 16).

        Physically distinct from :meth:`submit_order` (which only takes the FAK/FOK taker
        :class:`~veridex.venues.base.Order` and NEVER rests): the parameter type is a ``RestingOrder``,
        so a taker order can never travel this path and a resting order can never travel the taker
        path. Armed identically to a real submit (:meth:`_require_armed`: ``polymarket_write_enabled``
        true AND ``dry_run=False`` AND a write client present), then delegates the §6 resting-maker
        wire kwargs to the venue client. ``resting_order.native_price`` is ALREADY a native tick-unit
        share price (validated on the contract), so no decimal-odds value ever reaches the wire (§4.3).

        Args:
            resting_order: The GTC/GTD post-only resting order to rest on the book.

        Returns:
            A :class:`~veridex.venues.base.SubmitAck` parsed from the venue response.

        Raises:
            PolymarketWriteDisabled: Unless armed (write-enabled AND not DRY_RUN AND client present).
        """
        client = self._require_armed("submit_resting_order")
        # Defense-in-depth (real money): a valid native share price is strictly inside (0, 1). The
        # RestingOrder contract already enforces this, but the money path never trusts the wire to
        # catch a pathological price — fail CLOSED before any I/O.
        if not 0.0 < resting_order.native_price < 1.0:
            raise PolymarketWriteDisabled(
                f"refusing to rest order: native price {resting_order.native_price!r} outside (0, 1) "
                "— fail-closed on the money path"
            )
        response = await client.submit_resting_order(**resting_order.to_wire_kwargs())
        return _submit_ack_from_response(response)

    # -- read / reconciliation surface (ADDITIVE, E3-T2, IDM-005/DAT-004) ----------------------
    #
    # Exposes the vendored CLOB reads upward for E4 own-fill reconciliation + fee lookup. These are
    # RAW passthroughs (the venue's own record dicts, E3-T0 §3/§5/§8 shapes) — no reconciliation
    # logic here; E4 owns that. Own-order/own-fill reads need L2 auth (SEC-004 honest reconciliation),
    # so they are gated behind ``polymarket_write_enabled`` AND an injected write client (like
    # :meth:`get_order_status`). They never touch money, so — unlike a real submit — they do NOT
    # require ``dry_run=False``. The write path stays sealed and unchanged.

    async def get_orders(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Return the paginated list of open orders (E3-T0 §5), for E4 reconciliation.

        Args:
            **kwargs: Filter/pagination params passed through to the vendored
                ``get_orders(**kwargs)`` (e.g. ``market``, ``asset_id``).

        Returns:
            The raw open-order records (paginated, flattened) as returned by the venue.

        Raises:
            PolymarketWriteDisabled: Unless write-enabled AND a write client is injected.
        """
        self._require_write_enabled()
        client = self._require_write_client()
        return await client.get_orders(**kwargs)

    async def get_order(self, order_id: str) -> dict[str, Any]:
        """Return one raw open-order record by id/hash (E3-T0 §5), for E4 reconciliation.

        This is the RAW record (unlike :meth:`get_order_status`, which reconciles it into an honest
        :class:`~veridex.venues.base.OrderStatus`). E4 consumes the raw shape directly.

        Args:
            order_id: The venue order id / EIP-712 order hash.

        Returns:
            The raw open-order record dict.

        Raises:
            PolymarketWriteDisabled: Unless write-enabled AND a write client is injected.
        """
        self._require_write_enabled()
        client = self._require_write_client()
        return await client.get_order(order_id)

    async def get_market(self, condition_id: str) -> dict[str, Any]:
        """Return per-market info incl. the ``fd`` fee descriptor (E3-T0 §8).

        Fee/market info is a PUBLIC data endpoint (auth: none), so — unlike the own-order reads — it
        does NOT require ``polymarket_write_enabled``; it only needs an injected client to reach the
        wire (fail-closed when the live path is not wired). This is the fee source
        :func:`veridex.dust_execution.feesnapshot.pin_fee_snapshot` reads to pin a hashed snapshot.

        Args:
            condition_id: The market condition id.

        Returns:
            The raw market-info record dict (carries ``fd`` = fee descriptor).

        Raises:
            PolymarketWriteDisabled: When no write client is injected (live path not wired).
        """
        client = self._require_write_client()
        return await client.get_market(condition_id)

    async def get_fill_history(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Return own-fill / trade history (E3-T0 §3 ``get_trades`` shape), for E4 reconciliation.

        NET-NEW surface (no single vendored endpoint — G9): it maps to the §3 ``get_trades`` wire in
        the live wiring. A trade's ``taker_order_id`` (or ``maker_orders[].order_id``) equals the
        locally-computed EIP-712 order hash — the durable pre-submit join key E4 reconciles on (§3d).

        Args:
            **kwargs: Filter params passed through (e.g. ``market``, ``asset_id``, ``maker_address``,
                ``before``, ``after``) — the §3 ``TradeParams`` fields.

        Returns:
            The raw trade / fill records as returned by the venue.

        Raises:
            PolymarketWriteDisabled: Unless write-enabled AND a write client is injected.
        """
        self._require_write_enabled()
        client = self._require_write_client()
        return await client.get_fill_history(**kwargs)

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
