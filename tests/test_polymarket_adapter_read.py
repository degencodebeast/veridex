"""PolymarketAdapter READ-path tests (REQ-2D-203/204, AC-2D-202/203/405, §4.3) — TDD.

TRUST-ADJACENT: this adapter feeds the edge/execution path, so the load-bearing item is the
PRICE-UNIT DOCTRINE (§4.3). Polymarket's book is in NATIVE share prices ``q ∈ (0, 1)`` (a
probability-like cost for a $1-payout share). Veridex-side ``Quote.price`` is DECIMAL ODDS
(``1/q``); ``Quote.native_price`` preserves the native ``q`` for AUDIT only; ``QuoteLevel``s
carry NATIVE units. A native ``q`` leaking into ``.price`` corrupts every edge, so these tests
prove it never does.

The quote is DEPTH-AWARE: ``price`` is the cost-to-fill (size-weighted average native fill price,
converted to decimal) for ``for_size`` shares — NOT the midpoint. A book that cannot fill
``for_size`` yields a partial ``size`` and a cost-to-fill of what is available, never a midpoint.

Everything is OFFLINE: a fake book client returns a synthetic book; no network, no vendored
signing stack, no live CLOB. Writes are DISABLED by default (mainnet real money) — ``submit_order``
and ``cancel_order`` raise :class:`PolymarketWriteDisabled` under default config (AC-2D-203).
"""

from __future__ import annotations

from typing import Any

import pytest

from veridex.config import Settings
from veridex.venues.base import Order, Quote
from veridex.venues.polymarket import (
    PolymarketAdapter,
    PolymarketWriteDisabled,
    decimal_to_native,
    native_to_decimal,
    round_to_tick,
)
from veridex.venues.polymarket_resolver import ResolvedMarket

# ---------------------------------------------------------------------------
# Fixtures / fakes (no network)
# ---------------------------------------------------------------------------

_RESOLVED = ResolvedMarket(
    condition_id="0xcond",
    token_id_yes="111",
    token_id_no="222",
    tick_size=0.01,
)


class _FakeBookClient:
    """Duck-typed book client matching the vendored ``get_book(token_id)`` shape.

    Returns a synthetic book ``{"bids": [...], "asks": [...], "timestamp": ...}``. Records the
    token IDs it was asked for so tests can assert the adapter priced the right side's token.
    """

    def __init__(self, book: dict[str, Any]) -> None:
        self._book = book
        self.calls: list[str] = []

    async def get_book(self, token_id: str) -> dict[str, Any]:
        self.calls.append(token_id)
        return self._book


def _default_disabled_settings() -> Settings:
    """Settings with writes disabled (default). ``_env_file=None`` ignores any local .env."""
    return Settings(_env_file=None)


# ---------------------------------------------------------------------------
# Conversion helpers (consumed by T17 write path)
# ---------------------------------------------------------------------------


def test_native_to_decimal_inverts_price() -> None:
    assert native_to_decimal(0.5) == pytest.approx(2.0)
    assert native_to_decimal(0.25) == pytest.approx(4.0)


def test_decimal_to_native_inverts_price() -> None:
    assert decimal_to_native(2.0) == pytest.approx(0.5)
    assert decimal_to_native(4.0) == pytest.approx(0.25)


def test_conversions_guard_non_positive() -> None:
    for bad in (0.0, -0.1, -1.0):
        with pytest.raises(ValueError):
            native_to_decimal(bad)
        with pytest.raises(ValueError):
            decimal_to_native(bad)


def test_round_to_tick_rounds_native_price() -> None:
    assert round_to_tick(0.523, 0.01) == pytest.approx(0.52)
    assert round_to_tick(0.527, 0.01) == pytest.approx(0.53)
    assert round_to_tick(0.5, 0.1) == pytest.approx(0.5)
    with pytest.raises(ValueError):
        round_to_tick(0.5, 0.0)


# ---------------------------------------------------------------------------
# Depth-aware decimal quote (AC-2D-405 quote leg, AC-2D-202, §4.3)
# ---------------------------------------------------------------------------


async def test_quote_price_is_depth_averaged_decimal_odds() -> None:
    """VWAP native fill of two ask levels is q=0.5 → price == 1/0.5 == 2.0 (decimal), native q audit."""
    # asks: 50 shares @ 0.4, 50 @ 0.6. To fill 100 shares:
    #   notional = 0.4*50 + 0.6*50 = 50 ; filled = 100 ; avg_q = 0.5 → decimal 2.0.
    book = {
        "bids": [[0.3, 200.0]],
        "asks": [[0.4, 50.0], [0.6, 50.0]],
        "timestamp": 1_700_000_000,
    }
    adapter = PolymarketAdapter(_RESOLVED, _FakeBookClient(book), side="yes", for_size=100.0)

    quote = await adapter.quote_market("WC|yes")

    assert isinstance(quote, Quote)
    assert quote.price == pytest.approx(2.0)  # DECIMAL odds (1 / avg native q)
    assert quote.native_price == pytest.approx(0.5)  # native avg q (audit only)
    assert quote.size == pytest.approx(100.0)
    assert quote.for_size == pytest.approx(100.0)
    # The decimal price must NOT equal the native q — no native leak into .price.
    assert quote.price != pytest.approx(quote.native_price)


