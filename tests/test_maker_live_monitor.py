"""Offline tests for the read-only live FV-vs-venue monitor (``scripts/maker/live_monitor.py``).

Every live source is behind an injectable ``Protocol`` and every test drives ``run_monitor``
with offline fakes (canned ``MarketState`` FV, scripted mids, a deterministic clock, and an
injected no-op sleep) — NO network, NO real time, NO TxLINE token. The default live sources are
NEVER constructed here (asserted), and the module imports no network library at module scope.

The load-bearing correctness test is :func:`test_out_of_order_fv_never_mis_selects` (Codex Major):
``stream_marketstates`` yields records in ARRIVAL order, not ``state.ts`` order, but
``_aligned_mid`` assumes ascending timestamps — so the monitor MUST keep per-market FV history
sorted+deduped by ``ts`` on insert. That test is written to go RED against a naive append-order
history and GREEN only once the sorted insert is in place.
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.maker.live_monitor import (
    MatchedMarket,
    _mid_from_book,
    _scrub_token,
    match_markets,
    run_monitor,
    txline_configured,
    write_meta,
)
from tests.test_maker_leadlag_probe import (
    synth_fv_leads_venue,
    synth_symmetric_comovement,
)
from veridex.config import Settings
from veridex.ingest.marketstate import MarketState

_HEADLINE_BPS = 50
_FULL_KEY = "1X2_PARTICIPANT_RESULT||"


# --------------------------------------------------------------------------- offline fakes
class FakeFvSource:
    """A network-free ``FvSource``: replays canned ``MarketState``s in arrival order.

    ``stream`` deliberately performs NO ``await`` between yields, so under CPython's
    deterministic asyncio scheduling it drains fully the first time the consumer task runs.
    """

    def __init__(self, states: list[MarketState]) -> None:
        self._states = states

    async def stream(self) -> Any:  # AsyncIterator[MarketState]
        for state in self._states:
            yield state


class FakeMidSource:
    """A scripted ``MidSource``: returns the next ``(mid, book_ts)`` per token, then ``(None, None)``."""

    def __init__(self, scripts: dict[str, list[tuple[float | None, int | None]]]) -> None:
        self._scripts = {tok: list(seq) for tok, seq in scripts.items()}
        self._calls: dict[str, int] = {}

    async def fetch_mid(self, token_id: str) -> tuple[float | None, int | None]:
        i = self._calls.get(token_id, 0)
        self._calls[token_id] = i + 1
        seq = self._scripts.get(token_id, [])
        return seq[i] if i < len(seq) else (None, None)


class RaisingMidSource:
    """A ``MidSource`` that always raises — exercises the per-market poll-failure gap path."""

    async def fetch_mid(self, token_id: str) -> tuple[float | None, int | None]:
        raise RuntimeError("book fetch failed")


class ListRecorder:
    """A ``Recorder`` that collects rows in memory for assertions."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def record(self, row: dict[str, Any]) -> None:
        self.rows.append(dict(row))


class _SeqClock:
    """Deterministic ``now_fn``: returns each value in turn, then repeats the last."""

    def __init__(self, values: list[float]) -> None:
        self._values = list(values)
        self._i = 0

    def __call__(self) -> float:
        v = self._values[min(self._i, len(self._values) - 1)]
        self._i += 1
        return v


async def _no_sleep(_seconds: float) -> None:
    """Injected ``sleep_fn`` that consumes no real time (still an await point)."""
    return None


def _fv_state(fixture_id: int, ts: int, fv_by_side: dict[str, float]) -> MarketState:
    """Build a ``MarketState`` carrying the 1X2 full-match market with the given per-side FV (native prob)."""
    return MarketState(
        fixture_id=fixture_id,
        tick_seq=0,
        ts=ts,
        phase=1,
        markets={_FULL_KEY: {"stable_prob_bps": {side: round(fv * 1e4) for side, fv in fv_by_side.items()}}},
        scores={},
    )


def _gamma_market(question: str, condition_id: str, yes_token: str, no_token: str) -> dict[str, Any]:
    return {
        "conditionId": condition_id,
        "question": question,
        "outcomes": json.dumps(["Yes", "No"]),
        "clobTokenIds": json.dumps([yes_token, no_token]),
        "orderPriceMinTickSize": "0.01",
    }


class FakeGammaClient:
    """Injected Gamma client returning a fixed market list regardless of slug (offline)."""

    def __init__(self, markets: list[dict[str, Any]]) -> None:
        self._markets = markets

    async def get_markets(self, **_params: Any) -> list[dict[str, Any]]:
        return list(self._markets)


