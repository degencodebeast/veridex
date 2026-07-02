"""WD-3 — typed, secret-free run config (COM-001)."""

from __future__ import annotations

from pathlib import Path

from veridex_agent.config import AgentRunConfig, build_agent, build_policy_envelope, load_agent_run_config

SAMPLE = str(Path(__file__).parent.parent / "veridex_agent" / "sample_agent.toml")


def test_loads_sample_toml() -> None:
    config = load_agent_run_config(SAMPLE)
    assert isinstance(config, AgentRunConfig)
    assert config.strategy in {"baseline", "momentum", "llm"}


def test_config_carries_no_secret_fields() -> None:
    # COM-001: no credential keys may exist on the run config (secrets come from Settings).
    forbidden = {"jwt", "api_token", "private_key", "keypair", "openrouter_api_key", "anthropic_api_key"}
    assert forbidden.isdisjoint(set(AgentRunConfig.model_fields))


def test_build_agent_momentum() -> None:
    config = AgentRunConfig(agent_id="m", strategy="momentum", min_momentum_bps=50)
    agent = build_agent(config)
    assert agent.agent_id == "m"
    assert agent.proof_mode == "reproducible"


def test_build_agent_baseline() -> None:
    agent = build_agent(AgentRunConfig(agent_id="b", strategy="baseline"))
    assert agent.proof_mode == "reproducible"


def test_build_policy_envelope_from_config() -> None:
    config = AgentRunConfig(
        agent_id="m",
        strategy="momentum",
        max_stake=100.0,
        min_edge_bps=8,
        venue_allowlist=["sx_bet"],
        market_allowlist=["M"],
    )
    envelope = build_policy_envelope(config)
    assert envelope.max_stake == 100.0
    assert envelope.min_edge_bps == 8
    assert envelope.venue_allowlist == ["sx_bet"]
