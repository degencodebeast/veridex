"""VenueAdapter v2 contract tests (REQ-2D-401/402, §4.3) — TDD.

The v2 contract is TRUST-CRITICAL: every Polymarket adapter (T12-14, T17) builds on it.
The single most load-bearing item is the PRICE-UNIT DOCTRINE — Veridex-side price fields
are ALWAYS decimal odds; ``native_price`` is audit-only and must NEVER leak into ``price``.

Covered here:
- ``Order`` REQUIRES ``client_order_id`` (idempotency identity) and defaults ``tif="FAK"``;
  ``tif="GTC"`` is unrepresentable (Literal validation error) — GTC is banned for this lane.
- A partial fill surfaces the MATCHED ``filled_size``/``price`` (not the requested) — AC-2D-401.
- ``poll_order_terminal`` on timeout returns the LAST observed status (maps to UNRESOLVED,
  never a fabricated fill) and uses the INJECTED ``sleep`` (instant test) — AC-2D-402.
- ``poll_order_terminal`` returns early once a status flips terminal.
- Price-unit sanity: a v2 ``Quote.price`` is decimal odds; ``native_price`` is the venue-native
  value it derived from; the Fake's decimal<->native relationship is explicit and documented.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from veridex.execution.models import ExecutionStatus
from veridex.law.edge import executable_edge_bps
from veridex.venues.base import (
    TERMINAL_STATUSES,
    Order,
    Quote,
    QuoteLevel,
    map_venue_status,
    poll_order_terminal,
)
from veridex.venues.sx_bet import (
    FakeVenueAdapter,
    _fake_decimal_to_native,
    _fake_native_to_decimal,
)

# ---------------------------------------------------------------------------
# Test helper: a recording, instant sleep so poll tests never really wait.
# ---------------------------------------------------------------------------


class _RecordingSleep:
    """Awaitable stand-in for ``asyncio.sleep`` that records durations, never waits."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.calls.append(delay)


# ---------------------------------------------------------------------------
# Order: client_order_id required, tif FAK default, GTC unrepresentable
# ---------------------------------------------------------------------------


def test_order_requires_client_order_id() -> None:
    """Constructing an Order without client_order_id is a pydantic validation error."""
    with pytest.raises(ValidationError):
        Order(market_ref="m", side="over", size=100.0, price=2.0, venue="sx_bet")


def test_order_tif_defaults_to_fak() -> None:
    """tif defaults to FAK (fill-and-kill) when not supplied."""
    o = Order(market_ref="m", side="over", size=100.0, price=2.0, venue="sx_bet", client_order_id="c1")
    assert o.tif == "FAK"
    assert o.client_order_id == "c1"


def test_order_tif_fok_allowed() -> None:
    """FOK is a permitted TIF value."""
    o = Order(market_ref="m", side="over", size=100.0, price=2.0, venue="sx_bet", client_order_id="c1", tif="FOK")
    assert o.tif == "FOK"


