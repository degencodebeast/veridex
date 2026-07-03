"""Deploy preflight — fail-closed, NAMED preconditions for a Studio deploy (REQ-2D-701 / AC-2D-701).

A submitted config is TYPED (pydantic) at the wire, then BOUNDED here before it can become a
pinned :class:`AgentInstance`: an out-of-range knob FAILS the named ``config`` check rather than
minting a weird-but-hashable instance (Codex M5 gate 3). ``config_hash`` is pinned ONLY after the
config check passes. Feed health, market resolvability (execution enabled), and sane policy limits
are each their own NAMED check, so an operator sees exactly what is not ready.

PURE + OFFLINE: :func:`run_deploy_preflight` evaluates already-fetched values (a
:class:`~veridex.ingest.feed_health.FeedHealthReport`, a market-resolved flag, the built
:class:`~veridex.policy.envelope.PolicyEnvelope`) and does ZERO network I/O. The route
(:mod:`veridex.api.deploy`) fetches those inputs and turns any ``ok is False`` check into a 422
that names it; NO run starts on a preflight failure.

Trust doctrine: config changes BEHAVIOUR, never trust rules. The knobs bound here are strategy /
policy parameters (momentum v2 detector + market universe / sizing / risk caps); none of them
touch the law, evidence, checks, or scoring immutability.
"""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, Field

from veridex.ingest.feed_health import FeedHealthReport
from veridex.policy.envelope import PolicyEnvelope
from veridex.runtime.evidence import serialize_payload

# ---------------------------------------------------------------------------
# Bounded-knob spec: (field, low, high, low_inclusive, high_inclusive)
# ---------------------------------------------------------------------------

_NumericBound = tuple[str, float, float, bool, bool]

_NUMERIC_BOUNDS: tuple[_NumericBound, ...] = (
    # momentum v2 detector knobs (see veridex.strategies.momentum.SharpMomentumStrategy)
    ("alpha", 0.0, 1.0, False, True),  # EWMA smoothing in (0, 1]
    ("z_threshold", 0.0, 1_000_000.0, False, True),  # robust-z gate must be > 0
    ("ph_delta", 0.0, 1_000_000.0, True, True),  # Page-Hinkley tolerance >= 0
    ("ph_lambda", 0.0, 1_000_000.0, False, True),  # Page-Hinkley alarm > 0
    ("cooldown_ticks", 0.0, 1_000_000.0, True, True),
    ("warmup_ticks", 0.0, 1_000_000.0, True, True),
    ("min_movements", 2.0, 1_000_000.0, True, True),  # robust-z needs >= 2 samples
    ("lookback", 1.0, 1_000_000.0, True, True),  # window must retain >= 1 movement
    ("scale_floor", 0.0, 1_000_000.0, True, True),  # robust-z denominator scale >= 0
    ("persistence_logit", 0.0, 1_000_000.0, True, True),
    # market universe / sizing / risk caps
    ("min_edge_bps", 0.0, 100_000.0, True, True),
    ("max_stake", 0.0, 1_000_000_000.0, True, True),
    ("fixture_id", 0.0, 1_000_000_000_000.0, True, True),
    ("min_clv_horizon_s", 0.0, 1_000_000_000.0, True, True),
)


# ---------------------------------------------------------------------------
# Submitted config (the wire boundary AND the pinned instance's config)
# ---------------------------------------------------------------------------


