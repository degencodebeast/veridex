"""W1 — WS venue stream client + 10s PING heartbeat + offline fake (MM-R3 / WS build).

Every test drives the client through the injectable ``connect`` seam with an offline
``FakeVenueBookWs`` (canned verbatim frames + a scriptable disconnect) — NO network, NO
real time (``now_fn``/``sleep_fn`` injected). Mirrors the offline-fake discipline of
``tests/test_live_recorder_sources.py`` (``test_sources_are_injectable_no_network``).
"""

from __future__ import annotations

import ast
import asyncio
import itertools
import json
from pathlib import Path
from typing import Any

from tests.fixtures.polymarket_ws_frames import (
    BOOK_ASSET_ID,
    BOOK_FRAME,
    PRICE_CHANGE_FRAME,
    TICK_SIZE_CHANGE_FRAME,
)


async def _pump(cond: Any, *, limit: int = 50) -> None:
    """Yield to the event loop up to *limit* times until *cond()* is truthy (deterministic offline)."""
    for _ in range(limit):
        if cond():
            return
        await asyncio.sleep(0)


# --------------------------------------------------------------------------- W1-T1
async def test_stream_yields_parsed_frames_with_recv_ts() -> None:
    """Canned book+price_change frames → VenueBookFrames stamped with arrival recv_ts, in order."""
    from veridex.live_recorder.ws_book_source import (
        FakeVenueBookWs,
        FakeVenueWsConnection,
        stream_venue_book_frames,
    )

    conn = FakeVenueWsConnection([BOOK_FRAME, PRICE_CHANGE_FRAME])
    fake = FakeVenueBookWs([conn])
    clock = itertools.count(1000, 1000)

    async def block_sleep(_: float) -> None:
        await asyncio.Event().wait()  # heartbeat/backoff never fire in this test

    agen = stream_venue_book_frames(
        [BOOK_ASSET_ID], connect=fake.connect, now_fn=lambda: next(clock), sleep_fn=block_sleep
    ).__aiter__()
    first = await agen.__anext__()
    second = await agen.__anext__()
    await agen.aclose()

    assert [first.event_type, second.event_type] == ["book", "price_change"]
    assert first.recv_ts == 1000  # stamped by injected now_fn at arrival
    assert second.recv_ts == 2000
    assert first.token_id == BOOK_ASSET_ID  # book routes asset_id
    assert second.token_id is None  # price_change is per-change → frame-level None, W2 fans out
    assert first.payload["event_type"] == "book"
    assert second.payload["price_changes"][0]["side"] == "BUY"


# --------------------------------------------------------------------------- W1-T2
async def test_reconnect_on_disconnect_surfaces_gap(capsys: Any) -> None:
    """A scripted disconnect → exactly one reconnect (bounded backoff) + a surfaced gap; no secret logged."""
    from veridex.live_recorder.ws_book_source import (
        FakeVenueBookWs,
        FakeVenueWsConnection,
        stream_venue_book_frames,
    )

    secret = "SECRET_TOKEN_XYZ"
    conn1 = FakeVenueWsConnection([BOOK_FRAME], raise_exc=RuntimeError(f"boom {secret}"))
    conn2 = FakeVenueWsConnection([TICK_SIZE_CHANGE_FRAME])
    fake = FakeVenueBookWs([conn1, conn2])
    clock = itertools.count(1000, 1000)
    sleep_calls: list[float] = []

    async def sleep_fn(delay: float) -> None:
        sleep_calls.append(delay)
        if delay >= 5.0:  # heartbeat interval: block so it never fires here
            await asyncio.Event().wait()
        # backoff sleeps return immediately

    agen = stream_venue_book_frames(
        [BOOK_ASSET_ID], connect=fake.connect, now_fn=lambda: next(clock), sleep_fn=sleep_fn
    ).__aiter__()
    first = await agen.__anext__()  # book from conn1
    gap = await agen.__anext__()  # gap (conn1 raised)
    third = await agen.__anext__()  # tick_size_change from conn2
    await agen.aclose()

    assert first.event_type == "book"
    assert gap.event_type == "gap"
    assert gap.token_id is None
    assert third.event_type == "tick_size_change"
    assert fake.connect_calls == 2  # exactly one reconnect
    assert 1.0 in sleep_calls  # backoff started at 1s
    assert all(d <= 60.0 for d in sleep_calls)  # backoff bounded at 60s
    out = capsys.readouterr().out
    assert secret not in out  # scrubbed disconnect log
    assert "boom" not in out  # only the exception TYPE name is printed, never its value