async def _drive_single_market(
    ts: list[int],
    fv: list[float],
    mid: list[float | None],
    *,
    fixture_id: int = 100,
    txline_side: str = "part1",
    venue_market_ref: str = "1X2|home|full",
    token_id: str = "tok-home",
    freshness_s: int = 10**9,
) -> tuple[Any, ListRecorder]:
    """Reproduce a ``(ts, fv, mid)`` series through ``run_monitor`` for one market."""
    states = [_fv_state(fixture_id, ts[i], {txline_side: fv[i]}) for i in range(len(ts))]
    mid_source = FakeMidSource({token_id: [(mid[i], ts[i]) for i in range(len(ts))]})
    matched = [MatchedMarket(fixture_id, txline_side, venue_market_ref, token_id)]
    recorder = ListRecorder()
    clock = _SeqClock([ts[0], *ts])
    result = await run_monitor(
        matched=matched,
        fv_source=FakeFvSource(states),
        mid_source=mid_source,
        recorder=recorder,
        poll_interval_s=1.0,
        minutes=1e9,
        freshness_s=freshness_s,
        now_fn=clock,
        sleep_fn=_no_sleep,
        max_polls=len(ts),
    )
    return result, recorder


# --------------------------------------------------------------------------- tests
def test_records_match_injected_ticks_no_lookahead() -> None:
    """#1 Recorded rows carry the aligned FV/mid at each poll, and the FV used is never in the future."""
    states = [
        _fv_state(100, 100, {"part1": 0.60}),
        _fv_state(100, 110, {"part1": 0.70}),
    ]
    mid_source = FakeMidSource({"tok": [(0.50, 100), (0.55, 110)]})
    matched = [MatchedMarket(100, "part1", "1X2|home|full", "tok")]
    recorder = ListRecorder()
    clock = _SeqClock([105, 105, 115])  # start, poll@105, poll@115

    asyncio.run(
        run_monitor(
            matched=matched,
            fv_source=FakeFvSource(states),
            mid_source=mid_source,
            recorder=recorder,
            poll_interval_s=1.0,
            minutes=1e9,
            freshness_s=10**9,
            now_fn=clock,
            sleep_fn=_no_sleep,
            max_polls=2,
        )
    )

    rows = [r for r in recorder.rows if not r.get("gap")]
    assert len(rows) == 2
    assert rows[0]["ts"] == 105 and abs(rows[0]["fv"] - 0.60) < 1e-9 and rows[0]["mid"] == 0.50
    assert rows[1]["ts"] == 115 and abs(rows[1]["fv"] - 0.70) < 1e-9 and rows[1]["mid"] == 0.55
    # No look-ahead: the FV used is always at or before the poll ts (staleness >= 0).
    for row in rows:
        assert row["fv_staleness_s"] is not None and row["fv_staleness_s"] >= 0


def test_cadence_deltas_match_injected_venue_steps() -> None:
    """#2 The cadence deltas equal the gaps between the injected venue-mid change events."""
    ts = [0, 10, 30, 55]
    fv = [0.50, 0.50, 0.50, 0.50]
    mid: list[float | None] = [0.40, 0.45, 0.50, 0.55]
    result, _ = asyncio.run(_drive_single_market(ts, fv, mid, freshness_s=10**9))
    (cadence,) = result.cadence
    assert cadence.deltas == [20, 25]
    assert cadence.median == 22.5


def test_leading_market_next_rate_above_half() -> None:
    """#3a A market where the venue FOLLOWS a leading FV → NEXT-change hit rate materially > 0.5."""
    ts, fv, mid = synth_fv_leads_venue()
    result, _ = asyncio.run(_drive_single_market(ts, fv, list(mid)))
    agg = next(a for a in result.probe.aggregates if a.threshold_bps == _HEADLINE_BPS)
    assert agg.next_rate is not None
    assert agg.next_rate > 0.55


def test_null_market_next_rate_near_half() -> None:
    """#3b A symmetric co-moving (no-lead) market → NEXT-change hit rate ~0.5 (probe does not fabricate)."""
    ts, fv, mid = synth_symmetric_comovement()
    result, _ = asyncio.run(_drive_single_market(ts, fv, list(mid)))
    agg = next(a for a in result.probe.aggregates if a.threshold_bps == _HEADLINE_BPS)
    assert agg.next_rate is not None
    assert abs(agg.next_rate - 0.5) < 0.10


