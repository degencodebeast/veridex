"""WS dual-recv-timestamp lead monitor — does a genuine sub-2s FV-vs-venue arrival lead exist? (W4).

The polled (2s) ``live_monitor`` could not settle whether the TxLINE FV leads the Polymarket
venue mid at SUB-2s resolution: a 2s poll floor quantizes any faster lead away. This module is
the instrument that CAN settle it. It streams the live FV SSE and the live venue book-change
WebSocket concurrently, timestamps BOTH feeds by the SAME local clock at the instant each
message ARRIVES (``recv_ts = int(now_fn())`` — the clock-skew-free honesty rule), records a raw
dual-recv-timestamp event tape for audit/replay, and on shutdown reports two complementary reads:

1. the committed ``run_leadlag_probe`` NEXT-change hit + z at arrival resolution, and
2. the **direct arrival-lead distribution** — median ms lead, % FV-strictly-first, % tied.

The make-or-break honesty is the SAME-MS TIE POLICY (plan MAJOR-4). At ms resolution an FV
arrival and a venue mid change can share a ``recv_ts``. Ties are NEVER broken by append or
task-scheduling order (that would manufacture the very lead being measured):

* **Direct arrival-lead** — for each venue mid change, ``lead = venue_recv − nearest_prior_fv_
  change_recv`` with an EQUAL ``recv_ts`` scored as ZERO (a tied FV is neither before nor after),
  symmetric by construction.
* **leadlag-probe timeline** — all updates sharing a ``recv_ts`` are COLLAPSED into ONE
  ``(recv_ts, fv, mid)`` row (both feeds applied at that ms, then a single row emitted) so
  ``compress_to_change_events`` never sees a same-ms FV-vs-venue ordering.

Design invariants (each load-bearing, mirroring ``scripts/maker/live_monitor.py``)
----------------------------------------------------------------------------------
* **Injectable seams, offline-testable.** Every live source is a ``Protocol`` (:class:`FvSource`,
  :class:`VenueMidSource`, :class:`Recorder`); tests inject fakes and NO network library is
  imported at module scope (``httpx`` is lazy inside :class:`_DefaultFvSource`; the venue WS
  ``aiohttp`` is lazy inside ``ws_book_source``). The venue channel is PUBLIC (no auth).
* **Dual clock.** BOTH feeds are stamped by the SAME injected ``now_fn`` at arrival — the FV leg
  ``live_monitor`` omitted is added here; the venue leg's ``recv_ts`` is stamped inside
  ``stream_venue_book_frames``. A monotonic ``arrival_seq`` envelope gives deterministic ordering
  for audit (mirrors the R3 ``(recv_ts, sequence_no)`` sort) WITHOUT breaking same-ms ties.
* **Token hygiene.** FV creds are resolved fail-closed via :func:`require_live_creds` (raise
  BEFORE any I/O when absent), held privately, never logged/written; any diagnostic is scrubbed
  of the secret values (:func:`_scrub`). Artifacts carry only ``fv_configured: bool``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import itertools
import json
import signal
import statistics
import time
from collections import defaultdict
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple, Protocol, cast

from scripts.maker.leadlag_probe import (
    ProbeResult,
    render_markdown,
    run_leadlag_probe,
)
from veridex.ingest.marketstate import MarketState
from veridex.live_recorder.sources import _scrub, require_live_creds
from veridex.live_recorder.ws_book_source import (
    BookMidChange,
    BookStateMaintainer,
    TokenResolution,
    VenueBookFrame,
    stream_venue_book_frames,
)
from veridex.venues.polymarket_resolver import (
    MarketUnavailable,
    resolve_market,
    side_to_token,
)

__all__ = [
    "FvSource",
    "VenueMidSource",
    "Recorder",
    "MonitoredMarket",
    "MonitorEvent",
    "ArrivalLeadResult",
    "DualRecvAnalysis",
    "collapse_to_series",
    "direct_arrival_leads",
    "analyze_dual_recv",
    "match_markets",
    "run_monitor",
    "fv_configured",
    "write_meta",
    "JsonlRecorder",
    "build_parser",
    "main",
]

#: The REAL TxLINE 1X2 FULL-match market key the FV is read under (mirrors ``live_monitor``).
_TXLINE_1X2_FULL_MARKET_KEY = "1X2_PARTICIPANT_RESULT||"

#: (txline_side, venue_side, venue_market_ref) for the three 1X2 sides (same bridge as the resolver).
_SIDE_SPECS: tuple[tuple[str, str, str], ...] = (
    ("part1", "home", "1X2|home|full"),
    ("draw", "draw", "1X2|draw|full"),
    ("part2", "away", "1X2|away|full"),
)


# --------------------------------------------------------------------------- injectable seams
class FvSource(Protocol):
    """A live TxLINE FV source: yields :class:`MarketState` snapshots in ARRIVAL order."""

    def stream(self) -> AsyncIterator[MarketState]:
        """Return an async iterator of live :class:`MarketState` snapshots."""
        ...


class VenueMidSource(Protocol):
    """A live venue mid-change source: yields :class:`BookMidChange`s (each arrival-``recv_ts``-stamped)."""

    def stream(self) -> AsyncIterator[BookMidChange]:
        """Return an async iterator of venue mid changes."""
        ...


class Recorder(Protocol):
    """An append-only raw-event sink (one dict row per recorded dual-recv event)."""

    def record(self, row: dict[str, Any]) -> None:
        """Persist one event row."""
        ...


# --------------------------------------------------------------------------- data model
@dataclass(frozen=True)
class MonitoredMarket:
    """A resolved (fixture, side) → venue token binding the monitor subscribes and routes.

    Attributes:
        fixture_id: TxLINE fixture id (FV is read from the matching ``MarketState``).
        txline_side: TxLINE side token (``part1``/``draw``/``part2``) — the ``stable_prob_bps`` key.
        venue_market_ref: The venue market ref (e.g. ``"1X2|home|full"``) — the analysis grouping key.
        token_id: The Polymarket CLOB token id whose book changes are streamed.
        tick_size: The resolved tick size (``min_price_increment`` tracks it — see the plan nit).
    """

    fixture_id: int
    txline_side: str
    venue_market_ref: str
    token_id: str
    tick_size: float


class MonitorEvent(NamedTuple):
    """One raw dual-recv-timestamp event (an FV arrival OR a venue mid change).

    ``recv_ts`` is the local arrival clock (ms). ``arrival_seq`` is a monitor-monotonic counter for
    audit + WITHIN-source ordering (it NEVER breaks a same-``recv_ts`` FV-vs-venue tie — the tie
    policy collapses/zeroes those). ``source`` is ``"fv"`` or ``"venue"``; exactly one of
    ``fv``/``mid`` is set. ``venue_book_ts`` is the venue frame's own ms timestamp (venue events only).
    """

    recv_ts: int
    arrival_seq: int
    source: str
    token_id: str
    fixture_id: int
    venue_market_ref: str
    fv: float | None
    mid: float | None
    venue_book_ts: int | None


@dataclass(frozen=True)
class ArrivalLeadResult:
    """The direct arrival-lead distribution (the concrete sub-2s "lead in ms" answer).

    Each ``lead`` (ms) is ``venue_recv − nearest_prior_fv_change_recv`` for a venue mid change with
    a comparable FV change; an equal ``recv_ts`` is scored as ZERO (a tie is neither before nor
    after). Leads are per-market then pooled; the distribution is summarized honestly.

    Attributes:
        leads_ms: Every scored per-venue-change lead (ms, ``>= 0``), pooled across markets.
        n: ``len(leads_ms)``.
        median_ms: Median lead, or ``None`` when empty.
        pct_fv_strictly_first: Fraction of leads ``> 0`` (FV arrived strictly before the venue move).
        pct_tied: Fraction of leads ``== 0`` (FV and venue shared the arrival ms).
    """

    leads_ms: list[int]
    n: int
    median_ms: float | None
    pct_fv_strictly_first: float | None
    pct_tied: float | None


@dataclass(frozen=True)
class DualRecvAnalysis:
    """The on-shutdown analysis: raw tape, collapsed series, the lead-lag probe, the arrival-lead dist."""

    events: list[MonitorEvent]
    series_by_market: dict[tuple[int, str], tuple[list[int], list[float], list[float]]]
    probe: ProbeResult
    arrival_lead: ArrivalLeadResult
    leadlag_markdown: str
    arrival_lead_markdown: str


# --------------------------------------------------------------------------- market matching
async def match_markets(
    fixtures: list[dict[str, Any]],
    *,
    gamma_client: Any = None,
) -> list[MonitoredMarket]:
    """Resolve each fixture's three 1X2 sides to venue tokens; skip (honestly) any unavailable side.

    Mirrors ``live_monitor.match_markets`` but RETAINS the resolved ``tick_size`` (threaded into the
    book-state maintainer). A :class:`MarketUnavailable` (or an unmappable side) is logged and
    skipped — never fabricated (AC-2D-201).
    """
    matched: list[MonitoredMarket] = []
    for fixture in fixtures:
        fixture_id = int(fixture["fixture_id"])
        slug = str(fixture["event_slug"])
        home_team = fixture.get("home_team")
        away_team = fixture.get("away_team")
        for txline_side, venue_side, market_ref in _SIDE_SPECS:
            try:
                resolved = await resolve_market(
                    market_ref, slug, home_team=home_team, away_team=away_team, client=gamma_client
                )
                token_id = side_to_token(resolved, venue_side)
            except MarketUnavailable as exc:
                print(f"fixture {fixture_id} side {venue_side}: UNRESOLVED ({exc}) — skipped, not fabricated")
                continue
            except ValueError as exc:
                print(f"fixture {fixture_id} side {venue_side}: unmappable side ({exc}) — skipped")
                continue
            matched.append(
                MonitoredMarket(fixture_id, txline_side, market_ref, token_id, resolved.tick_size)
            )
    return matched


# --------------------------------------------------------------------------- FV read
def _fv_from_state(state: MarketState, txline_side: str) -> float | None:
    """Read the native-prob FV for *txline_side* from a state's 1X2 full-match market, or ``None``."""
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