def test_order_tif_gtc_is_unrepresentable() -> None:
    """GTC is banned for this lane — the Literal excludes it, so it fails validation."""
    with pytest.raises(ValidationError):
        Order(
            market_ref="m",
            side="over",
            size=100.0,
            price=2.0,
            venue="sx_bet",
            client_order_id="c1",
            tif="GTC",  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# Partial fill surfaces MATCHED values, not requested (AC-2D-401, SEC-004)
# ---------------------------------------------------------------------------


async def test_partial_fill_reports_matched_not_requested() -> None:
    """A partial fill's status/receipt reflect the MATCHED size/price, not the request."""
    adapter = FakeVenueAdapter(fill=True, fill_size=40.0)
    order = Order(
        market_ref="OU|2.5|full", side="under", size=100.0, price=2.05, venue="sx_bet", client_order_id="c-partial"
    )
    ack = await adapter.submit_order(order)
    status = await adapter.get_order_status(ack.venue_order_id)

    assert status.status == "partial"
    assert status.filled_size == 40.0  # matched, not the requested 100.0
    assert map_venue_status(status.status) is ExecutionStatus.PARTIAL

    receipt = adapter.normalize_receipt("e-partial", order, status, mode="paper")
    assert receipt.status is ExecutionStatus.PARTIAL
    assert receipt.filled_size == 40.0
    assert receipt.requested_size == 100.0  # request preserved separately, never conflated


# ---------------------------------------------------------------------------
# poll_order_terminal: timeout -> last status -> UNRESOLVED, no fabricated fill
# ---------------------------------------------------------------------------


async def test_poll_terminal_timeout_returns_unresolved_no_fabricated_fill() -> None:
    """A perpetually-pending order times out to the LAST status (UNRESOLVED); never guesses a fill."""
    adapter = FakeVenueAdapter(fill=True, stay_pending=True)
    sleep = _RecordingSleep()

    status = await poll_order_terminal(
        adapter, "vid-timeout", timeout_s=1.0, interval_s=0.5, sleep=sleep
    )

    # Honest timeout: last observed (still-pending) status, which maps to UNRESOLVED.
    assert status.status == "pending"
    assert status.status not in TERMINAL_STATUSES
    assert map_venue_status(status.status) is ExecutionStatus.UNRESOLVED
    # NEVER guess a fill on timeout.
    assert status.filled_size == 0.0
    # Used the INJECTED sleep (instant test) — no real waiting.
    assert len(sleep.calls) >= 1
    assert all(d == 0.5 for d in sleep.calls)


async def test_poll_terminal_returns_early_when_status_flips_terminal() -> None:
    """poll_order_terminal returns as soon as the status maps terminal — before timeout."""
    adapter = FakeVenueAdapter(fill=True, pending_polls=2)  # calls 1,2 pending; call 3 terminal
    sleep = _RecordingSleep()

    status = await poll_order_terminal(
        adapter, "vid-flip", timeout_s=100.0, interval_s=0.5, sleep=sleep
    )

    assert status.status == "filled"
    assert status.status in TERMINAL_STATUSES
    assert map_venue_status(status.status) is ExecutionStatus.FILLED
    assert adapter.status_calls == 3  # polled exactly until terminal
    assert len(sleep.calls) == 2  # one sleep before each re-poll (2 re-polls)


# ---------------------------------------------------------------------------
# PRICE-UNIT DOCTRINE: price = decimal odds; native_price = audit-only
# ---------------------------------------------------------------------------


async def test_quote_price_is_decimal_odds_native_is_audit() -> None:
    """A v2 Quote's price is decimal odds; native_price is the venue-native value it derived from."""
    adapter = FakeVenueAdapter(fill=True)
    q = await adapter.quote_market("OU|2.5|full")

    assert isinstance(q, Quote)
    # price is decimal odds — the unit law/edge.py consumes (p*price-1).
    assert q.price == pytest.approx(2.05)
    # native_price preserves the venue-native value the decimal derived from (audit only).
    assert q.native_price is not None
    assert q.native_price == pytest.approx(_fake_decimal_to_native(q.price))
    # The Fake's decimal<->native relationship is explicit and round-trips.
    assert _fake_native_to_decimal(q.native_price) == pytest.approx(q.price)
    # levels carry NATIVE venue book units, not decimal odds.
    assert q.levels
    assert isinstance(q.levels[0], QuoteLevel)
    assert q.levels[0].native_price == pytest.approx(q.native_price)


def test_edge_consumes_decimal_odds_not_native() -> None:
    """Sanity: the decimal-odds price is what produces a sane edge; native would be nonsense."""
    prob_bps = 5000  # p = 0.5
    decimal_price = 2.05
    # p*price-1 = 0.5*2.05-1 = 0.025 -> 250 bps.
    assert executable_edge_bps(prob_bps, decimal_price) == 250
    # If a native price (implied-prob percent ~48.8) leaked into edge, the result would be absurd.
    native = _fake_decimal_to_native(decimal_price)
    assert executable_edge_bps(prob_bps, native) != 250
