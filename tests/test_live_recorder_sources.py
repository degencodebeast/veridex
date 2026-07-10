"""E4 — live sources / public ``/book`` depth adapter tests (MM-R3).

Every source is behind an injectable ``Protocol`` and every test drives it with offline
fakes (canned ``MarketState`` FV, scripted ``/book`` JSON, canned trades) — NO network,
NO real time, NO TxLINE token. The default network adapter is NEVER constructed here
(asserted), and the module imports no network library at module scope.

Mirrors the offline-fake discipline of ``tests/test_maker_live_monitor.py`` and
``tests/test_maker_capture.py``.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any

import pytest

from veridex.ingest.marketstate import MarketState

_FULL_KEY = "1X2_PARTICIPANT_RESULT||"


def _fv_state(fixture_id: int, ts: int, fv_by_side: dict[str, float], *, phase: int = 1, suspended: bool = False) -> MarketState:
    """Build a ``MarketState`` carrying the 1X2 full-match market with the given per-side FV."""
    return MarketState(
        fixture_id=fixture_id,
        tick_seq=0,
        ts=ts,
        phase=phase,
        markets={
            _FULL_KEY: {
                "stable_prob_bps": {side: round(fv * 1e4) for side, fv in fv_by_side.items()},
                "suspended": suspended,
            }
        },
        scores={},
    )


async def _drain(agen: Any) -> list[Any]:
    return [item async for item in agen]


class _FakeResponse:
    """A canned httpx-like response: ``raise_for_status`` is a no-op, ``json`` returns the book."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeHttpClient:
    """An injected httpx-like client: records the GET and returns a canned ``/book`` response. No network."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get(self, url: str, params: dict[str, Any] | None = None) -> _FakeResponse:
        self.calls.append((url, dict(params or {})))
        return _FakeResponse(self._payload)


# --------------------------------------------------------------------------- E4-T1
def test_sources_are_injectable_no_network() -> None:
    """E4-T1: fakes satisfy the source Protocols and NO live client / network lib is built.

    Import-audit: no network lib at module scope. And a fully-injected flow never touches the
    default (network) ``_DefaultBookDepthSource``.
    """
    import veridex.live_recorder.sources as mod
    from veridex.live_recorder.contracts import BookLevel
    from veridex.live_recorder.sources import (
        BookDepthSource,
        BookSnapshot,
        FakeBookDepthSource,
        FakeFvSource,
        FvSource,
    )

    # (a) MODULE-scope import audit — no network library imported at import time.
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
    # No trust-path network client module pulled in at import time either.
    assert "veridex.ingest.live_client" not in source.split("def ")[0]

    # (b) Fakes structurally satisfy the runtime-checkable Protocols.
    fv_fake = FakeFvSource([_fv_state(100, 100, {"part1": 0.6})])
    book_fake = FakeBookDepthSource(
        {"tok": [BookSnapshot(
            token_id="tok",
            venue_market_ref="1X2|home|full",
            book_ts=1,
            tick_size=0.01,
            min_price_increment=0.01,
            bids=(BookLevel(price=0.4, size=5.0),),
            asks=(BookLevel(price=0.6, size=5.0),),
            is_snapshot=True,
        )]}
    )
    assert isinstance(fv_fake, FvSource)
    assert isinstance(book_fake, BookDepthSource)

    # (c) A fully-injected flow must never build the default (network) adapter.
    class _Boom:
        def __init__(self, *a: Any, **k: Any) -> None:
            raise AssertionError("default network source was constructed in an injected run")

    orig = mod._DefaultBookDepthSource
    mod._DefaultBookDepthSource = _Boom  # type: ignore[assignment]
    try:
        states = asyncio.run(_drain(fv_fake.stream()))
        snap = asyncio.run(book_fake.fetch_book("tok"))
    finally:
        mod._DefaultBookDepthSource = orig  # type: ignore[assignment]
    assert len(states) == 1 and states[0].ts == 100
    assert snap is not None and len(snap.bids) == 1 and len(snap.asks) == 1


# --------------------------------------------------------------------------- E4-T2
def test_default_book_source_keeps_depth_not_mid() -> None:
    """E4-T2: the default ``/book`` adapter preserves full depth (levels, not a mid); empty side never imputed."""
    from veridex.live_recorder.sources import _DefaultBookDepthSource

    book = {
        "bids": [{"price": "0.40", "size": "5"}, {"price": "0.39", "size": "7"}, {"price": "0.38", "size": "9"}],
        "asks": [{"price": "0.60", "size": "4"}, {"price": "0.61", "size": "6"}],
        "timestamp": "1710000000000",  # venue-native MILLISECONDS (client.py:529)
    }
    client = _FakeHttpClient(book)
    source = _DefaultBookDepthSource(client=client, tick_size=0.01, min_price_increment=0.01)

    snap = asyncio.run(source.fetch_book("tok-home"))

    # Hit the PUBLIC book endpoint with only the token_id (no wallet/credential).
    assert client.calls == [("/book", {"token_id": "tok-home"})]
    # DEPTH, not mid: all levels preserved, not collapsed to a single number.
    assert snap is not None
    assert len(snap.bids) == 3 and len(snap.asks) == 2
    assert [lvl.price for lvl in snap.bids] == [0.40, 0.39, 0.38]
    assert [lvl.size for lvl in snap.asks] == [4.0, 6.0]
    assert snap.book_ts == 1710000000000
    assert snap.token_id == "tok-home" and snap.is_snapshot is True

    # An empty bids side → empty tuple; snapshot STILL returned; the other side is intact (never imputed).
    empty_book = {"bids": [], "asks": [{"price": "0.60", "size": "4"}], "timestamp": "1710000001"}
    empty_source = _DefaultBookDepthSource(client=_FakeHttpClient(empty_book))
    empty_snap = asyncio.run(empty_source.fetch_book("tok-home"))
    assert empty_snap is not None
    assert empty_snap.bids == ()
    assert len(empty_snap.asks) == 1 and empty_snap.asks[0].price == 0.60


def test_book_snapshot_from_json_sorts_sides() -> None:
    """Unsorted ``/book`` JSON is normalised: asks ascending, bids descending by price."""
    from veridex.live_recorder.sources import book_snapshot_from_json

    book = {
        "bids": [{"price": "0.38", "size": "9"}, {"price": "0.40", "size": "5"}, {"price": "0.39", "size": "7"}],
        "asks": [{"price": "0.62", "size": "4"}, {"price": "0.60", "size": "6"}, {"price": "0.61", "size": "2"}],
        "timestamp": "1710000000000",
    }
    snap = book_snapshot_from_json(
        book, token_id="tok", venue_market_ref="m", tick_size=0.01, min_price_increment=0.01
    )
    assert [lvl.price for lvl in snap.asks] == [0.60, 0.61, 0.62]  # ascending
    assert [lvl.price for lvl in snap.bids] == [0.40, 0.39, 0.38]  # descending


# --------------------------------------------------------------------------- E4-T3
def test_marketstate_to_fair_value_labels_absent_message_id() -> None:
    """E4-T3: ``MarketState`` → ``FairValueEvent`` with an HONEST (gap-labeled) proof reference.

    A ``MarketState`` carries no ``messageId``, so the event MUST have ``message_id=None`` and
    ``proof_status="unavailable_no_message_id"`` — never a fabricated status.
    """
    from veridex.live_recorder.contracts import FairValueEvent
    from veridex.live_recorder.sources import marketstate_to_fair_value

    state = _fv_state(100, 1710000000, {"part1": 0.62}, phase=2, suspended=False)

    ev = marketstate_to_fair_value(
        state,
        "part1",
        "1X2|home|full",
        recv_ts=1710000000_500,
        sequence_no=7,
    )

    assert isinstance(ev, FairValueEvent)
    assert ev.message_id is None
    assert ev.proof_status == "unavailable_no_message_id"
    assert ev.proof_ts is None
    # FV + context carried through honestly.
    assert abs(ev.fv - 0.62) < 1e-9
    assert ev.fixture_id == 100 and ev.side == "part1" and ev.market_ref == "1X2|home|full"
    assert ev.phase == 2 and ev.suspended is False
    assert ev.source_ts == 1710000000 and ev.recv_ts == 1710000000_500
    assert ev.sequence_no == 7 and ev.event_type == "FairValueEvent"


def test_marketstate_to_fair_value_rejects_missing_fv() -> None:
    """E4-T3: a state with no FV for the side cannot fabricate an event — it raises (never invents a price)."""
    from veridex.live_recorder.sources import marketstate_to_fair_value

    state = _fv_state(100, 1710000000, {"part1": 0.62})
    with pytest.raises(ValueError):
        marketstate_to_fair_value(state, "part2", "1X2|away|full", recv_ts=1, sequence_no=0)


# --------------------------------------------------------------------------- E4-T4
def test_missing_credential_fails_closed_and_no_secret_in_output(capsys, tmp_path):
    """E4-T4: absent creds fail CLOSED (raise before I/O); scrubbing keeps the token out of any output."""
    from veridex.live_recorder.sources import _scrub, configured, require_live_creds

    # Fail closed on absent creds — raises before any I/O.
    with pytest.raises(ValueError):
        require_live_creds(env={})

    # A partial env still fails closed (both required creds must be present).
    with pytest.raises(ValueError):
        require_live_creds(env={"JWT": "only-one"})

    # Present creds resolve to the pair (never logged).
    fake_jwt, fake_token = "FAKE-JWT-abc123", "FAKE-APITOKEN-xyz789"
    creds = require_live_creds(env={"JWT": fake_jwt, "TXLINE_X_API_TOKEN": fake_token})
    assert creds == (fake_jwt, fake_token)

    # configured() is a boolean-only telemetry helper — never the secret value.
    assert configured({"JWT": fake_jwt, "TXLINE_X_API_TOKEN": fake_token}) is True
    assert configured({}) is False
    assert configured({"JWT": fake_jwt}) is False

    # _scrub strips the FAKE token value from any error/log text before printing.
    leaky = f"boom auth failed jwt={fake_jwt} token={fake_token} in url"
    scrubbed = _scrub(leaky, fake_jwt, fake_token)
    assert fake_jwt not in scrubbed and fake_token not in scrubbed
    assert "REDACTED" in scrubbed

    # If any artifact is written, it + captured stdout/stderr must be ABSENT the token value.
    print(scrubbed)  # exercise the print path
    artifact = tmp_path / "telemetry.json"
    import json as _json
    artifact.write_text(_json.dumps({"configured": configured({"JWT": fake_jwt, "TXLINE_X_API_TOKEN": fake_token})}))
    captured = capsys.readouterr()
    blob = artifact.read_text()
    assert fake_jwt not in blob and fake_token not in blob
    assert fake_jwt not in captured.out and fake_token not in captured.out
    assert fake_jwt not in captured.err and fake_token not in captured.err
    assert "configured" in blob