# --------------------------------------------------------------------------- W1-T3
async def test_heartbeat_sends_ping_and_cancels_cleanly() -> None:
    """Injected clock → the socket receives one literal PING; on shutdown the heartbeat is cancelled cleanly."""
    from veridex.live_recorder.ws_book_source import (
        HEARTBEAT_INTERVAL_S,
        FakeVenueBookWs,
        FakeVenueWsConnection,
        stream_venue_book_frames,
    )

    conn = FakeVenueWsConnection([BOOK_FRAME])  # one frame, then idle
    fake = FakeVenueBookWs([conn])
    clock = itertools.count(1000, 1000)
    fired = {"hb": False}

    async def sleep_fn(delay: float) -> None:
        if delay == HEARTBEAT_INTERVAL_S and not fired["hb"]:
            fired["hb"] = True  # fire exactly ONE heartbeat, then block subsequent sleeps
            return
        await asyncio.Event().wait()

    before = {t for t in asyncio.all_tasks() if not t.done()}
    agen = stream_venue_book_frames(
        [BOOK_ASSET_ID], connect=fake.connect, now_fn=lambda: next(clock), sleep_fn=sleep_fn
    ).__aiter__()
    first = await agen.__anext__()  # book frame — receive loop is NOT blocked by the heartbeat
    assert first.event_type == "book"

    await _pump(lambda: bool(conn.pings))
    assert conn.pings == ["PING"]  # literal text PING at the injected cadence

    await agen.aclose()  # shutdown → heartbeat task cancelled
    await asyncio.sleep(0)
    leaked = {t for t in asyncio.all_tasks() if not t.done()} - before - {asyncio.current_task()}
    assert leaked == set()  # no leaked heartbeat task


# --------------------------------------------------------------------------- W1-T4
async def test_subscribe_payload_shape() -> None:
    """On connect the socket receives exactly ``{"assets_ids":[...],"type":"market"}`` (no custom_feature_enabled)."""
    from veridex.live_recorder.ws_book_source import (
        FakeVenueBookWs,
        FakeVenueWsConnection,
        stream_venue_book_frames,
    )

    conn = FakeVenueWsConnection([BOOK_FRAME])
    fake = FakeVenueBookWs([conn])
    clock = itertools.count(1000, 1000)

    async def block_sleep(_: float) -> None:
        await asyncio.Event().wait()

    agen = stream_venue_book_frames(
        ["A", "B"], connect=fake.connect, now_fn=lambda: next(clock), sleep_fn=block_sleep
    ).__aiter__()
    await agen.__anext__()  # subscribe is sent before the first frame is yielded
    await agen.aclose()

    assert conn.sent, "no subscribe frame sent"
    payload = json.loads(conn.sent[0])
    assert payload == {"assets_ids": ["A", "B"], "type": "market"}
    assert "custom_feature_enabled" not in payload


# --------------------------------------------------------------------------- W1-T5
def test_module_imports_no_network() -> None:
    """Importing the module performs no network + no eager aiohttp import at module scope (AST audit)."""
    import veridex.live_recorder.ws_book_source as mod

    source = Path(mod.__file__).read_text()
    tree = ast.parse(source)
    top_level_imports: set[str] = set()
    for node in tree.body:  # MODULE scope only — lazy imports inside functions are allowed
        if isinstance(node, ast.Import):
            top_level_imports.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            top_level_imports.add(node.module.split(".")[0])
    forbidden = {"httpx", "requests", "websocket", "websockets", "aiohttp"}
    assert not (top_level_imports & forbidden), f"module-scope network import: {top_level_imports & forbidden}"
    # aiohttp must still be used — but only lazily, inside a function body.
    assert "aiohttp" in source, "expected a lazy aiohttp import inside the connect coroutine"
