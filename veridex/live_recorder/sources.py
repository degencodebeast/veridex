"""E4 live sources for the live-recorder lane (MM-R3).

Injectable source seams (each behind a runtime-checkable ``Protocol``) plus offline
``Fake*`` implementations, a default public-``/book`` DEPTH adapter, a ``MarketState`` →
:class:`~veridex.live_recorder.contracts.FairValueEvent` mapping with an HONEST proof
reference, and fail-closed credential + secret-scrubbing helpers.

Trust-boundary discipline (mirrors ``scripts/maker/live_monitor.py`` and CON-010):

* NO network library is imported at module scope — ``httpx`` is imported **lazily**
  inside :meth:`_DefaultBookDepthSource.fetch_book`, so importing this module (and the
  offline test-suite) touches no network. Every default adapter also takes an INJECTABLE
  client seam so tests inject a fake.
* Depth, not mid: :class:`_DefaultBookDepthSource` returns the full ``bids``/``asks``
  levels, never a collapsed mid. An empty book side is a legitimate empty tuple and is
  NEVER imputed.
* Proof honesty: a ``MarketState`` carries no ``messageId``, so
  :func:`marketstate_to_fair_value` sets ``message_id=None`` and
  ``proof_status="unavailable_no_message_id"`` — a status is never fabricated. The optional
  out-of-band batch resolver is REPORT-ONLY and MUST NOT be bound into any content hash.
* Secret hygiene: :func:`require_live_creds` fails closed BEFORE any I/O when a required
  credential is absent; :func:`_scrub` strips secret values from any text before it is
  printed; :func:`configured` exposes only a boolean for telemetry (never the secret).

This module imports nothing from ``veridex.scoring`` or ``veridex.maker``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from veridex.ingest.marketstate import MarketState
from veridex.live_recorder.contracts import BookLevel, FairValueEvent, VenueTradeEvent

# Public Polymarket CLOB book endpoint — PUBLIC (no wallet, no credential); mirrors
# ``scripts/maker/live_monitor.py::_CLOB_URL``.
_CLOB_URL = "https://clob.polymarket.com"

# TxLINE 1X2 full-match market key + native-prob scale; mirrors
# ``scripts/maker/live_monitor.py`` (``_TXLINE_1X2_FULL_MARKET_KEY`` / ``bps / 1e4``).
_TXLINE_1X2_FULL_MARKET_KEY = "1X2_PARTICIPANT_RESULT||"
_PROB_BPS_SCALE = 1e4

# Required live credentials (mirrors ``veridex.config.require_txline``: JWT + api-token).
_REQUIRED_CRED_KEYS = ("JWT", "TXLINE_X_API_TOKEN")


class BookSnapshot(BaseModel):
    """A full-depth venue book snapshot as sourced (NOT an event — no envelope/sequence_no/recv_ts).

    ``bids``/``asks`` are ordered tuples of :class:`~veridex.live_recorder.contracts.BookLevel`;
    an empty side is a legitimate empty tuple and is NEVER imputed. The recorder wraps this into a
    :class:`~veridex.live_recorder.contracts.VenueBookSnapshotEvent` at record time.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    token_id: str
    venue_market_ref: str
    book_ts: int
    tick_size: float
    min_price_increment: float
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    is_snapshot: bool


# --------------------------------------------------------------------------- source Protocols
@runtime_checkable
class FvSource(Protocol):
    """A fair-value source: streams :class:`MarketState`s in arrival order (mirrors live-monitor)."""

    def stream(self) -> AsyncIterator[MarketState]: ...


@runtime_checkable
class BookDepthSource(Protocol):
    """A venue book-DEPTH source: one full-depth :class:`BookSnapshot` per token, or ``None`` if unavailable."""

    def fetch_book(self, token_id: str) -> Awaitable[BookSnapshot | None]: ...


@runtime_checkable
class VenueTradeSource(Protocol):
    """An optional venue-trade source: streams :class:`VenueTradeEvent`s for a token."""

    def stream_trades(self, token_id: str) -> AsyncIterator[VenueTradeEvent]: ...


# --------------------------------------------------------------------------- offline fakes
class FakeFvSource:
    """A network-free ``FvSource``: replays canned ``MarketState``s in arrival order.

    ``stream`` performs NO ``await`` between yields (mirrors
    ``tests/test_maker_live_monitor.py::FakeFvSource``).
    """

    def __init__(self, states: list[MarketState]) -> None:
        self._states = list(states)

    async def stream(self) -> AsyncIterator[MarketState]:
        for state in self._states:
            yield state


class FakeBookDepthSource:
    """A scripted ``BookDepthSource``: returns the next canned ``BookSnapshot`` per token, then ``None``."""

    def __init__(self, scripts: Mapping[str, list[BookSnapshot | None]]) -> None:
        self._scripts = {tok: list(seq) for tok, seq in scripts.items()}
        self._calls: dict[str, int] = {}

    async def fetch_book(self, token_id: str) -> BookSnapshot | None:
        i = self._calls.get(token_id, 0)
        self._calls[token_id] = i + 1
        seq = self._scripts.get(token_id, [])
        return seq[i] if i < len(seq) else None


class FakeVenueTradeSource:
    """A network-free ``VenueTradeSource``: replays canned ``VenueTradeEvent``s per token."""

    def __init__(self, trades: Mapping[str, list[VenueTradeEvent]]) -> None:
        self._trades = {tok: list(seq) for tok, seq in trades.items()}

    async def stream_trades(self, token_id: str) -> AsyncIterator[VenueTradeEvent]:
        for trade in self._trades.get(token_id, []):
            yield trade


class _DefaultBookDepthSource:
    """Placeholder for the default public-``/book`` DEPTH adapter (implemented in E4-T2)."""

    def __init__(self, *a: Any, **k: Any) -> None:
        raise NotImplementedError("E4-T2")
