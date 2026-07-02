"""T15 — run_backtest: replay a ReplayPack through the SAME incremental core the live loop uses,
score it, and emit an honest BacktestReport (REQ-2D-302/303/304, AC-2D-301/302).

No external backtest engine (GUD-2D-301): ``CompetitionRun`` IS the runner. No network, no LLM —
the whole lane is deterministic and offline. Packs are built with the Task-3 recorder helpers.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from veridex.api.router import create_app
from veridex.backtest.report import BacktestReport
from veridex.backtest.runner import run_backtest
from veridex.ingest.recorder import SessionMeta, envelope_line
from veridex.ingest.replay_pack import pack_from_session
from veridex.runtime.orchestrator import deterministic_agent
from veridex.runtime.window import RunWindow
from veridex.scoring import score_run
from veridex.store import InMemoryStore

_FIXTURE_ID = 555


def _ou_record(ts_ms: int, under_pct: float) -> dict:
    """One raw native TxLINE OU record where 'Under' carries a scoreable (>=50%) prob."""
    return {
        "FixtureId": _FIXTURE_ID,
        "Ts": ts_ms,
        "InRunning": False,
        "SuperOddsType": "OU",
        "MarketPeriod": None,
        "MarketParameters": "line=2.5",
        "PriceNames": ["Over", "Under"],
        "Prices": [1900, 1900],
        "Pct": [round(100.0 - under_pct, 1), round(under_pct, 1)],
    }


def _build_pack(tmp_path: Path, n_ticks: int) -> Path:
    """Build a self-describing, hashed ReplayPack with ``n_ticks`` ticks for one fixture.

    'Under' drifts up 0.5pp/tick so pre-close decisions earn a real (non-zero) CLV against the
    reconstructed close.
    """
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    lines = [envelope_line(_ou_record(100_000 + i * 10_000, 60.0 + i * 0.5), 100 + i * 10) for i in range(n_ticks)]
    (session_dir / "records.jsonl").write_text("\n".join(lines) + "\n")
    (session_dir / "meta.json").write_text(
        SessionMeta(started_ts=99, endpoints=["/odds/stream"], tool_version="t").model_dump_json()
    )
    out_dir = tmp_path / "pack"
    pack_from_session(session_dir, out_dir)
    return out_dir


def _window(n_horizon: int = 0) -> RunWindow:
    return RunWindow(
        window_id="w_bt",
        fixture_id=_FIXTURE_ID,
        market_allowlist=["OU"],
        end_rule="pre_match",
        min_clv_horizon_s=n_horizon,
    )


async def test_run_backtest_populates_all_report_fields(tmp_path: Path) -> None:
    pack_dir = _build_pack(tmp_path, n_ticks=6)

    result, report = await run_backtest(
        pack_dir, _FIXTURE_ID, [deterministic_agent("baseline")], window=_window()
    )

    assert isinstance(report, BacktestReport)
    # Every §4.4 field present (or explicit null-with-warning).
    for field in (
        "window_id",
        "config_hash",
        "market_universe",
        "sample_size",
        "valid_count",
        "clv_confidence",
        "avg_clv",
        "clv_distribution",
        "sim_pnl",
        "threshold_sensitivity",
        "stale_rejected_quote_rate",
        "policy_pass_fail_rate",
        "low_sample_warning",
        "assumptions",
        "pack_id",
        "content_hash",
        "mode_label",
    ):
        assert hasattr(report, field)

    assert report.mode_label == "Backtest"
    assert report.source_mode == "replay"
    assert report.market_universe == ["OU"]
    # The run actually scored something (the pack is well-formed).
    assert report.sample_size > 0
    assert report.avg_clv is not None


async def test_small_sample_is_low_confidence_with_warning(tmp_path: Path) -> None:
    """AC-2D-302: valid_count <= 9 → low tier + low_sample_warning; ranking/means UNCHANGED."""
    pack_dir = _build_pack(tmp_path, n_ticks=6)  # 5 pre-close decisions → valid_count == 5

    result, report = await run_backtest(
        pack_dir, _FIXTURE_ID, [deterministic_agent("baseline")], window=_window()
    )

    assert report.valid_count <= 9
    assert report.clv_confidence == "low"
    assert report.low_sample_warning is not None
    # The warning is ADDITIVE — the scored metric stack is byte-identical to score_run's.
    assert report.leaderboard == score_run(result)


async def test_larger_sample_lifts_confidence_tier(tmp_path: Path) -> None:
    """A higher valid_count yields a higher tier than a tiny sample (AC-2D-302)."""
    pack_dir = _build_pack(tmp_path, n_ticks=13)  # 12 pre-close decisions → valid_count == 12

    _, report = await run_backtest(pack_dir, _FIXTURE_ID, [deterministic_agent("baseline")], window=_window())

    assert report.valid_count > 9
    assert report.clv_confidence in {"medium", "high"}
    assert report.low_sample_warning is None


async def test_deterministic_two_runs_identical_minus_wallclock(tmp_path: Path) -> None:
    """AC-2D-301: two run_backtest calls on the same pack → identical report minus wall-clock."""
    pack_dir = _build_pack(tmp_path, n_ticks=8)

    _, report_a = await run_backtest(pack_dir, _FIXTURE_ID, [deterministic_agent("baseline")], window=_window())
    _, report_b = await run_backtest(pack_dir, _FIXTURE_ID, [deterministic_agent("baseline")], window=_window())

    a = report_a.model_dump()
    b = report_b.model_dump()
    a.pop("generated_ts")
    b.pop("generated_ts")
    assert a == b


async def test_pack_id_and_content_hash_are_bound(tmp_path: Path) -> None:
    """Tamper-evident lineage: the report carries the pack's pack_id + content_hash."""
    pack_dir = _build_pack(tmp_path, n_ticks=6)
    stored_hash = json.loads((pack_dir / "pack.json").read_text())["content_hash"]

    _, report = await run_backtest(pack_dir, _FIXTURE_ID, [deterministic_agent("baseline")], window=_window())

    assert report.content_hash == stored_hash
    assert report.pack_id == pack_dir.name
    assert report.pack_id


