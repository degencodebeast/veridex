"""Venue adapter value types and Protocol contract.

Pure data layer: Pydantic v2 models and the :class:`VenueAdapter` Protocol.
No I/O, no network imports.  Import-safe anywhere — including offline tests.

The :class:`VenueAdapter` Protocol defines the four async I/O methods every
venue adapter must implement plus the synchronous :meth:`VenueAdapter.normalize_receipt`
bridge that converts a venue-specific :class:`OrderStatus` into the trust-path
:class:`~veridex.execution.models.ExecutionReceipt`.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from veridex.execution.models import ExecutionReceipt, ExecutionStatus

# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


class Quote(BaseModel):
    """A price/size snapshot for a market at a point in time.

    Attributes:
        market_ref: Venue-specific market identifier (e.g. ``"OU|2.5|full"``).
        price: Decimal odds or price offered.
        size: Available liquidity at this price.
        ts: Unix timestamp (seconds) when the quote was captured.
    """

    market_ref: str
    price: float
    size: float
    ts: int


class Order(BaseModel):
    """A bet order to be submitted to a venue.

    Attributes:
        market_ref: Venue-specific market identifier.
        side: Which side of the market (e.g. ``"over"``, ``"under"``).
        size: Stake or size to wager.
        price: Target price / odds.
        venue: Venue slug (e.g. ``"sx_bet"``).
    """

    market_ref: str
    side: str
    size: float
    price: float
    venue: str


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

    Attributes:
        venue_order_id: Opaque order reference from the venue.
        status: Venue-native status string (e.g. ``"filled"``, ``"partial"``,
            ``"rejected"``, ``"open"``, ``"cancelled"``).
        filled_size: Stake or size actually matched so far.
        price: Price / odds at which the fill occurred.
    """

    venue_order_id: str
    status: str
    filled_size: float
    price: float


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
# Unmapped strings fall back to UNRESOLVED.
_VENUE_STATUS_MAP: dict[str, ExecutionStatus] = {
    "filled": ExecutionStatus.FILLED,
    "partial": ExecutionStatus.PARTIAL,
    "partial_fill": ExecutionStatus.PARTIAL,
    "rejected": ExecutionStatus.REJECTED,
    "cancelled": ExecutionStatus.CANCELLED,
    "canceled": ExecutionStatus.CANCELLED,
    "open": ExecutionStatus.ACCEPTED,
    "pending": ExecutionStatus.SUBMITTED,
    "expired": ExecutionStatus.EXPIRED,
}


def map_venue_status(venue_status: str) -> ExecutionStatus:
    """Convert a venue-native status string to an :class:`ExecutionStatus`.

    Args:
        venue_status: The raw status string returned by the venue.

    Returns:
        The corresponding :class:`ExecutionStatus`; falls back to
        :attr:`ExecutionStatus.UNRESOLVED` for unknown strings.
    """
    return _VENUE_STATUS_MAP.get(venue_status.lower(), ExecutionStatus.UNRESOLVED)


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
