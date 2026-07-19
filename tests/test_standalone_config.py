"""WD-3 — typed, secret-free run config (COM-001)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

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


def test_build_agent_momentum_sharp_is_the_flagship_v2() -> None:
    # The flagship sharp-momentum v2 is deployable from the typed config (build_agent dispatch).
    config = AgentRunConfig(
        agent_id="v2",
        strategy="momentum-sharp",
        alpha=0.4,
        z_threshold=2.5,
        min_movements=8,
        lookback=64,
    )
    agent = build_agent(config)
    assert agent.agent_id == "v2"
    assert agent.proof_mode == "reproducible"


def test_v2_knobs_flow_into_config_hash() -> None:
    # Reproducibility: every behavioural v2 knob enters config_hash (same config ⇒ same identity).
    base = AgentRunConfig(agent_id="v2", strategy="momentum-sharp", alpha=0.4, min_movements=8, lookback=64)
    tweaked = base.model_copy(update={"alpha": 0.5})
    assert base.config_hash() != tweaked.config_hash()


def test_out_of_range_v2_knob_raises_at_config_load() -> None:
    # TYPED + BOUNDED: an out-of-range knob raises at construction (never a weird hashable instance).
    with pytest.raises(ValidationError):
        AgentRunConfig(agent_id="v2", strategy="momentum-sharp", alpha=1.5)  # alpha must be in (0, 1]
    with pytest.raises(ValidationError):
        AgentRunConfig(agent_id="v2", strategy="momentum-sharp", z_threshold=-1.0)  # must be > 0
    with pytest.raises(ValidationError):
        AgentRunConfig(agent_id="v2", strategy="momentum-sharp", min_movements=1)  # robust-z needs >= 2


def test_lookback_below_min_movements_raises_cross_field() -> None:
    # Cross-field: the robust-z window can never fire if lookback < min_movements — reject it.
    with pytest.raises(ValidationError):
        AgentRunConfig(agent_id="v2", strategy="momentum-sharp", lookback=4, min_movements=8)


def test_build_agent_cumulative_drift() -> None:
    # I-6: cumulative-drift dispatch repair. The strategy already exists (veridex.strategies.drift)
    # but strategy="cumulative-drift" was rejected by the config Literal and had no build branch.
    config = AgentRunConfig(agent_id="d", strategy="cumulative-drift")
    agent = build_agent(config)
    assert agent.agent_id == "d"
    assert agent.proof_mode == "reproducible"


@pytest.mark.parametrize("strategy", ["baseline", "momentum", "momentum-sharp", "cumulative-drift", "llm"])
def test_build_agent_covers_every_strategy(strategy: str) -> None:
    # No-regression guard: every declared strategy value still builds (the fix adds a branch,
    # breaks none). momentum-sharp's cross-field validator is satisfied by the field defaults
    # (lookback=8 >= min_movements=8).
    config = AgentRunConfig(agent_id="s", strategy=strategy)
    agent = build_agent(config)
    assert agent.agent_id == "s"


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
