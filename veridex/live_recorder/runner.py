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
import hashlib
import json
import signal
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from veridex.live_recorder.alignment import FvPoint, eligible_fv
from veridex.live_recorder.contracts import (
    DecisionEvent,
    FillAssumptionConfig,
    LatencyEvent,
    NoQuoteIntentEvent,
    QuoteIntentEvent,
    RecorderHeartbeatEvent,
    RiskGateEvent,
    TakeIntentEvent,
    VenueBookSnapshotEvent,
)
from veridex.live_recorder.executability import measure_take, queue_ahead_at
from veridex.live_recorder.recorder import LiveRecorder
from veridex.live_recorder.sources import (
    BookDepthSource,
    BookSnapshot,
    FvSource,
    marketstate_to_fair_value,
)

_NO_QUOTE_REASONS: tuple[str, ...] = (
    "stale",
    "event_suspension",
    "boundary",
    "fee_negative",
    "liquidity_missing",
    "risk_cap",
    "observe_only",
)


def _canonical_hash(payload: dict[str, Any]) -> str:
    """sha256 hexdigest of a canonical JSON dump (stable, sorted keys) of the decision inputs."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _mapping_hash(matched: list[RecorderMarket]) -> str:
    """CON-003 provenance: a stable sha256 over the resolved ``(fixture_id, side, token_id)`` set."""
    resolved = sorted(
        [m.fixture_id, m.txline_side, m.token_id] for m in matched
    )
    canonical = json.dumps(resolved, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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

    def _emit_decision(
        m: RecorderMarket,
        snap: BookSnapshot,
        book_id: str,
        aligned: FvPoint | None,
        book_obs_ts: int,
        decision_recv_ts: int,
    ) -> None:
        """Align → decide → record ONE DecisionEvent + matching intent + latency + risk-gates."""
        decision = decide_fn(aligned, snap, config)
        counters.decision_seq += 1
        decision_id = f"dec-{counters.decision_seq}"
        fv_event_id = counters.fv_ids.get(aligned.sequence_no, "unaligned") if aligned is not None else "unaligned"
        config_hash = config.config_hash()

        intent_kind = decision.intent_kind
        # Neutral honest default: a policy that abstains without naming a market condition is
        # "observe_only" (a policy-abstain reason), NOT a fabricated "liquidity_missing" claim.
        no_quote_reason = decision.no_quote_reason or "observe_only"
        executability = None
        queue_ahead: float | None = None
        if intent_kind in ("make", "take"):
            if decision.native_price is None or decision.desired_size is None:
                raise ValueError(f"{intent_kind} decision requires native_price and desired_size")
            if intent_kind == "take":
                # The session config is the pinned/attested one (its hash is stamped onto
                # DecisionEvent.config_hash). Bind measure_take to it so a decision.fee_config
                # whose hash differs from the sealed config RAISES (EXE-004/AC-010) — a
                # measurement can never be sealed under an unattested config.
                fee_config = decision.fee_config or config
                executability = measure_take(
                    snap,
                    decision.native_price,
                    decision.desired_size,
                    fee_config,
                    pinned_config_hash=config_hash,
                )
                if executability is None:
                    # No observable ask depth to clear against — abstain honestly (never a fabricated fill).
                    intent_kind = "no_quote"
                    no_quote_reason = "liquidity_missing"
            else:
                queue_ahead = queue_ahead_at(snap, decision.side, decision.native_price)

        fv_recv_ts = aligned.recv_ts if aligned is not None else decision_recv_ts
        model_inputs_hash = _canonical_hash(
            {
                "fv_event_id": fv_event_id,
                "book_snapshot_id": book_id,
                "config_hash": config_hash,
                "policy_hash": policy_hash,
                "fv_recv_ts": fv_recv_ts,
                "decision_recv_ts": decision_recv_ts,
                "intent_kind": intent_kind,
            }
        )

        _record(
            DecisionEvent(
                sequence_no=0,
                event_type="DecisionEvent",
                source_ts=None,
                recv_ts=decision_recv_ts,
                decision_id=decision_id,
                fixture_id=m.fixture_id,
                market_ref=m.venue_market_ref,
                side=m.txline_side,
                intent_kind=intent_kind,
                fv_event_id=fv_event_id,
                book_snapshot_id=book_id,
                reason_code=decision.reason_code,
                config_hash=config_hash,
                policy_hash=policy_hash,
                model_inputs_hash=model_inputs_hash,
            )
        )

        if intent_kind == "make":
            assert decision.native_price is not None and decision.desired_size is not None
            _record(
                QuoteIntentEvent(
                    sequence_no=0,
                    event_type="QuoteIntentEvent",
                    source_ts=None,
                    recv_ts=decision_recv_ts,
                    decision_id=decision_id,
                    native_price=decision.native_price,
                    desired_size=decision.desired_size,
                    side=decision.side,
                    ladder_rung=decision.ladder_rung,
                    quote_intent_type=decision.quote_intent_type,
                    queue_ahead_size=queue_ahead,
                )
            )
        elif intent_kind == "take":
            assert decision.native_price is not None and decision.desired_size is not None
            assert executability is not None
            _record(
                TakeIntentEvent(
                    sequence_no=0,
                    event_type="TakeIntentEvent",
                    source_ts=None,
                    recv_ts=decision_recv_ts,
                    decision_id=decision_id,
                    native_price=decision.native_price,
                    desired_size=decision.desired_size,
                    side=decision.side,
                    executability=executability,
                )
            )
        else:
            if no_quote_reason not in _NO_QUOTE_REASONS:
                raise ValueError(f"no_quote_reason {no_quote_reason!r} is not in the closed set {_NO_QUOTE_REASONS}")
            _record(
                NoQuoteIntentEvent(
                    sequence_no=0,
                    event_type="NoQuoteIntentEvent",
                    source_ts=None,
                    recv_ts=decision_recv_ts,
                    decision_id=decision_id,
                    no_quote_reason=cast(Any, no_quote_reason),
                )
            )

        _record(
            LatencyEvent(
                sequence_no=0,
                event_type="LatencyEvent",
                source_ts=None,
                recv_ts=decision_recv_ts,
                decision_id=decision_id,
                fv_recv_ts=fv_recv_ts,
                decision_ts=decision_recv_ts,
                book_obs_ts=book_obs_ts,
                chain_ms={
                    "fv_to_book": book_obs_ts - fv_recv_ts,
                    "book_to_decision": decision_recv_ts - book_obs_ts,
                },
            )
        )

        for gate, outcome, detail in decision.risk_gates:
            if outcome not in ("pass", "block"):
                raise ValueError(f"risk-gate outcome {outcome!r} must be 'pass' or 'block'")
            _record(
                RiskGateEvent(
                    sequence_no=0,
                    event_type="RiskGateEvent",
                    source_ts=None,
                    recv_ts=decision_recv_ts,
                    decision_id=decision_id,
                    gate=gate,
                    outcome=cast(Any, outcome),
                    detail=detail,
                )
            )

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
            # TYPE ONLY — never print the raw exception value (a custom FV source's
            # exception text could carry a secret).
            print(f"  FV consumer task died — subsequent polls degrade to fv=None: {type(exc).__name__}")

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
            venue_mids_seen = 0
            fv_aligned_this_poll = False
            for m, snap in zip(matched, snapshots, strict=True):
                if isinstance(snap, BaseException):
                    # One bad book never aborts the round: write an honest, labeled gap and continue.
                    gap_ts = int(now_fn())
                    recorder.record_gap(
                        from_ts=gap_ts,
                        to_ts=gap_ts,
                        source=m.token_id,
                        reason=f"book fetch failed: {type(snap).__name__}",
                    )
                    counters.gaps += 1
                    continue
                if snap is None:
                    continue
                venue_mids_seen += 1
                # Book observed on the recorder clock; book_ts stays the venue-native ms.
                book_obs_ts = int(now_fn())
                book_id = f"book-{m.token_id}-{book_obs_ts}"
                _record(
                    VenueBookSnapshotEvent(
                        sequence_no=0,
                        event_type="VenueBookSnapshotEvent",
                        source_ts=None,
                        recv_ts=book_obs_ts,
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
                # Decision's OWN recv_ts drives eligible_fv — the end-to-end no-look-ahead guarantee.
                decision_recv_ts = int(now_fn())
                aligned = eligible_fv(fv_hist[(m.fixture_id, m.txline_side)], decision_recv_ts)
                if aligned is not None:
                    fv_aligned_this_poll = True
                _emit_decision(m, snap, book_id, aligned, book_obs_ts, decision_recv_ts)

            # One liveness heartbeat per poll — what the poll loop saw this cycle.
            _record(
                RecorderHeartbeatEvent(
                    sequence_no=0,
                    event_type="RecorderHeartbeatEvent",
                    source_ts=None,
                    recv_ts=int(now_fn()),
                    poll_index=polls,
                    venue_mids_seen=venue_mids_seen,
                    fv_points_recv=counters.fv_points,
                    fv_aligned=fv_aligned_this_poll,
                )
            )

            polls += 1
            await sleep_fn(poll_interval_ms / 1000.0)
    finally:
        fv_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await fv_task
        # Seal inside the finally so a mid-session crash (or a raising decide_fn) still
        # finalizes meta.json over whatever was recorded — never leaves it start-only.
        recorder.finalize(
            ended_ts=int(now_fn()),
            mapping_hash=_mapping_hash(matched),
            poll_interval_ms=poll_interval_ms,
        )

    return SessionResult(
        polls=polls,
        events_recorded=counters.events,
        gaps=counters.gaps,
        fv_points=counters.fv_points,
    )
