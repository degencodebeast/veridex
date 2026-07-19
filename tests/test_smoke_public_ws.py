"""Focused tests for the operator-run public WebSocket smoke client."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "scripts" / "smoke_public_ws.py"
SMOKE = ROOT / "scripts" / "smoke_public.sh"
MAX_HTTP_RESPONSE_BYTES = 1024 * 1024
MAX_CANONICAL_EVENTS = 10_000


def _load_client() -> ModuleType:
    assert CLIENT.is_file(), "scripts/smoke_public_ws.py must provide the public WebSocket smoke"
    spec = importlib.util.spec_from_file_location("smoke_public_ws", CLIENT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeSocket:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self._messages = [json.dumps(message) for message in messages]

    async def recv(self) -> str:
        if self._messages:
            return self._messages.pop(0)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _FakeConnection:
    def __init__(self, socket: _FakeSocket) -> None:
        self._socket = socket

    async def __aenter__(self) -> _FakeSocket:
        return self._socket

    async def __aexit__(self, *_exc: object) -> None:
        return None


async def test_public_ws_smoke_reconnects_with_exact_exclusive_tail() -> None:
    module = _load_client()
    canonical = [{"competition_id": "c public/id", "seq": seq, "event_type": "market_tick"} for seq in (1, 2, 3)]
    requested_rest_urls: list[str] = []
    connected_ws_urls: list[str] = []
    sockets = [_FakeSocket(canonical[:1]), _FakeSocket(canonical[1:])]

    async def fetch_events(url: str, timeout: float) -> list[dict[str, Any]]:
        assert 0 < timeout <= 2
        requested_rest_urls.append(url)
        return canonical

    def connect_factory(url: str, **options: object) -> _FakeConnection:
        assert 0 < options["open_timeout"] <= 2
        assert 0 < options["close_timeout"] <= 2
        connected_ws_urls.append(url)
        return _FakeConnection(sockets.pop(0))

    result = await module.run_public_ws_smoke(
        "https://arena.example.test/proxy/",
        "c public/id",
        timeout=2,
        quiet_timeout=0.01,
        fetch_events=fetch_events,
        connect_factory=connect_factory,
    )

    assert requested_rest_urls == ["https://arena.example.test/proxy/competitions/c%20public%2Fid/events?since_seq=0"]
    assert connected_ws_urls == [
        "wss://arena.example.test/proxy/competitions/c%20public%2Fid/arena?since_seq=0",
        "wss://arena.example.test/proxy/competitions/c%20public%2Fid/arena?since_seq=1",
    ]
    assert result.first_seq == 1
    assert result.replayed_seqs == (2, 3)


async def test_public_ws_smoke_uses_one_global_deadline() -> None:
    module = _load_client()
    canonical = [{"seq": seq} for seq in (1, 2, 3)]

    async def fetch_events(_url: str, _timeout: float) -> list[dict[str, Any]]:
        await asyncio.sleep(0.02)
        return canonical

    class _DelayedSocket(_FakeSocket):
        async def recv(self) -> str:
            await asyncio.sleep(0.02)
            return await super().recv()

    sockets = [_DelayedSocket(canonical[:1]), _DelayedSocket(canonical[1:])]

    def connect_factory(_url: str, **_options: object) -> _FakeConnection:
        return _FakeConnection(sockets.pop(0))

    with pytest.raises(TimeoutError):
        await module.run_public_ws_smoke(
            "https://arena.example.test",
            "c_deadline",
            timeout=0.05,
            quiet_timeout=0.001,
            fetch_events=fetch_events,
            connect_factory=connect_factory,
        )


class _FakeHttpResponse:
    status = 200

    def __init__(self, read) -> None:
        self.read = read

    def __enter__(self):
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


async def test_http_body_read_obeys_timeout(monkeypatch) -> None:
    module = _load_client()

    def slow_read(_size: int = -1) -> bytes:
        time.sleep(0.1)
        return b"[]"

    monkeypatch.setattr(module, "urlopen", lambda *_args, **_kwargs: _FakeHttpResponse(slow_read))
    started = asyncio.get_running_loop().time()
    with pytest.raises(TimeoutError):
        await module._fetch_events("https://arena.example.test/events", timeout=0.02)
    assert asyncio.get_running_loop().time() - started < 0.08


async def test_http_body_read_rejects_oversize_response(monkeypatch) -> None:
    module = _load_client()

    def oversize_when_bounded(size: int = -1) -> bytes:
        if size < 0:
            return b"[]"
        return b" " * size

    response = _FakeHttpResponse(oversize_when_bounded)
    monkeypatch.setattr(module, "urlopen", lambda *_args, **_kwargs: response)

    with pytest.raises(module.SmokeError, match="response-size bound"):
        await module._fetch_events("https://arena.example.test/events", timeout=1)


def test_canonical_event_count_is_bounded() -> None:
    module = _load_client()
    events = [{"seq": seq} for seq in range(1, MAX_CANONICAL_EVENTS + 2)]

    with pytest.raises(module.SmokeError, match="event-count bound"):
        module._validate_canonical(events)


def test_default_transport_needs_no_undeclared_websocket_package(monkeypatch) -> None:
    module = _load_client()
    monkeypatch.setitem(sys.modules, "websockets", None)

    connection = module._default_connect(
        "ws://127.0.0.1:8000/competitions/c/arena?since_seq=0",
        open_timeout=1,
        close_timeout=1,
        ping_timeout=1,
        max_size=1024,
        max_queue=1,
    )

    assert hasattr(connection, "__aenter__")
    assert hasattr(connection, "__aexit__")


def test_public_smoke_help_documents_websocket_acceptance_inputs() -> None:
    proc = subprocess.run(
        ["bash", str(SMOKE), "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0
    assert "WS_COMPETITION_ID" in proc.stdout
    assert "WebSocket" in proc.stdout
