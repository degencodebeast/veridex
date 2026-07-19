#!/usr/bin/env python3
"""Bounded operator smoke for the public arena WebSocket upgrade and replay path."""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import os
import ssl
import struct
import sys
import threading
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any, NamedTuple
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


class SmokeError(RuntimeError):
    """Raised when public WebSocket acceptance cannot be proven."""


class SmokeResult(NamedTuple):
    """Sequences proven by one successful smoke run."""

    first_seq: int
    replayed_seqs: tuple[int, ...]


FetchEvents = Callable[[str, float], Awaitable[list[dict[str, Any]]]]
ConnectFactory = Callable[..., Any]

_WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_HTTP_RESPONSE_BYTES = 1024 * 1024
_MAX_CANONICAL_EVENTS = 10_000


def _urls(base_url: str, competition_id: str, since_seq: int) -> tuple[str, str]:
    parsed = urlsplit(base_url.rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SmokeError("BASE_URL must be an absolute http:// or https:// URL")
    if parsed.username is not None or parsed.password is not None:
        raise SmokeError("BASE_URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise SmokeError("BASE_URL must not contain a query string or fragment")

    competition = quote(competition_id, safe="")
    prefix = parsed.path.rstrip("/")
    query = urlencode({"since_seq": since_seq})
    rest_path = f"{prefix}/competitions/{competition}/events"
    ws_path = f"{prefix}/competitions/{competition}/arena"
    rest_url = urlunsplit((parsed.scheme, parsed.netloc, rest_path, query, ""))
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    ws_url = urlunsplit((ws_scheme, parsed.netloc, ws_path, query, ""))
    return rest_url, ws_url


async def _fetch_events(url: str, timeout: float) -> list[dict[str, Any]]:
    def fetch() -> list[dict[str, Any]]:
        request = Request(url, headers={"Accept": "application/json"})
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - validated http(s) URL
            if response.status != 200:
                raise SmokeError(f"canonical event tail returned HTTP {response.status}")
            body = response.read(_MAX_HTTP_RESPONSE_BYTES + 1)
        if len(body) > _MAX_HTTP_RESPONSE_BYTES:
            raise SmokeError("canonical event response exceeded the response-size bound")
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SmokeError("canonical event tail returned invalid JSON") from exc
        if not isinstance(payload, list):
            raise SmokeError("canonical event tail must be a JSON array")
        return payload

    loop = asyncio.get_running_loop()
    result: asyncio.Future[list[dict[str, Any]]] = loop.create_future()

    def publish(*, value: list[dict[str, Any]] | None = None, error: BaseException | None = None) -> None:
        if result.done():
            return
        if error is not None:
            result.set_exception(error)
        else:
            assert value is not None
            result.set_result(value)

    def run_fetch() -> None:
        try:
            value = fetch()
        except BaseException as exc:
            callback = partial(publish, error=exc)
        else:
            callback = partial(publish, value=value)
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(callback)

    threading.Thread(target=run_fetch, name="public-smoke-http", daemon=True).start()
    return await asyncio.wait_for(result, timeout=timeout)


def _validate_canonical(events: list[dict[str, Any]]) -> None:
    if len(events) > _MAX_CANONICAL_EVENTS:
        raise SmokeError("canonical event tail exceeded the event-count bound")
    if len(events) < 2:
        raise SmokeError("canonical event tail needs at least two events to prove reconnect replay")
    seqs: list[int] = []
    for event in events:
        if not isinstance(event, dict) or type(event.get("seq")) is not int:
            raise SmokeError("canonical event tail contains an event without an integer seq")
        seqs.append(event["seq"])
    if seqs != sorted(seqs) or len(seqs) != len(set(seqs)):
        raise SmokeError("canonical event tail sequences must be strictly increasing and unique")


async def _recv_json(socket: Any, timeout: float) -> dict[str, Any]:
    raw = await asyncio.wait_for(socket.recv(), timeout=timeout)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        event = json.loads(raw)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SmokeError("WebSocket returned a non-JSON event") from exc
    if not isinstance(event, dict):
        raise SmokeError("WebSocket event must be a JSON object")
    return event


class _WebSocketConnection:
    """Minimal RFC 6455 client for one read-only smoke connection."""

    def __init__(self, url: str, *, open_timeout: float, close_timeout: float, max_size: int) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in {"ws", "wss"} or parsed.hostname is None:
            raise SmokeError("WebSocket URL must use ws:// or wss://")
        if parsed.username is not None or parsed.password is not None:
            raise SmokeError("WebSocket URL must not contain credentials")
        self._parsed = parsed
        self._open_timeout = open_timeout
        self._close_timeout = close_timeout
        self._max_size = max_size
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def __aenter__(self) -> _WebSocketConnection:
        port = self._parsed.port or (443 if self._parsed.scheme == "wss" else 80)
        tls = ssl.create_default_context() if self._parsed.scheme == "wss" else None
        connect = asyncio.open_connection(
            self._parsed.hostname,
            port,
            ssl=tls,
            server_hostname=self._parsed.hostname if tls is not None else None,
        )
        self._reader, self._writer = await asyncio.wait_for(connect, timeout=self._open_timeout)
        try:
            await asyncio.wait_for(self._handshake(), timeout=self._open_timeout)
        except BaseException:
            await self._close_transport()
            raise
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._writer is not None and not self._writer.is_closing():
            with contextlib.suppress(OSError, TimeoutError):
                await asyncio.wait_for(self._send_frame(0x8, struct.pack("!H", 1000)), self._close_timeout)
        await self._close_transport()

    async def _handshake(self) -> None:
        if self._reader is None or self._writer is None:
            raise SmokeError("WebSocket transport was not opened")
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        target = urlunsplit(("", "", self._parsed.path or "/", self._parsed.query, ""))
        request = (
            f"GET {target} HTTP/1.1\r\n"
            f"Host: {self._parsed.netloc}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self._writer.write(request.encode("ascii"))
        await self._writer.drain()
        try:
            raw_headers = await self._reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
            raise SmokeError("WebSocket Upgrade returned an incomplete HTTP response") from exc

        lines = raw_headers.decode("iso-8859-1").split("\r\n")
        if len(lines) < 2 or not lines[0].startswith("HTTP/1.1 101 "):
            status = lines[0] if lines else "no status line"
            raise SmokeError(f"WebSocket Upgrade expected HTTP 101, got {status}")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line:
                continue
            name, separator, value = line.partition(":")
            if not separator:
                raise SmokeError("WebSocket Upgrade returned a malformed header")
            headers[name.strip().lower()] = value.strip()

        expected_accept = base64.b64encode(
            hashlib.sha1(f"{key}{_WEBSOCKET_GUID}".encode("ascii"), usedforsecurity=False).digest()
        ).decode("ascii")
        connection_tokens = {token.strip().lower() for token in headers.get("connection", "").split(",")}
        if headers.get("upgrade", "").lower() != "websocket" or "upgrade" not in connection_tokens:
            raise SmokeError("HTTP 101 response did not confirm WebSocket Upgrade")
        if headers.get("sec-websocket-accept") != expected_accept:
            raise SmokeError("WebSocket Upgrade returned an invalid Sec-WebSocket-Accept")

    async def recv(self) -> str:
        message = bytearray()
        message_started = False
        while True:
            final, opcode, payload = await self._read_frame()
            if opcode == 0x8:
                raise SmokeError("WebSocket closed before the expected canonical event arrived")
            if opcode == 0x9:
                await self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x1:
                if message_started:
                    raise SmokeError("WebSocket started a new text message before finishing the previous one")
                message_started = True
                message.extend(payload)
            elif opcode == 0x0 and message_started:
                message.extend(payload)
            else:
                raise SmokeError(f"WebSocket returned unsupported frame opcode {opcode}")
            if len(message) > self._max_size:
                raise SmokeError("WebSocket event exceeded the smoke client's message-size bound")
            if final:
                try:
                    return message.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise SmokeError("WebSocket text event was not valid UTF-8") from exc

    async def _read_frame(self) -> tuple[bool, int, bytes]:
        if self._reader is None:
            raise SmokeError("WebSocket transport was not opened")
        try:
            first, second = await self._reader.readexactly(2)
            final = bool(first & 0x80)
            if first & 0x70:
                raise SmokeError("WebSocket response used an unnegotiated extension")
            opcode = first & 0x0F
            if second & 0x80:
                raise SmokeError("WebSocket server frames must not be masked")
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", await self._reader.readexactly(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", await self._reader.readexactly(8))[0]
            if opcode >= 0x8 and (not final or length > 125):
                raise SmokeError("WebSocket returned an invalid control frame")
            if length > self._max_size:
                raise SmokeError("WebSocket frame exceeded the smoke client's message-size bound")
            payload = await self._reader.readexactly(length)
        except asyncio.IncompleteReadError as exc:
            raise SmokeError("WebSocket connection ended during a frame") from exc
        return final, opcode, payload

    async def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._writer is None:
            raise SmokeError("WebSocket transport was not opened")
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header = struct.pack("!BB", 0x80 | opcode, 0x80 | length)
        elif length <= 0xFFFF:
            header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, length)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self._writer.write(header + mask + masked)
        await self._writer.drain()

    async def _close_transport(self) -> None:
        if self._writer is None:
            return
        self._writer.close()
        with contextlib.suppress(OSError, TimeoutError):
            await asyncio.wait_for(self._writer.wait_closed(), timeout=self._close_timeout)


def _default_connect(url: str, **options: object) -> Any:
    open_timeout = float(options["open_timeout"])
    close_timeout = float(options["close_timeout"])
    max_size = int(options["max_size"])
    return _WebSocketConnection(
        url,
        open_timeout=open_timeout,
        close_timeout=close_timeout,
        max_size=max_size,
    )


def _remaining(deadline: float) -> float:
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise TimeoutError("public WebSocket acceptance deadline exceeded")
    return remaining


async def run_public_ws_smoke(
    base_url: str,
    competition_id: str,
    *,
    timeout: float = 15,
    quiet_timeout: float = 0.25,
    fetch_events: FetchEvents = _fetch_events,
    connect_factory: ConnectFactory = _default_connect,
) -> SmokeResult:
    """Prove public upgrade, canonical delivery, and exclusive reconnect replay."""
    if timeout <= 0 or quiet_timeout <= 0:
        raise SmokeError("timeouts must be positive")

    deadline = asyncio.get_running_loop().time() + timeout
    async with asyncio.timeout_at(deadline):
        rest_url, initial_ws_url = _urls(base_url, competition_id, since_seq=0)
        canonical = await fetch_events(rest_url, _remaining(deadline))
        _validate_canonical(canonical)

        def connection_options() -> dict[str, float | int]:
            remaining = _remaining(deadline)
            return {
                "open_timeout": remaining,
                "close_timeout": min(remaining, 5),
                "ping_timeout": remaining,
                "max_size": 1024 * 1024,
                "max_queue": 16,
            }

        async with connect_factory(initial_ws_url, **connection_options()) as socket:
            first = await _recv_json(socket, _remaining(deadline))
        if first != canonical[0]:
            raise SmokeError("first WebSocket event does not match the canonical REST event")

        first_seq = first["seq"]
        expected_tail = [event for event in canonical if event["seq"] > first_seq]
        _, reconnect_ws_url = _urls(base_url, competition_id, since_seq=first_seq)
        replayed: list[dict[str, Any]] = []
        async with connect_factory(reconnect_ws_url, **connection_options()) as socket:
            for _ in expected_tail:
                replayed.append(await _recv_json(socket, _remaining(deadline)))
            remaining = _remaining(deadline)
            deadline_limited = remaining <= quiet_timeout
            try:
                extra = await _recv_json(socket, min(quiet_timeout, remaining))
            except TimeoutError:
                if deadline_limited:
                    raise
            else:
                raise SmokeError(f"reconnect returned an unexpected extra event with seq={extra.get('seq')!r}")

        if replayed != expected_tail:
            raise SmokeError("reconnect did not return the exact canonical missing tail")
        replayed_seqs = tuple(event["seq"] for event in replayed)
        combined = (first_seq, *replayed_seqs)
        if len(combined) != len(set(combined)):
            raise SmokeError("duplicate sequence observed across disconnect and reconnect")
        return SmokeResult(first_seq=first_seq, replayed_seqs=replayed_seqs)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url")
    parser.add_argument("competition_id")
    parser.add_argument("--timeout", type=float, default=15)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        result = asyncio.run(run_public_ws_smoke(args.base_url, args.competition_id, timeout=args.timeout))
    except (OSError, SmokeError, TimeoutError) as exc:
        print(f"WS_SMOKE_FAILED: {exc}", file=sys.stderr)
        return 1

    tail = ",".join(str(seq) for seq in result.replayed_seqs)
    print(f"WS_SMOKE_OK competition_id={args.competition_id} first_seq={result.first_seq} replayed={tail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
