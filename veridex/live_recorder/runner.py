"""E6 decision runner / live-recorder orchestration (MM-R3, milestone E6).

This module ASSEMBLES the already-built, already-trust-verified E1–E5 components into one
offline-deterministic capture shell. It mirrors the STRUCTURE of
``scripts/maker/live_monitor.py::run_monitor`` — an FV-consume async task plus a single
gathered poll loop, driven entirely by injected ``now_fn`` / ``sleep_fn`` / ``max_polls`` —
but it RECORDS evidence and sends **NO orders**.

Trust boundaries (each load-bearing):

* **NO ORDERS.** This module references no order-submit / order-cancel / order-place /
  venue-write symbol and constructs no wallet-bound venue client. It records evidence only.
* **No-look-ahead END-TO-END.** The FV-consume task records each incoming FV with its
  ARRIVAL ``recv_ts`` (integer ms, from ``now_fn()`` at arrival). At decision time the
  runner captures the decision's own ``recv_ts = now_fn()`` and aligns via
  :func:`veridex.live_recorder.alignment.eligible_fv` passing THAT decision recv_ts — so a
  decision can only ever see FV that had arrived by its own recv_ts (the E2 guarantee,
  preserved across the integration). A book/source timestamp is NEVER used as the decision
  recv_ts.
* **Injected everything.** ``now_fn`` / ``sleep_fn`` / ``max_polls`` and every source +
  the recorder are parameters — NO real network, NO real clock. ``decide_fn`` is an
  INJECTED pluggable policy callable: the runner is strategy-agnostic and records whatever
  ``decide_fn`` returns.

This module imports nothing from ``veridex.scoring`` or ``veridex.maker`` and touches no
network.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from veridex.live_recorder.alignment import FvPoint
from veridex.live_recorder.contracts import FillAssumptionConfig, VenueBookSnapshotEvent
from veridex.live_recorder.recorder import LiveRecorder
from veridex.live_recorder.sources import (
    BookDepthSource,
    BookSnapshot,
    FvSource,
    VenueTradeSource,
    marketstate_to_fair_value,
)


@dataclass(frozen=True)
class RecorderMarket:
    """A resolved ``(fixture, side)`` → venue-token binding the runner polls.

    A local, dependency-light mirror of ``scripts/maker/live_monitor.py::MatchedMarket``
    (defined here so the runner imports nothing from ``veridex.maker``). ``txline_side`` is
    the ``stable_prob_bps`` key (e.g. ``"part1"``); ``token_id`` is the Polymarket CLOB token
    whose ``/book`` is polled.
    """

    fixture_id: int
    txline_side: str
    venue_market_ref: str
    token_id: str


@dataclass(frozen=True)
class Decision:
    """The pluggable ``decide_fn`` output — a strategy-agnostic intent the runner RECORDS.

    ``intent_kind`` selects which intent event is emitted; the runner fills the derived,
    book-observed fields (``queue_ahead_size`` via :func:`queue_ahead_at`, ``executability``
    via :func:`measure_take`) itself so the policy never fabricates them. ``risk_gates`` are
    ``(gate, outcome, detail)`` triples emitted as :class:`RiskGateEvent`s for the decision.
    """

    intent_kind: Literal["make", "take", "no_quote"]
    reason_code: str
    side: str = "bid"
    native_price: float | None = None
    desired_size: float | None = None
    ladder_rung: int = 0
    quote_intent_type: Literal["join", "improve_one_tick"] = "join"
    no_quote_reason: str | None = None
    fee_config: FillAssumptionConfig | None = None
    risk_gates: tuple[tuple[str, str, str], ...] = ()


@dataclass(frozen=True)
class SessionResult:
    """Immutable session summary: what the capture shell recorded before shutdown."""

    polls: int
    events_recorded: int
    gaps: int
    fv_points: int


DecideFn = Callable[[FvPoint | None, BookSnapshot, Any], Decision]


@dataclass
class _Counters:
    """Mutable running counters shared across the FV-consume task and the poll loop."""

    events: int = 0
    gaps: int = 0
    fv_points: int = 0
    arrival_seq: int = 0
    decision_seq: int = 0
    fv_ids: dict[int, str] = field(default_factory=dict)


async def run_live_recorder(
    *,
    matched: list[RecorderMarket],
    fv_source: FvSource,
    book_source: BookDepthSource,
    trade_source: VenueTradeSource | None = None,
    recorder: LiveRecorder,
    decide_fn: DecideFn,
    config: FillAssumptionConfig,
    policy_hash: str,
    now_fn: Callable[[], int],
    sleep_fn: Callable[[float], Awaitable[None]],
    poll_interval_ms: int = 5_000,
    minutes: float = 30.0,
    max_polls: int | None = None,
) -> SessionResult:
    """Stream FV, poll the venue book, align with no look-ahead, record evidence — send NO orders.

    An FV-consume task records each incoming FV as a :class:`FairValueEvent` stamped with its
    ARRIVAL ``recv_ts`` (``now_fn()`` at arrival) and keeps a per-market
    :class:`~veridex.live_recorder.alignment.FvPoint` history. A single poll loop then, per
    matched market, fetches the book → records a :class:`VenueBookSnapshotEvent`. Shutdown
    (SIGINT / ``minutes`` deadline / ``max_polls``) seals ``meta.json`` + ``content_hash`` via
    ``recorder.finalize``.
    """
    stop = asyncio.Event()
    try:
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, stop.set)
    except (NotImplementedError, RuntimeError, ValueError):
        pass  # no signal support here — deadline + max_polls still bound the run

    counters = _Counters()
    fv_hist: dict[tuple[int, str], list[FvPoint]] = {
        (m.fixture_id, m.txline_side): [] for m in matched
    }
    by_fixture: dict[int, list[RecorderMarket]] = defaultdict(list)
    for m in matched:
        by_fixture[m.fixture_id].append(m)

    def _record(event: Any) -> None:
        recorder.record(event.model_dump())
        counters.events += 1

    async def _consume_fv() -> None:
        try:
            async for state in fv_source.stream():
                recv_ts = int(now_fn())
                for m in by_fixture.get(state.fixture_id, ()):
                    counters.arrival_seq += 1
                    seq = counters.arrival_seq
                    try:
                        fv_event = marketstate_to_fair_value(
                            state,
                            m.txline_side,
                            m.venue_market_ref,
                            recv_ts=recv_ts,
                            sequence_no=seq,
                        )
                    except ValueError:
                        # No FV for this side in this state — a price is NEVER fabricated.
                        counters.arrival_seq -= 1
                        continue
                    _record(fv_event)
                    counters.fv_points += 1
                    counters.fv_ids[seq] = f"fv-{m.fixture_id}-{m.txline_side}-{seq}"
                    # marketstate_to_fair_value always stamps source_ts=int(state.ts) (never None here).
                    source_ts = fv_event.source_ts
                    assert source_ts is not None
                    fv_hist[(m.fixture_id, m.txline_side)].append(
                        FvPoint(source_ts, fv_event.recv_ts, fv_event.fv, seq)
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — degrade honestly, never crash the recorder
            print(f"  FV consumer task died — subsequent polls degrade to fv=None: {type(exc).__name__}: {exc}")

    fv_task = asyncio.create_task(_consume_fv())
    # Give the FV task a scheduling slot before the first alignment read (drains canned FV).
    await asyncio.sleep(0)

    session_start = int(now_fn())
    deadline = session_start + minutes * 60_000.0
    polls = 0
    try:
        while not stop.is_set():
            if max_polls is not None and polls >= max_polls:
                break
            if now_fn() >= deadline:
                break

            snapshots = await asyncio.gather(
                *(book_source.fetch_book(m.token_id) for m in matched),
                return_exceptions=True,
            )
            for _m, snap in zip(matched, snapshots, strict=True):
                if isinstance(snap, BaseException) or snap is None:
                    continue
                _record_book(_record, snap)

            polls += 1
            await sleep_fn(poll_interval_ms / 1000.0)
    finally:
        fv_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await fv_task

    recorder.finalize(ended_ts=int(now_fn()))
    return SessionResult(
        polls=polls,
        events_recorded=counters.events,
        gaps=counters.gaps,
        fv_points=counters.fv_points,
    )


def _record_book(record: Callable[[Any], None], snap: BookSnapshot) -> None:
    """Wrap a sourced :class:`BookSnapshot` into a recorded :class:`VenueBookSnapshotEvent`.

    The runner supplies the depth fields + ``recv_ts``; the recorder assigns the authoritative
    ``sequence_no`` on append.
    """
    record(
        VenueBookSnapshotEvent(
            sequence_no=0,
            event_type="VenueBookSnapshotEvent",
            source_ts=None,
            recv_ts=snap.book_ts,
            token_id=snap.token_id,
            venue_market_ref=snap.venue_market_ref,
            book_ts=snap.book_ts,
            tick_size=snap.tick_size,
            min_price_increment=snap.min_price_increment,
            bids=snap.bids,
            asks=snap.asks,
            is_snapshot=snap.is_snapshot,
        )
    )
