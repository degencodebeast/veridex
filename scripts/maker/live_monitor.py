"""Read-only LIVE FV-vs-venue monitor — is the live Polymarket book as slow as the backfill?

The committed offline lead-lag probe (``scripts/maker/leadlag_probe.py``) established that the
TxLINE FV LEADS the Polymarket venue mid on a **backfilled** venue series whose mid refreshes
every 20–40 min. Every reviewer flagged the same open question: if the *live* book updates far
faster, that 20–40 min lag is a backfill-fidelity artifact and the freshness edge evaporates on
real quotes. No historical analysis can settle it — it requires looking at the live market.

This module is that look. It is **read-only** (no orders, no venue writes): it streams live
TxLINE FV, polls the PUBLIC Polymarket ``/book`` for matched markets, aligns them with NO
look-ahead, records an append-only session, and on shutdown runs the venue-cadence + lead-lag
analysis via the committed probe primitives (consumed UNCHANGED).

Design invariants (each load-bearing)
-------------------------------------
* **Injectable seams, offline-testable.** Every live source is a ``Protocol`` (:class:`FvSource`,
  :class:`MidSource`, :class:`Recorder`); tests inject fakes and NO network library is imported at
  module scope (``httpx`` is lazy, inside the default mid source only).
* **⚠ Per-market FV history is kept SORTED + DEDUPED by ``state.ts`` on insert**
  (:func:`_insort_fv`). ``veridex.maker.tape._aligned_mid`` uses ``bisect_right`` and assumes
  ascending timestamps, but ``veridex.ingest.live_client.stream_marketstates`` yields records in
  ARRIVAL order, not ts order (a reconnect / delayed / duplicate SSE record is out of order). Left
  unsorted, ``bisect`` would select the wrong or a FUTURE FV — silently fabricating or erasing a
  lead and breaking the central no-look-ahead guarantee. A record whose ts predates the last venue
  sample for a market is recorded as an explicit ``gap`` (never back-filled into an analysed window).
* **No look-ahead.** Alignment is ``_aligned_mid`` (most-recent-at-or-before), valid ONLY because
  the FV history is kept ascending per above.
* **Token hygiene.** Creds are resolved only via :func:`veridex.config.require_txline` (fail-closed
  if absent), held privately in the default FV source, never logged/written; any error string is
  scrubbed of BOTH secret values (:func:`_scrub_token`) before printing. Artifacts carry only
  ``txline_configured: bool``.
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import contextlib
import json
import signal
import statistics
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from scripts.maker.leadlag_probe import (
    ProbeResult,
    compress_to_change_events,
    render_markdown,
    run_leadlag_probe,
)
from veridex.ingest.marketstate import MarketState
from veridex.maker.tape import _aligned_mid
from veridex.venues.polymarket_resolver import (
    MarketUnavailable,
    resolve_market,
    side_to_token,
)

__all__ = [
    "FvSource",
    "MidSource",
    "Recorder",
    "MatchedMarket",
    "MarketCadence",
    "AnalysisResult",
    "match_markets",
    "run_monitor",
    "analyze_samples",
    "render_cadence_markdown",
    "txline_configured",
    "write_meta",
    "JsonlRecorder",
    "build_parser",
    "main",
]

#: The REAL TxLINE 1X2 FULL-match market key the FV is read under (see ``veridex/maker/tape.py``).
_TXLINE_1X2_FULL_MARKET_KEY = "1X2_PARTICIPANT_RESULT||"

#: (txline_side, venue_side, venue_market_ref) for the three 1X2 sides. ``part1→home``,
#: ``part2→away``, ``draw→draw`` — the same bridge ``venue_price_source`` and the resolver use.
_SIDE_SPECS: tuple[tuple[str, str, str], ...] = (
    ("part1", "home", "1X2|home|full"),
    ("draw", "draw", "1X2|draw|full"),
    ("part2", "away", "1X2|away|full"),
)

#: The PUBLIC Polymarket CLOB base URL (mirrors
#: ``veridex/venues/_vendor/polymarket_clob/client.py::CLOB_URL``). Hardcoded so importing this
#: module never pulls the heavy vendored client (numpy / web3) or any network library.
_CLOB_URL = "https://clob.polymarket.com"

#: Backfill venue-mid cadence band (seconds) the live cadence is compared against (20–40 min).
BACKFILL_CADENCE_LOW_S = 1200
BACKFILL_CADENCE_HIGH_S = 2400


# --------------------------------------------------------------------------- injectable seams
class FvSource(Protocol):
    """A live TxLINE FV source: yields :class:`MarketState` snapshots in ARRIVAL order."""

    def stream(self) -> AsyncIterator[MarketState]:
        """Return an async iterator of live :class:`MarketState` snapshots."""
        ...


class MidSource(Protocol):
    """A public venue-book mid source: ``token_id -> (mid, book_ts)``; ``(None, None)`` if illiquid."""

    async def fetch_mid(self, token_id: str) -> tuple[float | None, int | None]:
        """Fetch the current mid for *token_id*, or ``(None, None)`` when a book side is empty."""
        ...


class Recorder(Protocol):
    """An append-only sample sink (one dict row per poll per market)."""

    def record(self, row: dict[str, Any]) -> None:
        """Persist one sample row."""
        ...


# --------------------------------------------------------------------------- data model
@dataclass(frozen=True)
class MatchedMarket:
    """A resolved (fixture, side) → venue token binding the monitor polls.

    Attributes:
        fixture_id: TxLINE fixture id (FV is read from the matching ``MarketState``).
        txline_side: TxLINE side token (``part1``/``draw``/``part2``) — the ``stable_prob_bps`` key.
        venue_market_ref: The venue market ref (e.g. ``"1X2|home|full"``) — the analysis grouping key.
        token_id: The Polymarket CLOB token id whose ``/book`` mid is polled.
    """

    fixture_id: int
    txline_side: str
    venue_market_ref: str
    token_id: str


@dataclass(frozen=True)
class MarketCadence:
    """Per-market venue-mid change cadence (seconds between consecutive change events)."""

    key: tuple[int, str]
    deltas: list[int]
    median: float | None
    p25: float | None
    p75: float | None
    n: int


@dataclass(frozen=True)
class AnalysisResult:
    """The on-shutdown analysis: recorded samples, per-market cadence, the lead-lag probe, reports."""

    samples: list[dict[str, Any]]
    series_by_market: dict[tuple[int, str], tuple[list[int], list[float], list[float]]]
    cadence: list[MarketCadence]
    pooled_cadence_deltas: list[int]
    pooled_cadence_median: float | None
    probe: ProbeResult
    cadence_markdown: str
    leadlag_markdown: str


# --------------------------------------------------------------------------- market matching
async def match_markets(
    fixtures: list[dict[str, Any]],
    *,
    gamma_client: Any = None,
) -> list[MatchedMarket]:
    """Resolve each fixture's three 1X2 sides to venue tokens; skip (honestly) any unavailable side.

    Mirrors ``scripts/txline_live/cp1_backfill.py``: per fixture × side, ``resolve_market`` +
    ``side_to_token``. A :class:`MarketUnavailable` (or a side that cannot map to a token) is logged
    and skipped — never fabricated — so a partial event reads as a partial event (AC-2D-201).

    Args:
        fixtures: Operator fixtures — dicts with ``fixture_id``, ``event_slug``, ``home_team``,
            ``away_team`` (same shape as ``scripts/txline_live/cp1/fixtures.json``).
        gamma_client: Injectable Gamma-shaped client. Tests inject a fake returning recorded JSON;
            ``None`` lets ``resolve_market`` lazily construct the live client.

    Returns:
        The resolvable :class:`MatchedMarket`s (0–3 per fixture).
    """
    matched: list[MatchedMarket] = []
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
            matched.append(MatchedMarket(fixture_id, txline_side, market_ref, token_id))
    return matched


# --------------------------------------------------------------------------- FV history (SORTED+DEDUPED)
def _insort_fv(ts_list: list[int], val_list: list[float], ts: int, val: float) -> None:
    """Insert ``(ts, val)`` keeping the parallel lists ASCENDING by ts and DEDUPED (latest wins).

    THE load-bearing correctness contract: ``_aligned_mid`` bisects assuming ascending ts, but the
    live FV stream arrives out of order. On a duplicate ts the LATEST value replaces the prior one
    (a corrected/refreshed snapshot supersedes). This is what makes the downstream no-look-ahead
    alignment valid on arrival-ordered input.
    """
    pos = bisect.bisect_left(ts_list, ts)
    if pos < len(ts_list) and ts_list[pos] == ts:
        val_list[pos] = val  # duplicate ts → keep the latest value
    else:
        ts_list.insert(pos, ts)
        val_list.insert(pos, val)


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


# --------------------------------------------------------------------------- the monitor
async def run_monitor(
    *,
    matched: list[MatchedMarket],
    fv_source: FvSource,
    mid_source: MidSource,
    recorder: Recorder,
    poll_interval_s: float = 5.0,
    minutes: float = 30.0,
    freshness_s: int = 120,
    now_fn: Callable[[], float] = time.time,
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    max_polls: int | None = None,
) -> AnalysisResult:
    """Stream FV, poll venue mids, align with no look-ahead, record, then analyse on shutdown.

    An FV consumer task insorts each matched market's FV into a SORTED+DEDUPED per-market history.
    A single gathered poll loop samples ALL mids every ``poll_interval_s`` (a per-market failure
    writes a ``gap`` marker and continues), aligns each to the FV history via ``_aligned_mid``
    (most-recent-at-or-before — no look-ahead), and records the row. Shutdown is any of: SIGINT
    (an :class:`asyncio.Event`), the ``minutes`` deadline, or ``max_polls``. ``now_fn`` / ``sleep_fn``
    / ``max_polls`` are injected so tests are deterministic with NO real time and NO network.

    Args:
        matched: Markets to monitor (from :func:`match_markets`).
        fv_source: Live FV source (injectable).
        mid_source: Public venue-book mid source (injectable).
        recorder: Append-only sample sink (injectable).
        poll_interval_s: Seconds between poll rounds.
        minutes: Session wall-clock budget (deadline).
        freshness_s: FV staleness bound for alignment; an FV older than this yields ``None``.
        now_fn: Wall-clock source (injected in tests).
        sleep_fn: Async sleep (injected in tests).
        max_polls: Optional hard cap on poll rounds (deterministic test shutdown).

    Returns:
        The :class:`AnalysisResult` (cadence + lead-lag) over the recorded session.
    """
    stop = asyncio.Event()
    try:
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, stop.set)
    except (NotImplementedError, RuntimeError, ValueError):
        pass  # no signal support on this platform / loop — deadline + max_polls still bound the run

    # SORTED+DEDUPED per-market FV history: key -> (ascending ts_list, parallel val_list).
    fv_hist: dict[tuple[int, str], tuple[list[int], list[float]]] = {
        (m.fixture_id, m.txline_side): ([], []) for m in matched
    }
    by_fixture: dict[int, list[MatchedMarket]] = defaultdict(list)
    for m in matched:
        by_fixture[m.fixture_id].append(m)

    async def _consume_fv() -> None:
        async for state in fv_source.stream():
            for m in by_fixture.get(state.fixture_id, ()):
                fv = _fv_from_state(state, m.txline_side)
                if fv is None:
                    continue
                ts_list, val_list = fv_hist[(m.fixture_id, m.txline_side)]
                _insort_fv(ts_list, val_list, int(state.ts), fv)

    fv_task = asyncio.create_task(_consume_fv())
    # Give the FV task a scheduling slot before the first alignment read (drains buffered/canned FV).
    await asyncio.sleep(0)

    samples: list[dict[str, Any]] = []
    session_start = now_fn()
    deadline = session_start + minutes * 60.0
    polls = 0
    try:
        while not stop.is_set():
            if max_polls is not None and polls >= max_polls:
                break
            now = now_fn()
            if now >= deadline:
                break
            tick_ts = int(now)

            results = await asyncio.gather(
                *(mid_source.fetch_mid(m.token_id) for m in matched),
                return_exceptions=True,
            )
            for m, res in zip(matched, results, strict=True):
                ts_list, val_list = fv_hist[(m.fixture_id, m.txline_side)]
                fv, fv_staleness_s = _aligned_mid(ts_list, val_list, tick_ts, freshness_s)
                if isinstance(res, BaseException):
                    # One bad book never aborts the round: record an honest gap and continue.
                    row = {
                        "ts": tick_ts,
                        "fixture_id": m.fixture_id,
                        "txline_side": m.txline_side,
                        "venue_market_ref": m.venue_market_ref,
                        "token_id": m.token_id,
                        "fv": fv,
                        "fv_staleness_s": fv_staleness_s,
                        "mid": None,
                        "book_ts": None,
                        "gap": True,
                    }
                else:
                    mid, book_ts = res
                    row = {
                        "ts": tick_ts,
                        "fixture_id": m.fixture_id,
                        "txline_side": m.txline_side,
                        "venue_market_ref": m.venue_market_ref,
                        "token_id": m.token_id,
                        "fv": fv,
                        "fv_staleness_s": fv_staleness_s,
                        "mid": mid,
                        "book_ts": book_ts,
                    }
                samples.append(row)
                recorder.record(row)

            polls += 1
            await sleep_fn(poll_interval_s)
    finally:
        fv_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await fv_task  # shutdown must not mask the result

    return analyze_samples(samples)


# --------------------------------------------------------------------------- analysis (on shutdown)
def _percentile(sorted_vals: list[int], q: float) -> float | None:
    """Linear-interpolated percentile ``q`` in ``[0, 1]`` over an ascending list, or ``None`` if empty."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def analyze_samples(samples: list[dict[str, Any]]) -> AnalysisResult:
    """Group samples by ``(fixture_id, venue_market_ref)`` and compute cadence + the lead-lag probe.

    A sample is analysable only when BOTH ``fv`` and ``mid`` are present; ``gap`` markers and
    illiquid (``mid=None``) rows break a segment so no cadence delta or change event ever spans a
    gap (they neither create nor stretch a spurious step). The lead-lag probe is the committed
    ``run_leadlag_probe``, consumed unchanged.

    Args:
        samples: The recorded session rows.

    Returns:
        The :class:`AnalysisResult`.
    """
    by_market: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for row in samples:
        by_market[(int(row["fixture_id"]), str(row["venue_market_ref"]))].append(row)

    cadence: list[MarketCadence] = []
    series_by_market: dict[tuple[int, str], tuple[list[int], list[float], list[float]]] = {}
    pooled_deltas: list[int] = []

    for key, rows in sorted(by_market.items(), key=lambda kv: str(kv[0])):
        rows = sorted(rows, key=lambda r: int(r["ts"]))
        # Split into contiguous liquid segments; a gap / illiquid row starts a new segment.
        segments: list[list[dict[str, Any]]] = []
        cur: list[dict[str, Any]] = []
        for r in rows:
            if r.get("gap") or r.get("mid") is None or r.get("fv") is None:
                if cur:
                    segments.append(cur)
                    cur = []
                continue
            cur.append(r)
        if cur:
            segments.append(cur)

        valid = [r for seg in segments for r in seg]
        if valid:
            series_by_market[key] = (
                [int(r["ts"]) for r in valid],
                [float(r["fv"]) for r in valid],
                [float(r["mid"]) for r in valid],
            )

        deltas: list[int] = []
        for seg in segments:
            events = compress_to_change_events(
                [int(r["ts"]) for r in seg],
                [float(r["fv"]) for r in seg],
                [float(r["mid"]) for r in seg],
            )
            ev_ts = [e.ts for e in events]
            deltas.extend(ev_ts[i] - ev_ts[i - 1] for i in range(1, len(ev_ts)))
        pooled_deltas.extend(deltas)
        ordered = sorted(deltas)
        cadence.append(
            MarketCadence(
                key=key,
                deltas=deltas,
                median=statistics.median(deltas) if deltas else None,
                p25=_percentile(ordered, 0.25),
                p75=_percentile(ordered, 0.75),
                n=len(deltas),
            )
        )

    probe = (
        run_leadlag_probe(series_by_market)
        if series_by_market
        else ProbeResult(evidence=[], aggregates=[], verdict="NO DATA")
    )
    pooled_median = statistics.median(pooled_deltas) if pooled_deltas else None

    return AnalysisResult(
        samples=samples,
        series_by_market=series_by_market,
        cadence=cadence,
        pooled_cadence_deltas=pooled_deltas,
        pooled_cadence_median=pooled_median,
        probe=probe,
        cadence_markdown=render_cadence_markdown(cadence, pooled_deltas, pooled_median),
        leadlag_markdown=render_markdown(probe),
    )


