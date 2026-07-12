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


def _levels_from_side(side: Any) -> tuple[BookLevel, ...]:
    """Parse one book side (``[{"price":..,"size":..}, ...]``) into ordered ``BookLevel``s.

    An empty/missing side yields an empty tuple — a level is NEVER fabricated (illiquid side is honest).
    """
    return tuple(
        BookLevel(price=float(level["price"]), size=float(level["size"]))
        for level in (side or [])
    )


def book_snapshot_from_json(
    book: Mapping[str, Any],
    *,
    token_id: str,
    venue_market_ref: str,
    tick_size: float,
    min_price_increment: float,
    is_snapshot: bool = True,
) -> BookSnapshot:
    """Map a public ``/book`` JSON payload to a full-depth :class:`BookSnapshot` (levels, never a mid).

    ``book['bids']`` / ``book['asks']`` are lists of ``{'price','size'}`` level dicts and the
    top-level timestamp is ``book['timestamp']`` (int-parsed) — mirrors
    ``scripts/maker/live_monitor.py::_mid_from_book`` but KEEPS the depth instead of collapsing.
    ``book_ts`` is venue-native MILLISECONDS (the ``/book`` timestamp is documented as ms in
    ``veridex/venues/_vendor/polymarket_clob/client.py:529``).

    Sides are normalised to CANONICAL ordering — ``asks`` ASCENDING and ``bids`` DESCENDING by
    price (mirrors how the vendored ``LOB`` keeps sorted arrays) — so a downstream depth walk
    can never under-count on a non-monotonic raw ``/book`` order.
    """
    raw_ts = book.get("timestamp")
    book_ts = int(raw_ts) if raw_ts is not None and raw_ts != "" else 0
    asks = tuple(sorted(_levels_from_side(book.get("asks")), key=lambda level: level.price))
    bids = tuple(sorted(_levels_from_side(book.get("bids")), key=lambda level: level.price, reverse=True))
    return BookSnapshot(
        token_id=token_id,
        venue_market_ref=venue_market_ref,
        book_ts=book_ts,
        tick_size=tick_size,
        min_price_increment=min_price_increment,
        bids=bids,
        asks=asks,
        is_snapshot=is_snapshot,
    )


# --------------------------------------------------------------------------- credential fail-closed + secret hygiene
def require_live_creds(env: Mapping[str, str]) -> tuple[str, str]:
    """Return ``(jwt, api_token)`` from *env*, or FAIL CLOSED (raise) if either is absent.

    Mirrors ``veridex.config.require_txline``: both credentials are REQUIRED and the guard raises
    BEFORE any network I/O when one is missing. The secret VALUES are never logged.
    """
    missing = [key for key in _REQUIRED_CRED_KEYS if not env.get(key)]
    if missing:
        raise ValueError(f"live creds missing: set {' and '.join(_REQUIRED_CRED_KEYS)} (absent: {', '.join(missing)})")
    return env[_REQUIRED_CRED_KEYS[0]], env[_REQUIRED_CRED_KEYS[1]]


def configured(env: Mapping[str, str]) -> bool:
    """Boolean-only telemetry: whether BOTH required creds are present (NEVER the secret values)."""
    return all(bool(env.get(key)) for key in _REQUIRED_CRED_KEYS)


