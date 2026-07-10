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

import pytest

from tests.fixtures.polymarket_ws_frames import (
    BOOK_ASSET_ID,
    BOOK_EMPTY_ASK_FRAME,
    BOOK_FRAME,
    BOOK_FRAME_BUY_ASSET,
    BOOK_FRAME_SELL_ASSET,
    PRICE_CHANGE_ADD_BID_FRAME,
    PRICE_CHANGE_ASSET_ID_BUY,
    PRICE_CHANGE_ASSET_ID_SELL,
    PRICE_CHANGE_BUY_AND_SELL_FRAME,
    PRICE_CHANGE_DELETE_BEST_ASK_FRAME,
    PRICE_CHANGE_FRAME,
    PRICE_CHANGE_MISMATCH_FRAME,
    PRICE_CHANGE_NO_MOVE_FRAME,
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


# =============================================================================== W2 helpers
def _frame(recv_ts: int, payload: dict[str, Any]) -> Any:
    """Wrap a raw fixture dict into a ``VenueBookFrame`` exactly as the W1 parser would."""
    from veridex.live_recorder.ws_book_source import VenueBookFrame

    event_type = payload["event_type"]
    token = payload.get("asset_id") if event_type in ("book", "tick_size_change") else None
    return VenueBookFrame(recv_ts=recv_ts, event_type=event_type, token_id=token, payload=payload)


# --------------------------------------------------------------------------- W2-a
def test_book_then_two_price_changes_evolves_mid() -> None:
    """W2-(a): book→price_change→price_change → correct evolving mid + change events at each recv_ts."""
    from veridex.live_recorder.ws_book_source import BookStateMaintainer

    m = BookStateMaintainer()
    seed = m.apply_frame(_frame(1000, BOOK_FRAME))  # best_bid .50 / best_ask .52 → mid .51
    add = m.apply_frame(_frame(2000, PRICE_CHANGE_ADD_BID_FRAME))  # +bid .51 → mid .515
    delete = m.apply_frame(_frame(3000, PRICE_CHANGE_DELETE_BEST_ASK_FRAME))  # -ask .52 → mid .52

    assert [c.mid for c in seed] == [pytest.approx(0.51)]
    assert seed[0].recv_ts == 1000
    assert [c.mid for c in add] == [pytest.approx(0.515)]
    assert add[0].recv_ts == 2000
    assert add[0].book_ts_ms == 123456789100  # frame timestamp coerced to int ms
    assert [c.mid for c in delete] == [pytest.approx(0.52)]
    assert delete[0].recv_ts == 3000
    # arrival_seq is monotonic across the whole stream (for W4 deterministic ordering).
    assert [seed[0].arrival_seq, add[0].arrival_seq, delete[0].arrival_seq] == [1, 2, 3]


# --------------------------------------------------------------------------- W2-b
def test_price_change_before_seed_is_gap_not_guessed() -> None:
    """W2-(b): a price_change with no prior book → NO mid change, NO guessed book state."""
    from veridex.live_recorder.ws_book_source import BookStateMaintainer

    m = BookStateMaintainer()
    changes = m.apply_frame(_frame(1000, PRICE_CHANGE_ADD_BID_FRAME))

    assert changes == []  # never guesses a mid from an unseeded book
    assert m.latest_snapshot(BOOK_ASSET_ID) is None  # no fabricated book


# --------------------------------------------------------------------------- W2-c
def test_size_zero_deletes_a_level() -> None:
    """W2-(c): a size=="0" change DELETES that price level (removed from the book, best moves)."""
    from veridex.live_recorder.ws_book_source import BookStateMaintainer

    m = BookStateMaintainer()
    m.apply_frame(_frame(1000, BOOK_FRAME))
    m.apply_frame(_frame(2000, PRICE_CHANGE_ADD_BID_FRAME))  # best_bid → .51 (so the delete frame validates)
    m.apply_frame(_frame(3000, PRICE_CHANGE_DELETE_BEST_ASK_FRAME))  # delete ask .52

    snap = m.latest_snapshot(BOOK_ASSET_ID)
    assert snap is not None
    ask_prices = [lvl.price for lvl in snap.asks]
    assert not any(p == pytest.approx(0.52) for p in ask_prices)  # .52 level gone
    assert snap.asks[0].price == pytest.approx(0.53)  # new best ask


# --------------------------------------------------------------------------- W2-d
def test_buy_and_sell_route_to_bid_and_ask() -> None:
    """W2-(d): a BUY change lands on the bid side, a SELL change lands on the ask side."""
    from veridex.live_recorder.ws_book_source import BookStateMaintainer

    m = BookStateMaintainer()
    m.apply_frame(_frame(1000, BOOK_FRAME))
    m.apply_frame(_frame(2000, PRICE_CHANGE_BUY_AND_SELL_FRAME))  # BUY .505 + SELL .515

    snap = m.latest_snapshot(BOOK_ASSET_ID)
    assert snap is not None
    assert any(lvl.price == pytest.approx(0.505) for lvl in snap.bids)  # BUY → bid
    assert any(lvl.price == pytest.approx(0.515) for lvl in snap.asks)  # SELL → ask


# --------------------------------------------------------------------------- W2-e
def test_multi_asset_price_change_routes_per_asset() -> None:
    """W2-(e): one price_change frame carrying multiple asset_ids fans out to the right BookStates."""
    from veridex.live_recorder.ws_book_source import BookStateMaintainer

    m = BookStateMaintainer()
    m.apply_frame(_frame(1000, BOOK_FRAME_BUY_ASSET))
    m.apply_frame(_frame(1000, BOOK_FRAME_SELL_ASSET))
    m.apply_frame(_frame(2000, PRICE_CHANGE_FRAME))  # BUY→buy-asset, SELL→sell-asset

    buy_snap = m.latest_snapshot(PRICE_CHANGE_ASSET_ID_BUY)
    sell_snap = m.latest_snapshot(PRICE_CHANGE_ASSET_ID_SELL)
    assert buy_snap is not None and sell_snap is not None  # neither book was invalidated
    assert any(lvl.price == pytest.approx(0.5) for lvl in buy_snap.bids)  # BUY change → buy asset bid
    assert any(lvl.price == pytest.approx(0.5) for lvl in sell_snap.asks)  # SELL change → sell asset ask


# --------------------------------------------------------------------------- W2-f
def test_best_bid_ask_mismatch_forces_gap_and_resnapshot() -> None:
    """W2-(f): computed best-bid/ask ≠ the frame's best_bid/best_ask → honest gap + discard book."""
    from veridex.live_recorder.ws_book_source import BookStateMaintainer

    m = BookStateMaintainer()
    m.apply_frame(_frame(1000, BOOK_FRAME))
    assert m.latest_snapshot(BOOK_ASSET_ID) is not None  # seeded

    changes = m.apply_frame(_frame(2000, PRICE_CHANGE_MISMATCH_FRAME))  # claims best_bid .99

    assert changes == []  # no mid emitted on a failed checksum
    assert m.latest_snapshot(BOOK_ASSET_ID) is None  # local book discarded → force re-snapshot


# --------------------------------------------------------------------------- W2-g
def test_tick_size_change_updates_tick_no_mid_change() -> None:
    """W2-(g): tick_size_change updates tick_size / min_price_increment and emits NO mid change."""
    from veridex.live_recorder.ws_book_source import BookStateMaintainer

    m = BookStateMaintainer()
    m.apply_frame(_frame(1000, BOOK_FRAME))
    changes = m.apply_frame(_frame(2000, TICK_SIZE_CHANGE_FRAME))  # 0.01 → 0.001

    assert changes == []  # a tick change is not a book delta → no mid move
    snap = m.latest_snapshot(BOOK_ASSET_ID)
    assert snap is not None
    assert snap.tick_size == pytest.approx(0.001)
    assert snap.min_price_increment == pytest.approx(0.001)


# --------------------------------------------------------------------------- W2-h
def test_empty_side_stays_empty_never_imputed() -> None:
    """W2-(h): a book seeded with an empty ask side keeps it empty (mid None, never imputed)."""
    from veridex.live_recorder.ws_book_source import BookStateMaintainer

    m = BookStateMaintainer()
    changes = m.apply_frame(_frame(1000, BOOK_EMPTY_ASK_FRAME))

    assert changes == []  # no ask → no mid → no emit (a mid is never fabricated)
    snap = m.latest_snapshot(BOOK_ASSET_ID)
    assert snap is not None
    assert snap.asks == ()  # empty side stays an empty tuple
    assert snap.bids  # bid side present


# --------------------------------------------------------------------------- W2-i
def test_delta_not_moving_mid_emits_nothing() -> None:
    """W2-(i): a delta that leaves the mid unchanged emits no BookMidChange."""
    from veridex.live_recorder.ws_book_source import BookStateMaintainer

    m = BookStateMaintainer()
    m.apply_frame(_frame(1000, BOOK_FRAME))  # mid .51
    changes = m.apply_frame(_frame(2000, PRICE_CHANGE_NO_MOVE_FRAME))  # deep bid .40, mid stays .51

    assert changes == []  # mid unchanged → nothing emitted
    snap = m.latest_snapshot(BOOK_ASSET_ID)
    assert snap is not None
    assert any(lvl.price == pytest.approx(0.40) for lvl in snap.bids)  # the level still merged in


# =============================================================================== W3
async def _drain(turns: int = 50) -> None:
    """Yield to the event loop *turns* times so a background consume task drains its canned frames."""
    for _ in range(turns):
        await asyncio.sleep(0)


async def _block_sleep(_: float) -> None:
    await asyncio.Event().wait()


# --------------------------------------------------------------------------- W3-1
async def test_ws_source_returns_fresh_cached_snapshot() -> None:
    """W3-(1): fresh frames → fetch_book returns the latest cached BookSnapshot (depth, not a mid); no network."""
    from veridex.live_recorder.sources import BookDepthSource
    from veridex.live_recorder.ws_book_source import (
        FakeVenueBookWs,
        FakeVenueWsConnection,
        WsBookDepthSource,
    )

    conn = FakeVenueWsConnection([BOOK_FRAME])
    fake = FakeVenueBookWs([conn])
    clock = {"now": 1000}
    src = WsBookDepthSource(
        [BOOK_ASSET_ID],
        connect=fake.connect,
        now_fn=lambda: clock["now"],
        sleep_fn=_block_sleep,
        max_cache_age_ms=10_000,
    )
    assert isinstance(src, BookDepthSource)  # satisfies the R3 Protocol drop-in
    await src.start()
    await _drain()

    snap = await src.fetch_book(BOOK_ASSET_ID)
    assert snap is not None
    assert snap.token_id == BOOK_ASSET_ID
    assert snap.bids and snap.asks  # depth preserved (not collapsed to a mid)
    assert snap.bids[0].price == pytest.approx(0.50)  # best bid (descending)
    assert snap.asks[0].price == pytest.approx(0.52)  # best ask (ascending)
    assert await src.fetch_book("never-seen-token") is None  # honest "no data"

    await src.aclose()
    assert fake.connect_calls >= 1  # drove the injected connect seam — no real network


# --------------------------------------------------------------------------- W3-2
async def test_ws_source_raises_stale_when_cache_ages_out() -> None:
    """W3-(2): advance the injected clock past max_cache_age_ms with no new frame → RAISE StaleVenueBook."""
    from veridex.live_recorder.ws_book_source import (
        FakeVenueBookWs,
        FakeVenueWsConnection,
        StaleVenueBook,
        WsBookDepthSource,
    )

    conn = FakeVenueWsConnection([BOOK_FRAME])
    fake = FakeVenueBookWs([conn])
    clock = {"now": 1000}
    src = WsBookDepthSource(
        [BOOK_ASSET_ID],
        connect=fake.connect,
        now_fn=lambda: clock["now"],
        sleep_fn=_block_sleep,
        max_cache_age_ms=5_000,
    )
    await src.start()
    await _drain()
    assert await src.fetch_book(BOOK_ASSET_ID) is not None  # fresh within the bound

    clock["now"] = 1000 + 5_001  # age the cache past the bound with no new frame
    with pytest.raises(StaleVenueBook):
        await src.fetch_book(BOOK_ASSET_ID)  # never serves the stale book as fresh

    await src.aclose()


# --------------------------------------------------------------------------- W3-3
async def test_ws_source_raises_across_reconnect_gap_until_fresh_book() -> None:
    """W3-(3): a disconnect → fetch_book RAISES until a FRESH book snapshot arrives (no stale bridge)."""
    from veridex.live_recorder.ws_book_source import (
        HEARTBEAT_INTERVAL_S,
        FakeVenueBookWs,
        FakeVenueWsConnection,
        StaleVenueBook,
        WsBookDepthSource,
    )

    conn1 = FakeVenueWsConnection([BOOK_FRAME], disconnect=True)
    conn2 = FakeVenueWsConnection([BOOK_FRAME])
    fake = FakeVenueBookWs([conn1, conn2])
    clock = {"now": 1000}
    release = asyncio.Event()

    async def sleep_fn(delay: float) -> None:
        if delay == HEARTBEAT_INTERVAL_S:
            await asyncio.Event().wait()  # heartbeat never fires
        else:
            await release.wait()  # backoff parks at the gap until the test releases it

    src = WsBookDepthSource(
        [BOOK_ASSET_ID],
        connect=fake.connect,
        now_fn=lambda: clock["now"],
        sleep_fn=sleep_fn,
        max_cache_age_ms=10_000,
    )
    await src.start()
    await _drain()  # seed from conn1, then disconnect → gap → park at backoff

    with pytest.raises(StaleVenueBook):
        await src.fetch_book(BOOK_ASSET_ID)  # gap window: no stale snapshot bridges it

    release.set()  # allow the reconnect
    await _drain()  # reconnect to conn2 → fresh book seeds again

    snap = await src.fetch_book(BOOK_ASSET_ID)
    assert snap is not None and snap.token_id == BOOK_ASSET_ID  # fresh book restores service

    await src.aclose()


# --------------------------------------------------------------------------- W3-4 (runner smoke)
_FULL_KEY = "1X2_PARTICIPANT_RESULT||"


def _runner_counter(start: int = 1_000) -> Any:
    state = {"now": start}

    def now() -> int:
        state["now"] += 1
        return state["now"]

    return now


async def test_ws_source_runner_records_gap_during_stale_window(tmp_path: Any) -> None:
    """W3-(4): wired into run_live_recorder, stale polls record RecorderGapEvents; fresh polls record snapshots."""
    from veridex.ingest.marketstate import MarketState
    from veridex.live_recorder.contracts import FillAssumptionConfig, LiveRecorderSessionMeta
    from veridex.live_recorder.recorder import LiveRecorder
    from veridex.live_recorder.runner import Decision, RecorderMarket, run_live_recorder
    from veridex.live_recorder.sources import FakeFvSource
    from veridex.live_recorder.ws_book_source import (
        FakeVenueBookWs,
        FakeVenueWsConnection,
        WsBookDepthSource,
    )

    now = _runner_counter()
    conn = FakeVenueWsConnection([BOOK_FRAME])  # one fresh book, then idle (cache ages out over polls)
    fake = FakeVenueBookWs([conn])
    src = WsBookDepthSource(
        [BOOK_ASSET_ID],
        connect=fake.connect,
        now_fn=now,
        sleep_fn=_block_sleep,
        max_cache_age_ms=25,  # small: fresh early polls, then the cache ages out mid-run
    )
    await src.start()
    await _drain()  # seed the book fresh before the runner starts polling

    matched = [RecorderMarket(100, "part1", "1X2|home|full", BOOK_ASSET_ID)]
    fv = FakeFvSource(
        [
            MarketState(
                fixture_id=100,
                tick_seq=0,
                ts=1_700_000_000,
                phase=1,
                markets={_FULL_KEY: {"stable_prob_bps": {"part1": 6000}, "suspended": False}},
                scores={},
            )
        ]
    )
    recorder = LiveRecorder(tmp_path, LiveRecorderSessionMeta(
        session_ts=1_700_000_000,
        endpoints={"venue": "wss://example.invalid"},
        tool_version="test-w3",
        config_hash="cfg",
        source_provenance={"venue": "poly-ws"},
        fixture_ids=(100,),
    ))
    config = FillAssumptionConfig(
        taker_fee_bps=10.0, fee_stress_multiplier=1.0, spread_assumption=0.0, slippage_assumption=0.0
    )

    def decide(_a: Any, _s: Any, _c: Any) -> Decision:
        return Decision(intent_kind="no_quote", reason_code="obs", no_quote_reason="observe_only")

    async def noop_sleep(_seconds: float) -> None:
        return None

    result = await run_live_recorder(
        matched=matched,
        fv_source=fv,
        book_source=src,
        recorder=recorder,
        decide_fn=decide,
        config=config,
        policy_hash="pol",
        now_fn=now,
        sleep_fn=noop_sleep,
        poll_interval_ms=5_000,
        minutes=30.0,
        max_polls=8,
    )
    recorder.close()
    await src.aclose()

    lines = [json.loads(line) for line in recorder.records_path.read_text().splitlines()]
    types = [e["event_type"] for e in lines]
    assert "VenueBookSnapshotEvent" in types  # fresh polls recorded real book snapshots
    assert "RecorderGapEvent" in types  # the stale window recorded honest gaps (never a stale book)
    assert result.gaps >= 1
    # every recorded snapshot came from the fresh cached book — never during a stale window.
    for event in lines:
        if event["event_type"] == "VenueBookSnapshotEvent":
            assert event["token_id"] == BOOK_ASSET_ID