# --------------------------------------------------------------------------- tie-neutral analysis
def collapse_to_series(events: list[MonitorEvent]) -> tuple[list[int], list[float], list[float]]:
    """Collapse a per-market dual-recv tape to a forward-filled ``(recv_ts, fv, mid)`` series.

    THE MAJOR-4 collapse: all updates sharing a ``recv_ts`` are applied together (in ``arrival_seq``
    order WITHIN each ``recv_ts`` — legitimate for same-source dedup, and order-invariant ACROSS
    sources because both feeds are applied before the single row is emitted), then ONE row is
    emitted once BOTH an fv and a mid are known. This guarantees ``compress_to_change_events`` never
    observes a same-ms FV-vs-venue ordering. Rows are ascending and index-aligned.
    """
    ordered = sorted(events, key=lambda e: (e.recv_ts, e.arrival_seq))
    ts_out: list[int] = []
    fv_out: list[float] = []
    mid_out: list[float] = []
    cur_fv: float | None = None
    cur_mid: float | None = None
    i = 0
    n = len(ordered)
    while i < n:
        recv = ordered[i].recv_ts
        j = i
        while j < n and ordered[j].recv_ts == recv:  # apply the whole same-recv_ts group first
            ev = ordered[j]
            if ev.source == "fv" and ev.fv is not None:
                cur_fv = ev.fv
            elif ev.source == "venue" and ev.mid is not None:
                cur_mid = ev.mid
            j += 1
        if cur_fv is not None and cur_mid is not None:
            ts_out.append(recv)
            fv_out.append(cur_fv)
            mid_out.append(cur_mid)
        i = j
    return ts_out, fv_out, mid_out