def render_cadence_markdown(
    cadence: list[MarketCadence],
    pooled_deltas: list[int],
    pooled_median: float | None,
) -> str:
    """Render the LIVE venue-mid cadence vs the 20–40 min backfill band as Markdown."""

    def _fmt(value: float | None) -> str:
        return f"{value:.1f}" if value is not None else "n/a"

    lines = [
        "# LIVE venue-mid cadence — is the live Polymarket book as slow as the backfill?",
        "",
        (
            "Seconds between consecutive venue-mid CHANGE events (per market and pooled), from the "
            "committed `compress_to_change_events`. Change events NEVER span a `gap`/illiquid marker. "
            f"Compared against the BACKFILL band **{BACKFILL_CADENCE_LOW_S}–{BACKFILL_CADENCE_HIGH_S} s** "
            "(20–40 min): a live median far below it means the specific 20–40 min backfill-staleness "
            "edge does not transfer as-is — but the live lead-lag test, not cadence alone, is the arbiter."
        ),
        "",
        f"## POOLED median inter-change gap: {_fmt(pooled_median)} s  (n={len(pooled_deltas)})",
        "",
        "| fixture_id | venue_market_ref | median_s | p25_s | p75_s | n |",
        "|---|---|---|---|---|---|",
    ]
    for c in cadence:
        lines.append(
            f"| {c.key[0]} | {c.key[1]} | {_fmt(c.median)} | {_fmt(c.p25)} | {_fmt(c.p75)} | {c.n} |"
        )
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- token hygiene
def _scrub_token(text: str, creds: tuple[str, str] | None) -> str:
    """Redact BOTH TxLINE secret values (jwt + api-token) from ``text`` before it is printed.

    Mirrors ``scripts/maker/capture_and_pin.py::_scrub_token`` but scrubs the two TxLINE secrets.
    Scrubbing the raw values (not trusting an exception's type/provenance) preserves the no-leak
    guarantee even when an error surfaces from OUTSIDE this module (e.g. a network-SDK error whose
    message embeds a credential in a request URL/header).
    """
    if not creds:
        return text
    for secret in creds:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def txline_configured(settings: Any) -> bool:
    """Whether BOTH TxLINE credentials are present — the boolean (never the secrets) artifacts carry."""
    return settings.txline_jwt is not None and settings.txline_api_token is not None