def _scrub(text: str, *secrets: str) -> str:
    """Redact each secret VALUE from *text* before it is printed/written.

    Mirrors ``scripts/maker/live_monitor.py::_scrub_token`` / ``capture_and_pin.py::_scrub_token`` —
    scrubs the raw values (not trusting an exception's provenance), so a credential embedded in an
    error surfacing from OUTSIDE this module (e.g. a network-SDK URL/header) is still redacted.
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


# --------------------------------------------------------------------------- FV proof-reference sourcing
def _fv_from_state(state: MarketState, side: str) -> float | None:
    """Native-prob FV for *side* from a state's 1X2 full-match market, or ``None``.

    Mirrors ``scripts/maker/live_monitor.py::_fv_from_state`` (``stable_prob_bps[side] / 1e4``).
    """
    market = state.markets.get(_TXLINE_1X2_FULL_MARKET_KEY)
    if not market:
        return None
    stable_prob_bps = market.get("stable_prob_bps")
    if not stable_prob_bps:
        return None
    bps = stable_prob_bps.get(side)
    if bps is None:
        return None
    return float(bps) / _PROB_BPS_SCALE


def _suspended_from_state(state: MarketState) -> bool:
    """Whether the 1X2 full-match market is suspended (defaults to ``False`` when unstated)."""
    market = state.markets.get(_TXLINE_1X2_FULL_MARKET_KEY) or {}
    return bool(market.get("suspended", False))


def marketstate_to_fair_value(
    state: MarketState,
    side: str,
    market_ref: str,
    *,
    recv_ts: int,
    sequence_no: int,
    event_type: str = "FairValueEvent",
) -> FairValueEvent:
    """Map a streamed :class:`MarketState` to a :class:`FairValueEvent` with an HONEST proof reference.

    ``MarketState`` carries NO ``messageId`` (see ``veridex.ingest.marketstate.MarketState``), so
    the event is stamped ``message_id=None`` + ``proof_status="unavailable_no_message_id"`` — a proof
    status is NEVER fabricated. ``source_ts`` is the state's integer-seconds venue clock; ``recv_ts``
    is the recorder's integer-ms arrival clock.

    Raises ``ValueError`` if *side* has no FV in the state (a price is never invented).
    """
    fv = _fv_from_state(state, side)
    if fv is None:
        raise ValueError(f"no fair value for side {side!r} in state (fixture {state.fixture_id}); FV is never fabricated")
    return FairValueEvent(
        sequence_no=sequence_no,
        event_type=event_type,
        source_ts=int(state.ts),
        recv_ts=int(recv_ts),
        fixture_id=int(state.fixture_id),
        market_ref=market_ref,
        side=side,
        fv=fv,
        phase=int(state.phase),
        suspended=_suspended_from_state(state),
        message_id=None,
        proof_ts=None,
        proof_status="unavailable_no_message_id",
    )


async def resolve_proofs_batch(
    pairs: Iterable[tuple[str, int]],
    *,
    validate: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    base_url: str | None = None,
    creds: tuple[str, str] | None = None,
    client: Any = None,
) -> dict[tuple[str, int], str]:
    """OPTIONAL, REPORT-ONLY out-of-band proof resolver over collected ``(message_id, ts)`` pairs.

    Reuses ``veridex.ingest.txline_client.validate_odds`` + ``veridex.ingest.odds_proof.classify_proof``
    (both lazy-imported, so this module carries no import-time HTTP coupling) to classify each pair.
    This is a SIDE report only — it MUST NOT be bound into any event ``content_hash`` (SEC-005), and it
    runs only when the caller supplies pairs. ``validate`` is injectable so the report is testable offline.
    """
    from veridex.ingest.odds_proof import ERROR, classify_proof

    resolver = validate
    if resolver is None:
        from veridex.ingest.txline_client import validate_odds

        resolver = validate_odds

    report: dict[tuple[str, int], str] = {}
    for message_id, ts in pairs:
        try:
            resp = await resolver(message_id, ts, base_url=base_url, creds=creds, client=client)
            report[(message_id, ts)] = classify_proof(resp)
        except Exception:  # noqa: BLE001 — report-only: an error is honest "unknown", never voids anything
            report[(message_id, ts)] = ERROR
    return report


class _DefaultBookDepthSource:
    """Public Polymarket ``/book`` DEPTH source — lazy-``httpx`` GET (offline-safe to import).

    Mirrors ``scripts/maker/live_monitor.py::_DefaultMidSource``'s lazy-httpx pattern (hits the
    PUBLIC book endpoint — no wallet, no credential) but KEEPS the full ``bids``/``asks`` depth
    instead of collapsing to a mid. An empty side stays an empty tuple (never imputed). The
    ``client`` seam is INJECTABLE so tests drive it with a fake and touch no network.
    """

    def __init__(
        self,
        *,
        clob_url: str = _CLOB_URL,
        timeout_s: float = 10.0,
        tick_size: float = 0.01,
        min_price_increment: float = 0.01,
        venue_market_ref: str | None = None,
        client: Any = None,
    ) -> None:
        self._clob_url = clob_url
        self._timeout_s = timeout_s
        self._tick_size = tick_size
        self._min_price_increment = min_price_increment
        self._venue_market_ref = venue_market_ref
        self._client = client

    async def fetch_book(self, token_id: str) -> BookSnapshot | None:
        if self._client is not None:
            response = await self._client.get("/book", params={"token_id": token_id})
            response.raise_for_status()
            book = response.json()
        else:
            import httpx

            async with httpx.AsyncClient(base_url=self._clob_url, timeout=self._timeout_s) as http:
                response = await http.get("/book", params={"token_id": token_id})
                response.raise_for_status()
                book = response.json()
        return book_snapshot_from_json(
            book,
            token_id=token_id,
            venue_market_ref=self._venue_market_ref or token_id,
            tick_size=self._tick_size,
            min_price_increment=self._min_price_increment,
        )
