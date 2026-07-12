"""Offline tests for the WS dual-recv-timestamp lead monitor (``scripts/maker/ws_leadlag_monitor.py``, W4).

Every live source is behind an injectable ``Protocol`` and every test drives the monitor / its
pure analysis with offline fakes — NO network, NO real time, NO TxLINE token. The default live
sources are NEVER constructed here (asserted), and the module imports no network library at
module scope.

The load-bearing test is :func:`test_null_simultaneous_pattern_is_tie_neutral` (plan MAJOR-4):
at ms resolution an FV arrival and a venue mid change can share a ``recv_ts``. Ties must be
scored as ZERO lead (never broken by append/task order) and same-``recv_ts`` updates collapsed
into ONE ``(recv_ts, fv, mid)`` row before the lead-lag probe sees them. That test asserts the
analysis is IDENTICAL whether the tied FV or the tied venue event is appended first.
"""

from __future__ import annotations

import ast
import asyncio
import random
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.polymarket_ws_frames import (
    BOOK_ASSET_ID,
    BOOK_FRAME,
    PRICE_CHANGE_ADD_BID_FRAME,
)
from tests.test_maker_leadlag_probe import synth_fv_leads_venue
from veridex.ingest.marketstate import MarketState

_HEADLINE_BPS = 50
_FULL_KEY = "1X2_PARTICIPANT_RESULT||"
_REF = "1X2|home|full"
_TOKEN = "T"


# --------------------------------------------------------------------------- offline fakes
class FakeFvSource:
    """A network-free ``FvSource``: replays canned ``MarketState``s in arrival order.

    ``stream`` performs NO ``await`` between yields, so under CPython's deterministic asyncio
    scheduling it drains fully the first time the consumer task runs (mirrors the live_monitor
    ``FakeFvSource``). ``now_fn`` is therefore called exactly once per state, in order.
    """

    def __init__(self, states: list[MarketState]) -> None:
        self._states = states

    async def stream(self) -> Any:  # AsyncIterator[MarketState]
        for state in self._states:
            yield state


class FakeVenueMidSource:
    """A scripted ``VenueMidSource``: yields pre-built ``BookMidChange``s with EXPLICIT recv_ts.

    The venue recv_ts is decoupled from ``now_fn`` (the real venue source stamps it inside
    ``stream_venue_book_frames``), so the FV/venue recv_ts interleave is fully deterministic.
    """

    def __init__(self, changes: list[Any]) -> None:
        self._changes = changes

    async def stream(self) -> Any:  # AsyncIterator[BookMidChange]
        for change in self._changes:
            yield change


class RaisingFvSource:
    """An ``FvSource`` whose stream raises — exercises the scrubbed FV-task diagnostic path."""

    def __init__(self, message: str) -> None:
        self._message = message

    async def stream(self) -> Any:
        raise RuntimeError(self._message)
        yield  # pragma: no cover - makes this an async generator


class ListRecorder:
    """A ``Recorder`` that collects rows in memory for assertions."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def record(self, row: dict[str, Any]) -> None:
        self.rows.append(dict(row))


class _SeqClock:
    """Deterministic ``now_fn``: returns each value in turn, then repeats the last."""

    def __init__(self, values: list[int]) -> None:
        self._values = list(values)
        self._i = 0

    def __call__(self) -> int:
        v = self._values[min(self._i, len(self._values) - 1)]
        self._i += 1
        return v


async def _block_sleep(_seconds: float) -> None:
    """Injected ``sleep_fn`` that blocks forever (the deadline watcher must never fire offline)."""
    await asyncio.Event().wait()


def _fv_state(fixture_id: int, ts: int, fv: float) -> MarketState:
    """A ``MarketState`` carrying the 1X2 full-match market with a single-side FV (native prob)."""
    return MarketState(
        fixture_id=fixture_id,
        tick_seq=0,
        ts=ts,
        phase=1,
        markets={_FULL_KEY: {"stable_prob_bps": {"part1": round(fv * 1e4)}}},
        scores={},
    )


def _split_synth(
    ts: list[int], fv: list[float], mid: list[float]
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Split an index-aligned synth series into FV-change and venue-mid-change event streams.

    Each carries the synth clock as its recv_ts; in ``synth_fv_leads_venue`` the FV changes to a
    new level (dense ticks) BEFORE the venue mid steps to it, so per block the FV change recv_ts
    is strictly less than the venue change recv_ts — an arrival lead by construction.
    """
    fv_events: list[tuple[int, float]] = []
    venue_events: list[tuple[int, float]] = []
    prev_fv: float | None = None
    prev_mid: float | None = None
    for t, f, m in zip(ts, fv, mid, strict=True):
        if f != prev_fv:
            fv_events.append((t, f))
            prev_fv = f
        if m != prev_mid:
            venue_events.append((t, m))
            prev_mid = m
    return fv_events, venue_events