def write_meta(
    path: Path,
    *,
    session_ts: int,
    matched: list[MatchedMarket],
    poll_interval_s: float,
    minutes: float,
    txline_configured: bool,
) -> None:
    """Write the session ``meta.json`` — secret-free telemetry (``txline_configured: bool`` only)."""
    meta = {
        "session_ts": session_ts,
        "poll_interval_s": poll_interval_s,
        "minutes": minutes,
        "txline_configured": bool(txline_configured),
        "matched_markets": [
            {
                "fixture_id": m.fixture_id,
                "txline_side": m.txline_side,
                "venue_market_ref": m.venue_market_ref,
                "token_id": m.token_id,
            }
            for m in matched
        ],
    }
    path.write_text(json.dumps(meta, sort_keys=True, indent=2))


# --------------------------------------------------------------------------- default LIVE sources
def _mid_from_book(book: dict[str, Any]) -> tuple[float | None, int | None]:
    """Public-book mid: ``(best_bid + best_ask) / 2`` (mirrors ``LOB.get_mid``); ``(None, None)`` if a side is empty.

    An empty bid OR ask side means no two-sided quote — the mid is NEVER imputed (illiquid).
    """
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    if not bids or not asks:
        return None, None
    best_bid = max(float(b["price"]) for b in bids)
    best_ask = min(float(a["price"]) for a in asks)
    raw_ts = book.get("timestamp")
    book_ts = int(raw_ts) if raw_ts is not None and raw_ts != "" else None
    return (best_bid + best_ask) / 2.0, book_ts


