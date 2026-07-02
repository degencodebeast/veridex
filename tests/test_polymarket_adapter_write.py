"""PolymarketAdapter WRITE-path tests (REQ-2D-403, AC-2D-404/405, SEC-004, §4.3) — TDD.

TRUST-CRITICAL: this is the ONLY path that can move real mainnet USDC, so the two load-bearing
items are proven here with INJECTED fake clients and ZERO network:

* PRICE-UNIT ON THE WIRE (§4.3): ``submit_order`` converts DECIMAL ODDS -> native share price
  ``q = 1/price`` -> tick-rounded to the market's ``tick_size``, and the FAK order carries that
  NATIVE tick-rounded price. A decimal-odds value must NEVER reach the wire.
* HONEST FILL RECONCILIATION (SEC-004): ``get_order_status`` reads the REAL ``size_matched`` and
  matched native ``price`` from the vendored ``get_order`` and reports THOSE — never the request.
  ``native_price`` (native q) is kept for audit; ``price`` is its decimal inverse.

Real-money safety is fail-closed: writes stay disabled unless ``polymarket_write_enabled`` is true
AND ``dry_run`` is off (both defaults keep the wire untouched). The venue write receipt is NOT
sealed evidence (AC-2D-404): its derived event carries ``evidence=False``.
"""

from __future__ import annotations

from typing import Any

import pytest

from veridex.competition.events import build_execution_receipt_event
from veridex.config import Settings
from veridex.execution.models import ExecutionStatus
from veridex.venues.base import Order, OrderStatus, build_receipt
from veridex.venues.polymarket import PolymarketAdapter, PolymarketWriteDisabled
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
    """Minimal book client for the adapter's read path (unused by write tests but required)."""

    def __init__(self, book: dict[str, Any] | None = None) -> None:
        self._book = book or {"bids": [], "asks": [], "timestamp": 0}

    async def get_book(self, token_id: str) -> dict[str, Any]:
        return self._book


