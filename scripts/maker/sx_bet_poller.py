"""SX Bet (SportX exchange) vs TxLINE FV single-clock lead-lag poller (SX-1).

Purpose
-------
The Polymarket lead-lag work proved the *venue* leads the de-margined TxLINE FV by ~6.5 s
(the venue is fast, so TxLINE trails). SX Bet is a DIFFERENT venue: a crypto sports-betting
EXCHANGE (order book) on SX Rollup whose book reportedly LAGS Polymarket by 30-90 s with
thinner depth. On a SLOW venue the de-margined TxLINE fair value could plausibly *lead* — which
would BREAK the null on a new venue. This poller is the instrument to test that forward, during
a live match.

Why a forward poller (not an archive replay)
--------------------------------------------
SX has NO historical archive — its public REST is LIVE-ONLY. Every TxLINE-covered World Cup
match (group/R16/QF) is already PLAYED, so SX serves no book for them. A lead-lag test therefore
requires FORWARD capture during an UPCOMING fixture (e.g. Norway-England, KO 2026-07-11 21:00 UTC;
Argentina-Switzerland, KO 2026-07-12 01:00 UTC). This script captures that tape.

The honest measurement: SINGLE CLOCK
------------------------------------
Both feeds are stamped by the SAME injected ``now_fn`` at the instant each ARRIVES in THIS
process (``recv_ts = int(now_fn())`` ms). One recorder, one clock — this avoids the cross-recorder
skew ceiling that capped the pmxt dual-recorder experiments. The three SX outcomes come back in ONE
HTTP round-trip, so a whole 3-way book snapshot shares a single ``recv_ts``.

Resolution note (why REST polling is ENOUGH — and the optional WS upgrade)
-------------------------------------------------------------------------
The SX leg is POLLED over public REST (default 1.0 s) BY DEFAULT. Unlike the Polymarket ~6.5 s test
— where a 2 s poll floor quantized a sub-2 s lead away and forced a WebSocket — the hypothesised SX
lag is 30-90 s, i.e. one-to-two orders of magnitude LARGER than a 1 s poll. So 1 s REST polling
resolves the effect with room to spare, and the REST path stays the default so tonight's capture is
never blocked on a credential.

SX ALSO exposes a real-time order-book WebSocket. As of 2026 it is **Centrifugo** (the legacy Ably
API is deprecated, shutdown 2026-07-01), reached at ``wss://realtime.sx.bet/connection/websocket``.
The ``order_book:market_{marketHash}`` channel pushes order changes (delayed at most 100 ms) whose
payload is an array of the SAME order objects the REST ``/orders`` path returns (``percentageOdds`` /
``totalBetSize`` / ``isMakerBettingOutcomeOne``), so mids are computed identically. Connecting needs
an API key (``x-api-key``) to mint a realtime token from ``GET /user/realtime-token/api-key``; REST
stays fully public. ``--use-ws`` opts into this push-latency path (seeded once from REST, then kept
current by Centrifugo deltas); WITHOUT it the REST path runs exactly as before. Doc refs:
docs.sx.bet /api-reference/centrifugo-overview, /centrifugo-order-book-updates, /api-key.

Design invariants (mirroring ``scripts/maker/live_monitor.py`` / ``ws_leadlag_monitor.py``)
-------------------------------------------------------------------------------------------
* **Injectable seams, offline-testable.** Every live source is a ``Protocol`` (:class:`FvSource`,
  :class:`SxBookSource`, :class:`Recorder`); NO network library is imported at module scope
  (``httpx`` is lazy inside the default sources). Constructing the module touches no network.
* **Single clock.** BOTH legs are stamped by the SAME injected ``now_fn`` at arrival.
* **Read-only.** SX is queried GET-only. NO orders are ever posted. SX REST needs no auth.
* **Token hygiene.** TxLINE FV creds are resolved fail-closed via :func:`require_live_creds`
  (raise BEFORE any I/O when absent), held privately, never logged/written; diagnostics are
  scrubbed of the secret values. Artifacts carry only ``fv_configured: bool``. SX needs no secret.
* **Honest resolution.** Empty/one-sided SX books emit ``None`` mids — never fabricated.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple, Protocol, cast

from veridex.ingest.marketstate import MarketState
from veridex.live_recorder.sources import _scrub, require_live_creds

__all__ = [
    "FvSource",
    "SxBookSource",
    "Recorder",
    "SxQuote",
    "SideBinding",
    "MonitoredMatch",
    "JsonlRecorder",
    "sx_quote_from_orders",
    "fv_from_state",
    "resolve_sx_markets",
    "run_poller",
    "build_parser",
    "main",
    # SX Centrifugo WS order-book source (opt-in via --use-ws; REST stays the default/fallback).
    "resolve_sx_api_key",
    "sx_ws_configured",
    "SxOrderBookFrame",
    "stream_sx_order_book_frames",
    "FakeSxWsConnection",
    "FakeSxWs",
]

# --------------------------------------------------------------------------- constants
SX_API_BASE = "https://api.sx.bet"
#: FIFA World Cup league on SX (verified live 2026-07-11). Override via --sx-league-id if it rotates.
SX_WORLD_CUP_LEAGUE_ID = 1715
#: SX market ``type`` for the 3-way match result, decomposed into three 2-way "X | Not X" markets.
SX_MATCH_RESULT_TYPE = 1
#: SX ``percentageOdds`` are implied-prob * 1e20. Taker prob for the OTHER side = (1e20 - pct)/1e20.
_SX_ODDS_SCALE = 1e20
#: baseToken 0x6629Ce1Cf35Cc1329ebB4F63202F3f197b3F050B (USDC on SX Rollup) has 6 decimals.
USDC_DECIMALS = 6

#: The REAL TxLINE 1X2 FULL-match market key the FV is read under (mirrors ``live_monitor``).
_TXLINE_1X2_FULL_MARKET_KEY = "1X2_PARTICIPANT_RESULT||"

#: Logical 1X2 sides. ``txline_side`` is the ``stable_prob_bps`` key; ``role`` maps to the SX team.
#: home == part1, away == part2, draw == the Tie market.
_SIDE_SPECS: tuple[tuple[str, str], ...] = (
    ("part1", "home"),
    ("draw", "draw"),
    ("part2", "away"),
)
_DRAW_OUTCOME_LABELS = ("tie", "draw")

# --------------------------------------------------------------------------- SX Centrifugo WS
#: SX real-time WebSocket endpoint (Centrifugo; the legacy Ably API is deprecated 2026-07-01).
SX_WS_URL = "wss://realtime.sx.bet/connection/websocket"
#: Mint a short-lived realtime token from an API key (``x-api-key`` header) → ``{"token": "..."}``.
SX_REALTIME_TOKEN_PATH = "/user/realtime-token/api-key"
#: Env var names holding the SX API key, in resolution order (docs use both spellings).
_SX_API_KEY_ENV_KEYS = ("SX_BET_API_KEY", "SX_API_KEY")
#: Centrifugo channel prefix for a single market's order book: ``order_book:market_{marketHash}``.
_SX_ORDER_BOOK_CHANNEL_PREFIX = "order_book:market_"
#: Our own honest internal frame markers (an ``order`` publication vs a disconnect ``gap``).
_SX_ORDERS = "orders"
_SX_GAP = "gap"
#: Reconnect backoff bounds (mirror ``ws_book_source``: 1 s → 60 s doubling).
_SX_BACKOFF_START_S = 1.0
_SX_BACKOFF_MAX_S = 60.0


def resolve_sx_api_key(env: Mapping[str, str]) -> str:
    """Return the SX API key from *env*, or FAIL CLOSED (raise) if absent — BEFORE any I/O.

    Mirrors :func:`veridex.live_recorder.sources.require_live_creds`: the guard raises when the
    secret is missing so no network call is ever attempted unauthenticated. The VALUE is returned
    for private in-process use only and is NEVER logged/written (telemetry is boolean-only via
    :func:`sx_ws_configured`).
    """
    for key in _SX_API_KEY_ENV_KEYS:
        value = env.get(key)
        if value:
            return value
    raise ValueError(
        f"SX WS requires an API key: set one of {' or '.join(_SX_API_KEY_ENV_KEYS)} "
        "in the environment (it lives in veridex/.env). REST polling needs no key — omit --use-ws."
    )


def sx_ws_configured(env: Mapping[str, str]) -> bool:
    """Boolean-only telemetry: whether an SX API key is present (NEVER the secret value)."""
    return any(bool(env.get(key)) for key in _SX_API_KEY_ENV_KEYS)


# --------------------------------------------------------------------------- data model
@dataclass(frozen=True)
class SxQuote:
    """One SX 2-way outcome's top-of-book, in implied probability [0, 1].

    ``ask`` = best price to BACK outcome-one; ``bid`` = best price to LAY it (= 1 - best back of
    outcome-two). ``mid`` = midpoint, or ``None`` when either side is empty (never fabricated).
    ``bid_size`` / ``ask_size`` are USDC depth at the touch. ``n_orders`` is the raw order count.
    """

    bid: float | None
    ask: float | None
    mid: float | None
    bid_size: float
    ask_size: float
    n_orders: int


@dataclass(frozen=True)
class SideBinding:
    """A resolved logical side -> SX market binding for one match outcome."""

    txline_side: str          # part1 / draw / part2 (the FV stable_prob_bps key)
    role: str                 # home / draw / away
    market_hash: str          # SX marketHash for the "outcomeName | Not outcomeName" market
    outcome_one_name: str      # the SX outcomeOneName (team, or "Tie")


@dataclass(frozen=True)
class MonitoredMatch:
    """A resolved match: TxLINE fixture id + its three SX side bindings."""

    fixture_id: int
    home_team: str
    away_team: str
    kickoff_ts: int
    bindings: tuple[SideBinding, ...]

    def market_hashes(self) -> list[str]:
        return [b.market_hash for b in self.bindings]


class SampleRow(NamedTuple):
    """One recorded single-clock row (SX book snapshot + the concurrent forward-filled FV)."""

    recv_ts: int
    fixture_id: int
    role: str
    sx_bid: float | None
    sx_ask: float | None
    sx_mid: float | None
    sx_bid_size: float
    sx_ask_size: float
    sx_n_orders: int
    fv: float | None
    fv_recv_ts: int | None


# --------------------------------------------------------------------------- injectable seams
class FvSource(Protocol):
    """A live TxLINE FV source: yields :class:`MarketState` snapshots in ARRIVAL order."""

    def stream(self) -> AsyncIterator[MarketState]:
        ...


class SxBookSource(Protocol):
    """A live SX order-book source: one poll returns ``{market_hash: [raw order dicts]}``."""

    async def poll(self, market_hashes: list[str]) -> dict[str, list[dict[str, Any]]]:
        ...


class Recorder(Protocol):
    """An append-only raw-row sink (one dict per recorded single-clock sample)."""

    def record(self, row: dict[str, Any]) -> None:
        ...


# --------------------------------------------------------------------------- pure book math
def _taker_prob_outcome_one(order: dict[str, Any]) -> float:
    """Implied prob a TAKER pays to back OUTCOME-ONE via *order* (maker bets outcome-two)."""
    pct = float(order["percentageOdds"])
    return (_SX_ODDS_SCALE - pct) / _SX_ODDS_SCALE


def _size_usdc(order: dict[str, Any]) -> float:
    return float(order["totalBetSize"]) / (10 ** USDC_DECIMALS)


def sx_quote_from_orders(orders: list[dict[str, Any]]) -> SxQuote:
    """Reduce a market's raw ACTIVE orders to a top-of-book :class:`SxQuote` for outcome-one.

    Two-way SX market (outcomeOne vs outcomeTwo):
      * orders with ``isMakerBettingOutcomeOne == False`` OFFER outcome-one to takers -> the ASK
        (best = the LOWEST taker prob, the cheapest back).
      * orders with ``isMakerBettingOutcomeOne == True`` OFFER outcome-two to takers; the implied
        outcome-one BID = ``1 - (best outcome-two back)`` (best = highest bid).
    A side with no orders yields ``None`` for that side and a ``None`` mid (never fabricated).
    """
    active = [o for o in orders if o.get("orderStatus", "ACTIVE") == "ACTIVE"]
    asks = [o for o in active if not o.get("isMakerBettingOutcomeOne")]
    bids_o2 = [o for o in active if o.get("isMakerBettingOutcomeOne")]

    ask = ask_size = None
    if asks:
        best = min(asks, key=_taker_prob_outcome_one)
        ask = _taker_prob_outcome_one(best)
        ask_size = _size_usdc(best)

    bid = bid_size = None
    if bids_o2:
        # best outcome-two back == lowest outcome-two taker prob -> highest outcome-one bid.
        best_o2 = min(bids_o2, key=_taker_prob_outcome_one)
        bid = 1.0 - _taker_prob_outcome_one(best_o2)
        bid_size = _size_usdc(best_o2)

    mid = (bid + ask) / 2.0 if (bid is not None and ask is not None) else None
    return SxQuote(
        bid=bid,
        ask=ask,
        mid=mid,
        bid_size=float(bid_size or 0.0),
        ask_size=float(ask_size or 0.0),
        n_orders=len(active),
    )


def fv_from_state(state: MarketState, txline_side: str) -> float | None:
    """Native-prob FV for *txline_side* from a state's 1X2 full-match market, or ``None``.

    Mirrors ``live_monitor._fv_from_state``: reads ``markets['1X2_PARTICIPANT_RESULT||']
    ['stable_prob_bps'][txline_side]`` (basis points) and scales to a [0, 1] probability.
    """
    market = state.markets.get(_TXLINE_1X2_FULL_MARKET_KEY)
    if not market:
        return None
    stable_prob_bps = market.get("stable_prob_bps")
    if not stable_prob_bps:
        return None
    bps = stable_prob_bps.get(txline_side)
    if bps is None:
        return None
    return float(bps) / 1e4


# --------------------------------------------------------------------------- SX market resolution
def resolve_sx_markets(
    fixture: Mapping[str, Any],
    active_markets: list[dict[str, Any]],
    *,
    kickoff_tolerance_s: int = 120,
) -> MonitoredMatch:
    """Bind a TxLINE fixture's three 1X2 sides to their SX ``type==1`` marketHashes by team name.

    No hardcoded hashes: matches SX markets whose ``gameTime`` is within *kickoff_tolerance_s* of
    the fixture kickoff and whose ``outcomeOneName`` equals the home team, the away team, or a draw
    label. Raises ``LookupError`` if any of the three sides cannot be bound (fail loud, never fake).
    """
    fixture_id = int(fixture["fixture_id"])
    home = str(fixture["home_team"])
    away = str(fixture["away_team"])
    kickoff = int(fixture["kickoff_ts"])

    near = [
        m for m in active_markets
        if abs(int(m.get("gameTime", 0)) - kickoff) <= kickoff_tolerance_s
        and int(m.get("type", -1)) == SX_MATCH_RESULT_TYPE
    ]

    def _find(role: str, team: str | None) -> dict[str, Any]:
        for m in near:
            name = str(m.get("outcomeOneName", "")).strip()
            if role == "draw":
                if name.lower() in _DRAW_OUTCOME_LABELS:
                    return m
            elif team is not None and name.casefold() == team.casefold():
                return m
        raise LookupError(
            f"fixture {fixture_id}: no SX type-{SX_MATCH_RESULT_TYPE} market for {role} "
            f"({team or 'Tie'}) within {kickoff_tolerance_s}s of kickoff {kickoff}"
        )

    bindings: list[SideBinding] = []
    for txline_side, role in _SIDE_SPECS:
        team = home if role == "home" else away if role == "away" else None
        m = _find(role, team)
        bindings.append(
            SideBinding(
                txline_side=txline_side,
                role=role,
                market_hash=str(m["marketHash"]),
                outcome_one_name=str(m.get("outcomeOneName", "")),
            )
        )
    return MonitoredMatch(
        fixture_id=fixture_id,
        home_team=home,
        away_team=away,
        kickoff_ts=kickoff,
        bindings=tuple(bindings),
    )


# --------------------------------------------------------------------------- default live sources
class _DefaultFvSource:
    """Live TxLINE FV source: wraps ``stream_marketstates`` in a reconnect/backoff loop (1s->60s).

    Creds are resolved ONCE via :func:`require_live_creds` in ``__init__`` (fail-closed BEFORE any
    network I/O when absent) and held privately — never logged. ``httpx`` is imported lazily inside
    ``stream_marketstates`` so constructing this source touches no network at import time.
    """

    def __init__(self, *, env: Mapping[str, str] | None = None, base_url: str | None = None) -> None:
        environ = env if env is not None else os.environ
        self._creds = require_live_creds(environ)
        self._base_url = base_url

    async def stream(self) -> AsyncIterator[MarketState]:
        from veridex.ingest.live_client import stream_marketstates

        backoff = 1.0
        while True:
            try:
                async for state in stream_marketstates(base_url=self._base_url, creds=self._creds):
                    backoff = 1.0
                    yield state
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — reconnect on any error; never leak creds
                print(f"  FV stream disconnected: {_scrub(str(exc), *self._creds)}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


class _DefaultSxBookSource:
    """Live SX order-book source over PUBLIC REST (no auth). ``httpx`` is lazy-imported per poll."""

    def __init__(self, *, base_url: str = SX_API_BASE, timeout_s: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s

    async def _get_json(self, url: str) -> dict[str, Any]:
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout_s) as http:
            resp = await http.get(url)
            resp.raise_for_status()
            return resp.json()

    async def poll(self, market_hashes: list[str]) -> dict[str, list[dict[str, Any]]]:
        joined = ",".join(market_hashes)
        payload = await self._get_json(f"{self._base_url}/orders?marketHashes={joined}")
        out: dict[str, list[dict[str, Any]]] = {h: [] for h in market_hashes}
        for order in payload.get("data") or []:
            out.setdefault(order["marketHash"], []).append(order)
        return out

    async def active_markets(self, league_id: int) -> list[dict[str, Any]]:
        url = f"{self._base_url}/markets/active?leagueIds={league_id}&type={SX_MATCH_RESULT_TYPE}"
        payload = await self._get_json(url)
        return (payload.get("data") or {}).get("markets") or []


# =========================================================================== SX WS book source
# A push-latency ``SxBookSource`` over SX's Centrifugo order-book WebSocket. It mirrors the R3 WS
# build (``veridex/live_recorder/ws_book_source.py``) discipline EXACTLY: an injectable ``connect``
# seam with ``aiohttp`` lazy-imported inside the connect coroutine (module constructs offline), a
# reconnect/backoff loop that emits an explicit ``gap`` frame on every disconnect (never a silent
# splice), and honest ``None`` mids on an empty/one-sided book. It keeps a per-market order book
# current (seeded once from PUBLIC REST, then updated by Centrifugo deltas) and answers ``poll`` —
# the SAME ``SxBookSource`` seam the REST path uses — so ``run_poller`` stamps ONE ``recv_ts`` for
# the whole snapshot on the SAME clock, keeping the WS and REST tapes byte-for-byte comparable.


class _WsInbound(NamedTuple):
    """A transport-neutral inbound message: ``kind`` in {"text","closed","error"} + text ``data``.

    Keeps ``aiohttp.WSMsgType`` out of module scope — the real adapter translates aiohttp
    ``WSMessage``s into this shape and the offline fake produces it directly (mirrors R3 ``WsInbound``).
    """

    kind: str
    data: str | None


class SxOrderBookFrame(NamedTuple):
    """One received SX order-book frame, stamped with its local arrival clock.

    ``recv_ts`` is int ms from the injected ``now_fn`` at receipt. ``event_type`` is ``"orders"``
    for a Centrifugo publication (``orders`` = its ``data`` array of order dicts) or ``"gap"`` — our
    honest disconnect marker (empty ``orders``, ``market_hash`` ``None``).
    """

    recv_ts: int
    event_type: str
    market_hash: str | None
    orders: list[dict[str, Any]]


class _SxWsDisconnected(Exception):
    """Raised internally when the socket returns a non-text (closed/error) message → reconnect."""


# --------------------------------------------------------------------------- real connect seam
class _AiohttpSxConn:
    """Adapter over an aiohttp ``ClientWebSocketResponse`` exposing the ``connect`` seam contract.

    Owns both the session and the ws so :meth:`close` tears down both. ``aiohttp`` is referenced
    only here and is lazy-imported by :func:`_default_sx_connect`, so module import stays
    network-library-free (mirrors R3 ``_AiohttpConn``).
    """

    def __init__(self, session: Any, ws: Any) -> None:
        self._session = session
        self._ws = ws

    async def send_str(self, data: str) -> None:
        await self._ws.send_str(data)

    async def receive(self) -> _WsInbound:
        import aiohttp  # noqa: PLC0415 — lazy: keep module import network-free

        msg = await self._ws.receive()
        if msg.type == aiohttp.WSMsgType.TEXT:
            return _WsInbound("text", msg.data)
        if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSE):
            return _WsInbound("closed", None)
        return _WsInbound("error", None)

    async def close(self) -> None:
        try:
            await self._ws.close()
        finally:
            await self._session.close()


async def _default_sx_connect(ws_url: str) -> _AiohttpSxConn:
    """Open a real aiohttp Centrifugo WS connection (``aiohttp`` lazy-imported here)."""
    import aiohttp  # noqa: PLC0415 — lazy network import: keep module import network-free

    session = aiohttp.ClientSession()
    try:
        ws = await session.ws_connect(ws_url)
    except BaseException:
        await session.close()
        raise
    return _AiohttpSxConn(session, ws)


# --------------------------------------------------------------------------- Centrifugo frame stream
def _sx_gap_frame(recv_ts: int) -> SxOrderBookFrame:
    """An explicit honest gap marker emitted on every disconnect (never a silent splice)."""
    return SxOrderBookFrame(recv_ts=recv_ts, event_type=_SX_GAP, market_hash=None, orders=[])


async def stream_sx_order_book_frames(
    market_hashes: Iterable[str],
    *,
    token_provider: Callable[[], Awaitable[str]],
    ws_url: str = SX_WS_URL,
    connect: Callable[[str], Awaitable[Any]] | None = None,
    now_fn: Callable[[], int],
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> AsyncIterator[SxOrderBookFrame]:
    """Stream SX ``order_book:market_{hash}`` publications as arrival-stamped :class:`SxOrderBookFrame`s.

    Speaks the Centrifugo bidirectional JSON protocol over the injectable ``connect`` seam: send the
    ``connect`` command with the minted token, then a ``subscribe`` command per market channel
    (``positioned``+``recoverable`` for at-least-once recovery across reconnects), and yield each
    inbound publication's ``data`` array. Centrifugo drives the heartbeat SERVER-side (empty ``{}``
    ping every ~30 s); the client answers each with an empty ``{}`` pong REACTIVELY — no client-side
    ping timer is needed (verified: docs.sx.bet/api-reference/centrifugo-overview, "Ping interval 30s").
    Reconnects on any disconnect with a 1 s→60 s doubling backoff (reset on the first healthy frame);
    ``CancelledError`` is re-raised; every disconnect logs only the exception TYPE (never its value,
    so a token embedded in an out-of-band error can never leak) and surfaces an explicit ``gap`` frame.

    Args:
        market_hashes: SX market hashes to subscribe (one ``order_book:market_{hash}`` channel each).
        token_provider: Async ``() -> token`` minting the realtime token from the API key (I/O; the
            key itself is resolved fail-closed by the caller BEFORE this coroutine runs).
        ws_url: Centrifugo WS URL override.
        connect: Injectable ``async (ws_url) -> conn`` seam (``send_str``/``receive``/``close``);
            ``None`` (production) builds a real aiohttp connection lazily.
        now_fn: Receive-time clock; ``recv_ts = int(now_fn())`` stamps each frame at arrival (ms).
        sleep_fn: Injectable sleep driving the reconnect backoff (deterministic offline).
    """
    connector = connect if connect is not None else _default_sx_connect
    channels = [f"{_SX_ORDER_BOOK_CHANNEL_PREFIX}{h}" for h in market_hashes]
    backoff = _SX_BACKOFF_START_S

    while True:
        try:
            token = await token_provider()  # mint from the API key (I/O; may raise if the relayer 4xx/5xx)
            conn = await connector(ws_url)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — reconnect on any connect/mint error; log TYPE only
            print(f"sx ws connect failed: {type(exc).__name__}")
            yield _sx_gap_frame(int(now_fn()))
            await sleep_fn(backoff)
            backoff = min(backoff * 2, _SX_BACKOFF_MAX_S)
            continue

        try:
            # Centrifugo handshake: connect first, then one subscribe per channel (commands are
            # processed in order server-side, so pipelining connect+subscribe is safe).
            await conn.send_str(json.dumps({"id": 1, "connect": {"token": token}}))
            for i, channel in enumerate(channels, start=2):
                await conn.send_str(
                    json.dumps({"id": i, "subscribe": {"channel": channel, "recoverable": True, "positioned": True}})
                )
            while True:
                msg = await conn.receive()
                if msg.kind != "text" or msg.data is None:
                    raise _SxWsDisconnected(msg.kind)
                recv_ts = int(now_fn())
                # A single WS text frame may batch several newline-delimited Centrifugo replies.
                for line in msg.data.split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        reply = json.loads(line)
                    except (ValueError, TypeError):
                        continue  # non-JSON line → ignore
                    if not isinstance(reply, dict):
                        continue
                    if reply == {}:  # server ping → reactive empty pong (keeps the socket alive)
                        await conn.send_str("{}")
                        continue
                    push = reply.get("push")
                    if not isinstance(push, dict):
                        continue  # connect/subscribe reply (or other) → not an order publication
                    pub = push.get("pub")
                    channel = str(push.get("channel", ""))
                    if not isinstance(pub, dict) or not channel.startswith(_SX_ORDER_BOOK_CHANNEL_PREFIX):
                        continue
                    data = pub.get("data")
                    if not isinstance(data, list):
                        continue  # empty/absent data → nothing to apply (never a fabricated order)
                    market_hash = channel[len(_SX_ORDER_BOOK_CHANNEL_PREFIX):]
                    backoff = _SX_BACKOFF_START_S  # reset-on-success (a healthy publication arrived)
                    yield SxOrderBookFrame(
                        recv_ts=recv_ts, event_type=_SX_ORDERS, market_hash=market_hash, orders=data
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — reconnect on any stream error; log TYPE only
            print(f"sx ws disconnected: {type(exc).__name__}")
        finally:
            with contextlib.suppress(Exception):  # best-effort teardown
                await conn.close()

        # Explicit honest gap on every disconnect, then a bounded backoff before reconnecting.
        yield _sx_gap_frame(int(now_fn()))
        await sleep_fn(backoff)
        backoff = min(backoff * 2, _SX_BACKOFF_MAX_S)


# --------------------------------------------------------------------------- per-market book state
def _order_is_active(order: dict[str, Any]) -> bool:
    """Whether an order is live. WS deltas carry ``status``; REST seeds carry ``orderStatus``."""
    raw = order.get("status", order.get("orderStatus", "ACTIVE"))
    return str(raw).upper() == "ACTIVE"


class _SxMarketBook:
    """A retained per-market order set (``orderHash -> (updateTime, order)``) + its seeded state.

    Seeded once from a REST ``/orders`` snapshot, then updated by Centrifugo deltas: an ACTIVE order
    is upserted, an ``INACTIVE``/``FILLED`` order is removed. Same-``orderHash`` updates are ordered
    by ``updateTime`` (a stale update is dropped — the documented dedup rule). :meth:`invalidate`
    discards the book on a disconnect so a stale pre-gap state is NEVER served as fresh.
    """

    def __init__(self) -> None:
        self._orders: dict[str, tuple[int, dict[str, Any]]] = {}
        self.seeded = False

    @staticmethod
    def _update_time(order: dict[str, Any]) -> int:
        try:
            return int(order.get("updateTime", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def seed(self, orders: list[dict[str, Any]]) -> None:
        """Replace the book with the ACTIVE orders from a REST snapshot (a fresh, honest re-seed)."""
        self._orders = {}
        for order in orders:
            order_hash = order.get("orderHash")
            if order_hash is None or not _order_is_active(order):
                continue
            self._orders[str(order_hash)] = (self._update_time(order), order)
        self.seeded = True

    def apply(self, orders: list[dict[str, Any]]) -> None:
        """Apply a Centrifugo delta batch: upsert ACTIVE, remove INACTIVE/FILLED, dedup by updateTime."""
        for order in orders:
            order_hash = order.get("orderHash")
            if order_hash is None:
                continue
            key = str(order_hash)
            update_time = self._update_time(order)
            prev = self._orders.get(key)
            if prev is not None and update_time < prev[0]:
                continue  # a stale (older-updateTime) repeat → never overwrite a newer state
            if _order_is_active(order):
                self._orders[key] = (update_time, order)
            else:
                self._orders.pop(key, None)  # cancelled/filled → leaves the book

    def invalidate(self) -> None:
        """Discard the local book → force a fresh REST re-seed (an honest resync, no silent splice)."""
        self._orders = {}
        self.seeded = False

    def active_orders(self) -> list[dict[str, Any]]:
        """The current active order dicts (the shape ``sx_quote_from_orders`` consumes)."""
        return [order for _, order in self._orders.values()]


class _WsSxBookSource:
    """A push-latency ``SxBookSource`` over SX's Centrifugo order-book WS (REST kept as the seed).

    The API key is resolved FAIL-CLOSED in ``__init__`` (raises BEFORE any I/O when absent), held
    privately, and NEVER logged/written. A background task streams :func:`stream_sx_order_book_frames`
    through a per-market :class:`_SxMarketBook`: it lazily seeds a market from PUBLIC REST on its
    first publication (and re-seeds after a gap invalidation), then applies deltas. :meth:`poll`
    returns the current active orders per market — the SAME ``SxBookSource`` seam the REST path uses,
    so ``run_poller`` stamps the tape on its own single clock exactly as for REST.
    """

    def __init__(
        self,
        *,
        seed_source: Any,
        env: Mapping[str, str] | None = None,
        ws_url: str = SX_WS_URL,
        base_url: str = SX_API_BASE,
        connect: Callable[[str], Awaitable[Any]] | None = None,
        token_provider: Callable[[], Awaitable[str]] | None = None,
        now_fn: Callable[[], int],
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
        timeout_s: float = 10.0,
    ) -> None:
        environ = env if env is not None else os.environ
        self._api_key = resolve_sx_api_key(environ)  # FAIL-CLOSED before any network I/O
        self._seed_source = seed_source
        self._ws_url = ws_url
        self._base_url = base_url.rstrip("/")
        self._connect = connect
        self._token_provider = token_provider
        self._now_fn = now_fn
        self._sleep_fn = sleep_fn
        self._timeout_s = timeout_s
        self._books: dict[str, _SxMarketBook] = {}
        self._market_hashes: list[str] = []
        self._task: asyncio.Task[None] | None = None
        self.gap_count = 0  # observable: how many honest gap markers were surfaced

    async def _mint_token(self) -> str:
        """Mint a Centrifugo realtime token from the API key (``x-api-key`` header). ``httpx`` lazy."""
        if self._token_provider is not None:
            return await self._token_provider()
        import httpx  # noqa: PLC0415 — lazy network import: keep module import network-free

        async with httpx.AsyncClient(timeout=self._timeout_s) as http:
            resp = await http.get(
                f"{self._base_url}{SX_REALTIME_TOKEN_PATH}", headers={"x-api-key": self._api_key}
            )
            resp.raise_for_status()
            return str(resp.json()["token"])

    async def start(self, market_hashes: list[str]) -> None:
        """Launch the background consume task subscribing every market (idempotent)."""
        self._market_hashes = list(market_hashes)
        for h in self._market_hashes:
            self._books.setdefault(h, _SxMarketBook())
        if self._task is None:
            self._task = asyncio.create_task(self._consume())

    async def aclose(self) -> None:
        """Cancel the background task cleanly (no leaked task, no partial state served)."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def poll(self, market_hashes: list[str]) -> dict[str, list[dict[str, Any]]]:
        """Return the current active orders per market (a local read of the maintained book state).

        A never-seeded / just-invalidated market returns ``[]`` → ``sx_quote_from_orders`` yields a
        ``None`` mid (honest — an empty book is never fabricated, and a gapped book is never spliced).
        """
        return {h: self._books.get(h, _SxMarketBook()).active_orders() for h in market_hashes}

    async def _seed_market(self, market_hash: str) -> None:
        try:
            seeded = await self._seed_source.poll([market_hash])
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a seed failure keeps the book unseeded (None mid), never crashes
            print(f"  SX WS REST seed error: {type(exc).__name__}")
            return
        self._books.setdefault(market_hash, _SxMarketBook()).seed(seeded.get(market_hash, []))

    async def _consume(self) -> None:
        agen = cast(
            AsyncGenerator[SxOrderBookFrame, None],
            stream_sx_order_book_frames(
                self._market_hashes,
                token_provider=self._mint_token,
                ws_url=self._ws_url,
                connect=self._connect,
                now_fn=self._now_fn,
                sleep_fn=self._sleep_fn,
            ),
        )
        try:
            async for frame in agen:
                if frame.event_type == _SX_GAP:
                    self.gap_count += 1
                    for book in self._books.values():
                        book.invalidate()  # disconnect → invalidate EVERY book, force a fresh re-seed
                    continue
                if frame.market_hash is None:
                    continue
                book = self._books.setdefault(frame.market_hash, _SxMarketBook())
                if not book.seeded:
                    await self._seed_market(frame.market_hash)  # snapshot+subscribe: seed then apply
                    book = self._books[frame.market_hash]
                book.apply(frame.orders)
        finally:
            await agen.aclose()