def direct_arrival_leads(events: list[MonitorEvent]) -> list[int]:
    """Per-market direct arrival leads: ``venue_recv − nearest_prior_fv_change_recv`` (tie == 0).

    An FV CHANGE is an fv event whose value differs from the running fv (the first fv seen counts).
    Every venue event is already a mid change. For each venue change we take the LATEST fv change
    with ``recv_ts <= venue_recv``: an equal ``recv_ts`` is included and yields lead 0 (a tie is
    neither before nor after → symmetric); a strictly-earlier one yields a positive lead. A venue
    change with no prior-or-tied fv change is not comparable and is skipped.
    """
    ordered = sorted(events, key=lambda e: (e.recv_ts, e.arrival_seq))
    fv_change_recvs: list[int] = []
    last_fv: float | None = None
    for ev in ordered:
        if ev.source == "fv" and ev.fv is not None and ev.fv != last_fv:
            fv_change_recvs.append(ev.recv_ts)
            last_fv = ev.fv
    leads: list[int] = []
    for ev in ordered:
        if ev.source != "venue" or ev.mid is None:
            continue
        prior = [r for r in fv_change_recvs if r <= ev.recv_ts]
        if not prior:
            continue  # no comparable FV change — not scored (honest: no data, not a fabricated 0)
        leads.append(ev.recv_ts - max(prior))
    return leads


