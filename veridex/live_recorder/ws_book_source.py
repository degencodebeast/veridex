"""WS venue book source for the live-recorder lane (MM-R3 WS build, W1).

W1 portion: the core Polymarket CLOB **market-channel** WebSocket stream client — connect,
subscribe, an app-level 10 s ``PING`` heartbeat, a reconnect/backoff loop with an explicit
gap signal, and minimal per-frame parsing (JSON-decode + ``event_type`` discriminant +
arrival ``recv_ts`` stamp). W2/W3 EXTEND this file (book-state merge, ``BookDepthSource``).

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
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from typing import Any, NamedTuple

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
    now_fn: Callable[[], float],
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