# --------------------------------------------------------------------------- W4-a
async def test_fv_leads_venue_pattern_detected() -> None:
    """W4-(a): an FV-leads-venue arrival pattern → NEXT-rate > 0.5 AND a positive median arrival-lead."""
    from scripts.maker.ws_leadlag_monitor import (
        MonitoredMarket,
        run_monitor,
    )
    from veridex.live_recorder.ws_book_source import BookMidChange

    ts, fv, mid = synth_fv_leads_venue()
    fv_events, venue_events = _split_synth(ts, fv, mid)

    states = [_fv_state(1, t, f) for t, f in fv_events]
    changes = [
        BookMidChange(
            token_id=_TOKEN,
            recv_ts=t,
            arrival_seq=0,
            mid=m,
            book_ts_ms=t,
            best_bid=None,
            best_ask=None,
        )
        for t, m in venue_events
    ]

    matched = [MonitoredMarket(1, "part1", _REF, _TOKEN, 0.01)]
    recorder = ListRecorder()
    analysis = await run_monitor(
        matched=matched,
        fv_source=FakeFvSource(states),
        venue_source=FakeVenueMidSource(changes),
        recorder=recorder,
        now_fn=_SeqClock([t for t, _ in fv_events]),
        sleep_fn=_block_sleep,
    )

    agg = next(a for a in analysis.probe.aggregates if a.threshold_bps == _HEADLINE_BPS)
    assert agg.next_rate is not None
    assert agg.next_n >= 100  # a real sample, not a lucky handful
    assert agg.next_rate > 0.5  # the venue FOLLOWS the leading FV

    lead = analysis.arrival_lead
    assert lead.n >= 100
    assert lead.median_ms is not None and lead.median_ms > 0.0  # FV arrives before the venue move
    assert lead.pct_fv_strictly_first is not None and lead.pct_fv_strictly_first > 0.9


# --------------------------------------------------------------------------- W4-b (MAJOR-4)
def _simultaneous_rows(n: int = 800, seed: int = 3, step: float = 0.01) -> list[tuple[int, float, float]]:
    """A no-lead random walk where FV and the venue mid move together at the SAME recv_ts."""
    rng = random.Random(seed)
    cur = 0.5
    rows: list[tuple[int, float, float]] = []
    for i in range(n):
        cur = cur + rng.choice([-1.0, 1.0]) * step
        if cur < 0.3 or cur > 0.7:  # reflect to stay mid-range without clamping autocorrelation
            bound = 0.3 if cur < 0.3 else 0.7
            cur = 2 * bound - cur
        mid = cur + rng.gauss(0, 0.0003)  # venue tracks FV contemporaneously (no lead)
        rows.append(((i + 1) * 10, cur, mid))
    return rows


def _tied_events(rows: list[tuple[int, float, float]], *, fv_first: bool) -> list[Any]:
    """Build the dual-recv tape with FV+venue sharing each recv_ts, appended in the chosen order."""
    from scripts.maker.ws_leadlag_monitor import MonitorEvent

    events: list[Any] = []
    for recv, fv, mid in rows:
        fv_ev = ("fv", fv, None)
        ve_ev = ("venue", None, mid)
        pair = (fv_ev, ve_ev) if fv_first else (ve_ev, fv_ev)
        for source, f, m in pair:
            events.append(
                MonitorEvent(
                    recv_ts=recv,
                    arrival_seq=len(events),  # append order == arrival_seq order
                    source=source,
                    token_id=_TOKEN,
                    fixture_id=1,
                    venue_market_ref=_REF,
                    fv=f,
                    mid=m,
                    venue_book_ts=recv if source == "venue" else None,
                )
            )
    return events


def test_null_simultaneous_pattern_is_tie_neutral() -> None:
    """W4-(b): SAME-recv_ts FV/venue → NEXT ~0.5, median lead == 0, and IDENTICAL under both append orders."""
    from scripts.maker.ws_leadlag_monitor import analyze_dual_recv

    rows = _simultaneous_rows()
    key = (1, _REF)

    fv_first = analyze_dual_recv({key: _tied_events(rows, fv_first=True)})
    venue_first = analyze_dual_recv({key: _tied_events(rows, fv_first=False)})

    # Tie neutrality: a tied FV is neither before nor after → every venue change scores lead 0.
    assert fv_first.arrival_lead.median_ms == 0.0
    assert fv_first.arrival_lead.pct_tied == 1.0
    assert fv_first.arrival_lead.pct_fv_strictly_first == 0.0

    # NEXT-change ~ 0.5: the probe does NOT manufacture a lead on lead-free simultaneous data.
    agg = next(a for a in fv_first.probe.aggregates if a.threshold_bps == _HEADLINE_BPS)
    assert agg.next_rate is not None
    assert agg.next_n >= 100
    assert abs(agg.next_rate - 0.5) < 0.1

    # THE MAJOR-4 GUARD: identical result regardless of which tied event is appended first.
    assert fv_first.series_by_market == venue_first.series_by_market
    agg_v = next(a for a in venue_first.probe.aggregates if a.threshold_bps == _HEADLINE_BPS)
    assert agg_v.next_rate == agg.next_rate
    assert agg_v.next_n == agg.next_n
    assert venue_first.arrival_lead == fv_first.arrival_lead


