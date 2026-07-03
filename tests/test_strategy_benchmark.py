"""M2 (SX) — competitor-strategy replication benchmark: Tasks 7-8b."""

from __future__ import annotations

import asyncio

import pytest

from veridex.backtest.benchmark import (
    CompetitorReplicationConfig,
    StrategyBenchmarkResult,
    run_strategy_benchmark,
    translate_sharpline,
)


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

    def fake_score_fn(fires):  # the ONLY source of scored numbers
        calls["n"] += 1
        return {"scored_count": len(fires), "avg_clv_bps": 0.0}

    class _Pack:  # minimal deterministic pack stub
        content_hash = "2" * 64
        ticks = [0.50, 0.52, 0.58, 0.70]  # a sharp move

    res = asyncio.run(run_strategy_benchmark(cfg, pack=_Pack(), score_fn=fake_score_fn))
    assert calls["n"] == 1  # scoring came from Veridex seam, not competitor code
    assert res.evidence_rung == "txline-only"
    assert res.pack_content_hash == "2" * 64