class _DefaultFvSource:
    """Live TxLINE FV source: wraps ``stream_marketstates`` in a reconnect/backoff loop (1s→60s).

    Creds are resolved ONCE via ``require_txline`` in ``__init__`` (fail-closed BEFORE any network
    I/O when absent) and held privately — never logged. ``httpx`` is imported lazily inside
    ``stream_marketstates``, so constructing this source touches no network at import time.
    """

    def __init__(self, *, settings: Any = None, base_url: str | None = None) -> None:
        from veridex.config import get_settings, require_txline

        settings = settings if settings is not None else get_settings()
        self._creds = require_txline(settings)  # fail-closed if either secret is missing
        self._base_url = base_url or settings.txline_base_url

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
                print(f"  FV stream disconnected: {_scrub_token(f'{type(exc).__name__}: {exc}', self._creds)}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


class _DefaultMidSource:
    """Public Polymarket ``/book`` mid source — lazy-``httpx`` GET (offline-safe to import).

    Mirrors ``_DefaultGammaClient``'s lazy-httpx pattern; hits the PUBLIC book endpoint (no wallet,
    no credential) and is NOT the wallet-bound vendored ``Polymarket`` class.
    """

    def __init__(self, *, clob_url: str = _CLOB_URL, timeout_s: float = 10.0) -> None:
        self._clob_url = clob_url
        self._timeout_s = timeout_s

    async def fetch_mid(self, token_id: str) -> tuple[float | None, int | None]:
        import httpx

        async with httpx.AsyncClient(base_url=self._clob_url, timeout=self._timeout_s) as http:
            response = await http.get("/book", params={"token_id": token_id})
            response.raise_for_status()
            book = response.json()
        return _mid_from_book(book)


class JsonlRecorder:
    """Append-only JSONL sample sink (one row per line)."""

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
        prog="live_monitor",
        description="Read-only LIVE FV-vs-venue monitor (cadence + live lead-lag).",
    )
    parser.add_argument("--fixtures", required=True, help="path to fixtures.json (fixture_id/event_slug/teams)")
    parser.add_argument("--poll-interval-s", type=float, default=5.0, dest="poll_interval_s")
    parser.add_argument("--minutes", type=float, default=30.0)
    parser.add_argument("--out", default=".omc/research/live-monitor", help="session output root")
    parser.add_argument(
        "--every",
        type=int,
        default=None,
        dest="every",
        help="(optional) also emit interim analysis every N polls (default: on-shutdown only)",
    )
    return parser


