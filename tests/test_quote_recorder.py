"""Tests for veridex.venues.quote_recorder (VenueQuoteFrame, cadence_report)."""

from __future__ import annotations

import pytest

from veridex.venues.quote_recorder import VenueQuoteFrame, cadence_report


def _f(ts: int) -> VenueQuoteFrame:
    return VenueQuoteFrame(
        ts=ts,
        fixture_id=1,
        market_ref="m",
        condition_id="0x",
        token_id="t",
        best_bid_decimal=1.9,
        best_ask_decimal=2.1,
        bid_size=1,
        ask_size=1,
        quote_status="live",
    )


def test_cadence_sufficient_when_sub_minute():
    rep = cadence_report([_f(0), _f(10), _f(20), _f(30)])
    assert rep["median_interval_s"] == 10.0 and rep["cadence_sufficient"] is True


def test_cadence_insufficient_when_sparse():
    rep = cadence_report([_f(0), _f(120), _f(240)])
    assert rep["cadence_sufficient"] is False


def test_quote_frame_stores_primitives_and_valid_status():
    f = VenueQuoteFrame(
        ts=1,
        fixture_id=1,
        market_ref="1X2|home|full",
        condition_id="0x",
        token_id="t",
        best_bid_decimal=1.9,
        best_ask_decimal=2.1,
        bid_size=100.0,
        ask_size=120.0,
        quote_status="live",
    )
    assert f.best_bid_decimal == 1.9 and f.best_ask_decimal == 2.1
    assert not hasattr(f, "spread")  # store primitives, not a derived spread
    assert f.provenance == "recorded-live-quote"


def test_quote_status_must_be_valid():
    with pytest.raises(ValueError):
        VenueQuoteFrame(
            ts=1,
            fixture_id=1,
            market_ref="m",
            condition_id="0x",
            token_id="t",
            best_bid_decimal=1.9,
            best_ask_decimal=2.1,
            bid_size=1,
            ask_size=1,
            quote_status="open",
        )