def _summarize_leads(leads: list[int]) -> ArrivalLeadResult:
    """Summarize pooled leads into the honest distribution (median, % strictly-first, % tied)."""
    n = len(leads)
    if n == 0:
        return ArrivalLeadResult(leads_ms=[], n=0, median_ms=None, pct_fv_strictly_first=None, pct_tied=None)
    return ArrivalLeadResult(
        leads_ms=leads,
        n=n,
        median_ms=float(statistics.median(leads)),
        pct_fv_strictly_first=sum(1 for lead in leads if lead > 0) / n,
        pct_tied=sum(1 for lead in leads if lead == 0) / n,
    )


def analyze_dual_recv(events_by_market: Mapping[tuple[int, str], list[MonitorEvent]]) -> DualRecvAnalysis:
    """Collapse each market's tape, run the lead-lag probe + the direct arrival-lead distribution.

    Both reads are tie-neutral by construction: the probe consumes the same-ms-COLLAPSED series;
    the arrival-lead scores an equal ``recv_ts`` as ZERO. Leads are computed per-market (never
    cross-market) then pooled for the distribution summary.
    """
    series_by_market: dict[tuple[int, str], tuple[list[int], list[float], list[float]]] = {}
    pooled_leads: list[int] = []
    all_events: list[MonitorEvent] = []
    for key, events in sorted(events_by_market.items(), key=lambda kv: str(kv[0])):
        all_events.extend(events)
        ts, fv, mid = collapse_to_series(events)
        if len(mid) >= 2:
            series_by_market[key] = (ts, fv, mid)
        pooled_leads.extend(direct_arrival_leads(events))

    probe = (
        run_leadlag_probe(series_by_market)
        if series_by_market
        else ProbeResult(evidence=[], aggregates=[], verdict="NO DATA")
    )
    arrival_lead = _summarize_leads(pooled_leads)
    return DualRecvAnalysis(
        events=all_events,
        series_by_market=series_by_market,
        probe=probe,
        arrival_lead=arrival_lead,
        leadlag_markdown=render_markdown(probe),
        arrival_lead_markdown=render_arrival_lead_markdown(arrival_lead),
    )


def render_arrival_lead_markdown(lead: ArrivalLeadResult) -> str:
    """Render the direct arrival-lead distribution — the concrete sub-2s "lead in ms" answer."""

    def _fmt(value: float | None) -> str:
        return f"{value:.1f}" if value is not None else "n/a"

    def _pct(value: float | None) -> str:
        return f"{value * 100:.1f}%" if value is not None else "n/a"

    return "\n".join(
        [
            "# WS dual-recv arrival lead — does the FV lead the venue mid at SUB-2s (ms) resolution?",
            "",
            (
                "For each venue mid CHANGE, `lead = venue_recv − nearest_prior_fv_change_recv`, both "
                "stamped by the SAME local arrival clock (ms). An EQUAL `recv_ts` is scored as ZERO "
                "(a tied FV is neither before nor after). A consistent positive median (with the "
                "NEXT-change probe > 0.5, z > 2, replicating across fixtures) = a real sub-second "
                "arrival lead; a distribution centred on ~0 = NO FV-vs-venue arrival lead in this "
                "subscribed sample at tick resolution. This settles the ARRIVAL-lead question only "
                "(execution / fill / cost / liquidity remain out of scope)."
            ),
            "",
            f"- scored venue changes (n): {lead.n}",
            f"- median arrival lead: {_fmt(lead.median_ms)} ms",
            f"- FV strictly first: {_pct(lead.pct_fv_strictly_first)}",
            f"- tied (same arrival ms): {_pct(lead.pct_tied)}",
            "",
            (
                "CAVEAT: a positive median arrival-lead in isolation is NOT evidence of an FV "
                "lead — a lagging-FV / venue-leading regime produces the same signature. This "
                "panel is conclusive ONLY jointly with the NEXT-change probe below (next_rate "
                "meaningfully > 0.5 AND z > 2). Read the probe verdict, not this number alone."
            ),
            "",
        ]
    )


