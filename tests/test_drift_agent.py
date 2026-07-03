"""M4 (S3) — CumulativeDriftAgent: real Agent that abstains on thin data (Tasks 11/12/12b).

The drift agent follows a SMOOTH, SUSTAINED multi-tick repricing (a cumulative logit drift
confirmed by a directional EWMA-slope trend), gated behind a minimum tick count and observation
horizon so it stays quiet on thin data. It is a REAL reproducible-proof
:class:`~veridex.runtime.orchestrator.Agent` — the exact seam ``run_backtest``/the orchestrator
require — NOT a bespoke callable. PROPOSER ONLY (gate 1): it emits ``FOLLOW_MOMENTUM``; the
deterministic law scores edge/CLV. Any ``reason``/``claimed_edge_bps`` is untrusted UX metadata.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from tests.test_replay_pack import _write_session
from veridex.backtest.runner import run_backtest
from veridex.deploy.preflight import DeployConfig, run_deploy_preflight
from veridex.ingest.marketstate import MarketState
from veridex.ingest.replay_pack import pack_from_session
from veridex.runtime.schemas import AgentAction
from veridex.runtime.window import RunWindow
from veridex.strategies.drift import cumulative_drift_agent


def _ms(prob_bps: int, *, mk: str = "1X2|home", side: str = "home", tick_seq: int = 0) -> MarketState:
    """A REAL MarketState carrying one non-suspended side at ``prob_bps`` (ts advances 60s/tick).

    Mirrors ``tests/test_momentum_v2.py::_state`` (a real ``MarketState`` fed to a real Agent), but
    advances ``ts`` by 60s per tick so an observation horizon can accrue past ``min_horizon_s``.
    """
    return MarketState(
        fixture_id=5,
        tick_seq=tick_seq,
        ts=1000 + tick_seq * 60,
        phase=2,
        markets={
            mk: {
                "stable_prob_bps": {side: prob_bps},
                "stable_price": {side: 2.0},
                "suspended": False,
            }
        },
        scores={},
    )


def _run(agent, series: list[int], *, mk: str = "1X2|home", side: str = "home") -> list[AgentAction]:
    """Feed ``series`` through the agent's REAL async ``decide`` seam, one MarketState per tick."""

    async def go() -> list[AgentAction]:
        return [await agent.decide(_ms(b, mk=mk, side=side, tick_seq=i)) for i, b in enumerate(series)]

    return asyncio.run(go())


def _typed(actions: list[AgentAction]) -> list[tuple[str, str | None]]:
    return [(a.type.value, a.params.get("side")) for a in actions]


# ------------------------------------------------------------------------------------------
# Task 11 — abstains on thin data, fires on sustained smooth drift, config_hash is real
# ------------------------------------------------------------------------------------------


def test_abstains_when_insufficient_ticks() -> None:
    # Only 3 ticks (< default min_tick_count=20): no matter how sharp, the agent abstains.
    actions = _run(cumulative_drift_agent(), [3000, 5000, 7000])
    assert all(a.type == "WAIT" for a in actions)


def test_fires_on_sustained_smooth_drift_with_enough_ticks() -> None:
    # A smooth monotone rise over >= min_tick_count ticks, spanning > min_horizon_s: must fire.
    series = [3000 + i * 160 for i in range(25)]  # 3000 -> 6840 bps, strictly increasing
    actions = _run(cumulative_drift_agent(), series)
    assert any(a.type != "WAIT" for a in actions), "sustained smooth drift past the gates must fire"


def test_config_hash_is_stable_and_param_sensitive() -> None:
    snapshot = MarketState(fixture_id=5, tick_seq=0, ts=0, phase=2, markets={}, scores={})
    a = cumulative_drift_agent()
    b = cumulative_drift_agent(cum_drift_logit_min=0.99)  # a different behavioural param
    assert a.proof_mode == "reproducible"
    assert a.config_hash is not None and b.config_hash is not None
    # CALLABLE config_hash (orchestrator finalize calls config_hash(market_state)):
    assert a.config_hash(snapshot) == a.config_hash(snapshot)  # stable across calls
    assert a.config_hash(snapshot) != b.config_hash(snapshot)  # param-sensitive


