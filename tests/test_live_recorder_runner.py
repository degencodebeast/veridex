"""E6 — decision runner / live-recorder orchestration tests (MM-R3).

The runner ASSEMBLES the already-built, already-trust-verified E1–E5 components: it
streams FV (recording each arrival with its integer-ms ``recv_ts``), polls the venue
book, aligns FV with the E2 two-dimensional no-look-ahead rule *using the decision's own
recv_ts*, and records a DecisionEvent + matching intent + latency + risk-gate line per
poll. It sends NO orders (records evidence only), writes an honest gap on a per-poll
failure (the session continues), and seals ``meta.json`` + ``content_hash`` on shutdown.

Every test drives the runner with offline fakes (canned ``MarketState`` FV, scripted
``/book`` snapshots, a real on-disk ``LiveRecorder``) and an INJECTED clock/sleep +
``max_polls`` — NO network, NO real time. Mirrors the offline-fake discipline of
``tests/test_live_recorder_sources.py`` and ``tests/test_maker_live_monitor.py``.
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from typing import Any

from veridex.ingest.marketstate import MarketState
from veridex.live_recorder.contracts import FillAssumptionConfig, LiveRecorderSessionMeta
from veridex.live_recorder.sources import (
    BookSnapshot,
    FakeBookDepthSource,
    FakeFvSource,
)
from veridex.live_recorder.contracts import BookLevel

_FULL_KEY = "1X2_PARTICIPANT_RESULT||"


def _fv_state(
    fixture_id: int,
    ts: int,
    fv_by_side: dict[str, float],
    *,
    phase: int = 1,
    suspended: bool = False,
) -> MarketState:
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


def _snap(token_id: str, *, book_ts: int = 1, bid: float = 0.4, ask: float = 0.6, size: float = 8.0) -> BookSnapshot:
    """A minimal two-sided full-depth book snapshot for the given token."""
    return BookSnapshot(
        token_id=token_id,
        venue_market_ref="1X2|home|full",
        book_ts=book_ts,
        tick_size=0.01,
        min_price_increment=0.01,
        bids=(BookLevel(price=bid, size=size),),
        asks=(BookLevel(price=ask, size=size),),
        is_snapshot=True,
    )


def _counter_clock(start: int = 1_000) -> Any:
    """A deterministic, strictly increasing integer-ms clock (one tick per call)."""
    state = {"now": start}

    def now() -> int:
        state["now"] += 1
        return state["now"]

    return now


async def _noop_sleep(_seconds: float) -> None:
    return None


def _start_meta(fixture_ids: tuple[int, ...] = (100,)) -> LiveRecorderSessionMeta:
    return LiveRecorderSessionMeta(
        session_ts=1_700_000_000,
        endpoints={"venue": "wss://example.invalid"},
        tool_version="test-e6",
        config_hash="cfg-hash",
        source_provenance={"venue": "poly"},
        fixture_ids=fixture_ids,
    )


def _config() -> FillAssumptionConfig:
    return FillAssumptionConfig(
        taker_fee_bps=10.0,
        fee_stress_multiplier=1.0,
        spread_assumption=0.0,
        slippage_assumption=0.0,
    )


# --------------------------------------------------------------------------- E6-T1
def test_runner_sends_no_orders(tmp_path: Path) -> None:
    """E6-T1: the runner records evidence and references NO order/write symbol anywhere.

    (a) Source audit: ``runner.py`` contains no ``submit_order``/``cancel_order``/
        ``place_order``/venue-write symbol and imports nothing from ``veridex.maker`` or
        ``veridex.scoring``.
    (b) A fully-injected offline run (fake FV, scripted book, real recorder, ``max_polls``)
        completes and returns a ``SessionResult`` — the fakes expose NO order API, so a
        completed run is proof the runner never attempted an order.
    """
    import veridex.live_recorder.runner as mod
    from veridex.live_recorder.runner import (
        Decision,
        RecorderMarket,
        SessionResult,
        run_live_recorder,
    )

    # (a) source audit — forbidden order/write symbols and trust-path imports never appear.
    source = Path(mod.__file__).read_text()
    forbidden = ("submit_order", "cancel_order", "place_order", "create_order", "post_order")
    for symbol in forbidden:
        assert symbol not in source, f"runner references an order/write symbol: {symbol}"
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")
            assert root[:2] != ["veridex", "maker"], f"runner imports veridex.maker ({node.module})"
            assert root[:2] != ["veridex", "scoring"], f"runner imports veridex.scoring ({node.module})"

    # (b) fully-injected offline run — completes, returns a SessionResult, touches no order API.
    from veridex.live_recorder.recorder import LiveRecorder

    matched = [RecorderMarket(100, "part1", "1X2|home|full", "tok")]
    fv = FakeFvSource([_fv_state(100, 1_700_000_000, {"part1": 0.6})])
    book = FakeBookDepthSource({"tok": [_snap("tok"), _snap("tok"), _snap("tok")]})
    recorder = LiveRecorder(tmp_path, _start_meta())

    def decide(_aligned: Any, _snapshot: Any, _config: Any) -> Decision:
        return Decision(intent_kind="no_quote", reason_code="skip", no_quote_reason="stale")

    result = asyncio.run(
        run_live_recorder(
            matched=matched,
            fv_source=fv,
            book_source=book,
            recorder=recorder,
            decide_fn=decide,
            config=_config(),
            policy_hash="pol-hash",
            now_fn=_counter_clock(),
            sleep_fn=_noop_sleep,
            poll_interval_ms=5_000,
            minutes=30.0,
            max_polls=2,
        )
    )
    recorder.close()

    assert isinstance(result, SessionResult)
    assert result.polls == 2