async def test_mode_label_honest_no_real_executable_edge(tmp_path: Path) -> None:
    pack_dir = _build_pack(tmp_path, n_ticks=6)

    _, report = await run_backtest(pack_dir, _FIXTURE_ID, [deterministic_agent("baseline")], window=_window())

    assert report.mode_label == "Backtest"
    assert "Live" not in report.mode_label
    assert report.real_executable_edge_bps is None


async def test_assumptions_block_is_explicit(tmp_path: Path) -> None:
    pack_dir = _build_pack(tmp_path, n_ticks=6)

    _, report = await run_backtest(pack_dir, _FIXTURE_ID, [deterministic_agent("baseline")], window=_window())

    assert report.assumptions.slippage_bps == 0
    assert report.assumptions.costs_bps == 0
    assert report.assumptions.quote_freshness_s is None
    assert report.assumptions.execution_mode == "paper"


def test_api_backtest_run_and_fetch(tmp_path: Path) -> None:
    """POST /backtests triggers a run + returns a ref; GET returns the labeled BacktestReport."""
    pack_dir = _build_pack(tmp_path, n_ticks=6)
    client = TestClient(create_app(store=InMemoryStore()))

    resp = client.post(
        "/backtests",
        json={
            "pack_dir": str(pack_dir),
            "fixture_id": _FIXTURE_ID,
            "window_id": "w_api",
            "market_allowlist": ["OU"],
            "end_rule": "pre_match",
            "min_clv_horizon_s": 0,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "backtest_id" in body
    assert body["mode_label"] == "Backtest"

    fetched = client.get(f"/backtests/{body['backtest_id']}")
    assert fetched.status_code == 200, fetched.text
    report = fetched.json()
    assert report["mode_label"] == "Backtest"
    assert report["window_id"] == "w_api"
    assert report["real_executable_edge_bps"] is None


def test_api_backtest_unknown_id_is_404(tmp_path: Path) -> None:
    client = TestClient(create_app(store=InMemoryStore()))
    assert client.get("/backtests/nope").status_code == 404
