"""Offline quote recording — venue quote frames + cadence honesty gate.

Pure data layer: no I/O, no network imports. Import-safe anywhere, including
offline tests, mirroring :mod:`veridex.venues.base`.

TRUST/DESIGN INVARIANTS: :class:`VenueQuoteFrame` stores bid/ask PRIMITIVES,
never a derived ``spread`` field — spread (or any other derived quantity) is
computed on read, not stored, so the recorded frame can never silently drift
from its inputs. :func:`cadence_report` is the honesty gate a later
freshness/staleness check (M8/StaleLine) consults to decide whether sub-minute
cadence was actually achieved; it must never claim sufficiency it can't back up.
"""

from __future__ import annotations

import statistics

from pydantic import BaseModel, field_validator

_VALID_QUOTE_STATUSES = frozenset({"live", "suspended", "halted"})


class VenueQuoteFrame(BaseModel):
    """A single recorded venue quote snapshot, in decimal-odds primitives.

    PRICE-UNIT DOCTRINE (mirrors :class:`veridex.venues.base.Quote`):
    :attr:`best_bid_decimal` / :attr:`best_ask_decimal` are DECIMAL ODDS —
    the trust-consuming unit — stored as raw primitives, NOT a derived
    ``spread`` (spread is computed on read). :attr:`native_price` preserves
    the venue-native value for AUDIT only.

    Attributes:
        ts: Unix timestamp (seconds) when the quote was captured.
        fixture_id: Identifier of the fixture/event this quote belongs to.
        market_ref: Venue-specific market identifier (e.g. ``"1X2|home|full"``).
        venue: Venue slug.
        condition_id: Venue market/condition identifier.
        token_id: Venue outcome token identifier.
        best_bid_decimal: Best bid, in decimal odds.
        best_ask_decimal: Best ask, in decimal odds.
        bid_size: Liquidity available at :attr:`best_bid_decimal`.
        ask_size: Liquidity available at :attr:`best_ask_decimal`.
        quote_status: Venue-reported market status — one of ``"live"``,
            ``"suspended"``, ``"halted"``.
        native_price: Venue-native price the decimal derived from (audit
            only); ``None`` if n/a.
        provenance: Origin tag for this frame; defaults to
            ``"recorded-live-quote"``.
    """

    ts: int
    fixture_id: int
    market_ref: str
    venue: str = "polymarket"
    condition_id: str
    token_id: str
    best_bid_decimal: float
    best_ask_decimal: float
    bid_size: float
    ask_size: float
    quote_status: str
    native_price: float | None = None
    provenance: str = "recorded-live-quote"

    @field_validator("quote_status")
    @classmethod
    def _validate_quote_status(cls, v: str) -> str:
        if v not in _VALID_QUOTE_STATUSES:
            raise ValueError(
                f"quote_status must be one of {sorted(_VALID_QUOTE_STATUSES)}, got {v!r}"
            )
        return v


def cadence_report(
    frames: list[VenueQuoteFrame],
    *,
    sub_minute_threshold_s: float = 60.0,
) -> dict[str, float | bool | int]:
    """Report whether *frames* were recorded at sub-minute cadence.

    HONESTY GATE (CON-S1B-004 / AC-009): this report is the ONLY source M8/
    StaleLine may later consult to decide whether sub-minute cadence was
    actually achieved — it must never claim ``cadence_sufficient`` when the
    inter-frame gaps don't back it up.

    Args:
        frames: Recorded quote frames, in any order; deltas are computed
            between consecutive ``ts`` values after sorting.
        sub_minute_threshold_s: Median inter-frame interval must be strictly
            below this (seconds) for cadence to count as sufficient.

    Returns:
        A dict with ``median_interval_s`` (float), ``cadence_sufficient``
        (bool), and ``n`` (int, ``len(frames)``). With fewer than two frames
        there are no intervals to measure, so ``cadence_sufficient`` is
        ``False`` and ``median_interval_s`` is ``float("inf")``.
    """
    n = len(frames)
    if n < 2:
        return {"median_interval_s": float("inf"), "cadence_sufficient": False, "n": n}

    timestamps = sorted(f.ts for f in frames)
    intervals = [b - a for a, b in zip(timestamps, timestamps[1:], strict=False)]
    median_interval_s = float(statistics.median(intervals))
    cadence_sufficient = median_interval_s < sub_minute_threshold_s
    return {"median_interval_s": median_interval_s, "cadence_sufficient": cadence_sufficient, "n": n}