# --------------------------------------------------------------------------- offline WS fakes
class FakeSxWsConnection:
    """A scripted offline Centrifugo WS connection: yields canned reply frames, then a disconnect/idle.

    Mirrors ``ws_book_source.FakeVenueWsConnection``. Each element of ``frames`` is a Centrifugo reply
    dict (e.g. ``{"push": {"channel": "order_book:market_0x..", "pub": {"data": [order, ...]}}}`` or an
    empty ``{}`` server ping). Records every ``send_str`` (connect/subscribe/pong) for assertions.
    After the canned frames it raises ``raise_exc``, returns a ``closed`` message (``disconnect=True``),
    or blocks until :meth:`close` (an idle healthy socket). No network, no real time.
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
        self.pongs: list[str] = []
        self.closed = False

    async def send_str(self, data: str) -> None:
        self.sent.append(data)
        if data == "{}":
            self.pongs.append(data)

    async def receive(self) -> _WsInbound:
        if self._i < len(self._frames):
            text = self._frames[self._i]
            self._i += 1
            return _WsInbound("text", text)
        if self._raise_exc is not None:
            exc = self._raise_exc
            self._raise_exc = None
            raise exc
        if self._disconnect:
            self._disconnect = False
            return _WsInbound("closed", None)
        await self._closed_event.wait()  # idle healthy socket → unblocked by close()
        return _WsInbound("closed", None)

    async def close(self) -> None:
        self.closed = True
        self._closed_event.set()


class FakeSxWs:
    """An injectable ``connect`` seam over a scripted list of :class:`FakeSxWsConnection`s.

    Each ``await fake.connect(ws_url)`` hands out the next scripted connection (further reconnects get
    a fresh idle connection). ``connect_calls`` counts reconnects for assertions (mirrors ``FakeVenueBookWs``).
    """

    def __init__(self, connections: Iterable[FakeSxWsConnection]) -> None:
        self._connections = list(connections)
        self._idx = 0
        self.connect_calls = 0

    async def connect(self, ws_url: str) -> FakeSxWsConnection:
        self.connect_calls += 1
        i = self._idx
        self._idx += 1
        if i < len(self._connections):
            return self._connections[i]
        extra = FakeSxWsConnection([])  # further reconnects → an idle healthy socket
        self._connections.append(extra)
        return extra


# --------------------------------------------------------------------------- recorder
class JsonlRecorder:
    """Append-only JSONL sink (one row per single-clock sample). Flushed each write."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8")

    def record(self, row: dict[str, Any]) -> None:
        self._fh.write(json.dumps(row, separators=(",", ":")) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


# --------------------------------------------------------------------------- orchestration
async def run_poller(
    *,
    match: MonitoredMatch,
    fv_source: FvSource,
    sx_source: SxBookSource,
    recorder: Recorder,
    now_fn: Callable[[], float],
    poll_interval_s: float,
    stop: asyncio.Event,
) -> int:
    """Run the single-clock capture until *stop* is set. Returns the number of rows recorded.

    Two concurrent tasks share ONE event loop and ONE ``now_fn``:
      * FV task forward-fills ``latest_fv[txline_side] = (value, recv_ts)`` from the SSE stream.
      * SX task polls the 3-way book every ``poll_interval_s``; on each arrival it stamps ONE
        ``recv_ts`` for the whole snapshot and emits three rows (home/draw/away), each pairing the
        SX mid with the concurrently forward-filled FV for the same side — all on the SAME clock.
    """
    latest_fv: dict[str, tuple[float, int]] = {}
    n_rows = 0

    async def fv_task() -> None:
        async for state in fv_source.stream():
            if int(getattr(state, "fixture_id", -1)) != match.fixture_id:
                continue
            recv_ts = int(now_fn())
            for b in match.bindings:
                v = fv_from_state(state, b.txline_side)
                if v is not None:
                    latest_fv[b.txline_side] = (v, recv_ts)
            if stop.is_set():
                return

    async def sx_task() -> None:
        nonlocal n_rows
        hashes = match.market_hashes()
        by_role = {b.market_hash: b for b in match.bindings}
        while not stop.is_set():
            try:
                books = await sx_source.poll(hashes)
                recv_ts = int(now_fn())  # ONE stamp for the whole 3-way snapshot
                for h in hashes:
                    b = by_role[h]
                    q = sx_quote_from_orders(books.get(h, []))
                    fv_pair = latest_fv.get(b.txline_side)
                    fv_val, fv_ts = (fv_pair if fv_pair else (None, None))
                    row = SampleRow(
                        recv_ts=recv_ts,
                        fixture_id=match.fixture_id,
                        role=b.role,
                        sx_bid=q.bid,
                        sx_ask=q.ask,
                        sx_mid=q.mid,
                        sx_bid_size=q.bid_size,
                        sx_ask_size=q.ask_size,
                        sx_n_orders=q.n_orders,
                        fv=fv_val,
                        fv_recv_ts=fv_ts,
                    )
                    recorder.record(row._asdict())
                    n_rows += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — keep polling through transient SX errors
                print(f"  SX poll error: {exc}")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=poll_interval_s)

    fv = asyncio.create_task(fv_task())
    sx = asyncio.create_task(sx_task())
    try:
        await stop.wait()
    finally:
        for t in (fv, sx):
            t.cancel()
        for t in (fv, sx):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
    return n_rows