def test_no_live_client_constructed_and_no_module_scope_network_import() -> None:
    """#4 Import-audit: no network lib at module scope; and a fully-injected run never builds a default source."""
    import scripts.maker.live_monitor as mod

    source = Path(mod.__file__).read_text()
    tree = ast.parse(source)
    top_level_imports: set[str] = set()
    for node in tree.body:  # MODULE scope only — lazy imports inside functions are allowed
        if isinstance(node, ast.Import):
            top_level_imports.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            top_level_imports.add(node.module.split(".")[0])
    forbidden = {"httpx", "requests", "websocket", "websockets", "aiohttp", "numpy", "web3"}
    assert not (top_level_imports & forbidden), f"module-scope network import: {top_level_imports & forbidden}"

    # A fully-injected run must never touch the default (network) source classes.
    class _Boom:
        def __init__(self, *a: Any, **k: Any) -> None:
            raise AssertionError("default live source was constructed in an injected run")

    orig_fv, orig_mid = mod._DefaultFvSource, mod._DefaultMidSource
    mod._DefaultFvSource, mod._DefaultMidSource = _Boom, _Boom  # type: ignore[assignment]
    try:
        result, recorder = asyncio.run(
            _drive_single_market([0, 1, 2], [0.5, 0.5, 0.5], [0.4, 0.45, 0.5])
        )
    finally:
        mod._DefaultFvSource, mod._DefaultMidSource = orig_fv, orig_mid  # type: ignore[assignment]
    assert len([r for r in recorder.rows if not r.get("gap")]) == 3


