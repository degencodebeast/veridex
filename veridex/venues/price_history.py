"""Backfilled venue price-history frames (M0 S1a, AC-014/AC-015).

T0 read-only-to-trust-path tool: pure data-shape module (no network at import time). A
``VenuePriceHistoryFrame`` records ONE (ts, price) point pulled from a venue's historical
prices endpoint, backfilled AFTER the fact rather than observed live — hence
``provenance="backfilled-price-history"`` (:class:`veridex.provenance.EvidenceRung`), a
lower evidence rung than a recorded live quote or a live fill receipt.
"""

from __future__ import annotations

from pydantic import BaseModel

from veridex.venues.polymarket import native_to_decimal


class VenuePriceHistoryFrame(BaseModel):
    """One backfilled (ts, native_price) point from a venue's price-history endpoint.

    TRUST INVARIANT (AC-014): ``native_price`` is stored EXACTLY as the venue returned it
    (the raw native share price ``q``); ``venue_decimal_price`` is ALWAYS derived via
    :func:`veridex.venues.polymarket.native_to_decimal` — NEVER the raw ``q`` itself and
    NEVER independently computed. This module carries no bid/ask/size/status fields — it is
    a price-only backfill artifact, not a live orderbook snapshot.
    """

    ts: int
    fixture_id: int
    market_ref: str
    venue: str = "polymarket"
    condition_id: str
    token_id: str
    native_price: float
    venue_decimal_price: float
    price_kind: str
    fidelity_s: int
    provenance: str = "backfilled-price-history"

    @classmethod
    def from_native(
        cls,
        *,
        ts: int,
        fixture_id: int,
        market_ref: str,
        condition_id: str,
        token_id: str,
        native_price: float,
        price_kind: str,
        fidelity_s: int,
    ) -> VenuePriceHistoryFrame:
        """Build a frame, deriving ``venue_decimal_price`` from ``native_price`` (AC-014)."""
        return cls(
            ts=ts,
            fixture_id=fixture_id,
            market_ref=market_ref,
            condition_id=condition_id,
            token_id=token_id,
            native_price=native_price,
            venue_decimal_price=native_to_decimal(native_price),
            price_kind=price_kind,
            fidelity_s=fidelity_s,
        )
