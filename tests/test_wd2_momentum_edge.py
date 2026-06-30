"""WD-2 — the momentum agent demonstrably earns positive CLV and beats the baseline.

Strategy Alpha Doctrine: TxLINE's de-margined consensus fair prob (`prob_bps`) drifts; the
momentum reader acts ahead of the drift; the LAW recomputes the edge from sealed evidence; and
CLV later proves the decision quality. The edge is EV captured from consensus drift — never
de-vigging (TxLINE already de-margins).
"""

from __future__ import annotations

from pathlib import Path

from veridex.ingest.marketstate import replay_marketstates
from veridex.runtime.competition import run_demo_competition
from veridex.runtime.orchestrator import deterministic_agent
from veridex.scoring import score_run
from veridex.strategies.momentum import momentum_agent

FIXTURE = str(Path(__file__).parent / "fixtures" / "wd2_momentum_replay.json")


async def test_momentum_beats_baseline_on_clv() -> None:
    ticks = replay_marketstates(FIXTURE)
    agents = [momentum_agent("mom", min_momentum_bps=50), deterministic_agent("base")]
    result = await run_demo_competition(ticks, agents, source_mode="replay", anchor_fn=None)

    by_id = {row["agent_id"]: row for row in score_run(result.run)}
    mom_avg = by_id["mom"]["avg_clv_bps"]
    base_avg = by_id["base"]["avg_clv_bps"]

    # The real edge: momentum is CLV-positive and strictly beats the favorite-flagging baseline.
    assert mom_avg is not None and mom_avg > 0
    assert base_avg is None or mom_avg > base_avg
    # A complete, anchorable proof card was produced for the demo.
    assert "evidence" in result.proof_card and "checks" in result.proof_card
    assert result.proof_card["evidence"]["evidence_hash"]


async def test_momentum_outranks_baseline() -> None:
    ticks = replay_marketstates(FIXTURE)
    agents = [momentum_agent("mom", min_momentum_bps=50), deterministic_agent("base")]
    result = await run_demo_competition(ticks, agents, source_mode="replay", anchor_fn=None)
    ranked = {row["agent_id"]: row["rank"] for row in score_run(result.run)}
    assert ranked["mom"] == 1
