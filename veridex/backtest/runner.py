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
from collections.abc import Collection
from pathlib import Path

from veridex.backtest.market_filter import filter_marketstates_to_allowlist
from veridex.backtest.pre_match import plan_pre_match_backtest
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
    market_key_allowlist: Collection[str] | None = None,
) -> tuple[RunResult, BacktestReport]:
    """Replay one fixture of a ReplayPack through ``CompetitionRun`` and score it into a report.

    The pack is loaded through the SAME normalizer the live loop uses (``verify=True`` refuses a
    tampered pack). For a ``pre_match`` window (D2) decisions STOP at kickoff (the first in-running
    tick) and are scored AGAINST the per-market CON-040 close — the last pre-kickoff line PER market,
    folded (via :func:`~veridex.backtest.pre_match.plan_pre_match_backtest`) into ONE
    :meth:`feed_closing` snapshot covering every scored market, so no market silently falls back to its
    entry tick. On a full-match pack this means in-running ticks are NEVER decided on and the full-time
    line is NEVER the close. Honest degrades (all-in-running / incomplete close) finalize on WINDOW CLV
    (never true ``clv_bps``) with a named reason on ``report.closing_note``; a pre-match-only pack (no
    kickoff observed) still scores true CLV but carries a "no verified kickoff" marker. All other
    windows close on the in-play line at window end, so every tick is a decision tick.

    Args:
        pack_dir: Directory of the self-describing ReplayPack (must contain ``pack.json``).
        fixture_id: The fixture within the pack to replay.
        agents: Participating agents (deterministic/offline — no network on this path).
        window: The coverage window (its ``end_rule`` selects the closing behaviour).
        policy_envelope: Optional operator envelope, folded into the report's ``config_hash``.
        replay_speed: Pacing hint in seconds between fed ticks (``0.0`` = as fast as possible).
            Pacing NEVER enters the sealed result — it only affects wall-clock cadence.
        market_key_allowlist: FU-2 eligibility gate. When provided, each loaded tick is narrowed to
            these EXACT full market_keys BEFORE the D2 plan/feed, so only eligible markets enter the
            scored universe (the SAME allowlist is passed for drift and baselines, keeping the
            comparison apples-to-apples). ``None`` (default) filters NOTHING — byte-identical to the
            pre-FU-2 path. This changes WHICH markets are fed/scored, never HOW CLV is computed.

    Returns:
        ``(run_result, backtest_report)`` — the sealed run and its honest, derived-only report.
    """
    marketstates = load_pack_marketstates(pack_dir, fixture_id, verify=True)
    if market_key_allowlist is not None:
        marketstates = filter_marketstates_to_allowlist(marketstates, market_key_allowlist)
    content_hash = _pack_content_hash(pack_dir)

    # Deterministic run id: same pack + same window → same sealed run (AC-2D-301).
    run_id = f"bt_{content_hash[:12]}_{window.window_id}"
    run = CompetitionRun(agents, source_mode=_SOURCE_MODE, run_id=run_id)

    # pre_match (D2): decisions STOP at kickoff (first in-running tick) and are scored against the
    # per-market CON-040 close (last pre-kickoff line per market, folded to cover EVERY scored market —
    # never the full-time line, never a single-market close that silently zeroes the rest). All other
    # end rules close on the in-play line at window end, so every tick is a decision tick.
    closing_note: str | None = None
    effective_window = window
    if window.end_rule == "pre_match":
        plan = plan_pre_match_backtest(marketstates)
        closing_note = plan.closing_note
        decision_states = plan.decision_states
        closing_state = plan.closing_state
        if plan.degraded:
            # Fail closed: no true pre-match close could be produced (all-in-running / incomplete). Finalize
            # on WINDOW CLV so no row overclaims a close it lacks; the named reason rides on closing_note.
            effective_window = window.model_copy(update={"end_rule": "manual_stop", "duration_s": None})
    else:
        decision_states = marketstates
        closing_state = None

    for state in decision_states:
        await run.feed(state)
        if replay_speed > 0:
            await asyncio.sleep(replay_speed)
    if closing_state is not None:
        await run.feed_closing(closing_state)

    result = await run.finalize(window=effective_window)

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
    # The close-provenance marker is a derived, NON-sealed annotation (report analog of the live runner's
    # ops markers): attach it AFTER the pure venue-free build so the builder stays close-provenance-blind.
    return result, report.model_copy(update={"closing_note": closing_note})