async def test_quote_prices_the_selected_sides_token() -> None:
    """The adapter fetches the book for the resolved token of the configured side."""
    book = {"bids": [], "asks": [[0.5, 100.0]], "timestamp": 1_700_000_000}
    client = _FakeBookClient(book)
    adapter = PolymarketAdapter(_RESOLVED, client, side="no", for_size=10.0)

    await adapter.quote_market("WC|no")

    assert client.calls == ["222"]  # token_id_no


async def test_quote_levels_carry_native_units_not_decimal() -> None:
    """QuoteLevel.native_price is the raw book price q, never the decimal 1/q."""
    book = {"bids": [[0.3, 100.0]], "asks": [[0.4, 50.0], [0.6, 50.0]], "timestamp": 1_700_000_000}
    adapter = PolymarketAdapter(_RESOLVED, _FakeBookClient(book), side="yes", for_size=100.0)

    quote = await adapter.quote_market("WC|yes")

    native_prices = sorted(level.native_price for level in quote.levels)
    assert native_prices == pytest.approx([0.4, 0.6])  # book prices, NOT 1/0.4, 1/0.6
    # None of the levels should look like a decimal-odds value (>1 for a q in (0,1)).
    assert all(0.0 < level.native_price < 1.0 for level in quote.levels)


# ---------------------------------------------------------------------------
# Thin book: partial fill, cost-to-fill of what's available, NOT a midpoint
# ---------------------------------------------------------------------------


async def test_thin_book_partial_size_cost_to_fill_not_midpoint() -> None:
    """When the book can't fill for_size, size is the available liquidity and price is the
    cost-to-fill of that liquidity — never a midpoint of best bid/ask."""
    # asks can only supply 30 shares @ 0.5; caller wants 100.
    # midpoint of best bid (0.3) and best ask (0.5) is 0.4 → decimal 2.5 (the WRONG answer).
    # cost-to-fill VWAP of available is q=0.5 → decimal 2.0 (the RIGHT answer).
    book = {"bids": [[0.3, 100.0]], "asks": [[0.5, 30.0]], "timestamp": 1_700_000_000}
    adapter = PolymarketAdapter(_RESOLVED, _FakeBookClient(book), side="yes", for_size=100.0)

    quote = await adapter.quote_market("WC|yes")

    assert quote.size == pytest.approx(30.0)  # partial: only what's available
    assert quote.for_size == pytest.approx(100.0)
    assert quote.native_price == pytest.approx(0.5)  # VWAP of available, not midpoint 0.4
    assert quote.price == pytest.approx(2.0)  # 1/0.5, NOT 1/0.4 == 2.5


# ---------------------------------------------------------------------------
# Empty / one-sided book: honest degrade, NEVER a fabricated price (guarded LOB hazards)
# ---------------------------------------------------------------------------


async def test_empty_book_degrades_without_crash_or_fabricated_price() -> None:
    book = {"bids": [], "asks": [], "timestamp": 1_700_000_000}
    adapter = PolymarketAdapter(_RESOLVED, _FakeBookClient(book), side="yes", for_size=100.0)

    quote = await adapter.quote_market("WC|yes")

    assert quote.size == pytest.approx(0.0)
    assert quote.price <= 0.0  # no executable price — NOT fabricated (edge law treats <=0 as no edge)
    assert quote.native_price is None  # nothing native to audit


async def test_one_sided_book_no_asks_degrades_without_crash() -> None:
    """Bids present but no asks (can't BUY the side) → honest degrade, no IndexError from get_mid."""
    book = {"bids": [[0.3, 100.0], [0.2, 50.0]], "asks": [], "timestamp": 1_700_000_000}
    adapter = PolymarketAdapter(_RESOLVED, _FakeBookClient(book), side="yes", for_size=100.0)

    quote = await adapter.quote_market("WC|yes")

    assert quote.size == pytest.approx(0.0)
    assert quote.price <= 0.0
    assert quote.native_price is None


# ---------------------------------------------------------------------------
# Write path DISABLED by default (AC-2D-203) — Polymarket is mainnet real money
# ---------------------------------------------------------------------------


async def test_submit_order_raises_write_disabled_under_default_config() -> None:
    adapter = PolymarketAdapter(
        _RESOLVED,
        _FakeBookClient({"bids": [], "asks": [], "timestamp": 0}),
        settings=_default_disabled_settings(),
    )
    order = Order(
        market_ref="WC|yes",
        side="yes",
        size=10.0,
        price=2.0,
        venue="polymarket",
        client_order_id="coid-1",
    )
    with pytest.raises(PolymarketWriteDisabled):
        await adapter.submit_order(order)


async def test_cancel_order_raises_write_disabled_under_default_config() -> None:
    adapter = PolymarketAdapter(
        _RESOLVED,
        _FakeBookClient({"bids": [], "asks": [], "timestamp": 0}),
        settings=_default_disabled_settings(),
    )
    with pytest.raises(PolymarketWriteDisabled):
        await adapter.cancel_order("venue-order-1")


async def test_get_order_status_raises_write_disabled_under_default_config() -> None:
    """get_order_status is gated identically to submit/cancel: in read-only mode there are no live
    orders to query, so it fails closed behind the write gate (real-money safety — all three pinned)."""
    adapter = PolymarketAdapter(
        _RESOLVED,
        _FakeBookClient({"bids": [], "asks": [], "timestamp": 0}),
        settings=_default_disabled_settings(),
    )
    with pytest.raises(PolymarketWriteDisabled):
        await adapter.get_order_status("venue-order-1")