class _CapturingWriteClient:
    """Fake CLOB write client that CAPTURES the ``limit_order`` payload — no network, no signing.

    Records exactly what would go on the wire so tests can assert the NATIVE tick-rounded price
    (never decimal odds). ``get_order`` returns a configured raw order dict for honest-fill tests.
    """

    def __init__(
        self,
        *,
        order_response: dict[str, Any] | None = None,
        get_order_response: dict[str, Any] | None = None,
        cancel_response: dict[str, Any] | None = None,
    ) -> None:
        self.limit_order_calls: list[dict[str, Any]] = []
        self.get_order_calls: list[str] = []
        self.cancel_calls: int = 0
        self._order_response = order_response or {"success": True, "orderID": "0xabc"}
        self._get_order_response = get_order_response or {}
        self._cancel_response = cancel_response or {"canceled": ["0xabc"], "success": True}

    async def limit_order(
        self,
        ticker: str,
        amount: float,
        price: float,
        tif: str = "GTC",
        round_price: bool = True,
        tick_size: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.limit_order_calls.append(
            {
                "ticker": ticker,
                "amount": amount,
                "price": price,
                "tif": tif,
                "round_price": round_price,
                "tick_size": tick_size,
            }
        )
        return self._order_response

    async def get_order(self, order_id: str, **kwargs: Any) -> dict[str, Any]:
        self.get_order_calls.append(order_id)
        return self._get_order_response

    async def cancel_all_orders(self, **kwargs: Any) -> dict[str, Any]:
        self.cancel_calls += 1
        return self._cancel_response


def _write_enabled_settings() -> Settings:
    return Settings(_env_file=None, polymarket_write_enabled=True)


def _disabled_settings() -> Settings:
    return Settings(_env_file=None)


def _order(*, price: float = 1.90, size: float = 1.0, side: str = "yes") -> Order:
    return Order(
        market_ref="WC|yes",
        side=side,
        size=size,
        price=price,
        venue="polymarket",
        client_order_id="coid-1",
    )


# ---------------------------------------------------------------------------
# PRICE-UNIT: submit puts NATIVE tick-rounded price on the wire (AC-2D-405 submit leg)
# ---------------------------------------------------------------------------


async def test_submit_puts_native_tick_rounded_price_on_wire_not_decimal_odds() -> None:
    """decimal odds 1.90 -> native 1/1.90 = 0.5263 -> tick-round(0.01) = 0.53 on the wire, FAK."""
    client = _CapturingWriteClient()
    adapter = PolymarketAdapter(
        _RESOLVED,
        _FakeBookClient(),
        side="yes",
        settings=_write_enabled_settings(),
        write_client=client,
        dry_run=False,
    )

    ack = await adapter.submit_order(_order(price=1.90, size=2.0, side="yes"))

    assert len(client.limit_order_calls) == 1
    payload = client.limit_order_calls[0]
    assert payload["price"] == pytest.approx(0.53)  # NATIVE q, tick-rounded — NOT decimal 1.90
    assert payload["price"] != pytest.approx(1.90)  # decimal odds NEVER on the wire
    assert 0.0 < payload["price"] < 1.0  # a native share price, not decimal odds
    assert payload["tif"] == "FAK"  # fill-and-kill
    assert payload["ticker"] == "111"  # yes-side token
    assert payload["amount"] == pytest.approx(2.0)  # BUY size (positive)
    assert payload["round_price"] is False  # we pre-rounded; the client must not re-round
    assert ack.accepted is True
    assert ack.venue_order_id == "0xabc"


async def test_submit_prices_selected_side_token() -> None:
    client = _CapturingWriteClient()
    adapter = PolymarketAdapter(
        _RESOLVED,
        _FakeBookClient(),
        side="no",
        settings=_write_enabled_settings(),
        write_client=client,
        dry_run=False,
    )

    await adapter.submit_order(_order(side="no"))

    assert client.limit_order_calls[0]["ticker"] == "222"  # no-side token


# ---------------------------------------------------------------------------
# HONEST FILL RECONCILIATION (SEC-004, AC-2D-405 fill leg) — THE trust item
# ---------------------------------------------------------------------------


async def test_get_order_status_reports_matched_fill_not_request() -> None:
    """get_order size_matched=0.4 @ native 0.55 -> filled_size=0.4, native_price=0.55, price=1.818."""
    client = _CapturingWriteClient(
        get_order_response={
            "id": "0xabc",
            "size_matched": 0.4,
            "price": 0.55,
            "original_size": 1.0,
            "status": "MATCHED",
        }
    )
    adapter = PolymarketAdapter(
        _RESOLVED,
        _FakeBookClient(),
        settings=_write_enabled_settings(),
        write_client=client,
        dry_run=False,
    )

    status = await adapter.get_order_status("0xabc")

    assert isinstance(status, OrderStatus)
    assert status.filled_size == pytest.approx(0.4)  # the MATCHED size, not the requested 1.0
    assert status.native_price == pytest.approx(0.55)  # native q kept for audit
    assert status.price == pytest.approx(1.0 / 0.55)  # decimal inverse of the matched native price
    assert status.status == "partial"  # 0.4 of 1.0 requested — reconciled from the number


async def test_receipt_reports_matched_fill_never_the_request() -> None:
    """build_receipt over the honest status reports filled_size=matched, requested_size=request."""
    client = _CapturingWriteClient(
        get_order_response={
            "id": "0xabc",
            "size_matched": 0.4,
            "price": 0.55,
            "original_size": 1.0,
            "status": "MATCHED",
        }
    )
    adapter = PolymarketAdapter(
        _RESOLVED,
        _FakeBookClient(),
        settings=_write_enabled_settings(),
        write_client=client,
        dry_run=False,
    )
    order = _order(price=1.0 / 0.55, size=1.0)

    status = await adapter.get_order_status("0xabc")
    receipt = adapter.normalize_receipt("exec-1", order, status, mode="live_guarded")

    assert receipt.requested_size == pytest.approx(1.0)  # the request
    assert receipt.filled_size == pytest.approx(0.4)  # the REAL matched fill, never the request
    assert receipt.filled_size != receipt.requested_size
    assert receipt.price == pytest.approx(1.0 / 0.55)  # decimal of the matched native price
    assert receipt.status == ExecutionStatus.PARTIAL


async def test_get_order_status_full_fill_maps_filled() -> None:
    client = _CapturingWriteClient(
        get_order_response={"id": "x", "size_matched": 1.0, "price": 0.5, "original_size": 1.0, "status": "MATCHED"}
    )
    adapter = PolymarketAdapter(
        _RESOLVED, _FakeBookClient(), settings=_write_enabled_settings(), write_client=client, dry_run=False
    )

    status = await adapter.get_order_status("x")

    assert status.status == "filled"
    assert status.filled_size == pytest.approx(1.0)
    assert status.price == pytest.approx(2.0)  # 1/0.5


async def test_get_order_status_zero_matched_fak_kill_is_rejected_not_fabricated() -> None:
    """A FAK that matched nothing reports zero fill and a terminal non-fill status — never a fake fill."""
    client = _CapturingWriteClient(
        get_order_response={"id": "x", "size_matched": 0.0, "price": 0.55, "original_size": 1.0, "status": "unmatched"}
    )
    adapter = PolymarketAdapter(
        _RESOLVED, _FakeBookClient(), settings=_write_enabled_settings(), write_client=client, dry_run=False
    )

    status = await adapter.get_order_status("x")

    assert status.filled_size == pytest.approx(0.0)
    assert status.status == "rejected"


# ---------------------------------------------------------------------------
# QUOTE-SIZE COUPLING (gate B): the depth quote is computed for the ORDER's size
# ---------------------------------------------------------------------------


async def test_quote_market_uses_passed_for_size_not_default() -> None:
    """quote_market(for_size=30) prices the top 30 shares (q=0.4), distinct from the default-100 VWAP."""
    book = {
        "bids": [[0.3, 10.0]],
        "asks": [[0.4, 30.0], [0.6, 70.0]],
        "timestamp": 1_700_000_000,
    }
    adapter = PolymarketAdapter(_RESOLVED, _FakeBookClient(book), side="yes", for_size=100.0)

    q30 = await adapter.quote_market("WC|yes", for_size=30.0)
    q100 = await adapter.quote_market("WC|yes")  # default for_size=100

    assert q30.size == pytest.approx(30.0)
    assert q30.for_size == pytest.approx(30.0)
    assert q30.native_price == pytest.approx(0.4)  # only the first level fills 30
    assert q30.price == pytest.approx(1.0 / 0.4)  # decimal of that native VWAP
    # the default-100 quote sweeps both levels -> a different VWAP, proving the size drove the depth.
    assert q100.size == pytest.approx(100.0)
    assert q100.native_price == pytest.approx((0.4 * 30 + 0.6 * 70) / 100)
    assert q30.native_price != pytest.approx(q100.native_price)


# ---------------------------------------------------------------------------
# REAL-MONEY SAFETY: write fails closed by default (write-disabled AND dry-run)
# ---------------------------------------------------------------------------


async def test_submit_refuses_when_write_disabled() -> None:
    client = _CapturingWriteClient()
    adapter = PolymarketAdapter(
        _RESOLVED, _FakeBookClient(), settings=_disabled_settings(), write_client=client, dry_run=False
    )

    with pytest.raises(PolymarketWriteDisabled):
        await adapter.submit_order(_order())

    assert client.limit_order_calls == []  # the wire is NEVER touched


async def test_submit_refuses_in_dry_run_even_when_write_enabled() -> None:
    """DRY_RUN is the safety default: even with write enabled, a dry-run submit never hits the wire."""
    client = _CapturingWriteClient()
    adapter = PolymarketAdapter(
        _RESOLVED, _FakeBookClient(), settings=_write_enabled_settings(), write_client=client, dry_run=True
    )

    with pytest.raises(PolymarketWriteDisabled):
        await adapter.submit_order(_order())

    assert client.limit_order_calls == []


async def test_dry_run_is_the_default() -> None:
    """An adapter constructed without an explicit ``dry_run`` defaults to the SAFE dry-run posture."""
    client = _CapturingWriteClient()
    adapter = PolymarketAdapter(
        _RESOLVED, _FakeBookClient(), settings=_write_enabled_settings(), write_client=client
    )

    with pytest.raises(PolymarketWriteDisabled):
        await adapter.submit_order(_order())

    assert client.limit_order_calls == []


async def test_cancel_refuses_in_dry_run_even_when_write_enabled() -> None:
    client = _CapturingWriteClient()
    adapter = PolymarketAdapter(
        _RESOLVED, _FakeBookClient(), settings=_write_enabled_settings(), write_client=client, dry_run=True
    )

    with pytest.raises(PolymarketWriteDisabled):
        await adapter.cancel_order("0xabc")

    assert client.cancel_calls == 0


async def test_cancel_when_armed_calls_vendored_cancel() -> None:
    client = _CapturingWriteClient()
    adapter = PolymarketAdapter(
        _RESOLVED, _FakeBookClient(), settings=_write_enabled_settings(), write_client=client, dry_run=False
    )

    ack = await adapter.cancel_order("0xabc")

    assert client.cancel_calls == 1
    assert ack.cancelled is True
    assert ack.venue_order_id == "0xabc"


# ---------------------------------------------------------------------------
# EVIDENCE (AC-2D-404): the venue write receipt is NEVER sealed evidence
# ---------------------------------------------------------------------------


def test_polymarket_receipt_event_is_not_sealed_evidence() -> None:
    """The derived EXECUTION_RECEIPT event carries evidence=False — venue data never enters the seal."""
    order = _order(price=1.0 / 0.55, size=1.0)
    status = OrderStatus(
        venue_order_id="0xabc", status="partial", filled_size=0.4, price=1.0 / 0.55, native_price=0.55
    )
    receipt = build_receipt("exec-1", order, status, mode="live_guarded")

    event = build_execution_receipt_event(
        competition_id="comp-1",
        run_id="run-1",
        seq=1,
        event_ts=0,
        execution_id="exec-1",
        receipt_payload=receipt.model_dump(mode="json"),
    )

    assert event.evidence is False  # not sealed evidence (SEC-2D-401 / AC-2D-404)