# --------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="SX Bet vs TxLINE FV single-clock lead-lag poller (read-only, forward capture)."
    )
    p.add_argument("--fixtures", required=True,
                   help="path to fixtures.json (list of {fixture_id, home_team, away_team, kickoff_ts})")
    p.add_argument("--fixture-id", type=int, required=True,
                   help="which fixture_id in --fixtures to capture (must be an UPCOMING/live match)")
    p.add_argument("--out", required=True, help="output JSONL tape path")
    p.add_argument("--poll-interval-s", type=float, default=1.0,
                   help="SX REST poll cadence in seconds (default 1.0; the 30-90s effect is coarse)")
    p.add_argument("--duration-s", type=float, default=0.0,
                   help="auto-stop after N seconds (0 = run until SIGINT/SIGTERM)")
    p.add_argument("--sx-league-id", type=int, default=SX_WORLD_CUP_LEAGUE_ID,
                   help=f"SX leagueId to resolve markets under (default {SX_WORLD_CUP_LEAGUE_ID}, FIFA WC)")
    p.add_argument("--sx-base-url", default=SX_API_BASE)
    p.add_argument("--fv-base-url", default=None, help="TxLINE base URL override (else Settings default)")
    p.add_argument("--use-ws", action="store_true",
                   help="source the SX book from the Centrifugo order-book WebSocket (push-latency, "
                        "seeded from REST) instead of REST polling; needs an SX API key. Default: REST.")
    p.add_argument("--sx-ws-url", default=SX_WS_URL, help=f"SX Centrifugo WS URL (default {SX_WS_URL})")
    return p