class DeployConfig(BaseModel):
    """The Studio-submitted, non-secret agent config (validated + bounded before it is pinned).

    Types are enforced at the pydantic boundary (a non-numeric knob is rejected 422 before it can
    be hashed); numeric BOUNDS are enforced by the named ``config`` preflight check so an
    out-of-range value fails preflight with a legible reason rather than minting a weird instance.
    Credentials never live here (COM-001) — they are resolved inside the runner seams.

    Attributes:
        template_id: Strategy-archetype identifier (the template the instance was configured from).
        agent_id: Stable identifier for the deployed agent.
        strategy: Strategy family — ``baseline`` | ``momentum`` | ``momentum-sharp`` | ``llm``.
        source_mode: ``replay`` or ``live``.
        execution_mode: ``paper`` | ``dry_run`` | ``live_guarded`` (capital-exposure guard).
        market_allowlist: Markets the agent may score / route to (policy + live-window scope).
        venue_allowlist: Venues an order may route to (policy scope).
        min_edge_bps: Minimum recomputed edge required to act (policy).
        max_stake: Max stake per order (policy).
        window_id: Live coverage window id (``source_mode == "live"``).
        fixture_id: The TxLINE fixture the live window covers.
        end_rule: How a live window closes.
        duration_s: Window duration seconds (required iff ``end_rule == "fixed_duration"``).
        min_clv_horizon_s: Entries within this many seconds of close are pending_horizon.
        alpha, z_threshold, ph_delta, ph_lambda, cooldown_ticks, warmup_ticks, min_movements,
        lookback, scale_floor, persistence_logit: Momentum-v2 detector knobs.
    """

    template_id: str
    agent_id: str
    strategy: Literal["baseline", "momentum", "momentum-sharp", "llm"] = "momentum-sharp"
    source_mode: Literal["replay", "live"] = "live"
    execution_mode: Literal["paper", "dry_run", "live_guarded"] = "paper"
    market_allowlist: list[str] = Field(default_factory=list)
    venue_allowlist: list[str] = Field(default_factory=list)
    min_edge_bps: int = 0
    max_stake: float = 0.0
    window_id: str = ""
    fixture_id: int = 0
    end_rule: Literal["pre_match", "fixed_duration", "manual_stop"] = "pre_match"
    duration_s: int | None = None
    min_clv_horizon_s: int = 60
    # momentum v2 knobs (defaults mirror veridex.strategies.momentum.sharp_momentum_agent)
    alpha: float = 0.4
    z_threshold: float = 2.5
    ph_delta: float = 0.01
    ph_lambda: float = 0.15
    cooldown_ticks: int = 3
    warmup_ticks: int = 10
    min_movements: int = 8
    lookback: int = 64
    scale_floor: float = 0.02
    persistence_logit: float = 0.06

    def config_hash(self) -> str:
        """SHA-256 over the canonical serialization of this (non-secret) config.

        Uses the ONE canonical serializer (:func:`serialize_payload`) so the pin is byte-stable
        across processes and consistent with the arena/agent ``config_hash`` composition. Callers
        MUST only pin this after the ``config`` preflight check passes.

        Returns:
            The hex SHA-256 of the serialized config.
        """
        return hashlib.sha256(serialize_payload(self.model_dump()).encode("utf-8")).hexdigest()

    def to_policy_envelope(self) -> PolicyEnvelope:
        """Build the single-order policy envelope this config commits to (mirrors the CLI builder).

        Returns:
            A conservative single-order :class:`~veridex.policy.envelope.PolicyEnvelope` (one order
            per run/session/day, kill-switch off) scoped to the config's market/venue allowlists.
        """
        return PolicyEnvelope(
            max_stake=self.max_stake,
            max_orders_per_run=1,
            max_orders_per_session=1,
            max_orders_per_day=1,
            venue_allowlist=list(self.venue_allowlist),
            market_allowlist=list(self.market_allowlist),
            min_edge_bps=self.min_edge_bps,
            max_slippage_bps=100,
            max_price=1000.0,
            max_quote_age_s=60,
            cooldown_s=0,
            human_approval_threshold=self.max_stake + 1.0,
            kill_switch=False,
        )


# ---------------------------------------------------------------------------
# Named-check value type
# ---------------------------------------------------------------------------


class PreflightCheck(BaseModel):
    """One NAMED deploy precondition verdict.

    Attributes:
        name: Stable check identifier (``config`` | ``feed_health`` | ``market_mapped`` |
            ``policy_limits``).
        ok: ``True`` pass, ``False`` fail (blocks the deploy), or ``None`` for a not-applicable /
            operator-pending check that does not contribute to the verdict.
        detail: Human-readable explanation of the verdict.
    """

    name: str
    ok: bool | None
    detail: str


# ---------------------------------------------------------------------------
# Named checks
# ---------------------------------------------------------------------------


def _bound_ok(value: float, low: float, high: float, low_incl: bool, high_incl: bool) -> bool:
    lo = value >= low if low_incl else value > low
    hi = value <= high if high_incl else value < high
    return lo and hi


def _check_config(config: DeployConfig) -> PreflightCheck:
    """Bounded validation of every numeric knob — the named ``config`` check (fail-closed)."""
    offenders: list[str] = []
    dumped = config.model_dump()
    for field, low, high, low_incl, high_incl in _NUMERIC_BOUNDS:
        value = dumped[field]
        if not isinstance(value, (int, float)) or not _bound_ok(float(value), low, high, low_incl, high_incl):
            lo_b = "[" if low_incl else "("
            hi_b = "]" if high_incl else ")"
            offenders.append(f"{field}={value} out of range {lo_b}{low:g}, {high:g}{hi_b}")

    # Cross-field sanity (momentum-sharp ONLY — v1 momentum never uses min_movements): the robust-z
    # window can never fire if lookback can't retain the required samples. Scoped to match the
    # AgentRunConfig validator so a non-sharp deploy is not falsely rejected.
    if config.strategy == "momentum-sharp" and config.lookback < config.min_movements:
        offenders.append(f"lookback={config.lookback} < min_movements={config.min_movements} (can never fire)")

    if offenders:
        return PreflightCheck(name="config", ok=False, detail="; ".join(offenders))
    return PreflightCheck(name="config", ok=True, detail="all config knobs within bounds")


