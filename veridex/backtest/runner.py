"""T15 — run_backtest: replay a ReplayPack through the SAME incremental core the live loop uses.

This is product infrastructure, NOT a generic eval engine: there is NO external backtest library
(GUD-2D-301). ``CompetitionRun`` IS the runner — a backtest is just a replay-sourced run driven
through ``feed()``/``finalize()``, scored, and projected into a :class:`BacktestReport`.

Determinism (AC-2D-301): the ``run_id`` is pinned from the pack's ``content_hash`` + the window id,
so the whole sealed ``RunResult`` — and therefore the report (minus its wall-clock ``generated_ts``)
— is byte-identical across repeat runs on the same pack. No network, no LLM on this path.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from veridex.backtest.report import (
    BACKTEST_EXECUTION_MODE,
    BacktestReport,
    build_backtest_report,
)
from veridex.ingest.replay_pack import load_pack_marketstates
from veridex.policy.envelope import PolicyEnvelope
from veridex.runtime.orchestrator import Agent, CompetitionRun, RunResult
from veridex.runtime.window import RunWindow

#: A backtest is always replay-sourced (its close is the pack's reconstructed CON-040 close).
_SOURCE_MODE = "replay"


def _pack_content_hash(pack_dir: Path) -> str:
    """Read the pack's stored ``content_hash`` from its manifest (bound into the report)."""
    return str(json.loads((pack_dir / "pack.json").read_text())["content_hash"])


async def run_backtest(
    pack_dir: Path,
    fixture_id: int,
    agents: list[Agent],
    *,
    window: RunWindow,
    policy_envelope: PolicyEnvelope | None = None,
    replay_speed: float = 0.0,
) -> tuple[RunResult, BacktestReport]:
    """Replay one fixture of a ReplayPack through ``CompetitionRun`` and score it into a report.

    The pack is loaded through the SAME normalizer the live loop uses (``verify=True`` refuses a
    tampered pack). For a ``pre_match`` window the LAST tick is fed via :meth:`feed_closing` — the
    agents are scored AGAINST that reconstructed close, they never decide on it. All other windows
    close on the in-play line at window end, so every tick is a decision tick.

    Args:
        pack_dir: Directory of the self-describing ReplayPack (must contain ``pack.json``).
        fixture_id: The fixture within the pack to replay.
        agents: Participating agents (deterministic/offline — no network on this path).
        window: The coverage window (its ``end_rule`` selects the closing behaviour).
        policy_envelope: Optional operator envelope, folded into the report's ``config_hash``.
        replay_speed: Pacing hint in seconds between fed ticks (``0.0`` = as fast as possible).
            Pacing NEVER enters the sealed result — it only affects wall-clock cadence.

    Returns:
        ``(run_result, backtest_report)`` — the sealed run and its honest, derived-only report.
    """
    marketstates = load_pack_marketstates(pack_dir, fixture_id, verify=True)
    content_hash = _pack_content_hash(pack_dir)

    # Deterministic run id: same pack + same window → same sealed run (AC-2D-301).
    run_id = f"bt_{content_hash[:12]}_{window.window_id}"
    run = CompetitionRun(agents, source_mode=_SOURCE_MODE, run_id=run_id)

    # pre_match: the final tick is the reconstructed close — fed as a closing tick (no decision on it).
    split_closing = window.end_rule == "pre_match" and len(marketstates) >= 2
    decision_states = marketstates[:-1] if split_closing else marketstates

    for state in decision_states:
        await run.feed(state)
        if replay_speed > 0:
            await asyncio.sleep(replay_speed)
    if split_closing:
        await run.feed_closing(marketstates[-1])

    result = await run.finalize(window=window)

    report = build_backtest_report(
        result,
        window=window,
        pack_id=pack_dir.name,
        content_hash=content_hash,
        source_mode=_SOURCE_MODE,
        execution_mode=BACKTEST_EXECUTION_MODE,
        policy_envelope=policy_envelope,
        generated_ts=int(time.time()),
    )
    return result, report
