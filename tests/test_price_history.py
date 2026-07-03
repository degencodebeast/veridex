import pytest

from veridex.venues.price_history import VenuePriceHistoryFrame
from veridex.venues.polymarket import native_to_decimal


def test_from_native_sets_decimal_via_native_to_decimal_not_the_raw_q():
    f = VenuePriceHistoryFrame.from_native(
        ts=1000,
        fixture_id=17952170,
        market_ref="1X2|home|full",
        condition_id="0xabc",
        token_id="tok1",
        native_price=0.62,
        price_kind="clob-prices-history",
        fidelity_s=60,
    )
    assert f.native_price == 0.62
    assert f.venue_decimal_price == native_to_decimal(0.62)
    assert f.venue_decimal_price != 0.62
    assert f.provenance == "backfilled-price-history"
