"""WD-1 — the harness manifest and the verifier-reconstructed manifest must agree."""

from __future__ import annotations

from veridex.api.demo_fixtures import build_demo_ticks, contrarian_agent
from veridex.runtime.competition import run_demo_competition
from veridex.runtime.orchestrator import deterministic_agent
from veridex.verifier.recompute import verify_run


async def test_harness_manifest_equals_verifier_manifest() -> None:
    ticks = build_demo_ticks()
    agents = [deterministic_agent("agent-alpha"), contrarian_agent("agent-beta")]
    result = await run_demo_competition(ticks, agents, source_mode="replay", anchor_fn=None)

    report = verify_run(result.run)
    assert report.manifest == result.manifest
    assert report.manifest_hash == result.manifest_hash