def test_no_token_leak_in_artifacts_or_scrub(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """#5 Creds never reach any artifact/stdout; ``_scrub_token`` redacts both jwt and api-token."""
    jwt, token = "JWT-SECRET-123", "APITOKEN-456"

    # Scrub redacts BOTH secret values from an arbitrary error string.
    scrubbed = _scrub_token(f"boom jwt={jwt} tok={token} in url", (jwt, token))
    assert jwt not in scrubbed and token not in scrubbed

    # meta.json carries only the boolean, never the secrets.
    settings = Settings(_env_file=None, JWT=jwt, TXLINE_X_API_TOKEN=token)  # type: ignore[call-arg]
    assert txline_configured(settings) is True
    meta_path = tmp_path / "meta.json"
    matched = [MatchedMarket(100, "part1", "1X2|home|full", "tok")]
    write_meta(
        meta_path,
        session_ts=1700000000,
        matched=matched,
        poll_interval_s=5.0,
        minutes=30.0,
        txline_configured=True,
    )
    meta_text = meta_path.read_text()
    assert "txline_configured" in meta_text
    assert jwt not in meta_text and token not in meta_text

    # A full run's samples never carry a secret, and nothing printed leaks one.
    result, recorder = asyncio.run(
        _drive_single_market([0, 1, 2], [0.5, 0.5, 0.5], [0.4, 0.45, 0.5])
    )
    samples_blob = json.dumps(recorder.rows)
    captured = capsys.readouterr()
    assert jwt not in samples_blob and token not in samples_blob
    assert jwt not in captured.out and token not in captured.out


def test_fail_closed_when_creds_absent() -> None:
    """#6 The default FV source fails closed (before any network I/O) when TxLINE creds are absent."""
    import scripts.maker.live_monitor as mod

    settings = Settings(_env_file=None)  # no JWT / TXLINE_X_API_TOKEN
    with pytest.raises(ValueError):
        mod._DefaultFvSource(settings=settings)


def test_illiquid_book_recorded_as_none_and_excluded() -> None:
    """#7 An empty-book mid is ``(None, None)`` → recorded with ``mid=None`` and excluded from the series."""
    # _mid_from_book: an empty bid/ask side yields (None, None), never imputed.
    assert _mid_from_book({"bids": [], "asks": [{"price": "0.6", "size": "5"}], "timestamp": "1"}) == (None, None)
    assert _mid_from_book({"bids": [{"price": "0.4", "size": "5"}], "asks": [], "timestamp": "1"}) == (None, None)

    ts = [0, 10, 20]
    fv = [0.5, 0.5, 0.5]
    mid: list[float | None] = [0.40, None, 0.50]  # middle poll returns an illiquid (None) mid
    result, recorder = asyncio.run(_drive_single_market(ts, fv, mid, freshness_s=10**9))
    rows = [r for r in recorder.rows if not r.get("gap")]
    none_rows = [r for r in rows if r["mid"] is None]
    assert len(none_rows) == 1 and none_rows[0]["ts"] == 10
    # Excluded from the analysed series: only the two liquid samples survive.
    (_key, (series_ts, _fv, series_mid)) = next(iter(result.series_by_market.items()))
    assert 10 not in series_ts
    assert all(m is not None for m in series_mid)


def test_market_unavailable_skips_side_and_continues() -> None:
    """#8 A ``MarketUnavailable`` side is skipped/logged; the other sides still match."""
    markets = [
        _gamma_market("Will Portugal win the match?", "0xHOME", "tok-home-yes", "tok-home-no"),
        _gamma_market("Will the match end in a draw?", "0xDRAW", "tok-draw-yes", "tok-draw-no"),
        # NOTE: no "Will Croatia win…" market → the away side is MarketUnavailable.
    ]
    fixtures = [
        {"fixture_id": 1, "event_slug": "fifwc-prt-hrv", "home_team": "Portugal", "away_team": "Croatia"}
    ]
    matched = asyncio.run(match_markets(fixtures, gamma_client=FakeGammaClient(markets)))
    sides = {m.txline_side for m in matched}
    assert sides == {"part1", "draw"}  # away (part2) skipped, home + draw survive
    assert all(isinstance(m, MatchedMarket) for m in matched)


def test_out_of_order_fv_never_mis_selects() -> None:
    """#9 (Codex Major) Out-of-order FV arrival must not mis-select or look ahead.

    The FV stream emits ``ts`` in ARRIVAL order ``[100, 110, 105]``. A poll at ``ts=107`` MUST align
    to FV@105 (the most-recent-at-or-before), never FV@110 (a future value) and never FV@100 (the
    value a naive bisect over the unsorted append-order array would wrongly pick). A later duplicate
    ``ts=105`` keeps the LATEST value.
    """
    # Arrival order is NOT ts order: 100, then 110, then a delayed 105.
    states = [
        _fv_state(100, 100, {"part1": 0.61}),
        _fv_state(100, 110, {"part1": 0.63}),
        _fv_state(100, 105, {"part1": 0.62}),
    ]
    mid_source = FakeMidSource({"tok": [(0.50, 107)]})
    matched = [MatchedMarket(100, "part1", "1X2|home|full", "tok")]
    recorder = ListRecorder()
    clock = _SeqClock([107, 107])  # start, single poll @107

    asyncio.run(
        run_monitor(
            matched=matched,
            fv_source=FakeFvSource(states),
            mid_source=mid_source,
            recorder=recorder,
            poll_interval_s=1.0,
            minutes=1e9,
            freshness_s=10**9,
            now_fn=clock,
            sleep_fn=_no_sleep,
            max_polls=1,
        )
    )
    (row,) = [r for r in recorder.rows if not r.get("gap")]
    assert abs(row["fv"] - 0.62) < 1e-9, f"expected FV@105=0.62, got {row['fv']}"
    assert abs(row["fv"] - 0.63) > 1e-9  # never a FUTURE value
    assert abs(row["fv"] - 0.61) > 1e-9  # never the naive append-order (unsorted-bisect) value

    # Duplicate ts keeps the latest value.
    states_dup = [
        _fv_state(100, 100, {"part1": 0.61}),
        _fv_state(100, 110, {"part1": 0.63}),
        _fv_state(100, 105, {"part1": 0.62}),
        _fv_state(100, 105, {"part1": 0.99}),  # same ts, later arrival → wins
    ]
    recorder2 = ListRecorder()
    clock2 = _SeqClock([107, 107])
    asyncio.run(
        run_monitor(
            matched=matched,
            fv_source=FakeFvSource(states_dup),
            mid_source=FakeMidSource({"tok": [(0.50, 107)]}),
            recorder=recorder2,
            poll_interval_s=1.0,
            minutes=1e9,
            freshness_s=10**9,
            now_fn=clock2,
            sleep_fn=_no_sleep,
            max_polls=1,
        )
    )
    (row2,) = [r for r in recorder2.rows if not r.get("gap")]
    assert abs(row2["fv"] - 0.99) < 1e-9, f"duplicate ts must keep latest 0.99, got {row2['fv']}"


def test_poll_failure_records_gap_and_continues() -> None:
    """A per-market poll failure writes a ``gap`` marker and the run still completes (one bad book never aborts)."""
    states = [_fv_state(100, 0, {"part1": 0.5})]
    matched = [MatchedMarket(100, "part1", "1X2|home|full", "tok")]
    recorder = ListRecorder()
    clock = _SeqClock([0, 0, 1, 2])
    asyncio.run(
        run_monitor(
            matched=matched,
            fv_source=FakeFvSource(states),
            mid_source=RaisingMidSource(),
            recorder=recorder,
            poll_interval_s=1.0,
            minutes=1e9,
            freshness_s=10**9,
            now_fn=clock,
            sleep_fn=_no_sleep,
            max_polls=3,
        )
    )
    gaps = [r for r in recorder.rows if r.get("gap")]
    assert len(gaps) == 3  # every poll failed → three honest gap markers, no crash
