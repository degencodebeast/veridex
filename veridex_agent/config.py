"""WD-3 — typed, secret-free standalone run config (REQ-052 / COM-001).

The run config (TOML) carries ONLY non-secret strategy/policy knobs. Credentials — TxLINE
JWT/``X-Api-Token``, the Solana keypair, venue keys — are resolved from
:class:`veridex.config.Settings` (env / ``veridex/.env``) at use time, NEVER from this file. The
config also builds the agent and the policy envelope so the CLI stays a thin wrapper.
"""

from __future__ import annotations

import hashlib
import tomllib
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from veridex.policy.envelope import PolicyEnvelope
from veridex.runtime.evidence import serialize_payload
from veridex.runtime.orchestrator import Agent, deterministic_agent, llm_agent
from veridex.runtime.window import RunWindow
from veridex.strategies.drift import cumulative_drift_agent
from veridex.strategies.momentum import momentum_agent, sharp_momentum_agent


class AgentRunConfig(BaseModel):
    """Non-secret configuration for one standalone agent run.

    Attributes:
        agent_id: Stable agent identifier.
        strategy: ``"baseline"`` | ``"momentum"`` | ``"momentum-sharp"`` (flagship v2) |
            ``"cumulative-drift"`` | ``"llm"``.
        model_id: OpenRouter ``provider/model`` slug (LLM strategy only); ``None`` → config default.
        source_mode: ``"replay"`` or ``"live"``.
        fixture_path: Replay fixture path (required when ``source_mode == "replay"``).
        window_id: Stable identifier for the live coverage window (``source_mode == "live"``).
        fixture_id: The TxLINE fixture the live window covers (``source_mode == "live"``).
        end_rule: How a live window closes — ``pre_match`` | ``fixed_duration`` | ``manual_stop``.
        duration_s: Window duration in seconds; required IFF ``end_rule == "fixed_duration"``.
        min_clv_horizon_s: DEC-2D-2 horizon — an entry within this many seconds of close is
            excluded from CLV means (pending_horizon).
        lookback: Momentum window (v1 momentum window; the v2 robust-z movement window). ``>= 1``,
            and for ``momentum-sharp`` must be ``>= min_movements`` (else robust-z can never fire).
        min_momentum_bps: Minimum momentum to flag a side (momentum strategy).
        alpha: v2 EWMA smoothing factor in ``(0, 1]``.
        z_threshold: v2 minimum robust z-score to flag (``> 0``).
        ph_delta: v2 Page-Hinkley per-step tolerance (``>= 0``).
        ph_lambda: v2 Page-Hinkley alarm threshold (``> 0``).
        cooldown_ticks: v2 ticks a market is suppressed after firing (``>= 0``).
        warmup_ticks: v2 ticks observed before any action (``>= 0``).
        min_movements: v2 minimum per-side movement samples before robust-z can fire (``>= 2``).
        scale_floor: v2 minimum robust-z denominator scale (``>= 0``).
        persistence_logit: v2 minimum cumulative logit move over the persistence window (``>= 0``).
        cum_drift_logit_min: Cumulative-drift minimum RISING logit drift required to flag a side
            (``>= 0``).
        ewma_slope_alpha: Cumulative-drift EWMA smoothing factor for trend strength in ``(0, 1]``.
        trend_strength_min: Cumulative-drift minimum EWMA-slope trend strength in ``[-1, 1]``.
        min_tick_count: Cumulative-drift minimum observed ticks before a side can fire (``>= 1``).
        min_horizon_s: Cumulative-drift minimum observation horizon in seconds (``>= 0``).
        close_quality_required: Cumulative-drift — when ``True``, suspended/low-quality markets
            are skipped.
        drift_cooldown_ticks: Cumulative-drift ticks a market is suppressed after it fires
            (``>= 0``). A distinct field from ``cooldown_ticks`` (momentum-sharp's own cooldown
            knob) since the two strategies tune this independently with different defaults.
        market_allowlist: Allowed market keys (policy envelope + live window market prefixes).
        venue_allowlist: Allowed venues (policy envelope).
        max_stake: Max stake per order (policy envelope).
        min_edge_bps: Min recomputed edge to act (policy envelope).
        execution_mode: ``"paper"`` | ``"dry_run"`` | ``"live_guarded"``.
        anchor: When ``True``, anchor the proof on Solana (needs ``SOLANA_KEYPAIR_PATH``).
    """

    agent_id: str
    strategy: Literal["baseline", "momentum", "momentum-sharp", "cumulative-drift", "llm"]
    model_id: str | None = None
    source_mode: Literal["replay", "live"] = "replay"
    fixture_path: str | None = None
    window_id: str = ""
    fixture_id: int = 0
    end_rule: Literal["pre_match", "fixed_duration", "manual_stop"] = "pre_match"
    duration_s: int | None = None
    min_clv_horizon_s: int = 60
    lookback: int = Field(default=8, ge=1)
    min_momentum_bps: int = 50
    # Momentum v2 (sharp-move) detector knobs — TYPED + BOUNDED so an out-of-range value raises at
    # config-load (never a weird-but-hashable instance). All enter config_hash (behavioural surface).
    alpha: float = Field(default=0.4, gt=0.0, le=1.0)
    z_threshold: float = Field(default=2.5, gt=0.0)
    ph_delta: float = Field(default=0.01, ge=0.0)
    ph_lambda: float = Field(default=0.15, gt=0.0)
    cooldown_ticks: int = Field(default=3, ge=0)
    warmup_ticks: int = Field(default=10, ge=0)
    min_movements: int = Field(default=8, ge=2)
    scale_floor: float = Field(default=0.02, ge=0.0)
    persistence_logit: float = Field(default=0.06, ge=0.0)
    # Cumulative-drift detector knobs (defaults mirror veridex.strategies.drift.cumulative_drift_agent).
    cum_drift_logit_min: float = Field(default=0.15, ge=0.0)
    ewma_slope_alpha: float = Field(default=0.2, gt=0.0, le=1.0)
    trend_strength_min: float = Field(default=0.5, ge=-1.0, le=1.0)
    min_tick_count: int = Field(default=20, ge=1)
    min_horizon_s: int = Field(default=600, ge=0)
    close_quality_required: bool = True
    drift_cooldown_ticks: int = Field(default=5, ge=0)
    market_allowlist: list[str] = Field(default_factory=list)
    venue_allowlist: list[str] = Field(default_factory=list)
    max_stake: float = 0.0
    min_edge_bps: int = 0
    execution_mode: Literal["paper", "dry_run", "live_guarded"] = "paper"
    anchor: bool = False

    @model_validator(mode="after")
    def _check_sharp_window(self) -> AgentRunConfig:
        """Cross-field guard: for ``momentum-sharp``, the robust-z window must retain enough samples.

        ``lookback`` caps the retained per-side movements; if it is smaller than ``min_movements``
        the robust-z gate can never accumulate enough samples to fire. Rejecting it at config-load
        keeps a deployed v2 instance from being a valid-but-inert (never-fires) configuration.
        """
        if self.strategy == "momentum-sharp" and self.lookback < self.min_movements:
            raise ValueError(
                f"lookback ({self.lookback}) must be >= min_movements ({self.min_movements}) for momentum-sharp"
            )
        return self

    def config_hash(self) -> str:
        """SHA-256 over the canonical serialization of this (non-secret) config.

        Pins the launched run to its exact strategy/policy/window knobs — the "agent instance" that
        the standalone core records in its run manifest alongside the ``policy_hash`` + window
        (SEC-2D-401). The config carries ONLY non-secret fields (credentials live in
        :class:`veridex.config.Settings`), so this hash never binds a secret. The canonical
        serializer is the SAME one the arena uses for ``config_hash``, so the pin is stable.

        Returns:
            The hex SHA-256 of the serialized config.
        """
        return hashlib.sha256(serialize_payload(self.model_dump()).encode("utf-8")).hexdigest()


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
    if config.strategy == "momentum-sharp":
        return sharp_momentum_agent(
            config.agent_id,
            alpha=config.alpha,
            z_threshold=config.z_threshold,
            ph_delta=config.ph_delta,
            ph_lambda=config.ph_lambda,
            cooldown_ticks=config.cooldown_ticks,
            warmup_ticks=config.warmup_ticks,
            min_movements=config.min_movements,
            lookback=config.lookback,
            scale_floor=config.scale_floor,
            persistence_logit=config.persistence_logit,
        )
    if config.strategy == "cumulative-drift":
        return cumulative_drift_agent(
            cum_drift_logit_min=config.cum_drift_logit_min,
            ewma_slope_alpha=config.ewma_slope_alpha,
            trend_strength_min=config.trend_strength_min,
            min_tick_count=config.min_tick_count,
            min_horizon_s=config.min_horizon_s,
            close_quality_required=config.close_quality_required,
            cooldown_ticks=config.drift_cooldown_ticks,
            agent_id=config.agent_id,
        )
    if config.strategy == "llm":
        return llm_agent(config.agent_id, model_id=config.model_id)
    raise ValueError(f"unknown strategy: {config.strategy!r}")


def build_run_window(config: AgentRunConfig) -> RunWindow:
    """Build the live :class:`~veridex.runtime.window.RunWindow` from the run config.

    The window's ``market_allowlist`` (prefix match) is shared with the policy envelope's
    market allowlist — the same operator-committed market scope governs both what the live source
    scores and what the execution lane may route. Credentials are NOT part of the window (they are
    resolved from :class:`veridex.config.Settings` inside the live client seam).

    Args:
        config: The validated run config (``source_mode == "live"``).

    Returns:
        A :class:`~veridex.runtime.window.RunWindow` (``duration_s`` validated against ``end_rule``).
    """
    return RunWindow(
        window_id=config.window_id,
        fixture_id=config.fixture_id,
        market_allowlist=config.market_allowlist,
        end_rule=config.end_rule,
        duration_s=config.duration_s,
        min_clv_horizon_s=config.min_clv_horizon_s,
    )


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
