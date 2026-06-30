"""WD-3 — typed, secret-free standalone run config (REQ-052 / COM-001).

The run config (TOML) carries ONLY non-secret strategy/policy knobs. Credentials — TxLINE
JWT/``X-Api-Token``, the Solana keypair, venue keys — are resolved from
:class:`veridex.config.Settings` (env / ``veridex/.env``) at use time, NEVER from this file. The
config also builds the agent and the policy envelope so the CLI stays a thin wrapper.
"""

from __future__ import annotations

import tomllib
from typing import Literal

from pydantic import BaseModel, Field

from veridex.policy.envelope import PolicyEnvelope
from veridex.runtime.orchestrator import Agent, deterministic_agent, llm_agent
from veridex.strategies.momentum import momentum_agent


class AgentRunConfig(BaseModel):
    """Non-secret configuration for one standalone agent run.

    Attributes:
        agent_id: Stable agent identifier.
        strategy: ``"baseline"`` | ``"momentum"`` | ``"llm"``.
        model_id: OpenRouter ``provider/model`` slug (LLM strategy only); ``None`` → config default.
        source_mode: ``"replay"`` or ``"live"``.
        fixture_path: Replay fixture path (required when ``source_mode == "replay"``).
        lookback: Momentum window (momentum strategy).
        min_momentum_bps: Minimum momentum to flag a side (momentum strategy).
        market_allowlist: Allowed market keys (policy envelope).
        venue_allowlist: Allowed venues (policy envelope).
        max_stake: Max stake per order (policy envelope).
        min_edge_bps: Min recomputed edge to act (policy envelope).
        execution_mode: ``"paper"`` | ``"dry_run"`` | ``"live_guarded"``.
        anchor: When ``True``, anchor the proof on Solana (needs ``SOLANA_KEYPAIR_PATH``).
    """

    agent_id: str
    strategy: Literal["baseline", "momentum", "llm"]
    model_id: str | None = None
    source_mode: Literal["replay", "live"] = "replay"
    fixture_path: str | None = None
    lookback: int = 8
    min_momentum_bps: int = 50
    market_allowlist: list[str] = Field(default_factory=list)
    venue_allowlist: list[str] = Field(default_factory=list)
    max_stake: float = 0.0
    min_edge_bps: int = 0
    execution_mode: Literal["paper", "dry_run", "live_guarded"] = "paper"
    anchor: bool = False


def load_agent_run_config(path: str) -> AgentRunConfig:
    """Load and validate an :class:`AgentRunConfig` from a TOML file.

    Args:
        path: Filesystem path to the run-config TOML.

    Returns:
        The validated :class:`AgentRunConfig`.
    """
    with open(path, "rb") as handle:
        data = tomllib.load(handle)
    return AgentRunConfig.model_validate(data)


def build_agent(config: AgentRunConfig) -> Agent:
    """Construct the orchestrator :class:`~veridex.runtime.orchestrator.Agent` for this config.

    Args:
        config: The validated run config.

    Returns:
        The agent for ``config.strategy`` (``"llm"`` resolves credentials lazily at decide-time).

    Raises:
        ValueError: If ``config.strategy`` is unknown.
    """
    if config.strategy == "baseline":
        return deterministic_agent(config.agent_id)
    if config.strategy == "momentum":
        return momentum_agent(config.agent_id, lookback=config.lookback, min_momentum_bps=config.min_momentum_bps)
    if config.strategy == "llm":
        return llm_agent(config.agent_id, model_id=config.model_id)
    raise ValueError(f"unknown strategy: {config.strategy!r}")


def build_policy_envelope(config: AgentRunConfig) -> PolicyEnvelope:
    """Build a :class:`~veridex.policy.envelope.PolicyEnvelope` from the run config.

    Args:
        config: The validated run config.

    Returns:
        A populated policy envelope (kill switch off; single-order caps for a solo run).
    """
    return PolicyEnvelope(
        max_stake=config.max_stake,
        max_orders_per_run=1,
        max_orders_per_session=1,
        max_orders_per_day=1,
        venue_allowlist=config.venue_allowlist,
        market_allowlist=config.market_allowlist,
        min_edge_bps=config.min_edge_bps,
        max_slippage_bps=100,
        max_price=1000.0,
        max_quote_age_s=60,
        cooldown_s=0,
        human_approval_threshold=config.max_stake + 1.0,
        kill_switch=False,
    )