# --------------------------------------------------------------------------- W4-c
async def test_ws_merge_path_offline_via_fake_ws() -> None:
    """The default venue source runs the real W1/W2 WS→merge path over a FAKE connect seam (no network)."""
    from scripts.maker.ws_leadlag_monitor import _DefaultVenueMidSource
    from veridex.live_recorder.ws_book_source import FakeVenueBookWs, FakeVenueWsConnection

    conn = FakeVenueWsConnection([BOOK_FRAME, PRICE_CHANGE_ADD_BID_FRAME])
    fake = FakeVenueBookWs([conn])
    clock = iter(range(1000, 100000, 1000))

    source = _DefaultVenueMidSource(
        [BOOK_ASSET_ID], connect=fake.connect, now_fn=lambda: next(clock), sleep_fn=_block_sleep
    )
    agen = source.stream().__aiter__()
    first = await agen.__anext__()
    second = await agen.__anext__()
    await agen.aclose()

    assert first.mid == pytest.approx(0.51)  # seeded book: best_bid .50 / best_ask .52
    assert second.mid == pytest.approx(0.515)  # +bid .51 → mid .515 (a real merge)
    assert first.token_id == BOOK_ASSET_ID


def test_no_live_client_when_fakes_injected(monkeypatch: Any) -> None:
    """W4-(c): fakes injected → NO real FV/WS client is constructed, and module import does no network."""
    import scripts.maker.ws_leadlag_monitor as mod

    # (1) module-scope import audit: no network library imported at module scope.
    source = Path(mod.__file__).read_text()
    tree = ast.parse(source)
    top_level: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            top_level.add(node.module.split(".")[0])
    assert not (top_level & {"httpx", "requests", "websocket", "websockets", "aiohttp"})

    # (2) running with fakes must never construct the default (network) sources.
    def _explode(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("default live source constructed while fakes were injected")

    monkeypatch.setattr(mod, "_DefaultFvSource", _explode)
    monkeypatch.setattr(mod, "_DefaultVenueMidSource", _explode)

    async def _run() -> Any:
        return await mod.run_monitor(
            matched=[mod.MonitoredMarket(1, "part1", _REF, _TOKEN, 0.01)],
            fv_source=FakeFvSource([_fv_state(1, 10, 0.5)]),
            venue_source=FakeVenueMidSource([]),
            recorder=ListRecorder(),
            now_fn=_SeqClock([10]),
            sleep_fn=_block_sleep,
        )

    asyncio.run(_run())  # must not raise (defaults never touched)


# --------------------------------------------------------------------------- W4-d
async def test_no_secret_in_output(tmp_path: Path, capsys: Any) -> None:
    """W4-(d): a FAKE token never appears in any written artifact or captured stdout (scrubbed)."""
    from scripts.maker.ws_leadlag_monitor import (
        JsonlRecorder,
        MonitoredMarket,
        run_monitor,
        write_meta,
    )
    from veridex.live_recorder.ws_book_source import BookMidChange

    secret = "SECRET_TOKEN_XYZ"
    matched = [MonitoredMarket(1, "part1", _REF, _TOKEN, 0.01)]
    session_dir = tmp_path / "1234"
    session_dir.mkdir()
    recorder = JsonlRecorder(session_dir / "events.jsonl")
    write_meta(
        session_dir / "meta.json",
        session_ts=1234,
        matched=matched,
        minutes=1.0,
        fv_configured=True,
    )
    changes = [
        BookMidChange(token_id=_TOKEN, recv_ts=r, arrival_seq=0, mid=m, book_ts_ms=r, best_bid=None, best_ask=None)
        for r, m in [(20, 0.51), (40, 0.52)]
    ]
    try:
        analysis = await run_monitor(
            matched=matched,
            fv_source=RaisingFvSource(f"boom {secret}"),  # FV task dies with a secret in its message
            venue_source=FakeVenueMidSource(changes),
            recorder=recorder,
            now_fn=_SeqClock([10, 30]),
            sleep_fn=_block_sleep,
            fv_creds=(secret, "other"),  # used ONLY to scrub the diagnostic, never written
        )
        (session_dir / "leadlag.md").write_text(analysis.leadlag_markdown)
        (session_dir / "arrival_lead.md").write_text(analysis.arrival_lead_markdown)
    finally:
        recorder.close()

    out = capsys.readouterr().out
    assert secret not in out  # the FV-task diagnostic scrubs the secret VALUE from the message
    for path in session_dir.rglob("*"):
        if path.is_file():
            assert secret not in path.read_text(), f"secret leaked into {path.name}"


def test_missing_creds_fail_closed() -> None:
    """W4-(d): the default FV source fails closed (raises) BEFORE any network I/O when creds are absent."""
    from scripts.maker.ws_leadlag_monitor import _DefaultFvSource

    with pytest.raises((ValueError, KeyError)):
        _DefaultFvSource(env={})  # no JWT / TXLINE_X_API_TOKEN → fail closed
