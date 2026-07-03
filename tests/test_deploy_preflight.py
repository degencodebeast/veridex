"""T21 — deploy preflight: fail-closed, NAMED preconditions (REQ-2D-701 / AC-2D-701).

The submitted Studio config is TYPED + BOUNDED before it can become a pinned AgentInstance: an
out-of-range knob FAILS PREFLIGHT with a named ``config`` check rather than minting a
weird-but-hashable instance. Feed health, market resolvability (execution enabled), and sane
policy limits are each their own NAMED check. Every check runs offline over injected values —
no live network. The endpoint turns any ``ok is False`` check into a 422 that names it, and NO
run starts on a preflight failure.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from veridex.api.deploy import DeployDeps
from veridex.api.router import create_app
from veridex.deploy.preflight import DeployConfig, PreflightCheck, run_deploy_preflight
from veridex.ingest.feed_health import FeedHealthReport
from veridex.store import InMemoryStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_VALID: dict[str, Any] = {
    "template_id": "sharp-momentum-v2",
    "agent_id": "studio-agent",
    "strategy": "momentum-sharp",
    "source_mode": "live",
    "execution_mode": "paper",
    "market_allowlist": ["OU|FT|2.5"],
    "venue_allowlist": ["fake"],
    "min_edge_bps": 0,
    "max_stake": 0.0,
    "window_id": "w1",
    "fixture_id": 1,
    "end_rule": "pre_match",
    "alpha": 0.4,
    "z_threshold": 2.5,
    "ph_delta": 0.01,
    "ph_lambda": 0.15,
    "cooldown_ticks": 3,
    "warmup_ticks": 10,
    "min_movements": 8,
    "lookback": 64,
    "scale_floor": 0.02,
    "persistence_logit": 0.06,
}


def _healthy_live_feed() -> FeedHealthReport:
    return FeedHealthReport(
        source_mode="live",
        txline_configured=True,
        connected=True,
        last_tick_ts=1000,
        ticks_seen=5,
        fixture_id=1,
        staleness_s=1,
        stale=False,
    )


def _stale_live_feed() -> FeedHealthReport:
    return FeedHealthReport(
        source_mode="live",
        txline_configured=True,
        connected=True,
        last_tick_ts=1000,
        ticks_seen=5,
        fixture_id=1,
        staleness_s=999,
        stale=True,
    )


def _check(checks: list[PreflightCheck], name: str) -> PreflightCheck:
    return next(c for c in checks if c.name == name)


def _client(deps: DeployDeps) -> TestClient:
    return TestClient(create_app(store=InMemoryStore(), deploy_deps=deps))


# ---------------------------------------------------------------------------
# Pure preflight — TYPED + BOUNDED config validation (named ``config`` check)
# ---------------------------------------------------------------------------


def test_valid_config_passes_every_check() -> None:
    config = DeployConfig(**_VALID)
    checks = run_deploy_preflight(
        config,
        feed_report=_healthy_live_feed(),
        market_resolved=None,
        envelope=config.to_policy_envelope(),
    )
    # No boolean-verdict check failed (paper → market check is not_applicable, ok=None).
    assert all(c.ok is not False for c in checks)
    assert _check(checks, "config").ok is True


def test_out_of_range_alpha_fails_named_config_check() -> None:
    config = DeployConfig(**{**_VALID, "alpha": 1.5})  # alpha must be in (0, 1]
    checks = run_deploy_preflight(
        config, feed_report=_healthy_live_feed(), market_resolved=None, envelope=config.to_policy_envelope()
    )
    cfg = _check(checks, "config")
    assert cfg.ok is False
    assert "alpha" in cfg.detail


def test_zero_lookback_fails_named_config_check() -> None:
    config = DeployConfig(**{**_VALID, "lookback": 0})  # lookback must be >= 1
    checks = run_deploy_preflight(
        config, feed_report=_healthy_live_feed(), market_resolved=None, envelope=config.to_policy_envelope()
    )
    cfg = _check(checks, "config")
    assert cfg.ok is False
    assert "lookback" in cfg.detail


def test_negative_z_threshold_fails_named_config_check() -> None:
    config = DeployConfig(**{**_VALID, "z_threshold": -1.0})  # must be > 0
    checks = run_deploy_preflight(
        config, feed_report=_healthy_live_feed(), market_resolved=None, envelope=config.to_policy_envelope()
    )
    assert _check(checks, "config").ok is False


def test_v1_momentum_lookback_below_min_movements_is_not_cross_field_rejected() -> None:
    # v1 "momentum" never uses min_movements — the sharp-only cross-field must NOT falsely reject it.
    config = DeployConfig(
        **{**_VALID, "strategy": "momentum", "source_mode": "replay", "lookback": 4, "min_movements": 8}
    )
    checks = run_deploy_preflight(
        config, feed_report=None, market_resolved=None, envelope=config.to_policy_envelope()
    )
    assert _check(checks, "config").ok is True


def test_sharp_lookback_below_min_movements_still_fails_cross_field() -> None:
    config = DeployConfig(**{**_VALID, "strategy": "momentum-sharp", "lookback": 4, "min_movements": 8})
    checks = run_deploy_preflight(
        config, feed_report=_healthy_live_feed(), market_resolved=None, envelope=config.to_policy_envelope()
    )
    cfg = _check(checks, "config")
    assert cfg.ok is False
    assert "min_movements" in cfg.detail


def test_min_movements_below_two_fails_named_config_check() -> None:
    config = DeployConfig(**{**_VALID, "min_movements": 1})  # robust-z needs >= 2 samples
    checks = run_deploy_preflight(
        config, feed_report=_healthy_live_feed(), market_resolved=None, envelope=config.to_policy_envelope()
    )
    cfg = _check(checks, "config")
    assert cfg.ok is False
    assert "min_movements" in cfg.detail


# ---------------------------------------------------------------------------
# Pure preflight — feed / market / policy named checks
# ---------------------------------------------------------------------------


def test_stale_live_feed_fails_named_feed_check() -> None:
    config = DeployConfig(**_VALID)
    checks = run_deploy_preflight(
        config, feed_report=_stale_live_feed(), market_resolved=None, envelope=config.to_policy_envelope()
    )
    assert _check(checks, "feed_health").ok is False


def test_replay_source_needs_no_live_feed() -> None:
    config = DeployConfig(**{**_VALID, "source_mode": "replay"})
    checks = run_deploy_preflight(
        config, feed_report=None, market_resolved=None, envelope=config.to_policy_envelope()
    )
    assert _check(checks, "feed_health").ok is True


def test_unresolvable_market_when_execution_enabled_fails_named_market_check() -> None:
    config = DeployConfig(**{**_VALID, "execution_mode": "live_guarded"})
    checks = run_deploy_preflight(
        config, feed_report=_healthy_live_feed(), market_resolved=False, envelope=config.to_policy_envelope()
    )
    assert _check(checks, "market_mapped").ok is False


def test_paper_mode_market_check_is_not_applicable() -> None:
    config = DeployConfig(**_VALID)  # paper
    checks = run_deploy_preflight(
        config, feed_report=_healthy_live_feed(), market_resolved=None, envelope=config.to_policy_envelope()
    )
    assert _check(checks, "market_mapped").ok is None


def test_empty_allowlist_with_execution_fails_named_policy_check() -> None:
    config = DeployConfig(**{**_VALID, "source_mode": "replay", "execution_mode": "dry_run", "market_allowlist": []})
    checks = run_deploy_preflight(
        config, feed_report=None, market_resolved=None, envelope=config.to_policy_envelope()
    )
    assert _check(checks, "policy_limits").ok is False


# ---------------------------------------------------------------------------
# Endpoint — each preflight failure → 422 that NAMES the failing check; NO run starts
# ---------------------------------------------------------------------------


def test_endpoint_out_of_range_config_returns_422_named() -> None:
    client = _client(DeployDeps(feed_report=_healthy_live_feed(), market_resolved=True, anchor_fn=None))
    resp = client.post("/agents/deploy", json={**_VALID, "alpha": 9.9})
    assert resp.status_code == 422, resp.text
    assert "config" in resp.json()["detail"]["failed_checks"]


def test_endpoint_stale_feed_returns_422_named() -> None:
    client = _client(DeployDeps(feed_report=_stale_live_feed(), market_resolved=True, anchor_fn=None))
    resp = client.post("/agents/deploy", json=_VALID)
    assert resp.status_code == 422, resp.text
    assert "feed_health" in resp.json()["detail"]["failed_checks"]


def test_endpoint_unresolvable_market_returns_422_named() -> None:
    client = _client(DeployDeps(feed_report=_healthy_live_feed(), market_resolved=False, anchor_fn=None))
    resp = client.post("/agents/deploy", json={**_VALID, "execution_mode": "live_guarded"})
    assert resp.status_code == 422, resp.text
    assert "market_mapped" in resp.json()["detail"]["failed_checks"]


def test_endpoint_insane_policy_returns_422_named() -> None:
    client = _client(DeployDeps(feed_report=_healthy_live_feed(), market_resolved=True, anchor_fn=None))
    resp = client.post(
        "/agents/deploy",
        json={**_VALID, "source_mode": "replay", "execution_mode": "dry_run", "market_allowlist": []},
    )
    assert resp.status_code == 422, resp.text
    assert "policy_limits" in resp.json()["detail"]["failed_checks"]


def test_endpoint_preflight_failure_starts_no_run() -> None:
    app = create_app(
        store=InMemoryStore(),
        deploy_deps=DeployDeps(feed_report=_healthy_live_feed(), market_resolved=True, anchor_fn=None),
    )
    client = TestClient(app)
    resp = client.post("/agents/deploy", json={**_VALID, "alpha": 42.0})
    assert resp.status_code == 422
    # Fail-closed: no background run task was created, no instance was pinned.
    assert not getattr(app.state, "deploy_background_tasks", set())
    assert not getattr(app.state, "deploy_instances", {})


def test_endpoint_wrong_type_is_rejected_before_pin() -> None:
    client = _client(DeployDeps(feed_report=_healthy_live_feed(), market_resolved=True, anchor_fn=None))
    resp = client.post("/agents/deploy", json={**_VALID, "alpha": "not-a-number"})
    assert resp.status_code == 422  # typed: a non-numeric knob never becomes a hashable instance
