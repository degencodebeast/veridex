"""CON-016 participant 1X2 tracked-series tests (E2).

The tracked series is the raw, de-vigged fair-probability signal for ONE
participant's 1X2 win, kept only while a fixture is in-running, the 1X2 market
is present + un-suspended, and the implied probability is inside the near-certain
band. Discovery (pack 17588234) confirmed: ``MarketState.phase`` is 0=pre-match /
1=in-running, the 1X2 market surfaces under ``1X2_PARTICIPANT_RESULT|half=1|`` and
its side tokens are ``part1`` / ``draw`` / ``part2`` (home/away is NOT a token).
"""

from __future__ import annotations

from pathlib import Path

from veridex.backtest.event_probe.series import TrackedTick, build_tracked_series
from veridex.ingest.marketstate import MarketState
from veridex.strategies.market_quality import DEFAULT_MARKET_QUALITY_CONFIG

_IN_RUNNING = 1
_PRE_MATCH = 0
_KEY_1X2 = "1X2_PARTICIPANT_RESULT|half=1|"


def _state(
    *,
    phase: int,
    ts: int = 1000,
    part1_bps: int = 5500,
    part2_bps: int = 3000,
    draw_bps: int = 1500,
    suspended: bool = False,
    tick_seq: int = 0,
) -> MarketState:
    """Build a MarketState carrying a single 1X2 market with the given tokens."""
    return MarketState(
        fixture_id=17588234,
        tick_seq=tick_seq,
        ts=ts,
        phase=phase,
        markets={
            _KEY_1X2: {
                "stable_prob_bps": {"part1": part1_bps, "draw": draw_bps, "part2": part2_bps},
                "stable_price": {},
                "suspended": suspended,
            }
        },
        scores={},
    )


def test_series_uses_stable_prob_bps_over_10000() -> None:
    """E2-t1: prob is stable_prob_bps[token] / 10000 (5500 -> 0.55)."""
    states = [_state(phase=_IN_RUNNING, part1_bps=5500)]

    series = build_tracked_series(states, participant=1)

    assert len(series) == 1
    assert isinstance(series[0], TrackedTick)
    assert series[0].prob == 0.55


def test_series_in_running_only() -> None:
    """E2-t2: pre-match ticks (phase != in-running) are excluded."""
    states = [
        _state(phase=_PRE_MATCH, ts=100, part1_bps=5000),
        _state(phase=_IN_RUNNING, ts=200, part1_bps=6000),
    ]

    series = build_tracked_series(states, participant=1)

    assert [t.ts for t in series] == [200]
    assert series[0].prob == 0.6


def test_series_skips_suspended() -> None:
    """E2-t3: a tick whose 1X2 market is suspended is excluded."""
    states = [
        _state(phase=_IN_RUNNING, ts=100, suspended=True, part1_bps=5000),
        _state(phase=_IN_RUNNING, ts=200, suspended=False, part1_bps=5000),
    ]

    series = build_tracked_series(states, participant=1)

    assert [t.ts for t in series] == [200]


def test_series_near_certain_band_guard_only() -> None:
    """E2-t4: prob < band_lo or > band_hi is excluded, using ONLY band constants.

    The band bounds are read from ``MarketQualityConfig`` (via the default config);
    ``series.py`` must NOT import or call ``evaluate_market_quality`` -- proven here
    structurally against the module source.
    """
    states = [
        _state(phase=_IN_RUNNING, ts=100, part1_bps=200, part2_bps=9700, draw_bps=100),   # 0.02 < 0.05
        _state(phase=_IN_RUNNING, ts=200, part1_bps=9800, part2_bps=100, draw_bps=100),   # 0.98 > 0.95
        _state(phase=_IN_RUNNING, ts=300, part1_bps=5500, part2_bps=3000, draw_bps=1500),  # in-band
    ]

    series = build_tracked_series(states, participant=1)

    assert [t.ts for t in series] == [300]
    assert DEFAULT_MARKET_QUALITY_CONFIG.band_lo <= series[0].prob <= DEFAULT_MARKET_QUALITY_CONFIG.band_hi

    # Structural proof: the eligibility evaluator is never imported/called here.
    import veridex.backtest.event_probe.series as series_mod

    source = Path(series_mod.__file__).read_text()
    assert "evaluate_market_quality" not in source


def test_series_participant_token_mapping() -> None:
    """E2-t5: participant 1 reads part1, participant 2 reads part2 (never inverted)."""
    states = [_state(phase=_IN_RUNNING, part1_bps=6000, part2_bps=2500, draw_bps=1500)]

    p1 = build_tracked_series(states, participant=1)
    p2 = build_tracked_series(states, participant=2)

    assert [t.prob for t in p1] == [0.6]
    assert [t.prob for t in p2] == [0.25]


def test_series_skips_state_missing_1x2_key() -> None:
    """E2-t6: an in-running state with NO 1X2 key yields [] (no crash).

    This is the dominant real-data branch -- ~31,737 states in pack 17588234
    carry no ``1X2_PARTICIPANT_RESULT|half=1|`` market -- so the missing-key path
    must be skipped, never raise a KeyError.
    """
    state = MarketState(
        fixture_id=17588234,
        tick_seq=0,
        ts=1000,
        phase=_IN_RUNNING,
        markets={
            "OVERUNDER_PARTICIPANT_GOALS|half=1|line=2.5": {
                "stable_prob_bps": {"over": 5000, "under": 5000},
                "stable_price": {},
                "suspended": False,
            }
        },
        scores={},
    )

    assert build_tracked_series([state], 1) == []


def test_series_band_boundary_strict() -> None:
    """E2-t7: the near-certain band is STRICT -- exactly band_lo/band_hi are KEPT.

    prob == 0.05 (500 bps) and prob == 0.95 (9500 bps) sit on the band edge and
    are kept; 0.0499 (499 bps) and 0.9501 (9501 bps) fall just outside and are
    excluded. Pins the ``<``/``>`` semantics that t4's 0.02/0.98 does not nail.
    """
    states = [
        _state(phase=_IN_RUNNING, ts=100, part1_bps=500),   # exactly band_lo -> kept
        _state(phase=_IN_RUNNING, ts=200, part1_bps=9500),  # exactly band_hi -> kept
        _state(phase=_IN_RUNNING, ts=300, part1_bps=499),   # just below band_lo -> excluded
        _state(phase=_IN_RUNNING, ts=400, part1_bps=9501),  # just above band_hi -> excluded
    ]

    series = build_tracked_series(states, participant=1)

    assert [t.ts for t in series] == [100, 200]
    assert [t.prob for t in series] == [0.05, 0.95]
