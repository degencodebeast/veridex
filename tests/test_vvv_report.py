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
from tests.test_replay_pack import _write_session
from veridex.ingest.replay_pack import pack_from_session
from veridex.runtime.window import RunWindow
from veridex.venues.venue_price_source import TimedVenueQuote

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

    # A synthetic sealed run with ONE FOLLOW_MOMENTUM pick on 1X2|home / home @ 6000 bps at tick 0.
    row = {
        "raw_prescore": {
            "raw_action": {"type": "FOLLOW_MOMENTUM", "params": {"market_key": "1X2|home", "side": "home"}}
        },
        "tick_seq": 0,
    }
    result = SimpleNamespace(score_rows=[row])
    states_by_tick = {0: _ms(6000)}

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


async def test_vvv_report_uses_4arg_timed_source(tmp_path: Path) -> None:
    """End-to-end: a 4-arg ``TimedVenueQuote`` source drives BOTH the agent and the post-build edge.

    Before the migration this fails (the agent errors on every 4-arg call, fires nothing → the edge is
    ``None``); after it, venue decimal 5.0 fires every fixture-5 side and the estimated edge attaches.
    """
    from veridex.backtest.vvv_report import vvv_report_with_estimated_edge

    session_dir = _write_session(tmp_path)
    pack_dir = tmp_path / "pack"
    pack_from_session(session_dir, pack_dir)

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


# ``_ms`` builds a real one-side MarketState (fixture_id=5, mk="1X2|home", side="home"); reuse it here.
from tests.test_drift_agent import _ms  # noqa: E402  (after module docstring/imports by intent)