def _check_feed(
    config: DeployConfig, feed_report: FeedHealthReport | None, source_resolved: bool | None
) -> PreflightCheck:
    """Feed/source readiness — MODE-AWARE (REQ-2D-703).

    A ``replay`` deploy needs no live feed; instead the named ``feed_health`` check verifies the
    replay SOURCE resolves (the injected/demo-fixture ticks loaded to non-empty marketstates). When
    the caller did not resolve a source (``source_resolved is None``) the check stays permissive —
    replay has no live-feed precondition — preserving the pure-preflight contract. A ``live``
    deploy STAYS fail-closed: it must have a connected, fresh feed (the correct 422 until a live
    feed is wired — never weakened, never fabricated)."""
    if config.source_mode == "replay":
        if source_resolved is False:
            return PreflightCheck(
                name="feed_health", ok=False, detail="replay source did not resolve (no replay ticks)"
            )
        detail = (
            "replay source resolved (demo replay ticks ready)"
            if source_resolved is True
            else "replay source — no live feed required"
        )
        return PreflightCheck(name="feed_health", ok=True, detail=detail)
    if feed_report is None or not feed_report.connected:
        return PreflightCheck(name="feed_health", ok=False, detail="live feed not connected")
    if feed_report.stale:
        return PreflightCheck(
            name="feed_health", ok=False, detail=f"live feed stale (staleness_s={feed_report.staleness_s})"
        )
    return PreflightCheck(name="feed_health", ok=True, detail="live feed connected and fresh")


def _check_market(config: DeployConfig, market_resolved: bool | None) -> PreflightCheck:
    """Market-mapping check — required for real-venue (``live_guarded``) execution; else n/a.

    ``paper``/``dry_run`` use no real venue (dry_run routes through an offline fake adapter), so the
    market-mapping precondition is not applicable and does not gate the deploy. A real-money
    ``live_guarded`` deploy fails closed unless the market resolved to concrete identifiers.
    """
    if config.execution_mode != "live_guarded":
        return PreflightCheck(
            name="market_mapped", ok=None, detail=f"not applicable for execution_mode={config.execution_mode}"
        )
    if market_resolved is True:
        return PreflightCheck(name="market_mapped", ok=True, detail="market resolved to concrete identifiers")
    return PreflightCheck(
        name="market_mapped",
        ok=False,
        detail="market did not resolve to Polymarket identifiers (execution enabled) — failing closed",
    )


def _check_policy(config: DeployConfig, envelope: PolicyEnvelope) -> PreflightCheck:
    """Sane-policy-limits check — reject insane caps and an empty trade universe under execution."""
    problems: list[str] = []
    if envelope.max_stake < 0.0:
        problems.append(f"max_stake={envelope.max_stake} < 0")
    if envelope.min_edge_bps < 0:
        problems.append(f"min_edge_bps={envelope.min_edge_bps} < 0")
    if envelope.max_price <= 0.0:
        problems.append(f"max_price={envelope.max_price} <= 0")
    if envelope.max_slippage_bps < 0:
        problems.append(f"max_slippage_bps={envelope.max_slippage_bps} < 0")
    if envelope.human_approval_threshold < 0.0:
        problems.append(f"human_approval_threshold={envelope.human_approval_threshold} < 0")
    if config.execution_mode != "paper" and not config.market_allowlist:
        problems.append("market_allowlist is empty while execution is enabled (nothing to trade)")
    if config.execution_mode != "paper" and not config.venue_allowlist:
        problems.append("venue_allowlist is empty while execution is enabled (nowhere to route)")

    if problems:
        return PreflightCheck(name="policy_limits", ok=False, detail="; ".join(problems))
    return PreflightCheck(name="policy_limits", ok=True, detail="policy limits are sane")


def run_deploy_preflight(
    config: DeployConfig,
    *,
    feed_report: FeedHealthReport | None,
    market_resolved: bool | None,
    envelope: PolicyEnvelope,
    source_resolved: bool | None = None,
) -> list[PreflightCheck]:
    """Evaluate every NAMED deploy precondition over already-fetched inputs (pure, offline).

    Args:
        config: The typed, submitted config to bound-check + pin.
        feed_report: The live/replay feed-health report (``None`` acceptable for replay).
        market_resolved: Whether the target market resolved to concrete identifiers, or ``None``
            when not checked (only gates a ``live_guarded`` deploy).
        envelope: The policy envelope built from ``config`` (checked for sane limits).
        source_resolved: For a ``replay`` deploy, whether the replay SOURCE resolved to non-empty
            marketstates (the route resolves the bundled/injected pack and passes the verdict).
            ``None`` → not resolved by the caller (pure-preflight default; replay stays permissive).
            Ignored for a ``live`` deploy, which is gated by ``feed_report``.

    Returns:
        The ordered list of :class:`PreflightCheck` verdicts (``config``, ``feed_health``,
        ``market_mapped``, ``policy_limits``). The caller treats any ``ok is False`` as fail-closed.
    """
    return [
        _check_config(config),
        _check_feed(config, feed_report, source_resolved),
        _check_market(config, market_resolved),
        _check_policy(config, envelope),
    ]
