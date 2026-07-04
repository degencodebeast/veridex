"""C-2 (S5) — the time-aligned venue seam: ``TimedVenueQuote`` + the ``VenuePriceSource`` type.

The seam is the ONLY door venue data enters the VvV trust path. A source is keyed by the DECISION
coordinate ``(fixture_id, market_key, side, ts)`` — so a quote is time-aligned to the tick the agent
decides on — and returns a :class:`TimedVenueQuote` (a decimal price + its measured ``staleness_s``)
or ``None`` when no quote is available at/under the caller's freshness bound (CON-006: no
interpolation; missing/too-stale ⇒ ``None``). The numbers a quote carries are venue numbers and
NEVER enter ``AgentAction.params`` / evidence (CON-002) — that boundary is proven in the agent tests.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from veridex.venues.venue_price_source import TimedVenueQuote, VenuePriceSource


def test_timed_venue_quote_carries_decimal_price_and_staleness() -> None:
    """A quote is a decimal price time-aligned to a decision, plus the staleness of that alignment."""
    q = TimedVenueQuote(venue_decimal_price=1.90, staleness_s=120)

    assert q.venue_decimal_price == 1.90
    assert q.staleness_s == 120


def test_timed_venue_quote_requires_both_fields() -> None:
    """Both fields are load-bearing: a quote with no staleness can't be freshness-bounded (CON-006)."""
    with pytest.raises(ValidationError):
        TimedVenueQuote(venue_decimal_price=1.90)  # type: ignore[call-arg]  # missing staleness_s
    with pytest.raises(ValidationError):
        TimedVenueQuote(staleness_s=120)  # type: ignore[call-arg]  # missing venue_decimal_price


def test_venue_price_source_is_a_4arg_time_aligned_quote_callable() -> None:
    """A conforming source is keyed by ``(fixture_id, market_key, side, ts)`` → ``TimedVenueQuote | None``."""
    src: VenuePriceSource = lambda fixture_id, market_key, side, ts: (
        TimedVenueQuote(venue_decimal_price=2.0, staleness_s=0) if side == "home" else None
    )

    quote = src(5, "1X2|home", "home", 1000)
    assert quote is not None
    assert quote.venue_decimal_price == 2.0
    # No quote for the coordinate the source can't price → None (fail-safe; the agent WAITs).
    assert src(5, "1X2|home", "away", 1000) is None