async def _amain(args: argparse.Namespace) -> int:
    fixtures = json.loads(Path(args.fixtures).read_text(encoding="utf-8"))
    fixture = next((f for f in fixtures if int(f["fixture_id"]) == args.fixture_id), None)
    if fixture is None:
        raise SystemExit(f"fixture_id {args.fixture_id} not found in {args.fixtures}")

    sx = _DefaultSxBookSource(base_url=args.sx_base_url)
    markets = await sx.active_markets(args.sx_league_id)
    match = resolve_sx_markets(fixture, markets)

    print(f"Resolved SX markets for fixture {match.fixture_id}  {match.home_team} vs {match.away_team}:")
    for b in match.bindings:
        print(f"  · {b.role:<5} ({b.txline_side}) -> {b.outcome_one_name:<14} {b.market_hash}")

    # SX book source: WS (push-latency, seeded from the REST source) when --use-ws, else REST polling.
    # The key is resolved FAIL-CLOSED here (before any I/O); telemetry stays boolean-only.
    print(f"sx_ws_configured: {sx_ws_configured(os.environ)}")
    sx_ws: _WsSxBookSource | None = None
    if args.use_ws:
        sx_ws = _WsSxBookSource(  # raises BEFORE any network I/O if the SX API key is absent
            seed_source=sx,
            ws_url=args.sx_ws_url,
            base_url=args.sx_base_url,
            now_fn=lambda: int(time.time() * 1000),
        )
        await sx_ws.start(match.market_hashes())
        sx_source: SxBookSource = sx_ws
        print(f"SX book via Centrifugo WS ({args.sx_ws_url}); REST-seeded, --use-ws.")
    else:
        sx_source = sx
        print("SX book via public REST (default; pass --use-ws for the Centrifugo WS path).")

    fv_source = _DefaultFvSource(base_url=args.fv_base_url)  # fail-closed on missing creds
    recorder = JsonlRecorder(Path(args.out))
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # pragma: no cover — non-unix
            loop.add_signal_handler(sig, stop.set)

    if args.duration_s and args.duration_s > 0:
        loop.call_later(args.duration_s, stop.set)

    print(f"Capturing to {args.out} (poll {args.poll_interval_s}s). Ctrl-C to stop.")
    try:
        n = await run_poller(
            match=match,
            fv_source=fv_source,
            sx_source=sx_source,
            recorder=recorder,
            now_fn=lambda: time.time() * 1000.0,
            poll_interval_s=args.poll_interval_s,
            stop=stop,
        )
    finally:
        if sx_ws is not None:
            await sx_ws.aclose()
        recorder.close()
    print(f"Recorded {n} rows -> {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
