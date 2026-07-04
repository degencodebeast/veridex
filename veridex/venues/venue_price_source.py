"""C-2 (S5) — the time-aligned venue price seam: the ONLY door venue data enters the VvV trust path.

Where S5's first cut injected a market-key-only ``Callable[[str], float | None]``, the trust path a
real Polymarket backfill demands is TIME-ALIGNED: a quote must be looked up for the exact decision
coordinate — the ``(fixture_id, market_key, side, ts)`` the agent is deciding on — so the price the
edge is estimated against is the one that was on offer *at that tick*, carrying the measured
``staleness_s`` of that alignment (never an interpolated or future price; CON-006).

Two boundaries stay load-bearing (proven in the agent/producer tests, not here):

  * **Venue data enters ONLY through this injected source (SEC-003).** The agent never reads a venue
    price out of the (evidence-sealed) ``market_state``; it calls the source with the decision
    coordinate and reads ``.venue_decimal_price``.
  * **The quote's NUMBERS never ride into evidence (CON-002).** ``venue_decimal_price`` / ``staleness_s``
    are venue numbers: a venue-driven change of ACTION (fire vs wait) legitimately changes evidence,
    but the numbers themselves never enter ``AgentAction.params`` / law / scoring / the sealed
    ``run_events``.

``None`` is the fail-safe: no quote at/under the caller's freshness bound ⇒ ``None`` ⇒ no edge to
estimate ⇒ the agent WAITs (there is nothing to price against). Staleness/freshness bounding is the
SOURCE's job (C-4); this module only fixes the shape of the seam both consumers share.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeAlias

from pydantic import BaseModel


class TimedVenueQuote(BaseModel):
    """A venue DECIMAL price time-aligned to a decision tick, with the staleness of that alignment.

    Attributes:
        venue_decimal_price: The venue's DECIMAL odds (``1/q`` and thus always ``> 1.0``) on offer for
            the priced side at the decision tick. Consumers feed this straight into the raw EV math
            (``executable_edge_bps`` / ``vvv_signal``) — frames are already decimal (AC-014), so
            ``native_to_decimal`` is NOT re-applied at this seam.
        staleness_s: How stale (seconds) the aligned quote is relative to the decision ``ts`` — the
            gap between the decision and the quote actually used. Reported/attached at the report
            layer; the freshness BOUND that turns a too-stale quote into ``None`` lives in the source.
    """

    venue_decimal_price: float
    staleness_s: int


#: A time-aligned venue price source: ``(fixture_id, market_key, side, ts) -> TimedVenueQuote | None``.
#: ``None`` means "no quote available at/under the caller's freshness bound" — the fail-safe the agent
#: WAITs on. The venue's identity (which artifact it prices against) is pinned SEPARATELY via the
#: agent's ``venue_source_id`` (config-hash identity), never smuggled through the returned numbers.
VenuePriceSource: TypeAlias = Callable[[int, str, str, int], TimedVenueQuote | None]
