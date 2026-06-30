"""WD-7 — score_run rows surface valid_count (CLV sample-size source)."""

from __future__ import annotations

from veridex.api.demo_fixtures import build_demo_ticks, contrarian_agent
from veridex.runtime.competition import run_demo_competition
from veridex.runtime.orchestrator import deterministic_agent


async def test_score_rows_carry_valid_count() -> None:
    ticks = build_demo_ticks()
    agents = [deterministic_agent("agent-alpha"), contrarian_agent("agent-beta")]
    result = await run_demo_competition(ticks, agents, source_mode="replay", anchor_fn=None)
    for row in result.scores:
        assert "valid_count" in row
        assert isinstance(row["valid_count"], int)
        assert row["valid_count"] >= 0
        # valid_count is law-acceptance; never less than the scored action_count.
        assert row["valid_count"] >= row["action_count"]
