"""Backfilled venue price-history frames (M0 S1a, AC-014/AC-015).

T0 read-only-to-trust-path tool: pure data-shape module (no network at import time). A
``VenuePriceHistoryFrame`` records ONE (ts, price) point pulled from a venue's historical
prices endpoint, backfilled AFTER the fact rather than observed live — hence
``provenance="backfilled-price-history"`` (:class:`veridex.provenance.EvidenceRung`), a
lower evidence rung than a recorded live quote or a live fill receipt.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, model_validator

from veridex.venues.polymarket import native_to_decimal
from veridex.venues.polymarket_resolver import ResolvedMarket, side_to_token


class VenuePriceHistoryFrame(BaseModel):
    """One backfilled (ts, native_price) point from a venue's price-history endpoint.

    TRUST INVARIANT (AC-014): ``native_price`` is stored EXACTLY as the venue returned it
    (the raw native share price ``q``); ``venue_decimal_price`` is ALWAYS derived via
    :func:`veridex.venues.polymarket.native_to_decimal` — NEVER the raw ``q`` itself and
    NEVER independently computed. This module carries no bid/ask/size/status fields — it is
    a price-only backfill artifact, not a live orderbook snapshot.

    The invariant is enforced STRUCTURALLY at the model boundary (see
    :meth:`_check_venue_decimal_price_matches_native`), not just on the ``from_native``/
    ``fetch_price_history`` construction path — a direct construction (or a deserialized/
    loaded frame) with a mismatched pair fails closed with a :class:`ValidationError` rather
    than silently carrying a wrong decimal price into downstream edge math.
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

    @model_validator(mode="after")
    def _check_venue_decimal_price_matches_native(self) -> VenuePriceHistoryFrame:
        """Fail closed (AC-014) unless ``venue_decimal_price == native_to_decimal(native_price)``.

        Relative tolerance (not a bare absolute epsilon): decimal odds scale with
        ``1/native_price``, so a fixed absolute tolerance would be too strict for tiny
        native prices (e.g. ``native_price=0.01`` -> decimal ``100``) and too loose for
        native prices near 1.
        """
        expected = native_to_decimal(self.native_price)
        if not math.isclose(self.venue_decimal_price, expected, rel_tol=1e-9):
            raise ValueError(
                f"venue_decimal_price={self.venue_decimal_price!r} does not match "
                f"native_to_decimal(native_price={self.native_price!r})={expected!r} (AC-014)"
            )
        return self

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


class VenuePriceHistoryPack(BaseModel):
    """Self-describing, hashed manifest for a backfilled price-history artifact.

    TRUST INVARIANT (AC-015): this pack has NO ``evidence_hash`` field. Its
    ``artifact_content_hash`` is a SIBLING artifact hash over the backfill frames file — a
    completely separate hash scope from the sealed TxLINE evidence hash
    (:mod:`veridex.runtime.evidence`) or the :class:`veridex.ingest.replay_pack.ReplayPack`
    content-hash scope. Never conflate the two: this pack does not participate in evidence
    sealing, it only proves the backfill frames file it ships wasn't corrupted/tampered.
    """

    pack_version: int = 1
    fixture_id: int
    frames_file: str
    artifact_content_hash: str
    provenance: str = "backfilled-price-history"


def compute_price_history_hash(pack_dir: Path, frames_file: str) -> str:
    """sha256 over the length-prefixed (name, bytes) pair for *frames_file* in *pack_dir*.

    Mirrors :func:`veridex.ingest.replay_pack._compute_content_hash`'s length-prefixing
    scheme (4-byte name length + name + 8-byte file length + file bytes) so the (name,
    bytes) decomposition is provably injective — a single-file instance of that same scheme,
    applied to this SIBLING artifact (AC-015), not the ReplayPack's own hash scope.
    """
    name_bytes = frames_file.encode("utf-8")
    file_bytes = (pack_dir / frames_file).read_bytes()
    digest = hashlib.sha256()
    digest.update(len(name_bytes).to_bytes(4, "big"))
    digest.update(name_bytes)
    digest.update(len(file_bytes).to_bytes(8, "big"))
    digest.update(file_bytes)
    return digest.hexdigest()


class PriceHistoryClient(Protocol):
    """Structural protocol for a venue's price-history read client.

    Tests ALWAYS inject a fake returning recorded points — no network in tests; only the
    operator's separate live backfill step hits a real venue endpoint (mirrors
    :class:`veridex.venues.polymarket_resolver.GammaClient`'s injection pattern).
    """

    async def get_prices_history(self, token_id: str) -> list[dict[str, Any]]:
        """Return recorded price points for *token_id*, each ``{"t": <ts>, "p": <native price>}``."""
        ...


async def fetch_price_history(
    resolved: ResolvedMarket,
    side: str,
    *,
    fixture_id: int,
    market_ref: str,
    fidelity_s: int,
    client: PriceHistoryClient,
) -> list[VenuePriceHistoryFrame]:
    """Backfill a bet side's price history into frames (AC-014: native->decimal enforced).

    Resolves *side* to its token via :func:`veridex.venues.polymarket_resolver.side_to_token`,
    fetches that token's recorded points from *client*, and converts each point to a
    :class:`VenuePriceHistoryFrame` via :meth:`VenuePriceHistoryFrame.from_native` — never
    hand-computing ``venue_decimal_price``.
    """
    token_id = side_to_token(resolved, side)
    points = await client.get_prices_history(token_id)
    return [
        VenuePriceHistoryFrame.from_native(
            ts=int(point["t"]),
            fixture_id=fixture_id,
            market_ref=market_ref,
            condition_id=resolved.condition_id,
            token_id=token_id,
            native_price=float(point["p"]),
            price_kind="clob-prices-history",
            fidelity_s=fidelity_s,
        )
        for point in points
    ]
