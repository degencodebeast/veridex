"""Tests for veridex.venues — venue adapter protocol, FakeVenueAdapter, and SXBetAdapter skeleton.

Covers:
- Full fake adapter round-trip: quote → submit → status → normalize_receipt.
- Fake cancel returns CancelAck(cancelled=True).
- normalize_receipt is deterministic (same inputs → same output).
- Importing sx_bet is offline-safe: httpx/aiohttp must NOT be imported at module load.
- No literal credentials anywhere in the sx_bet source.
- Live SX Bet smoke-test is skip-gated for offline runs.
"""

import os
import sys

import pytest

from veridex.execution.models import ExecutionStatus
from veridex.venues.base import Order, OrderStatus
from veridex.venues.sx_bet import FakeVenueAdapter

# ---------------------------------------------------------------------------
# Happy-path: quote → submit → status → normalize
# ---------------------------------------------------------------------------


async def test_fake_quote_submit_status_normalize() -> None:
    """Full fake adapter round-trip returns a valid ExecutionReceipt."""
    a = FakeVenueAdapter(fill=True)
    q = await a.quote_market("OU|2.5|full")
    ack = await a.submit_order(Order(market_ref="OU|2.5|full", side="over", size=100.0, price=q.price, venue="sx_bet", client_order_id="c1"))
    assert ack.accepted and a.submit_calls == 1
    st = await a.get_order_status(ack.venue_order_id)
    rcpt = a.normalize_receipt(
        "e1",
        Order(market_ref="OU|2.5|full", side="over", size=100.0, price=q.price, venue="sx_bet", client_order_id="c1"),
        st,
        mode="dry_run",
    )
    assert rcpt.status in (ExecutionStatus.FILLED, ExecutionStatus.PARTIAL)
    assert rcpt.execution_id == "e1"
    assert rcpt.mode == "dry_run"


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


async def test_fake_cancel() -> None:
    """Fake adapter cancel always returns CancelAck(cancelled=True)."""
    a = FakeVenueAdapter()
    c = await a.cancel_order("o1")
    assert c.cancelled is True


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_normalize_receipt_pure_deterministic() -> None:
    """normalize_receipt is pure: identical inputs produce identical receipts."""
    a = FakeVenueAdapter()
    st = OrderStatus(venue_order_id="o1", status="filled", filled_size=100.0, price=2.0)
    o = Order(market_ref="m", side="over", size=100.0, price=2.0, venue="sx_bet", client_order_id="c1")
    r1 = a.normalize_receipt("e1", o, st, mode="dry_run")
    r2 = a.normalize_receipt("e1", o, st, mode="dry_run")
    assert r1.model_dump() == r2.model_dump()
    assert r1.status is ExecutionStatus.FILLED


# ---------------------------------------------------------------------------
# Offline-safety invariants
# ---------------------------------------------------------------------------


def test_import_sx_bet_is_offline_safe() -> None:
    """Importing veridex.venues.sx_bet must not eagerly pull in httpx or aiohttp.

    Checked in a fresh interpreter: other tests in this process (e.g. the vendored
    Polymarket client's import test) legitimately import httpx/aiohttp, which would
    pollute this process's ``sys.modules`` and make an in-process check order-fragile.
    A subprocess isolates the assertion to sx_bet's OWN import graph.
    """
    import subprocess

    code = (
        "import sys, veridex.venues.sx_bet\n"
        "assert 'httpx' not in sys.modules, 'sx_bet eagerly imported httpx'\n"
        "assert 'aiohttp' not in sys.modules, 'sx_bet eagerly imported aiohttp'\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_venues_no_secrets_in_module() -> None:
    """The sx_bet module must not contain literal credential strings."""
    import pathlib

    import veridex.venues.sx_bet as m

    src = pathlib.Path(m.__file__).read_text()
    assert "Bearer " not in src
    assert "api_key=" not in src


# ---------------------------------------------------------------------------
# Partial-fill path
# ---------------------------------------------------------------------------


async def test_fake_partial_fill() -> None:
    """FakeVenueAdapter with fill_size < order size returns PARTIAL status."""
    a = FakeVenueAdapter(fill=True, fill_size=50.0)
    q = await a.quote_market("OU|2.5|full")
    ack = await a.submit_order(Order(market_ref="OU|2.5|full", side="under", size=100.0, price=q.price, venue="sx_bet", client_order_id="c1"))
    st = await a.get_order_status(ack.venue_order_id)
    rcpt = a.normalize_receipt(
        "e2",
        Order(market_ref="OU|2.5|full", side="under", size=100.0, price=q.price, venue="sx_bet", client_order_id="c1"),
        st,
        mode="paper",
    )
    assert rcpt.status is ExecutionStatus.PARTIAL
    assert rcpt.filled_size == 50.0


# ---------------------------------------------------------------------------
# Rejection path
# ---------------------------------------------------------------------------


async def test_fake_rejection() -> None:
    """FakeVenueAdapter with fill=False returns a rejected status."""
    a = FakeVenueAdapter(fill=False)
    q = await a.quote_market("OU|2.5|full")
    ack = await a.submit_order(Order(market_ref="OU|2.5|full", side="over", size=100.0, price=q.price, venue="sx_bet", client_order_id="c1"))
    st = await a.get_order_status(ack.venue_order_id)
    rcpt = a.normalize_receipt(
        "e3",
        Order(market_ref="OU|2.5|full", side="over", size=100.0, price=q.price, venue="sx_bet", client_order_id="c1"),
        st,
        mode="dry_run",
    )
    assert rcpt.status is ExecutionStatus.REJECTED


# ---------------------------------------------------------------------------
# SX Bet live smoke-test (gated — requires creds + venue_enabled)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not os.getenv("SX_BET_ENABLED"), reason="SX Bet live smoke requires SX_BET_ENABLED + creds")
async def test_sx_bet_live_quote_smoke() -> None:
    """Smoke-test against the real SX Bet API. Only runs with live creds."""
    ...  # gated; only meaningful with real creds + venue_enabled
