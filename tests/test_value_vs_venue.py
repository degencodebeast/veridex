"""M6 (S5) Task 18/18b — ValueVsVenue: a real Agent that prices fair vs an INJECTED venue quote.

The agent compares the TxLINE de-margined fair probability against a venue decimal price supplied
ONLY through an injected ``venue_price_source`` — never read from the (evidence-sealed) market
state. Its ``vvv_signal`` core is pure: no quote ⇒ no edge. And — THE trust test — the emitted
``AgentAction.params`` carry ONLY TxLINE-derived fields (market_key/side/reason/confidence), never
a venue-derived value, because ``AgentAction.model_dump()`` is sealed into the ``evidence_hash``
(SEC-003 / INVARIANT 4: venue data enters via the injected source, never via the action).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from tests.test_drift_agent import _ms
from tests.test_replay_pack import _write_session
from veridex.ingest.replay_pack import pack_from_session
from veridex.runtime.window import RunWindow
from veridex.venues.polymarket import native_to_decimal

_FIXTURE_ID = 5


def _window() -> RunWindow:
    return RunWindow(
        window_id="w_vvv_report",
        fixture_id=_FIXTURE_ID,
        market_allowlist=["1X2"],
        end_rule="pre_match",
        min_clv_horizon_s=0,
    )


def test_no_executable_edge_without_a_quote() -> None:
    """No venue quote ⇒ both edge fields are None and the signal does not fire (fail-safe)."""
    from veridex.strategies.value_vs_venue import vvv_signal

    signal = vvv_signal(6000, None)

    assert signal["gap_bps"] is None
    assert signal["estimated_executable_edge_bps"] is None
    assert signal["fired"] is False


def test_edge_uses_decimal_price_not_native_q() -> None:
    """Edge math consumes the DECIMAL price (via native_to_decimal), never the raw native q."""
    from veridex.strategies.value_vs_venue import vvv_signal

    # native q=0.50 -> decimal 2.0; fair 6000 bps (0.60): edge = 0.60*2.0 - 1 = +0.20 -> +2000 bps.
    signal = vvv_signal(6000, native_to_decimal(0.50))

    assert signal["gap_bps"] is not None
    assert signal["estimated_executable_edge_bps"] is not None
    assert signal["estimated_executable_edge_bps"] > 0
    assert signal["fired"] is True


def test_agent_is_a_real_veridex_agent_with_injected_venue_source() -> None:
    """The factory returns a REAL reproducible-proof Agent with a callable decide + config_hash."""
    from veridex.strategies.value_vs_venue import value_vs_venue_agent

    agent = value_vs_venue_agent(venue_price_source=lambda mk: 2.0)
    snapshot = _ms(6000)

    assert agent.proof_mode == "reproducible"
    assert callable(agent.decide)
    assert agent.config_hash is not None
    # CALLABLE config_hash (the orchestrator's finalize calls config_hash(market_state)):
    assert agent.config_hash(snapshot) == agent.config_hash(snapshot)  # stable across calls
    other = value_vs_venue_agent(venue_price_source=lambda mk: 2.0, min_edge_bps=9999)
    assert agent.config_hash(snapshot) != other.config_hash(snapshot)  # param-sensitive


def test_vvv_action_params_do_not_smuggle_venue_data_into_evidence() -> None:
    """THE trust test: the fired action's params carry NO venue-derived value (INVARIANT 4)."""
    from veridex.strategies.value_vs_venue import value_vs_venue_agent

    agent = value_vs_venue_agent(venue_price_source=lambda mk: 2.0)
    action = asyncio.run(agent.decide(_ms(6000)))

    assert action.type != "WAIT", "fair 6000 @ decimal 2.0 clears a 0 min-edge and must fire"
    # params are a SUBSET of the TxLINE-derived allowlist — nothing else may ride into the seal.
    assert set(action.params) <= {"market_key", "side", "reason", "confidence"}
    # ...and specifically NONE of the venue-derived quantities leak in as params.
    for forbidden in ("venue_decimal_price", "estimated_executable_edge_bps", "gap_bps"):
        assert forbidden not in action.params
    # Defence in depth: the whole sealed action_payload must contain no venue-derived number.
    for forbidden_value in ("2000", "1000", "2.0"):
        assert forbidden_value not in action.model_dump_json()


def test_value_vs_venue_strategy_is_accepted_by_preflight() -> None:
    """Step 6: value-vs-venue is a first-class deploy strategy the preflight ``config`` check accepts."""
    from veridex.deploy.preflight import DeployConfig, run_deploy_preflight

    config = DeployConfig(
        template_id="value-vs-venue",
        agent_id="studio-vvv",
        strategy="value-vs-venue",
        source_mode="replay",
    )
    checks = run_deploy_preflight(
        config, feed_report=None, market_resolved=None, envelope=config.to_policy_envelope()
    )
    cfg = next(c for c in checks if c.name == "config")
    assert cfg.ok is True


# ------------------------------------------------------------------------------------------
# Task 18b — the VvV PRODUCER: a real BacktestReport with a POST-BUILD estimated edge (S5).
# ------------------------------------------------------------------------------------------


async def test_vvv_produces_report_with_estimated_edge_but_scored_path_stays_venue_blind(tmp_path: Path) -> None:
    """The producer attaches an estimated edge POST-build; the scored/ranked path stays venue-blind."""
    from veridex.backtest.vvv_report import vvv_report_with_estimated_edge

    session_dir = _write_session(tmp_path)
    pack_dir = tmp_path / "pack"
    pack_from_session(session_dir, pack_dir)

    assumptions = {"no_interpolation": True, "slippage_bps": 0, "costs_bps": 0}
    result, report = await vvv_report_with_estimated_edge(
        pack_dir,
        _FIXTURE_ID,
        venue_price_source=lambda mk: 2.0,
        window=_window(),
        min_edge_bps=0,
        assumptions=assumptions,
    )

    # An estimated (venue-derived) edge IS produced, with a machine-readable rung + explicit assumptions.
    assert report.estimated_executable_edge_bps is not None
    assert report.estimated_edge_rung in {"backfilled-price-history", "recorded-live-quote"}
    assert report.estimated_edge_assumptions["no_interpolation"] is True
    # ...but the REAL executable edge stays null (paper venue — no live fill).
    assert report.real_executable_edge_bps is None
    # ...and the estimated edge NEVER enters any ranked leaderboard row (SEC-005).
    for row in report.leaderboard:
        assert "estimated_executable_edge_bps" not in row
    # The report is for the same sealed run the producer scored.
    assert report.run_id == result.run_id