def test_cumulative_drift_strategy_is_accepted_by_preflight() -> None:
    # Step 6: the drift agent is a first-class deploy strategy. The momentum-sharp cross-field
    # (lookback < min_movements) does NOT apply to it, so a config with lookback < min_movements
    # still passes the named ``config`` check (the sharp branch stays scoped to "momentum-sharp").
    config = DeployConfig(
        template_id="cumulative-drift",
        agent_id="studio-drift",
        strategy="cumulative-drift",
        source_mode="replay",
        lookback=4,
        min_movements=8,
    )
    checks = run_deploy_preflight(
        config, feed_report=None, market_resolved=None, envelope=config.to_policy_envelope()
    )
    cfg = next(c for c in checks if c.name == "config")
    assert cfg.ok is True


# ------------------------------------------------------------------------------------------
# Task 12 — prefix-invariance / no-lookahead (trust test: each agent owns its rolling state)
# ------------------------------------------------------------------------------------------


def test_decision_at_t_is_prefix_invariant() -> None:
    # Discriminating params: firing happens INSIDE a 4-tick prefix, so a global/cross-agent-shared
    # detector would leak one agent's history into the next and change the decision (proven RED).
    kw = {"min_tick_count": 3, "min_horizon_s": 0, "cum_drift_logit_min": 0.05, "cooldown_ticks": 0}
    prefix = [3000, 4000, 5000, 6000]

    a = _typed(_run(cumulative_drift_agent(**kw), prefix))
    b = _typed(_run(cumulative_drift_agent(**kw), prefix))  # a SECOND, independent fresh agent
    assert a == b  # two fresh agents on the same prefix decide identically (no shared/global state)
    assert any(t != "WAIT" for t, _ in a)  # ...and the prefix actually fires (the test discriminates)

    # A separate agent's LONGER future must not perturb the prefix-4 decisions (causal, no lookahead).
    longer = prefix + [5000, 4000, 3000, 2000]  # continues, then reverses
    full = _typed(_run(cumulative_drift_agent(**kw), longer))
    assert full[: len(prefix)] == a


# ------------------------------------------------------------------------------------------
# Task 12b — drift COMPOSES with run_backtest: a real hashed pack → a real BacktestReport (S3)
# ------------------------------------------------------------------------------------------


def _real_pack(tmp_path: Path) -> Path:
    session_dir = _write_session(tmp_path)  # records.jsonl + meta.json (fixture 5, 1X2)
    pack_dir = tmp_path / "pack"
    pack_from_session(session_dir, pack_dir)  # builds pack.json + odds file + real content_hash
    return pack_dir


def _window() -> RunWindow:
    return RunWindow(
        window_id="w_drift_bt",
        fixture_id=5,
        market_allowlist=["1X2"],
        end_rule="pre_match",
        min_clv_horizon_s=0,
    )


async def test_drift_produces_a_real_backtest_report_via_run_backtest(tmp_path: Path) -> None:
    # THE anti-false-green proof: run_backtest awaits agent.decide and calls agent.config_hash(ms)
    # during finalize. If drift were not a REAL Agent (bad await / bare-string config_hash) this
    # raises inside run_backtest. A returned BacktestReport therefore PROVES drift is a valid Agent.
    pack_dir = _real_pack(tmp_path)
    drift = cumulative_drift_agent()

    result, report = await run_backtest(pack_dir, 5, [drift], window=_window())

    assert report.run_id == result.run_id  # the sealed run and its report are the same run
    assert report.real_executable_edge_bps is None  # rung-3 stays honest null (no fabricated edge)
    assert hasattr(report, "avg_clv")
    assert hasattr(report, "clv_distribution")
