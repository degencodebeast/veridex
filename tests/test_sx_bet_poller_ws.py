"""Offline tests for the SX Centrifugo order-book WebSocket source (``scripts/maker/sx_bet_poller.py``).

Every test drives the WS source / its frame stream through the injectable ``connect`` seam with an
offline :class:`FakeSxWs` (canned Centrifugo reply frames + a scriptable disconnect) and an injected
``token_provider`` — NO network, NO real time, NO SX API key value. Mirrors the R3 WS-build discipline
in ``tests/test_live_recorder_ws_book_source.py`` (canned frames → parsed mids, disconnect → gap,
fail-closed key, module-scope no-network import audit).
"""

from __future__ import annotations

import ast
import asyncio
import itertools
from pathlib import Path
from typing import Any

from scripts.maker.sx_bet_poller import (
    FakeSxWs,
    FakeSxWsConnection,
    SxOrderBookFrame,
    _SxMarketBook,
    _WsSxBookSource,
    resolve_sx_api_key,
    stream_sx_order_book_frames,
    sx_quote_from_orders,
    sx_ws_configured,
)

_MARKET = "0x04b9af76dfb92e71500975db77b1de0bb32a0b2413f1b3facbb25278987519a7"
_CHANNEL = f"order_book:market_{_MARKET}"
_SCALE = 100_000_000_000_000_000_000  # 1e20 as an int, the SX percentageOdds scale

# --- 1e6 USDC-denominated size (matches USDC_DECIMALS in the poller) ---
_SIZE = "500000000"  # 500.0 USDC


def _order(*, order_hash: str, pct: int, maker_outcome_one: bool, status: str = "ACTIVE",
           update_time: int = 1000, size: str = _SIZE) -> dict[str, Any]:
    """A minimal SX order dict (the fields the book math + maintainer read)."""
    return {
        "orderHash": order_hash,
        "marketHash": _MARKET,
        "status": status,
        "percentageOdds": str(pct),
        "totalBetSize": size,
        "isMakerBettingOutcomeOne": maker_outcome_one,
        "updateTime": str(update_time),
    }


def _push(orders: list[dict[str, Any]], *, channel: str = _CHANNEL) -> dict[str, Any]:
    """A Centrifugo publication reply frame carrying an order-book ``data`` array."""
    return {"push": {"channel": channel, "pub": {"data": orders}}}


# A two-sided (uncrossed) book for outcome-one:
#   ASK — maker bets outcome TWO (offers outcome-one to takers): taker prob = (1e20 - 40e18)/1e20 = 0.60.
#   BID — maker bets outcome ONE (offers outcome-two): outcome-one bid = pct/1e20 = 55e18/1e20 = 0.55.
# → bid 0.55 < ask 0.60, mid 0.575.
_ASK_ORDER = _order(order_hash="0xask", pct=40 * 10**18, maker_outcome_one=False, update_time=1000)
_BID_ORDER = _order(order_hash="0xbid", pct=55 * 10**18, maker_outcome_one=True, update_time=1000)


def _expected_mid(orders: list[dict[str, Any]]) -> float | None:
    return sx_quote_from_orders(orders).mid


# --------------------------------------------------------------------------- key hygiene
def test_resolve_sx_api_key_fails_closed_when_absent() -> None:
    """No key in env → raise BEFORE any I/O (fail-closed); a present key resolves by name."""
    import pytest

    with pytest.raises(ValueError, match="SX WS requires an API key"):
        resolve_sx_api_key({})
    assert resolve_sx_api_key({"SX_BET_API_KEY": "secret-value"}) == "secret-value"
    assert resolve_sx_api_key({"SX_API_KEY": "alt"}) == "alt"


def test_sx_ws_configured_is_boolean_only() -> None:
    """Telemetry is a pure bool — the secret value never appears."""
    assert sx_ws_configured({}) is False
    assert sx_ws_configured({"SX_BET_API_KEY": "x"}) is True
    assert sx_ws_configured({"SX_API_KEY": "y"}) is True


def test_ws_source_constructs_offline_and_fails_closed_without_key() -> None:
    """Constructing ``_WsSxBookSource`` touches no network; a missing key raises in __init__."""
    import pytest

    with pytest.raises(ValueError, match="SX WS requires an API key"):
        _WsSxBookSource(seed_source=object(), env={}, now_fn=lambda: 0)
    # With a key it constructs fine, still no network (no task started until .start()).
    src = _WsSxBookSource(seed_source=object(), env={"SX_BET_API_KEY": "k"}, now_fn=lambda: 0)
    assert src.gap_count == 0


# --------------------------------------------------------------------------- pure book maintenance
def test_market_book_seed_apply_and_mid() -> None:
    """Seed from a REST snapshot, apply a delta, and the reduced mid matches ``sx_quote_from_orders``."""
    book = _SxMarketBook()
    book.seed([_ASK_ORDER, _BID_ORDER])
    assert book.seeded
    assert _expected_mid(book.active_orders()) == _expected_mid([_ASK_ORDER, _BID_ORDER])

    # A cancel (INACTIVE) for the ask removes it → one-sided book → None mid (never fabricated).
    book.apply([_order(order_hash="0xask", pct=40 * 10**18, maker_outcome_one=False,
                       status="INACTIVE", update_time=2000)])
    assert _expected_mid(book.active_orders()) is None


