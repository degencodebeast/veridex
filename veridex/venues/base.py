"""Venue adapter value types and Protocol contract.

Pure data layer: Pydantic v2 models and the :class:`VenueAdapter` Protocol.
No I/O, no network imports.  Import-safe anywhere — including offline tests.

The :class:`VenueAdapter` Protocol defines the four async I/O methods every
venue adapter must implement plus the synchronous :meth:`VenueAdapter.normalize_receipt`
bridge that converts a venue-specific :class:`OrderStatus` into the trust-path
:class:`~veridex.execution.models.ExecutionReceipt`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel

from veridex.execution.models import ExecutionReceipt, ExecutionStatus

# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


class QuoteLevel(BaseModel):
    """One level of book depth, in **native venue book units** (raw, audit/analysis only).

    ``native_price`` is the venue's own price unit (e.g. SX-style implied odds, Polymarket
    probability) — NOT decimal odds. Levels are raw book data, never edge inputs; the
    edge/policy path reads :attr:`Quote.price` (decimal odds), never a level.

    Attributes:
        native_price: Price at this level in the venue's native unit.
        size: Liquidity available at this level.
    """

    native_price: float
    size: float


class Quote(BaseModel):
    """A price/size snapshot for a market at a point in time (v2).

    PRICE-UNIT DOCTRINE: :attr:`price` is **decimal odds** — the executable cost-to-fill for
    :attr:`for_size`, NOT a midpoint — because decimal odds is the one unit the trust core
    consumes (``veridex.law.edge`` computes ``p*price-1``, the policy gate, the UI). Adapters
    own native<->decimal conversion at the venue boundary. :attr:`native_price` preserves the
    venue-native value the decimal derived from, for AUDIT only; :attr:`levels` carry NATIVE
    venue book units (raw depth, not edge inputs).

    Attributes:
        market_ref: Venue-specific market identifier (e.g. ``"OU|2.5|full"``).
        price: DECIMAL ODDS — executable cost-to-fill for ``for_size`` (not a midpoint).
        native_price: Venue-native price the decimal derived from (audit only); ``None`` if n/a.
        size: Liquidity available at/under :attr:`price`.
        for_size: Size the :attr:`price` was quoted to fill; ``None`` if unspecified.
        levels: Optional book depth in NATIVE venue units (raw, not edge inputs).
        ts: Unix timestamp (seconds) when the quote was captured.
    """

    market_ref: str
    price: float
    native_price: float | None = None
    size: float
    for_size: float | None = None
    levels: list[QuoteLevel] = []
    ts: int


class Order(BaseModel):
    """A bet order to be submitted to a venue (v2).

    PRICE-UNIT DOCTRINE: :attr:`price` is **decimal odds** (the Veridex lingua franca); the
    adapter converts it to the venue-native unit at submit time. GTC is BANNED for this lane —
    it is unrepresentable because :attr:`tif` is a ``Literal["FAK", "FOK"]``. :attr:`client_order_id`
    is REQUIRED as the idempotency / dedup identity.

    Attributes:
        market_ref: Venue-specific market identifier.
        side: Which side of the market (e.g. ``"over"``, ``"under"``).
        size: Stake or size to wager.
        price: DECIMAL ODDS target; the adapter converts to native at submit.
        venue: Venue slug (e.g. ``"sx_bet"``).
        tif: Time-in-force — ``"FAK"`` (fill-and-kill, default) or ``"FOK"`` (fill-or-kill).
            ``"GTC"`` is intentionally unrepresentable for this lane.
        client_order_id: Caller-supplied idempotency / dedup identity (REQUIRED).
    """

    market_ref: str
    side: str
    size: float
    price: float
    venue: str
    tif: Literal["FAK", "FOK"] = "FAK"
    client_order_id: str


class SubmitAck(BaseModel):
    """Acknowledgement from the venue for an order submission.

    Attributes:
        venue_order_id: Opaque order reference assigned by the venue.
        accepted: ``True`` if the venue accepted the order; ``False`` if rejected.
    """

    venue_order_id: str
    accepted: bool


class OrderStatus(BaseModel):
    """Current state of an order at the venue.

    PRICE-UNIT DOCTRINE: :attr:`price` is the **decimal odds** of the actual matched price;
    :attr:`native_price` preserves the venue-native matched price for AUDIT only.

    Attributes:
        venue_order_id: Opaque order reference from the venue.
        status: Venue-native status string (e.g. ``"filled"``, ``"partial"``,
            ``"rejected"``, ``"open"``, ``"cancelled"``).
        filled_size: Stake or size actually matched so far.
        price: DECIMAL ODDS of the actual matched price.
        native_price: Venue-native matched price (audit only); ``None`` if n/a.
    """

    venue_order_id: str
    status: str
    filled_size: float
    price: float
    native_price: float | None = None


class CancelAck(BaseModel):
    """Acknowledgement from the venue for a cancel request.

    Attributes:
        venue_order_id: Opaque order reference that was cancelled.
        cancelled: ``True`` if the order was successfully cancelled.
    """

    venue_order_id: str
    cancelled: bool


# ---------------------------------------------------------------------------
# Status mapping helper
# ---------------------------------------------------------------------------

# Map venue-native status strings to ExecutionStatus values.
# Unmapped strings fall back to UNRESOLVED. Note: transient "pending"/"open" are NOT mapped here
# because map_venue_status is consumed only at the receipt boundary (build_receipt), which is
# reached only AFTER poll_order_terminal resolves to a terminal status OR times out. A non-terminal
# status surviving to that boundary means we timed out without resolution, so UNRESOLVED is the
# honest label — never guess a fill. poll_order_terminal checks terminality via TERMINAL_STATUSES
# (native strings), NOT via this map, so transient states are polled through correctly regardless.
_VENUE_STATUS_MAP: dict[str, ExecutionStatus] = {
    "filled": ExecutionStatus.FILLED,
    "partial": ExecutionStatus.PARTIAL,
    "partial_fill": ExecutionStatus.PARTIAL,
    "rejected": ExecutionStatus.REJECTED,
    "cancelled": ExecutionStatus.CANCELLED,
    "canceled": ExecutionStatus.CANCELLED,
    "expired": ExecutionStatus.EXPIRED,
}

# Venue-native status strings that are terminal for polling purposes. poll_order_terminal stops
# once the observed status is one of these; anything else (e.g. "pending", "open") is transient.
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"filled", "partial", "rejected", "cancelled", "canceled", "expired"}
)


def map_venue_status(venue_status: str) -> ExecutionStatus:
    """Convert a venue-native status string to an :class:`ExecutionStatus`.

    Args:
        venue_status: The raw status string returned by the venue.

    Returns:
        The corresponding :class:`ExecutionStatus`; falls back to
        :attr:`ExecutionStatus.UNRESOLVED` for unknown strings.
    """
    return _VENUE_STATUS_MAP.get(venue_status.lower(), ExecutionStatus.UNRESOLVED)


async def poll_order_terminal(
    adapter: VenueAdapter,
    venue_order_id: str,
    *,
    timeout_s: float = 10.0,
    interval_s: float = 0.5,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> OrderStatus:
    """Poll ``adapter.get_order_status`` until the status is terminal OR the timeout elapses.

    A receipt must be built from a TERMINAL status (or an honest timeout), never a transient one.
    On timeout this returns the LAST observed :class:`OrderStatus` unchanged — it NEVER fabricates
    a fill. A non-terminal last status (e.g. ``"pending"``) maps to
    :attr:`~veridex.execution.models.ExecutionStatus.UNRESOLVED` at the receipt boundary.

    Terminality is judged by :data:`TERMINAL_STATUSES` (native strings), decoupled from
    :func:`map_venue_status`, so transient states are polled through regardless of their mapping.

    Elapsed time is accumulated from ``interval_s`` each loop (NOT a wall clock), so an injected
    no-op ``sleep`` makes the poll instant and deterministic in tests.

    Args:
        adapter: The venue adapter to query.
        venue_order_id: Opaque order reference returned by :meth:`VenueAdapter.submit_order`.
        timeout_s: Max accumulated interval time before giving up and returning the last status.
        interval_s: Delay between polls; also the increment used to accumulate elapsed time.
        sleep: Awaitable delay function; injectable so tests never really wait.

    Returns:
        The first terminal :class:`OrderStatus` observed, or — on timeout — the LAST observed
        (possibly non-terminal, i.e. UNRESOLVED-mapping) status.
    """
    last = await adapter.get_order_status(venue_order_id)
    elapsed = 0.0
    while last.status.lower() not in TERMINAL_STATUSES and elapsed < timeout_s:
        await sleep(interval_s)
        elapsed += interval_s
        last = await adapter.get_order_status(venue_order_id)
    return last


def build_receipt(
    execution_id: str,
    order: Order,
    status: OrderStatus,
    *,
    mode: str,
) -> ExecutionReceipt:
    """Build an :class:`ExecutionReceipt` from raw venue output.

    Pure module-level helper shared by all venue adapters so each
    ``normalize_receipt`` implementation stays a one-line delegation.
    Contains no credentials.

    Args:
        execution_id: Stable identifier shared with the parent
            :class:`~veridex.execution.models.ExecutionRecord`.
        order: The original order submitted to the venue.
        status: Current order status returned by the venue.
        mode: Execution mode label (``"dry_run"``, ``"paper"``,
            ``"live_guarded"``).

    Returns:
        An :class:`ExecutionReceipt` with no credentials attached.
    """
    return ExecutionReceipt(
        execution_id=execution_id,
        venue=order.venue,
        market_ref=order.market_ref,
        side=order.side,
        requested_size=order.size,
        filled_size=status.filled_size,
        price=status.price,
        status=map_venue_status(status.status),
        venue_order_id=status.venue_order_id,
        mode=mode,
    )


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class VenueAdapter(Protocol):
    """Structural protocol every venue adapter must satisfy.

    All four I/O methods are async; :meth:`normalize_receipt` is sync (pure
    mapping — no network).  HTTP client imports must be lazy inside the async
    methods so that ``import veridex.venues.<adapter>`` is offline-safe.
    """

    async def quote_market(self, market_ref: str) -> Quote:
        """Fetch a current price/size quote for *market_ref*.

        Args:
            market_ref: Venue-specific market identifier.

        Returns:
            A :class:`Quote` snapshot.
        """
        ...

    async def submit_order(self, order: Order) -> SubmitAck:
        """Submit *order* to the venue.

        Args:
            order: The order to submit.

        Returns:
            A :class:`SubmitAck` with the venue's order ID and acceptance flag.
        """
        ...

    async def get_order_status(self, venue_order_id: str) -> OrderStatus:
        """Query the current status of a previously submitted order.

        Args:
            venue_order_id: Opaque order reference returned by the venue.

        Returns:
            An :class:`OrderStatus` snapshot.
        """
        ...

    async def cancel_order(self, venue_order_id: str) -> CancelAck:
        """Request cancellation of an open order.

        Args:
            venue_order_id: Opaque order reference to cancel.

        Returns:
            A :class:`CancelAck` confirming whether the cancel succeeded.
        """
        ...

    def normalize_receipt(
        self,
        execution_id: str,
        order: Order,
        status: OrderStatus,
        *,
        mode: str,
    ) -> ExecutionReceipt:
        """Convert venue status into a trust-path :class:`ExecutionReceipt`.

        This is the bridge between the async shell (venues/) and the
        deterministic trust core.  It is pure/sync — no I/O.

        Args:
            execution_id: Stable identifier shared with the parent
                :class:`~veridex.execution.models.ExecutionRecord`.
            order: The original order that was submitted.
            status: Current :class:`OrderStatus` from the venue.
            mode: Execution mode label (``"dry_run"``, ``"paper"``,
                ``"live_guarded"``).

        Returns:
            An :class:`ExecutionReceipt` with no credentials attached.
        """
        ...