async def _run_cli(args: argparse.Namespace) -> AnalysisResult:
    """Operator entrypoint: match markets (live Gamma), run the monitor, write artifacts + reports."""
    from veridex.config import get_settings

    settings = get_settings()
    fixtures = json.loads(Path(args.fixtures).read_text())
    if not isinstance(fixtures, list) or not fixtures:
        raise SystemExit(f"fixtures file {args.fixtures} must be a non-empty JSON list")

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
        poll_interval_s=args.poll_interval_s,
        minutes=args.minutes,
        txline_configured=txline_configured(settings),
    )
    recorder = JsonlRecorder(session_dir / "samples.jsonl")
    try:
        result = await run_monitor(
            matched=matched,
            fv_source=_DefaultFvSource(settings=settings),
            mid_source=_DefaultMidSource(),
            recorder=recorder,
            poll_interval_s=args.poll_interval_s,
            minutes=args.minutes,
        )
    finally:
        recorder.close()

    research = Path(".omc/research")
    research.mkdir(parents=True, exist_ok=True)
    (research / "live-venue-cadence.md").write_text(result.cadence_markdown)
    (research / "live-leadlag-probe.md").write_text(result.leadlag_markdown)
    print(f"session: {session_dir}")
    print(f"pooled cadence median: {result.pooled_cadence_median} s (backfill band {BACKFILL_CADENCE_LOW_S}-{BACKFILL_CADENCE_HIGH_S} s)")
    print(f"lead-lag verdict: {result.probe.verdict}")
    return result


def main(argv: list[str] | None = None) -> int:
    """Parse ``argv`` and run the operator monitor session."""
    args = build_parser().parse_args(argv)
    asyncio.run(_run_cli(args))
    return 0


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(main())