def test_market_book_dedups_by_update_time() -> None:
    """A stale (older-updateTime) repeat never overwrites a newer state (the documented dedup rule)."""
    book = _SxMarketBook()
    book.seed([_order(order_hash="0xask", pct=40 * 10**18, maker_outcome_one=False, update_time=5000)])
    # An OLDER update trying to cancel is dropped; the newer active order survives.
    book.apply([_order(order_hash="0xask", pct=40 * 10**18, maker_outcome_one=False,
                       status="INACTIVE", update_time=100)])
    assert len(book.active_orders()) == 1


def test_market_book_invalidate_clears_and_unseeds() -> None:
    """A gap invalidation discards the book (no stale splice) and forces a re-seed."""
    book = _SxMarketBook()
    book.seed([_ASK_ORDER, _BID_ORDER])
    book.invalidate()
    assert not book.seeded
    assert book.active_orders() == []


# --------------------------------------------------------------------------- Centrifugo frame stream
async def _token() -> str:
    return "TESTTOKEN"


async def test_stream_yields_order_frames_with_recv_ts_and_handshake() -> None:
    """Canned publications → SxOrderBookFrames stamped at arrival; connect+subscribe sent first."""
    conn = FakeSxWsConnection([_push([_ASK_ORDER, _BID_ORDER])])
    fake = FakeSxWs([conn])
    clock = itertools.count(1000, 1000)

    async def block_sleep(_: float) -> None:
        await asyncio.Event().wait()

    agen = stream_sx_order_book_frames(
        [_MARKET], token_provider=_token, connect=fake.connect,
        now_fn=lambda: next(clock), sleep_fn=block_sleep,
    ).__aiter__()
    frame = await agen.__anext__()
    await agen.aclose()

    assert isinstance(frame, SxOrderBookFrame)
    assert frame.event_type == "orders"
    assert frame.market_hash == _MARKET
    assert frame.recv_ts == 1000  # stamped by injected now_fn at arrival
    assert _expected_mid(frame.orders) == _expected_mid([_ASK_ORDER, _BID_ORDER])

    import json

    connect_cmd = json.loads(conn.sent[0])
    assert connect_cmd["connect"]["token"] == "TESTTOKEN"
    sub_cmd = json.loads(conn.sent[1])
    assert sub_cmd["subscribe"]["channel"] == _CHANNEL
    assert sub_cmd["subscribe"] == {"channel": _CHANNEL, "recoverable": True, "positioned": True}


async def test_stream_answers_server_ping_with_pong() -> None:
    """An empty ``{}`` server ping is answered REACTIVELY with an empty ``{}`` pong (keep-alive)."""
    conn = FakeSxWsConnection([{}, _push([_ASK_ORDER, _BID_ORDER])])
    fake = FakeSxWs([conn])
    clock = itertools.count(1000, 1000)

    async def block_sleep(_: float) -> None:
        await asyncio.Event().wait()

    agen = stream_sx_order_book_frames(
        [_MARKET], token_provider=_token, connect=fake.connect,
        now_fn=lambda: next(clock), sleep_fn=block_sleep,
    ).__aiter__()
    frame = await agen.__anext__()  # ping consumed silently, then the order frame surfaces
    await agen.aclose()

    assert frame.event_type == "orders"
    assert conn.pongs == ["{}"]  # exactly one reactive pong


async def test_stream_reconnects_on_disconnect_surfaces_gap_no_secret(capsys: Any) -> None:
    """A scripted disconnect → an explicit gap + exactly one reconnect; only the exc TYPE is logged."""
    secret = "TOKEN_SECRET_XYZ"
    conn1 = FakeSxWsConnection([_push([_ASK_ORDER])], raise_exc=RuntimeError(f"boom {secret}"))
    conn2 = FakeSxWsConnection([_push([_ASK_ORDER, _BID_ORDER])])
    fake = FakeSxWs([conn1, conn2])
    clock = itertools.count(1000, 1000)
    sleep_calls: list[float] = []

    async def sleep_fn(delay: float) -> None:
        sleep_calls.append(delay)  # backoff sleeps return immediately

    agen = stream_sx_order_book_frames(
        [_MARKET], token_provider=_token, connect=fake.connect,
        now_fn=lambda: next(clock), sleep_fn=sleep_fn,
    ).__aiter__()
    first = await agen.__anext__()   # orders from conn1
    gap = await agen.__anext__()     # gap (conn1 raised)
    third = await agen.__anext__()   # orders from conn2 (reconnected)
    await agen.aclose()

    assert first.event_type == "orders"
    assert gap.event_type == "gap"
    assert gap.market_hash is None
    assert gap.orders == []
    assert third.event_type == "orders"
    assert fake.connect_calls == 2  # exactly one reconnect
    assert 1.0 in sleep_calls and all(d <= 60.0 for d in sleep_calls)  # bounded 1s→60s backoff
    out = capsys.readouterr().out
    assert secret not in out and "boom" not in out  # only the exception TYPE name is printed


