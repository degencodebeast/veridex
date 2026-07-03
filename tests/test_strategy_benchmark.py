"""M2 (SX) — competitor-strategy replication benchmark: Tasks 7-8b."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.test_replay_pack import _write_session
from veridex.backtest.benchmark import (
    StrategyBenchmarkResult,
    benchmark_on_pack,
    extract_prob_series,
    run_strategy_benchmark,
    translate_sharpline,
    translate_threshold,
)
from veridex.ingest.replay_pack import load_pack_marketstates, pack_from_session


def test_benchmark_result_rung_must_be_txline_only():
    r = StrategyBenchmarkResult(
        benchmark_id="b1",
        source_strategy="sharpline",
        veridex_config_hash="0" * 64,
        pack_content_hash="1" * 64,
        evidence_rung="txline-only",
        fire_count=3,
        scored_count=2,
        avg_clv_bps=12.0,
        abstain_count=1,
        provenance="txline-only",
    )
    assert r.evidence_rung == "txline-only"


def test_benchmark_result_rejects_venue_edge_rung():
    with pytest.raises(ValueError):
        StrategyBenchmarkResult(
            benchmark_id="b2",
            source_strategy="sharpline",
            veridex_config_hash="0" * 64,
            pack_content_hash="1" * 64,
            evidence_rung="live-fill-receipt",  # forbidden for competitors
            fire_count=1,
            scored_count=1,
            avg_clv_bps=1.0,
            abstain_count=0,
            provenance="live-fill-receipt",
        )


def test_sharpline_benchmark_is_scored_through_injected_veridex_scoring_only():
    cfg = translate_sharpline(
        {"zGate": 1.5, "phThresh": 0.25, "lambda": 0.92, "cooldown": 3, "warmup": 10}
    )
    assert cfg.source_repo == "sharpline" and cfg.strategy in {"momentum-sharp", "cumulative-drift"}
    calls = {"n": 0}
    seen = {}

    def fake_score_fn(fires):  # the ONLY source of scored numbers
        calls["n"] += 1
        seen["fires"] = fires
        return {"scored_count": len(fires), "avg_clv_bps": 0.0}

    class _Pack:  # minimal deterministic pack stub — a translator-seam/scoring test, not a firing
        # test (warmup=10 exceeds this series' length, so fires is always empty here by design)
        content_hash = "2" * 64
        ticks = [0.50, 0.52, 0.58, 0.70]

    res = asyncio.run(run_strategy_benchmark(cfg, pack=_Pack(), score_fn=fake_score_fn))
    assert calls["n"] == 1  # scoring came from Veridex seam, not competitor code
    assert res.evidence_rung == "txline-only"
    assert res.pack_content_hash == "2" * 64
    # AC-003: the result's scored fields are score_fn's return, copied verbatim, not recomputed.
    assert res.scored_count == len(seen["fires"])
    assert res.avg_clv_bps == 0.0


def _real_pack(tmp_path: Path) -> Path:
    session_dir = _write_session(tmp_path)  # records.jsonl + meta.json (fixture 5, 1X2)
    pack_dir = tmp_path / "pack"
    pack_from_session(session_dir, pack_dir)  # builds pack.json + odds file + real content_hash
    return pack_dir


def _first_market_side(marketstates):
    ms0 = next(m for m in marketstates if m.markets)  # first tick with a non-suspended market
    market_key = next(iter(ms0.markets))
    side = next(iter(ms0.markets[market_key]["stable_prob_bps"]))
    return market_key, side


def test_benchmark_runs_on_a_real_loaded_pack_and_scores_via_veridex_seam(tmp_path):
    pack_dir = _real_pack(tmp_path)
    ms = load_pack_marketstates(pack_dir, fixture_id=5, verify=True)  # real loader, hash verified
    assert ms and hasattr(ms[0], "markets")
    market_key, side = _first_market_side(ms)  # derived from real data, not hardcoded
    series = extract_prob_series(ms, market_key=market_key, side=side)
    assert isinstance(series, list) and all(isinstance(x, float) for x in series)
    cfg = translate_sharpline(
        {"zGate": 1.5, "phThresh": 0.25, "lambda": 0.92, "cooldown": 3, "warmup": 10}
    )
    calls = {"n": 0}
    seen = {}

    def fake_score_fn(fires):
        calls["n"] += 1
        seen["fires"] = fires
        return {"scored_count": len(fires), "avg_clv_bps": 0.0}

    res = asyncio.run(
        benchmark_on_pack(
            cfg,
            pack_dir=pack_dir,
            fixture_id=5,
            market_key=market_key,
            side=side,
            score_fn=fake_score_fn,
        )
    )
    assert calls["n"] == 1  # scored ONLY through the injected Veridex seam
    assert res.evidence_rung == "txline-only"  # competitors are rung-1
    assert len(res.pack_content_hash) == 64  # from the REAL loaded pack, not a stub
    # AC-003: the result's scored fields are score_fn's return, copied verbatim, not recomputed.
    assert res.scored_count == len(seen["fires"])
    assert res.avg_clv_bps == 0.0


def test_translate_threshold_maps_sports_workbench_params_into_a_veridex_config():
    cfg = translate_threshold({"moveThreshold": 2.5, "cooldown": 4})
    assert cfg.source_repo == "sports-workbench"
    assert cfg.source_strategy == "sports-workbench"
    assert cfg.strategy == "threshold-move"
    # translated_params carries Veridex-side names, not the competitor's own keys.
    assert cfg.translated_params == {"move_threshold_pct": 2.5, "cooldown_ticks": 4.0}
    assert "sports-workbench" in cfg.notes and "no sports-workbench code imported" in cfg.notes


def test_translate_threshold_defaults_cooldown_when_absent():
    cfg = translate_threshold({"moveThreshold": 1.0})
    assert cfg.translated_params["cooldown_ticks"] == 0.0


def test_detector_fires_on_a_flat_then_jump_sharp_move():
    # flat reference then a single-tick reprice — the canonical sharp move; must FIRE
    cfg = translate_sharpline(
        {"zGate": 1.5, "phThresh": 0.25, "lambda": 0.92, "cooldown": 1, "warmup": 3}
    )

    class _Pack:
        content_hash = "3" * 64
        ticks = [0.50, 0.50, 0.50, 0.50, 0.70, 0.70]  # flat, then jump

    seen = {}

    def score_fn(fires):
        seen["fires"] = fires
        return {"scored_count": len(fires), "avg_clv_bps": 0.0}

    res = asyncio.run(run_strategy_benchmark(cfg, pack=_Pack(), score_fn=score_fn))
    assert res.fire_count > 0  # F1: the detector's firing substance is exercised
    assert seen["fires"]  # F3: flat-then-jump actually fires
