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


# --------------------------------------------------------------------------- E4-T1
def test_sources_are_injectable_no_network() -> None:
    """E4-T1: fakes satisfy the source Protocols and NO live client / network lib is built.

    Import-audit: no network lib at module scope. And a fully-injected flow never touches the
    default (network) ``_DefaultBookDepthSource``.
    """
    import veridex.live_recorder.sources as mod
    from veridex.live_recorder.sources import (
        BookDepthSource,
        BookSnapshot,
        FakeBookDepthSource,
        FakeFvSource,
        FvSource,
    )
    from veridex.live_recorder.contracts import BookLevel

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
