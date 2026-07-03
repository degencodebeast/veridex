"""M2 (SX) — competitor-strategy replication benchmark: Tasks 7-8b."""

from __future__ import annotations

import pytest

from veridex.backtest.benchmark import CompetitorReplicationConfig, StrategyBenchmarkResult


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