async def test_connect_failure_surfaces_gap_and_backs_off(capsys: Any) -> None:
    """A token-mint / connect failure yields a gap and retries with bounded backoff (no secret leak)."""

    async def boom_token() -> str:
        raise RuntimeError("mint failed SECRET")

    fake = FakeSxWs([FakeSxWsConnection([_push([_ASK_ORDER, _BID_ORDER])])])
    clock = itertools.count(1000, 1000)
    sleeps: list[float] = []

    async def sleep_fn(delay: float) -> None:
        sleeps.append(delay)

    # token_provider raises on the FIRST call only, then succeeds via a swap-in.
    calls = {"n": 0}

    async def token_provider() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            await boom_token()
        return "TESTTOKEN"

    agen = stream_sx_order_book_frames(
        [_MARKET], token_provider=token_provider, connect=fake.connect,
        now_fn=lambda: next(clock), sleep_fn=sleep_fn,
    ).__aiter__()
    gap = await agen.__anext__()     # connect failed → gap
    frame = await agen.__anext__()   # retry succeeded → orders
    await agen.aclose()

    assert gap.event_type == "gap"
    assert frame.event_type == "orders"
    assert 1.0 in sleeps
    assert "SECRET" not in capsys.readouterr().out


# --------------------------------------------------------------------------- source integration
async def _pump(cond: Any, *, limit: int = 100) -> None:
    for _ in range(limit):
        if cond():
            return
        await asyncio.sleep(0)


class _FakeRestSeed:
    """A fake REST seed ``SxBookSource``: returns a canned snapshot per market, records calls."""

    def __init__(self, snapshot: dict[str, list[dict[str, Any]]]) -> None:
        self._snapshot = snapshot
        self.poll_calls: list[list[str]] = []

    async def poll(self, market_hashes: list[str]) -> dict[str, list[dict[str, Any]]]:
        self.poll_calls.append(list(market_hashes))
        return {h: list(self._snapshot.get(h, [])) for h in market_hashes}


async def test_ws_source_seeds_from_rest_then_applies_delta_and_polls_mid() -> None:
    """End-to-end: REST seed (bid) + WS delta (ask) → poll() returns a two-sided book with a real mid."""
    seed = _FakeRestSeed({_MARKET: [_BID_ORDER]})  # REST seeds one side (bid only → None mid alone)
    conn = FakeSxWsConnection([_push([_ASK_ORDER])])  # WS delta adds the ask
    fake = FakeSxWs([conn])

    src = _WsSxBookSource(
        seed_source=seed,
        env={"SX_BET_API_KEY": "k"},
        connect=fake.connect,
        token_provider=_token,
        now_fn=lambda: 0,
    )
    await src.start([_MARKET])
    try:
        await _pump(lambda: seed.poll_calls and src._books[_MARKET].seeded)
        await _pump(lambda: len(src._books[_MARKET].active_orders()) >= 2)
        books = await src.poll([_MARKET])
    finally:
        await src.aclose()

    assert seed.poll_calls == [[_MARKET]]  # seeded once via REST on the first publication
    assert _expected_mid(books[_MARKET]) == _expected_mid([_ASK_ORDER, _BID_ORDER])


async def test_ws_source_gap_invalidates_book_no_stale_splice() -> None:
    """A disconnect increments gap_count and invalidates the book → poll() yields an empty (None-mid) book."""
    seed = _FakeRestSeed({_MARKET: [_BID_ORDER]})
    conn = FakeSxWsConnection([_push([_ASK_ORDER])], disconnect=True)  # deliver, then disconnect
    fake = FakeSxWs([conn])

    async def sleep_fn(_: float) -> None:
        await asyncio.Event().wait()  # park at the post-gap backoff so no re-seed races in

    src = _WsSxBookSource(
        seed_source=seed,
        env={"SX_BET_API_KEY": "k"},
        connect=fake.connect,
        token_provider=_token,
        now_fn=lambda: 0,
        sleep_fn=sleep_fn,
    )
    await src.start([_MARKET])
    try:
        await _pump(lambda: src.gap_count >= 1)
        books = await src.poll([_MARKET])
    finally:
        await src.aclose()

    assert src.gap_count >= 1
    assert books[_MARKET] == []  # invalidated → empty → sx_quote_from_orders yields a None mid
    assert _expected_mid(books[_MARKET]) is None


# --------------------------------------------------------------------------- import hygiene
def test_module_imports_no_network() -> None:
    """Importing the poller performs no network + no eager network import at module scope (AST audit)."""
    import scripts.maker.sx_bet_poller as mod

    source = Path(mod.__file__).read_text()
    tree = ast.parse(source)
    top_level_imports: set[str] = set()
    for node in tree.body:  # MODULE scope only — lazy imports inside functions/methods are allowed
        if isinstance(node, ast.Import):
            top_level_imports.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            top_level_imports.add(node.module.split(".")[0])
    forbidden = {"httpx", "requests", "websocket", "websockets", "aiohttp"}
    assert not (top_level_imports & forbidden), f"module-scope network import: {top_level_imports & forbidden}"
