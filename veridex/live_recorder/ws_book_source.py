"""WS venue book source for the live-recorder lane (MM-R3 WS build, W1).

W1 portion: the core Polymarket CLOB **market-channel** WebSocket stream client — connect,
subscribe, an app-level 5 s ``PING`` heartbeat (the server drops the connection after ~10 s
of silence), a reconnect/backoff loop with an explicit gap signal, and minimal per-frame
parsing (JSON-decode + ``event_type`` discriminant + arrival ``recv_ts`` stamp). W2/W3
EXTEND this file (book-state merge, ``BookDepthSource``).

Trust-boundary discipline (mirrors ``veridex/live_recorder/sources.py`` and
``veridex/ingest/live_client.py``):

* NO network library is imported at module scope — ``aiohttp`` is imported **lazily** inside
  :func:`_default_connect`, so importing this module (and the offline test-suite) touches no
  network. The ``connect`` seam is INJECTABLE so tests drive it with :class:`FakeVenueBookWs`.
* Dual-clock honesty: each inbound frame is stamped ``recv_ts = int(now_fn())`` the INSTANT it
  is received (the receive-time clock R3 pins to ms), never a venue-native timestamp.
* Honest gaps: a disconnect/reconnect surfaces an explicit ``event_type="gap"`` frame — never a
  silent splice (mirrors ``record.py:157``).
* Secret hygiene: the market channel is PUBLIC (no auth), and the disconnect log prints only the
  exception TYPE name (never its value), so a secret embedded in an out-of-band error can never
  leak. Heartbeat ``PONG`` replies are consumed and ignored.

The reconnect loop mirrors ``scripts/maker/live_monitor.py::_DefaultFvSource.stream``
(1 s→60 s doubling backoff, reset-on-success, re-raise ``CancelledError``, scrubbed log).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, NamedTuple, cast

from veridex.live_recorder.contracts import BookLevel
from veridex.live_recorder.sources import BookSnapshot, book_snapshot_from_json

# Public Polymarket CLOB market-channel WS endpoint — PUBLIC (no wallet, no credential).
_MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# App-level heartbeat: the client MUST send the literal text ``PING`` (NOT JSON) on an interval
# or the server drops the socket after ~10 s. Sent at 5 s to stay safely under the drop timeout.
HEARTBEAT_INTERVAL_S = 5.0
_PING = "PING"

# Backoff bounds for the reconnect loop (mirrors ``_DefaultFvSource.stream``: 1 s → 60 s).
_BACKOFF_START_S = 1.0
_BACKOFF_MAX_S = 60.0

# Frame discriminator values we surface (``gap`` is our own honest disconnect marker).
_BOOK = "book"
_PRICE_CHANGE = "price_change"
_TICK_SIZE_CHANGE = "tick_size_change"
_GAP = "gap"
_KNOWN_EVENT_TYPES = frozenset({_BOOK, _PRICE_CHANGE, _TICK_SIZE_CHANGE})


class VenueBookFrame(NamedTuple):
    """One received market-channel frame, stamped with its local arrival clock.

    ``recv_ts`` is integer ms from the injected ``now_fn`` at the instant of receipt.
    ``token_id`` is the frame-level asset for ``book``/``tick_size_change`` (routed from
    ``asset_id``); for ``price_change`` the token is PER-change, so the frame-level ``token_id``
    is ``None`` and W2 fans each change out to its own book. ``event_type == "gap"`` is our
    honest disconnect marker (empty ``payload``). ``payload`` is the raw decoded JSON dict.
    """

    recv_ts: int
    event_type: str
    token_id: str | None
    payload: dict[str, Any]


class WsInbound(NamedTuple):
    """A transport-neutral inbound message: ``kind`` in {"text","closed","error"} + text ``data``.

    Keeps ``aiohttp.WSMsgType`` out of module scope — the real adapter translates aiohttp
    ``WSMessage``s into this shape, and the offline fake produces it directly.
    """

    kind: str
    data: str | None


class _VenueWsDisconnected(Exception):
    """Raised internally when the socket returns a non-text (closed/error) message → reconnect."""


# --------------------------------------------------------------------------- real connect seam
class _AiohttpConn:
    """Adapter over an aiohttp ``ClientWebSocketResponse`` exposing the ``connect`` seam contract.

    Owns both the session and the ws so :meth:`close` tears down both. ``aiohttp`` is referenced
    only here (translating ``WSMessage`` → :class:`WsInbound`) and is lazy-imported by
    :func:`_default_connect`, so module import stays network-library-free.
    """

    def __init__(self, session: Any, ws: Any) -> None:
        self._session = session
        self._ws = ws

    async def send_str(self, data: str) -> None:
        await self._ws.send_str(data)

    async def receive(self) -> WsInbound:
        import aiohttp  # noqa: PLC0415 — lazy: keep module import network-free

        msg = await self._ws.receive()
        if msg.type == aiohttp.WSMsgType.TEXT:
            return WsInbound("text", msg.data)
        if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSE):
            return WsInbound("closed", None)
        return WsInbound("error", None)

    async def close(self) -> None:
        try:
            await self._ws.close()
        finally:
            await self._session.close()


async def _default_connect(ws_url: str) -> _AiohttpConn:
    """Open a real aiohttp market-channel WS connection (``aiohttp`` lazy-imported here)."""
    import aiohttp  # noqa: PLC0415 — lazy network import (CON-010): keep module import network-free

    session = aiohttp.ClientSession()
    try:
        ws = await session.ws_connect(ws_url)
    except BaseException:
        await session.close()
        raise
    return _AiohttpConn(session, ws)


# --------------------------------------------------------------------------- frame parsing
def _parse_frame(text: str, recv_ts: int) -> VenueBookFrame | None:
    """JSON-decode one text frame → a :class:`VenueBookFrame`, or ``None`` to ignore it.

    Ignores non-JSON text (e.g. the server's literal ``PONG`` heartbeat reply) and any frame
    whose ``event_type`` is not one we surface. W1 does MINIMAL parsing only (decode +
    discriminate + ``recv_ts`` stamp); the book/delta mapping is W2.
    """
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return None  # non-JSON (e.g. a "PONG" reply) → consumed and ignored
    if not isinstance(payload, dict):
        return None
    event_type = payload.get("event_type")
    if event_type not in _KNOWN_EVENT_TYPES:
        return None
    # book / tick_size_change carry a frame-level asset_id; price_change is per-change (→ None).
    token_id = payload.get("asset_id") if event_type in (_BOOK, _TICK_SIZE_CHANGE) else None
    return VenueBookFrame(recv_ts=recv_ts, event_type=event_type, token_id=token_id, payload=payload)


def _gap_frame(recv_ts: int) -> VenueBookFrame:
    """An explicit honest gap marker emitted on every disconnect (never a silent splice)."""
    return VenueBookFrame(recv_ts=recv_ts, event_type=_GAP, token_id=None, payload={})


# --------------------------------------------------------------------------- heartbeat
async def _run_heartbeat(conn: Any, sleep_fn: Callable[[float], Awaitable[None]]) -> None:
    """Send the literal ``PING`` every ``HEARTBEAT_INTERVAL_S`` (interval driven by ``sleep_fn``).

    Cancels cleanly (re-raises ``CancelledError``) on reconnect/shutdown. A send failure closes the
    connection so the receive loop unblocks and triggers a reconnect, then the task exits.
    """
    while True:
        await sleep_fn(HEARTBEAT_INTERVAL_S)
        try:
            await conn.send_str(_PING)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a PING send failure → force reconnect, never leak the error
            with contextlib.suppress(Exception):  # best-effort teardown
                await conn.close()
            return


# --------------------------------------------------------------------------- stream client
async def stream_venue_book_frames(
    assets_ids: Iterable[str],
    *,
    ws_url: str = _MARKET_WS_URL,
    connect: Callable[[str], Awaitable[Any]] | None = None,
    now_fn: Callable[[], int],
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> AsyncIterator[VenueBookFrame]:
    """Stream Polymarket market-channel frames as :class:`VenueBookFrame`s (arrival-stamped).

    Connects via the injectable ``connect`` seam (defaults to a lazy-``aiohttp`` connection),
    subscribes with ``{"assets_ids":[...],"type":"market"}``, runs a concurrent app-level ``PING``
    heartbeat, and reconnects on any disconnect with a 1 s→60 s doubling backoff (reset on the
    first frame of a healthy connection). ``CancelledError`` is re-raised; every disconnect logs
    only the exception TYPE name (never its value) and surfaces an explicit ``gap`` frame.

    Args:
        assets_ids: Token ids to subscribe (the market-channel ``assets_ids`` set).
        ws_url: Market-channel WS URL override.
        connect: Injectable ``async (ws_url) -> conn`` seam. ``conn`` must expose
            ``send_str(str)``, ``receive() -> WsInbound``, and ``close()``. ``None`` (production)
            builds a real aiohttp connection lazily.
        now_fn: Receive-time clock; ``recv_ts = int(now_fn())`` stamps each frame at arrival (ms).
        sleep_fn: Injectable sleep driving BOTH the heartbeat interval and the reconnect backoff
            (deterministic offline).
    """
    connector = connect if connect is not None else _default_connect
    assets = list(assets_ids)
    subscribe_msg = json.dumps({"assets_ids": assets, "type": "market"})
    backoff = _BACKOFF_START_S

    while True:
        try:
            conn = await connector(ws_url)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — reconnect on any connect error; log TYPE only
            print(f"venue ws connect failed: {type(exc).__name__}")
            yield _gap_frame(int(now_fn()))
            await sleep_fn(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX_S)
            continue

        heartbeat: asyncio.Task[None] | None = None
        try:
            await conn.send_str(subscribe_msg)
            heartbeat = asyncio.create_task(_run_heartbeat(conn, sleep_fn))
            while True:
                msg = await conn.receive()
                if msg.kind != "text" or msg.data is None:
                    raise _VenueWsDisconnected(msg.kind)
                recv_ts = int(now_fn())
                frame = _parse_frame(msg.data, recv_ts)
                if frame is not None:
                    backoff = _BACKOFF_START_S  # reset-on-success (a healthy frame arrived)
                    yield frame
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — reconnect on any stream error; log TYPE only
            print(f"venue ws disconnected: {type(exc).__name__}")
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):  # swallow on teardown
                    await heartbeat
            with contextlib.suppress(Exception):  # best-effort teardown
                await conn.close()

        # Explicit honest gap on every disconnect, then a bounded backoff before reconnecting.
        yield _gap_frame(int(now_fn()))
        await sleep_fn(backoff)
        backoff = min(backoff * 2, _BACKOFF_MAX_S)


# --------------------------------------------------------------------------- offline fake
class FakeVenueWsConnection:
    """A scripted offline WS connection: yields canned frames, then a disconnect or idle-block.

    Mirrors the ``FakeBookDepthSource`` offline style (``sources.py:109-120``). Records every
    ``send_str`` (subscribe + ``PING``s) for assertions. After the canned frames it either raises
    ``raise_exc``, returns a ``closed`` message (``disconnect=True``), or blocks until :meth:`close`
    (an idle healthy socket). No network, no real time.
    """

    def __init__(
        self,
        frames: Iterable[dict[str, Any]],
        *,
        disconnect: bool = False,
        raise_exc: BaseException | None = None,
    ) -> None:
        self._frames = [json.dumps(f) for f in frames]
        self._i = 0
        self._disconnect = disconnect
        self._raise_exc = raise_exc
        self._closed_event = asyncio.Event()
        self.sent: list[str] = []
        self.pings: list[str] = []
        self.closed = False

    async def send_str(self, data: str) -> None:
        self.sent.append(data)
        if data == _PING:
            self.pings.append(data)

    async def receive(self) -> WsInbound:
        if self._i < len(self._frames):
            text = self._frames[self._i]
            self._i += 1
            return WsInbound("text", text)
        # Frames exhausted: scripted raise / clean close / idle-block-until-closed.
        if self._raise_exc is not None:
            exc = self._raise_exc
            self._raise_exc = None
            raise exc
        if self._disconnect:
            self._disconnect = False
            return WsInbound("closed", None)
        await self._closed_event.wait()  # idle healthy socket → unblocked by close()
        return WsInbound("closed", None)

    async def close(self) -> None:
        self.closed = True
        self._closed_event.set()


class FakeVenueBookWs:
    """An injectable ``connect`` seam over a scripted list of :class:`FakeVenueWsConnection`s.

    Each ``await fake.connect(ws_url)`` hands out the next scripted connection (further reconnects
    get a fresh idle connection). ``connect_calls`` counts reconnects for assertions.
    """

    def __init__(self, connections: Iterable[FakeVenueWsConnection]) -> None:
        self._connections = list(connections)
        self._idx = 0
        self.connect_calls = 0

    async def connect(self, ws_url: str) -> FakeVenueWsConnection:
        self.connect_calls += 1
        i = self._idx
        self._idx += 1
        if i < len(self._connections):
            return self._connections[i]
        extra = FakeVenueWsConnection([])  # further reconnects → an idle healthy socket
        self._connections.append(extra)
        return extra


# =========================================================================== W2 book-state
# Book-state maintainer: seed from a full ``book`` snapshot, merge incremental ``price_change``
# deltas onto the retained book (the vendored ``LOB.update`` does NOT merge — this is net-new),
# self-validate every merge against the frame's ``best_bid``/``best_ask`` checksum, and emit a
# ``BookMidChange`` ONLY when the mid actually moves. On a failed checksum / stale timestamp /
# a delta-before-seed, the local book is DISCARDED and a fresh ``book`` re-snapshot is required
# — the maintainer never silently drifts or guesses state.


class BookMidChange(NamedTuple):
    """A mid-price change emitted by :class:`BookStateMaintainer` (only when the mid truly moves).

    ``recv_ts`` is the frame's local arrival clock (ms); ``arrival_seq`` is a maintainer-monotonic
    counter (deterministic ordering for W4); ``book_ts_ms`` is the frame ``timestamp`` coerced to
    int ms; ``best_bid``/``best_ask`` are the maintainer's own computed top-of-book (which, post
    self-validation, equal the frame's).
    """

    token_id: str
    recv_ts: int
    arrival_seq: int
    mid: float
    book_ts_ms: int
    best_bid: float | None
    best_ask: float | None


class TokenResolution(NamedTuple):
    """Per-token config threaded from resolution (frames carry no tick/venue ref).

    If only a ``tick_size`` is known, the caller sets ``min_price_increment = tick_size``.
    """

    tick_size: float
    min_price_increment: float
    venue_market_ref: str


class StaleVenueBook(Exception):
    """Raised by :meth:`WsBookDepthSource.fetch_book` when the cached book is stale or gapped.

    A typed exception (never a ``None``) so ``run_live_recorder``'s per-market
    ``gather(return_exceptions=True)`` path records an honest ``RecorderGapEvent`` instead of
    silently serving a stale snapshot as a fresh observation.
    """


def _coerce_ms(raw: Any) -> int:
    """Coerce a venue ``timestamp`` (13-digit ms string / int) to int ms; absent → 0."""
    if raw is None or raw == "":
        return 0
    return int(raw)


class BookState:
    """A retained per-token order book: ``price -> size`` maps plus the derived top-of-book.

    Sizes are kept as parsed floats; a deleted level is removed from the map entirely (a
    ``size == 0`` change). :meth:`invalidate` discards the whole book (forcing a fresh
    ``book`` re-snapshot) — used on a gap, a failed best_bid/ask checksum, or a stale frame.
    """

    def __init__(
        self,
        token_id: str,
        *,
        tick_size: float,
        min_price_increment: float,
        venue_market_ref: str,
    ) -> None:
        self.token_id = token_id
        self.tick_size = tick_size
        self.min_price_increment = min_price_increment
        self.venue_market_ref = venue_market_ref
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.seeded = False
        self.last_ts = 0
        self.best_bid: float | None = None
        self.best_ask: float | None = None
        self.mid: float | None = None

    def recompute(self) -> None:
        """Recompute best-bid (max bid price) / best-ask (min ask price) / mid from the maps."""
        self.best_bid = max(self.bids) if self.bids else None
        self.best_ask = min(self.asks) if self.asks else None
        if self.best_bid is not None and self.best_ask is not None:
            self.mid = (self.best_bid + self.best_ask) / 2.0
        else:
            self.mid = None  # a one-sided (or empty) book has NO mid — never imputed

    def invalidate(self) -> None:
        """Discard the local book → require a fresh ``book`` re-snapshot (an honest resync)."""
        self.bids.clear()
        self.asks.clear()
        self.seeded = False
        self.best_bid = self.best_ask = self.mid = None

    def to_snapshot(self) -> BookSnapshot:
        """Render the retained book as a full-depth :class:`BookSnapshot` (canonical ordering)."""
        bids = tuple(
            sorted(
                (BookLevel(price=p, size=s) for p, s in self.bids.items() if s > 0.0),
                key=lambda lvl: lvl.price,
                reverse=True,
            )
        )
        asks = tuple(
            sorted(
                (BookLevel(price=p, size=s) for p, s in self.asks.items() if s > 0.0),
                key=lambda lvl: lvl.price,
            )
        )
        return BookSnapshot(
            token_id=self.token_id,
            venue_market_ref=self.venue_market_ref,
            book_ts=self.last_ts,
            tick_size=self.tick_size,
            min_price_increment=self.min_price_increment,
            bids=bids,
            asks=asks,
            is_snapshot=True,
        )


class BookStateMaintainer:
    """Maintains a :class:`BookState` per ``token_id`` and emits :class:`BookMidChange`s.

    Feed it :class:`VenueBookFrame`s (from :func:`stream_venue_book_frames`) via
    :meth:`apply_frame`. ``tick_size``/``min_price_increment``/``venue_market_ref`` come from an
    optional ``resolutions`` map (frames carry only a ``token_id``); an unresolved token falls
    back to a ``0.01`` tick and the frame's ``market`` as the venue ref.
    """

    def __init__(self, *, resolutions: Mapping[str, TokenResolution] | None = None) -> None:
        self._resolutions = dict(resolutions or {})
        self._books: dict[str, BookState] = {}
        self._seq = 0

    def latest_snapshot(self, token_id: str) -> BookSnapshot | None:
        """The current full-depth snapshot for a token, or ``None`` if unseeded/invalidated."""
        state = self._books.get(token_id)
        if state is None or not state.seeded:
            return None
        return state.to_snapshot()

    def state(self, token_id: str) -> BookState | None:
        """The raw :class:`BookState` for a token (or ``None`` if never referenced)."""
        return self._books.get(token_id)

    def apply_frame(self, frame: VenueBookFrame) -> list[BookMidChange]:
        """Apply one frame; return the (0+) mid changes it produced (empty on a gap/resync)."""
        event_type = frame.event_type
        if event_type == _GAP:
            for state in self._books.values():
                state.invalidate()  # a disconnect invalidates EVERY local book → force resnapshot
            return []
        if event_type == _BOOK:
            return self._apply_book(frame)
        if event_type == _PRICE_CHANGE:
            return self._apply_price_change(frame)
        if event_type == _TICK_SIZE_CHANGE:
            self._apply_tick_size(frame)
            return []
        return []

    # ------------------------------------------------------------------ internals
    def _get_or_create(self, token_id: str, *, market: str | None = None) -> BookState:
        state = self._books.get(token_id)
        if state is None:
            res = self._resolutions.get(token_id)
            if res is not None:
                state = BookState(
                    token_id,
                    tick_size=res.tick_size,
                    min_price_increment=res.min_price_increment,
                    venue_market_ref=res.venue_market_ref,
                )
            else:
                state = BookState(
                    token_id,
                    tick_size=0.01,
                    min_price_increment=0.01,
                    venue_market_ref=market or token_id,
                )
            self._books[token_id] = state
        return state

    def _apply_book(self, frame: VenueBookFrame) -> list[BookMidChange]:
        token_id = frame.token_id
        if token_id is None:
            return []
        payload = frame.payload
        state = self._get_or_create(token_id, market=payload.get("market"))
        # Tolerate the (documented-but-erroneous) buys/sells key spelling; canonical is bids/asks.
        book_json = dict(payload)
        if "bids" not in book_json and "buys" in book_json:
            book_json["bids"] = book_json["buys"]
        if "asks" not in book_json and "sells" in book_json:
            book_json["asks"] = book_json["sells"]
        snap = book_snapshot_from_json(
            book_json,
            token_id=token_id,
            venue_market_ref=state.venue_market_ref,
            tick_size=state.tick_size,
            min_price_increment=state.min_price_increment,
        )
        old_mid = state.mid  # a re-snapshot with the SAME mid must not emit a spurious change
        state.bids = {lvl.price: lvl.size for lvl in snap.bids}
        state.asks = {lvl.price: lvl.size for lvl in snap.asks}
        state.seeded = True
        state.last_ts = _coerce_ms(payload.get("timestamp"))
        state.recompute()
        return self._emit_if_moved(state, frame.recv_ts, old_mid)

    def _apply_price_change(self, frame: VenueBookFrame) -> list[BookMidChange]:
        payload = frame.payload
        ts = _coerce_ms(payload.get("timestamp"))
        # Fan the top-level changes out per asset_id (one frame can carry MULTIPLE assets).
        by_asset: dict[str, list[dict[str, Any]]] = {}
        for change in payload.get("price_changes", []):
            asset_id = change.get("asset_id")
            if asset_id is None:
                continue
            by_asset.setdefault(asset_id, []).append(change)

        results: list[BookMidChange] = []
        for asset_id, changes in by_asset.items():
            state = self._books.get(asset_id)
            if state is None or not state.seeded:
                continue  # a delta before any book → resync signal, NEVER a guessed book
            if ts < state.last_ts:
                state.invalidate()  # out-of-order / stale timestamp → honest gap + resnapshot
                continue
            old_mid = state.mid
            last_applied: dict[str, Any] | None = None
            malformed = False
            for change in changes:
                side = str(change.get("side", "")).upper()
                if side == "BUY":
                    book = state.bids
                elif side == "SELL":
                    book = state.asks
                else:
                    continue  # unknown side → never guess which book it belongs to
                try:
                    price = float(change["price"])
                    size = float(change["size"])
                except (KeyError, TypeError, ValueError):
                    malformed = True  # missing/non-numeric price or size → fail closed on this book
                    break
                if size == 0.0:
                    book.pop(price, None)  # size=="0" DELETES the level
                else:
                    book[price] = size
                last_applied = change
            if malformed:
                state.invalidate()  # malformed change → discard the book, force a fresh re-snapshot
                continue
            state.last_ts = ts
            state.recompute()
            # Self-validate the merge against the frame's own best_bid/best_ask checksum.
            if last_applied is not None and not self._topofbook_matches(state, last_applied):
                state.invalidate()  # checksum divergence → honest gap + force re-snapshot
                continue
            results.extend(self._emit_if_moved(state, frame.recv_ts, old_mid))
        return results

    def _apply_tick_size(self, frame: VenueBookFrame) -> None:
        token_id = frame.token_id
        if token_id is None:
            return
        state = self._get_or_create(token_id, market=frame.payload.get("market"))
        new_tick = frame.payload.get("new_tick_size")
        if new_tick is not None and new_tick != "":
            tick = float(new_tick)
            state.tick_size = tick
            state.min_price_increment = tick  # min price increment tracks the tick size

    @staticmethod
    def _side_matches(computed: float | None, raw: Any) -> bool:
        """Whether a computed best price equals the frame's claim (``"0"``/empty == no side)."""
        if raw is None or raw == "":
            return computed is None
        value = float(raw)
        if value <= 0.0:  # "0" is the venue's sentinel for an empty side
            return computed is None
        if computed is None:
            return False
        return math.isclose(computed, value, abs_tol=1e-9)

    def _topofbook_matches(self, state: BookState, change: dict[str, Any]) -> bool:
        return self._side_matches(state.best_bid, change.get("best_bid")) and self._side_matches(
            state.best_ask, change.get("best_ask")
        )

    def _emit_if_moved(self, state: BookState, recv_ts: int, old_mid: float | None) -> list[BookMidChange]:
        if state.mid is None or state.mid == old_mid:
            return []  # no mid, or the mid did not actually move → emit nothing
        self._seq += 1
        return [
            BookMidChange(
                token_id=state.token_id,
                recv_ts=recv_ts,
                arrival_seq=self._seq,
                mid=state.mid,
                book_ts_ms=state.last_ts,
                best_bid=state.best_bid,
                best_ask=state.best_ask,
            )
        ]


# =========================================================================== W3 BookDepthSource
# ``WsBookDepthSource`` is the R3 ``BookDepthSource`` drop-in: a background task consumes the WS
# frame stream through a ``BookStateMaintainer``, keeping a per-token latest ``BookSnapshot`` +
# its arrival ``recv_ts`` + connection/gap state. ``fetch_book`` NEVER serves a stale/gapped book
# as fresh — it raises :class:`StaleVenueBook` so the runner records an honest gap.


@dataclass
class _CacheEntry:
    """The per-token freshness cache: the last snapshot, its arrival clock, and its gap state."""

    snapshot: BookSnapshot | None
    last_recv_ts: int
    gapped: bool


def _frame_token_ids(frame: VenueBookFrame) -> set[str]:
    """The set of token_ids a non-gap frame touches (per-change for a ``price_change``)."""
    event_type = frame.event_type
    if event_type in (_BOOK, _TICK_SIZE_CHANGE):
        return {frame.token_id} if frame.token_id is not None else set()
    if event_type == _PRICE_CHANGE:
        return {
            change["asset_id"]
            for change in frame.payload.get("price_changes", [])
            if change.get("asset_id") is not None
        }
    return set()


class WsBookDepthSource:
    """A WS-backed :class:`~veridex.live_recorder.sources.BookDepthSource` (R3 drop-in).

    Freshness is POLL-QUANTIZED (a per-token latest-snapshot cache), NOT sub-2s — the sub-2s
    arrival-lead question is W4's separate lead monitor. Start/stop is via :meth:`start` /
    :meth:`aclose` or the async-context-manager protocol.

    ``fetch_book`` (the load-bearing honesty surface):

    * never-seen token → ``None`` (the runner silently skips — an honest "no data").
    * disconnected / mid-resync / cache older than ``max_cache_age_ms`` → RAISE
      :class:`StaleVenueBook` (the runner records a ``RecorderGapEvent``); NEVER the old book.
    * fresh (within ``max_cache_age_ms``, connected) → the latest cached :class:`BookSnapshot`.
    """

    def __init__(
        self,
        assets_ids: Iterable[str],
        *,
        connect: Callable[[str], Awaitable[Any]],
        now_fn: Callable[[], int],
        max_cache_age_ms: int,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
        ws_url: str = _MARKET_WS_URL,
        resolutions: Mapping[str, TokenResolution] | None = None,
    ) -> None:
        self._assets = list(assets_ids)
        self._connect = connect
        self._now_fn = now_fn
        self._max_cache_age_ms = max_cache_age_ms
        self._sleep_fn = sleep_fn
        self._ws_url = ws_url
        self._maintainer = BookStateMaintainer(resolutions=resolutions)
        self._entries: dict[str, _CacheEntry] = {}
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Launch the background consume task (idempotent)."""
        if self._task is None:
            self._task = asyncio.create_task(self._consume())

    async def aclose(self) -> None:
        """Cancel the background task cleanly (no leaked task, no partial state served)."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def __aenter__(self) -> WsBookDepthSource:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _consume(self) -> None:
        # The stream is an async GENERATOR (declared ``AsyncIterator`` at W1); cast so its
        # ``aclose`` is visible for deterministic teardown on task cancellation.
        agen = cast(
            AsyncGenerator[VenueBookFrame, None],
            stream_venue_book_frames(
                self._assets,
                ws_url=self._ws_url,
                connect=self._connect,
                now_fn=self._now_fn,
                sleep_fn=self._sleep_fn,
            ),
        )
        try:
            async for frame in agen:
                self._on_frame(frame)
        finally:
            await agen.aclose()

    def _on_frame(self, frame: VenueBookFrame) -> None:
        try:
            self._process_frame(frame)
        except Exception:  # noqa: BLE001 — fail closed: ANY frame-processing error gaps EVERY cache
            # A malformed/unexpected frame must NEVER leave a fresh-returning cache. Gap every known
            # token so ``fetch_book`` raises ``StaleVenueBook`` until a fresh book re-seeds it, and
            # swallow so the consume task survives to keep processing (a subsequent book re-seeds).
            for entry in self._entries.values():
                entry.gapped = True

    def _process_frame(self, frame: VenueBookFrame) -> None:
        self._maintainer.apply_frame(frame)
        if frame.event_type == _GAP:
            # A disconnect gaps EVERY known token — fetch raises until a fresh book re-seeds it.
            for entry in self._entries.values():
                entry.gapped = True
            return
        for token_id in _frame_token_ids(frame):
            snap = self._maintainer.latest_snapshot(token_id)
            if snap is not None:
                self._entries[token_id] = _CacheEntry(snapshot=snap, last_recv_ts=frame.recv_ts, gapped=False)
            else:
                # Token touched but the maintainer has no valid book (resync). Gap a KNOWN token;
                # leave a never-seeded token unknown (fetch returns None → runner skips).
                existing = self._entries.get(token_id)
                if existing is not None:
                    existing.gapped = True

    async def fetch_book(self, token_id: str) -> BookSnapshot | None:
        """Return the fresh cached book, ``None`` if never seen, or raise on a stale/gapped book."""
        entry = self._entries.get(token_id)
        if entry is None or entry.snapshot is None:
            return None  # never seen → honest "no data" (runner skips)
        if entry.gapped:
            raise StaleVenueBook("venue book is disconnected / mid-resync — refusing to serve a stale snapshot")
        age = int(self._now_fn()) - entry.last_recv_ts
        if age > self._max_cache_age_ms:
            raise StaleVenueBook(
                f"venue book cache is stale ({age}ms > {self._max_cache_age_ms}ms) — refusing to serve as fresh"
            )
        return entry.snapshot