# --------------------------------------------------------------------------- the monitor
async def run_monitor(
    *,
    matched: list[MonitoredMarket],
    fv_source: FvSource,
    venue_source: VenueMidSource,
    recorder: Recorder,
    now_fn: Callable[[], int],
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    minutes: float = 30.0,
    fv_creds: tuple[str, str] | None = None,
) -> DualRecvAnalysis:
    """Stream FV + venue mid changes concurrently, record the dual-recv tape, analyse on shutdown.

    Two consumer tasks run concurrently: the FV task stamps each arrival ``recv_ts = int(now_fn())``
    (the leg ``live_monitor`` omitted) and fans it out to every matched side of its fixture; the
    venue task consumes already-``recv_ts``-stamped :class:`BookMidChange`s. Both feeds share the
    ONE injected ``now_fn`` clock. Every event is appended to a per-market tape (keyed
    ``(fixture_id, venue_market_ref)``) and recorded. Shutdown is any of: both sources ending
    (offline fakes), the ``minutes`` deadline, or SIGINT. If the FV task dies unexpectedly it is
    diagnosed via a SCRUBBED print (never an unscrubbed token) and the venue task continues.

    Args:
        matched: Markets to monitor (from :func:`match_markets`).
        fv_source: Live FV source (injectable).
        venue_source: Live venue mid-change source (injectable).
        recorder: Append-only raw-event sink (injectable).
        now_fn: Receive-time clock (ms) shared by BOTH feeds — the clock-skew-free honesty rule.
        sleep_fn: Async sleep driving the deadline watcher (injected/blocking in tests).
        minutes: Session wall-clock budget (deadline).
        fv_creds: Optional ``(jwt, api_token)`` used ONLY to scrub an FV-task diagnostic; never logged.

    Returns:
        The :class:`DualRecvAnalysis` (lead-lag probe + arrival-lead distribution) over the session.
    """
    stop = asyncio.Event()
    try:
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, stop.set)
    except (NotImplementedError, RuntimeError, ValueError):
        pass  # no signal support on this platform / loop — deadline + EOF still bound the run

    events_by_market: dict[tuple[int, str], list[MonitorEvent]] = defaultdict(list)
    by_fixture: dict[int, list[MonitoredMarket]] = defaultdict(list)
    for m in matched:
        by_fixture[m.fixture_id].append(m)
    token_market: dict[str, MonitoredMarket] = {m.token_id: m for m in matched}
    seq = itertools.count(1)

    def _record(ev: MonitorEvent) -> None:
        events_by_market[(ev.fixture_id, ev.venue_market_ref)].append(ev)
        recorder.record(
            {
                "recv_ts": ev.recv_ts,
                "arrival_seq": ev.arrival_seq,
                "source": ev.source,
                "token_id": ev.token_id,
                "fixture_id": ev.fixture_id,
                "venue_market_ref": ev.venue_market_ref,
                "fv": ev.fv,
                "mid": ev.mid,
                "venue_book_ts": ev.venue_book_ts,
            }
        )

    async def _consume_fv() -> None:
        try:
            async for state in fv_source.stream():
                if stop.is_set():
                    break
                recv_ts = int(now_fn())  # stamp the FV arrival on the shared local clock
                for m in by_fixture.get(state.fixture_id, ()):
                    fv = _fv_from_state(state, m.txline_side)
                    if fv is None:
                        continue
                    _record(
                        MonitorEvent(recv_ts, next(seq), "fv", m.token_id, m.fixture_id, m.venue_market_ref, fv, None, None)
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — diagnose (scrubbed) then degrade, never crash the monitor
            print(f"  FV consumer task died — venue tape continues: {_scrub_diag(exc, fv_creds)}")

    async def _consume_venue() -> None:
        try:
            async for change in venue_source.stream():
                if stop.is_set():
                    break
                m = token_market.get(change.token_id)
                if m is None:
                    continue  # a frame for an unsubscribed token — never fabricate a market
                _record(
                    MonitorEvent(
                        change.recv_ts, next(seq), "venue", m.token_id, m.fixture_id, m.venue_market_ref,
                        None, change.mid, change.book_ts_ms,
                    )
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — diagnose (scrubbed) then degrade, never crash the monitor
            print(f"  venue consumer task died — FV tape continues: {_scrub_diag(exc, fv_creds)}")

    print(f"[ws-monitor] {len(matched)} market(s) | session {minutes:g}min | dual-recv tape (fv + venue)")
    for m in matched:
        print(f"  · fixture {m.fixture_id}  {m.venue_market_ref}  (txline {m.txline_side})")

    fv_task = asyncio.create_task(_consume_fv())
    venue_task = asyncio.create_task(_consume_venue())

    async def _await_both() -> None:
        await asyncio.gather(fv_task, venue_task)  # both sources ended (offline fakes / clean shutdown)

    async def _deadline() -> None:
        await sleep_fn(minutes * 60.0)
        stop.set()

    async def _stop_wait() -> None:
        await stop.wait()

    completion_task = asyncio.create_task(_await_both())
    deadline_task = asyncio.create_task(_deadline())
    stop_task = asyncio.create_task(_stop_wait())
    try:
        await asyncio.wait({completion_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (deadline_task, stop_task, completion_task, fv_task, venue_task):
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.gather(
                completion_task, deadline_task, stop_task, fv_task, venue_task, return_exceptions=True
            )

    return analyze_dual_recv(events_by_market)


# --------------------------------------------------------------------------- token hygiene
def _scrub_diag(exc: BaseException, creds: tuple[str, str] | None) -> str:
    """Render an exception TYPE + scrubbed message (never an unscrubbed token)."""
    text = f"{type(exc).__name__}: {exc}"
    return _scrub(text, *creds) if creds else _scrub(text)


def fv_configured(env: Mapping[str, str]) -> bool:
    """Boolean-only telemetry: whether the FV creds are present (NEVER the secret values)."""
    from veridex.live_recorder.sources import configured

    return configured(env)


def write_meta(
    path: Path,
    *,
    session_ts: int,
    matched: list[MonitoredMarket],
    minutes: float,
    fv_configured: bool,
) -> None:
    """Write the session ``meta.json`` — secret-free telemetry (``fv_configured: bool`` only)."""
    meta = {
        "session_ts": session_ts,
        "minutes": minutes,
        "fv_configured": bool(fv_configured),
        "monitored_markets": [
            {
                "fixture_id": m.fixture_id,
                "txline_side": m.txline_side,
                "venue_market_ref": m.venue_market_ref,
                "token_id": m.token_id,
                "tick_size": m.tick_size,
            }
            for m in matched
        ],
    }
    path.write_text(json.dumps(meta, sort_keys=True, indent=2))


# --------------------------------------------------------------------------- default LIVE sources
class _DefaultFvSource:
    """Live TxLINE FV source: wraps ``stream_marketstates`` in a reconnect/backoff loop (1s→60s).

    Creds are resolved ONCE via :func:`require_live_creds` in ``__init__`` (fail-closed BEFORE any
    network I/O when absent) and held privately — never logged. ``httpx`` is imported lazily inside
    ``stream_marketstates``, so constructing this source touches no network at import time.
    """

    def __init__(self, *, env: Mapping[str, str] | None = None, base_url: str | None = None) -> None:
        import os

        environ = env if env is not None else os.environ
        self._creds = require_live_creds(environ)  # fail-closed if either secret is missing
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
            except Exception as exc:  # noqa: BLE001 — reconnect on any stream error; never leak creds
                print(f"  FV stream disconnected: {_scrub_diag(exc, self._creds)}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


class _DefaultVenueMidSource:
    """Live venue mid-change source: the W1/W2 WS frame stream through a :class:`BookStateMaintainer`.

    The market WS channel is PUBLIC (no auth). ``aiohttp`` is lazy-imported inside
    ``stream_venue_book_frames`` so constructing this source touches no network at import time; the
    ``connect`` seam is injectable (offline fakes yield canned frames).
    """

    def __init__(
        self,
        assets_ids: Iterable[str],
        *,
        connect: Callable[[str], Awaitable[Any]] | None = None,
        now_fn: Callable[[], int],
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
        resolutions: Mapping[str, TokenResolution] | None = None,
    ) -> None:
        self._assets = list(assets_ids)
        self._connect = connect
        self._now_fn = now_fn
        self._sleep_fn = sleep_fn
        self._resolutions = dict(resolutions or {})

    async def stream(self) -> AsyncIterator[BookMidChange]:
        maintainer = BookStateMaintainer(resolutions=self._resolutions)
        # The stream is an async GENERATOR (declared ``AsyncIterator`` at W1); cast so its
        # ``aclose`` is visible for deterministic teardown on cancellation (mirrors W3).
        agen = cast(
            AsyncGenerator[VenueBookFrame, None],
            stream_venue_book_frames(
                self._assets, connect=self._connect, now_fn=self._now_fn, sleep_fn=self._sleep_fn
            ),
        )
        try:
            async for frame in agen:
                for change in maintainer.apply_frame(frame):
                    yield change
        finally:
            await agen.aclose()


class JsonlRecorder:
    """Append-only JSONL raw-event sink (one row per line)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self._path.open("a", encoding="utf-8")

    def record(self, row: dict[str, Any]) -> None:
        self._fh.write(json.dumps(row, sort_keys=True) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


# --------------------------------------------------------------------------- operator CLI
def build_parser() -> argparse.ArgumentParser:
    """Build the operator argument parser (constructs NO live source — offline-safe, ``--help`` works)."""
    parser = argparse.ArgumentParser(
        prog="ws_leadlag_monitor",
        description="Read-only WS dual-recv-timestamp FV-vs-venue lead monitor (settles the sub-2s question).",
    )
    parser.add_argument("--fixtures", required=True, help="path to fixtures.json (fixture_id/event_slug/teams)")
    parser.add_argument("--minutes", type=float, default=30.0)
    parser.add_argument("--out", default=".omc/research/ws-leadlag-monitor", help="session output root")
    parser.add_argument(
        "--base-url",
        type=str,
        default=None,
        dest="base_url",
        help="override TxLINE base URL (e.g. https://txline.txodds.com/api for mainnet); default uses config",
    )
    return parser


async def _run_cli(args: argparse.Namespace) -> DualRecvAnalysis:
    """Operator entrypoint: match markets (live Gamma), run the monitor, write artifacts + reports."""
    import os

    fixtures = json.loads(Path(args.fixtures).read_text())
    if not isinstance(fixtures, list) or not fixtures:
        raise SystemExit(f"fixtures file {args.fixtures} must be a non-empty JSON list")

    creds = require_live_creds(os.environ)  # fail-closed BEFORE any resolve / network / file I/O
    matched = await match_markets(fixtures)  # live Gamma resolve (fail-closed per side)
    if not matched:
        raise SystemExit("no markets resolved — nothing to monitor (all sides UNRESOLVED)")

    session_ts = int(time.time())
    session_dir = Path(args.out) / str(session_ts)
    session_dir.mkdir(parents=True, exist_ok=True)
    write_meta(
        session_dir / "meta.json",
        session_ts=session_ts,
        matched=matched,
        minutes=args.minutes,
        fv_configured=fv_configured(os.environ),
    )
    resolutions = {
        m.token_id: TokenResolution(
            tick_size=m.tick_size, min_price_increment=m.tick_size, venue_market_ref=m.venue_market_ref
        )
        for m in matched
    }
    recorder = JsonlRecorder(session_dir / "events.jsonl")
    try:
        result = await run_monitor(
            matched=matched,
            fv_source=_DefaultFvSource(base_url=args.base_url),
            venue_source=_DefaultVenueMidSource(
                [m.token_id for m in matched], now_fn=lambda: int(time.time() * 1000), resolutions=resolutions
            ),
            recorder=recorder,
            now_fn=lambda: int(time.time() * 1000),
            minutes=args.minutes,
            fv_creds=creds,  # scrub-only; already validated fail-closed above
        )
    finally:
        recorder.close()

    (session_dir / "leadlag-probe.md").write_text(result.leadlag_markdown)
    (session_dir / "arrival-lead.md").write_text(result.arrival_lead_markdown)
    research = Path(".omc/research")
    research.mkdir(parents=True, exist_ok=True)
    (research / "ws-leadlag-probe.md").write_text(result.leadlag_markdown)
    (research / "ws-arrival-lead.md").write_text(result.arrival_lead_markdown)
    print(f"session: {session_dir}")
    print(f"lead-lag verdict: {result.probe.verdict}")
    print(
        f"arrival lead: median {result.arrival_lead.median_ms} ms | "
        f"FV-first {result.arrival_lead.pct_fv_strictly_first} | tied {result.arrival_lead.pct_tied} "
        f"(n={result.arrival_lead.n})"
    )
    return result


def main(argv: list[str] | None = None) -> int:
    """Parse ``argv`` and run the operator monitor session."""
    args = build_parser().parse_args(argv)
    asyncio.run(_run_cli(args))
    return 0


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(main())
