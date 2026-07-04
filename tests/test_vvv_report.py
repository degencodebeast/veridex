"""C-2 (S5, Codex M1) — the VvV PRODUCER seam migrated to the 4-arg time-aligned venue source.

The producer (``vvv_report_with_estimated_edge`` + its inner ``_estimated_edge_over_fired_picks``) is
the OTHER consumer of the venue seam: it re-prices the strategy's ACTUAL fired picks against the
injected source to attach a POST-BUILD estimated edge. These tests prove that consumer now speaks the
same 4-arg ``(fixture_id, market_key, side, ts) -> TimedVenueQuote | None`` contract as the agent —
and that a market-key-only stub (the old shape) no longer works, so the migration can't be half-done.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_drift_agent import _ms
from tests.test_value_vs_venue import _write_1x2_session
from veridex.ingest.marketstate import MarketState
from veridex.runtime.window import RunWindow
from veridex.venues.polymarket import decimal_to_native
from veridex.venues.price_history import VenuePriceHistoryFrame
from veridex.venues.venue_price_source import (
    TimedVenueQuote,
    build_backfilled_venue_source,
)

_FIXTURE_ID = 5


def _q(price: float, *, staleness_s: int = 0) -> TimedVenueQuote:
    return TimedVenueQuote(venue_decimal_price=price, staleness_s=staleness_s)


def _window() -> RunWindow:
    return RunWindow(
        window_id="w_vvv_report",
        fixture_id=_FIXTURE_ID,
        market_allowlist=["1X2"],
        end_rule="pre_match",
        min_clv_horizon_s=0,
    )


def test_estimated_edge_seam_calls_the_4arg_timed_source_and_rejects_market_key_only_stub() -> None:
    """The inner producer seam re-prices a fired pick via ``source(fixture_id, market_key, side, ts)``.

    Deterministic proof of the migration: over a single synthetic FIRED pick, a 4-arg
    ``TimedVenueQuote`` source yields the pick's estimated edge, while the OLD market-key-only stub
    (``lambda mk: price``) now raises ``TypeError`` because the seam calls it with four arguments.
    """
    from veridex.backtest.vvv_report import _estimated_edge_over_fired_picks
    from veridex.strategies.value_vs_venue import vvv_signal

    # A synthetic sealed run with ONE FOLLOW_MOMENTUM pick on the REAL 1X2-full key / part1 @ 6000 bps.
    row = {
        "raw_prescore": {
            "raw_action": {
                "type": "FOLLOW_MOMENTUM",
                "params": {"market_key": "1X2_PARTICIPANT_RESULT||", "side": "part1"},
            }
        },
        "tick_seq": 0,
    }
    result = SimpleNamespace(score_rows=[row])
    states_by_tick = {0: _ms(6000, mk="1X2_PARTICIPANT_RESULT||", side="part1")}

    edge = _estimated_edge_over_fired_picks(
        result,  # type: ignore[arg-type]
        states_by_tick,
        venue_price_source=lambda fixture_id, market_key, side, ts: _q(2.0),
        min_edge_bps=0,
    )
    # 6000 bps (0.60) @ decimal 2.0 → edge = 0.60*2.0-1 = +0.20 → +2000 bps (the fired pick's edge).
    assert edge == vvv_signal(6000, 2.0)["estimated_executable_edge_bps"]
    assert edge == 2000

    # The OLD market-key-only stub is called with 4 args by the migrated seam → TypeError.
    with pytest.raises(TypeError):
        _estimated_edge_over_fired_picks(
            result,  # type: ignore[arg-type]
            states_by_tick,
            venue_price_source=lambda mk: 2.0,  # type: ignore[arg-type,misc]
            min_edge_bps=0,
        )


def test_estimated_edge_prices_seconds_tick_against_seconds_frames() -> None:
    """Units regression (Run-002-VvV): the producer re-prices a fired pick via a SECONDS-keyed source.

    ``_estimated_edge_over_fired_picks`` queries the source with the tick's ``MarketState.ts``, which is
    ALREADY unix seconds (the normalizer floors ms→s). It is passed through unconverted, so a seconds ts
    within the 900s bound MATCHES → the fired pick is priced → a real estimated edge. Re-dividing the
    already-seconds ts by 1000 (the reverted e4e5608 bug) made staleness ≈ 1.78M ≫ 900 → ``None`` →
    skipped → edge ``None``. The fixed ``_q`` stubs above ignore ``ts``, so only a REAL ts-sensitive
    ``build_backfilled_venue_source`` catches the units mismatch.
    """
    from types import SimpleNamespace

    from veridex.backtest.vvv_report import _estimated_edge_over_fired_picks
    from veridex.strategies.value_vs_venue import vvv_signal

    frame_late_s = 1_782_642_000  # unix SECONDS (10-digit), Polymarket-canonical
    query_ts_s = 1_782_642_003  # decision ts in unix SECONDS (10-digit) — the real MarketState.ts scale
    frames = [
        VenuePriceHistoryFrame(
            ts=ts,
            fixture_id=_FIXTURE_ID,
            market_ref="1X2|home|full",
            condition_id="0xcond",
            token_id="tok-home",
            native_price=decimal_to_native(2.0),
            venue_decimal_price=2.0,
            price_kind="clob-prices-history",
            fidelity_s=60,
        )
        for ts in (1_782_641_900, frame_late_s)
    ]
    src, _sid = build_backfilled_venue_source(
        frames,
        price_history_artifact_hashes=["ph#1"],
        coverage_artifact_hash="cov#1",
        freshness_s=900,
        haircut_ladder_bps=[0, 100, 200, 300],
    )
    state = MarketState(
        fixture_id=_FIXTURE_ID,
        tick_seq=0,
        ts=query_ts_s,
        phase=0,
        markets={
            "1X2_PARTICIPANT_RESULT||": {
                "stable_prob_bps": {"part1": 6000},
                "stable_price": {"part1": 2.0},
                "suspended": False,
            }
        },
        scores={},
    )
    row = {
        "raw_prescore": {
            "raw_action": {
                "type": "FOLLOW_MOMENTUM",
                "params": {"market_key": "1X2_PARTICIPANT_RESULT||", "side": "part1"},
            }
        },
        "tick_seq": 0,
    }
    result = SimpleNamespace(score_rows=[row])

    edge = _estimated_edge_over_fired_picks(
        result,  # type: ignore[arg-type]
        {0: state},
        venue_price_source=src,
        min_edge_bps=0,
    )

    # 6000 bps (0.60) @ decimal 2.0 → edge +0.20 → +2000 bps; None here means a re-divided ts overran the bound.
    assert edge is not None, "seconds frames priced at a seconds tick must MATCH (staleness 3s), not be skipped"
    assert edge == vvv_signal(6000, 2.0)["estimated_executable_edge_bps"] == 2000


async def test_vvv_report_uses_4arg_timed_source(tmp_path: Path) -> None:
    """End-to-end: a 4-arg ``TimedVenueQuote`` source drives BOTH the agent and the post-build edge.

    Before the migration this fails (the agent errors on every 4-arg call, fires nothing → the edge is
    ``None``); after it, venue decimal 5.0 fires every fixture-5 side and the estimated edge attaches.
    """
    from veridex.backtest.vvv_report import vvv_report_with_estimated_edge

    # REAL-FORMAT 1X2-full pack so the agent's market-identity bridge resolves each side to its frame ref.
    pack_dir = _write_1x2_session(tmp_path, [40.0, 25.0, 35.0])

    result, report = await vvv_report_with_estimated_edge(
        pack_dir,
        _FIXTURE_ID,
        venue_price_source=lambda fixture_id, market_key, side, ts: _q(5.0, staleness_s=60),
        venue_source_id="src#1",
        window=_window(),
        min_edge_bps=0,
        assumptions={"no_interpolation": True},
    )

    assert report.estimated_executable_edge_bps is not None  # fired + attached post-build
    assert report.real_executable_edge_bps is None  # paper venue — no live fill (CON-003)
    assert report.run_id == result.run_id
