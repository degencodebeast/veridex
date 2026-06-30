"""WD-3 — the decoupled standalone-run core (one agent, no competition container)."""

from __future__ import annotations

from pathlib import Path

from veridex.ingest.marketstate import replay_marketstates
from veridex.strategies.momentum import momentum_agent
from veridex_agent.run import StandaloneRunResult, standalone_run

FIXTURE = str(Path(__file__).parent / "fixtures" / "wd2_momentum_replay.json")


async def test_standalone_run_produces_a_verified_proof() -> None:
    ticks = replay_marketstates(FIXTURE)
    result = await standalone_run(ticks, momentum_agent("mom"), source_mode="replay", anchor_fn=None)
    assert isinstance(result, StandaloneRunResult)
    assert result.source_mode == "replay"
    assert result.anchor_status == "not_anchored"  # anchor_fn=None → offline
    assert result.verified is True
    assert result.verify_report["evidence_match"] is True
    assert "checks" in result.proof_card
    assert len(result.scores) == 1  # exactly one agent — no competition/ranking framing


async def test_standalone_run_anchors_when_anchor_fn_supplied() -> None:
    ticks = replay_marketstates(FIXTURE)

    async def fake_anchor(manifest_hash: str) -> str:
        assert len(manifest_hash) == 64
        return "FAKESIG"

    result = await standalone_run(ticks, momentum_agent("mom"), source_mode="replay", anchor_fn=fake_anchor)
    assert result.anchor_status == "anchored"
    assert result.signature == "FAKESIG"
    assert result.proof_card["anchor"]["signature"] == "FAKESIG"
