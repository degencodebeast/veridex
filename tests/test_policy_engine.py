"""Tests for the deny-by-default policy engine (P2B-1).

Covers the pure/sync evaluation contract: a clean action is approved, every hard rule
denies (and collects ALL reasons, not the first), the human-approval threshold escalates,
the policy hash is deterministic, and the package stays free of LLM SDK imports.
"""

from pathlib import Path

from veridex.policy.engine import PolicyContext, PolicyDecision, evaluate
from veridex.policy.envelope import PolicyEnvelope
from veridex.verifier.import_audit import assert_no_llm_imports


def _env(**kw: object) -> PolicyEnvelope:
    base: dict[str, object] = {
        "max_stake": 100.0,
        "max_orders_per_run": 5,
        "max_orders_per_session": 20,
        "max_orders_per_day": 50,
        "venue_allowlist": ["sx_bet"],
        "market_allowlist": ["OU|2.5|full"],
        "min_edge_bps": 50,
        "max_slippage_bps": 100,
        "max_price": 3.0,
        "max_quote_age_s": 10,
        "cooldown_s": 0,
        "human_approval_threshold": 1000.0,
        "kill_switch": False,
    }
    base.update(kw)
    return PolicyEnvelope(**base)


def _ctx(**kw: object) -> PolicyContext:
    base: dict[str, object] = {
        "recomputed_edge_bps": 120,
        "stake": 50.0,
        "venue": "sx_bet",
        "market_key": "OU|2.5|full",
        "price": 2.0,
        "slippage_bps": 10,
        "quote_age_s": 1,
        "orders_this_run": 0,
        "seconds_since_last_order": None,
        "agent_eligible": True,
    }
    base.update(kw)
    return PolicyContext(**base)


def test_clean_action_approved() -> None:
    r = evaluate(_ctx(), _env())
    assert r.decision is PolicyDecision.APPROVED and r.reason_codes == []


def test_kill_switch_denies() -> None:
    r = evaluate(_ctx(), _env(kill_switch=True))
    assert r.decision is PolicyDecision.DENIED and "kill_switch_on" in r.reason_codes


def test_edge_below_min_denies() -> None:
    r = evaluate(_ctx(recomputed_edge_bps=20), _env(min_edge_bps=50))
    assert r.decision is PolicyDecision.DENIED and "edge_below_min" in r.reason_codes


def test_stale_quote_denies() -> None:
    r = evaluate(_ctx(quote_age_s=60), _env(max_quote_age_s=10))
    assert r.decision is PolicyDecision.DENIED and "quote_stale" in r.reason_codes


def test_order_cap_denies() -> None:
    r = evaluate(_ctx(orders_this_run=5), _env(max_orders_per_run=5))
    assert r.decision is PolicyDecision.DENIED and "order_cap_run" in r.reason_codes


def test_venue_not_allowed_denies() -> None:
    r = evaluate(_ctx(venue="polymarket"), _env(venue_allowlist=["sx_bet"]))
    assert r.decision is PolicyDecision.DENIED and "venue_not_allowed" in r.reason_codes


def test_market_not_allowed_denies() -> None:
    r = evaluate(_ctx(market_key="ML|home"), _env(market_allowlist=["OU|2.5|full"]))
    assert r.decision is PolicyDecision.DENIED and "market_not_allowed" in r.reason_codes


def test_slippage_over_max_denies() -> None:
    r = evaluate(_ctx(slippage_bps=500), _env(max_slippage_bps=100))
    assert r.decision is PolicyDecision.DENIED and "slippage_over_max" in r.reason_codes


def test_price_over_max_denies() -> None:
    r = evaluate(_ctx(price=9.0), _env(max_price=3.0))
    assert r.decision is PolicyDecision.DENIED and "price_over_max" in r.reason_codes


def test_stake_over_max_denies() -> None:
    r = evaluate(_ctx(stake=250.0), _env(max_stake=100.0))
    assert r.decision is PolicyDecision.DENIED and "stake_over_max" in r.reason_codes


def test_cooldown_active_denies() -> None:
    r = evaluate(_ctx(seconds_since_last_order=2), _env(cooldown_s=30))
    assert r.decision is PolicyDecision.DENIED and "cooldown_active" in r.reason_codes


def test_cooldown_inactive_when_no_prior_order() -> None:
    # No prior order in this run -> cooldown must NOT fire even with a positive cooldown_s.
    r = evaluate(_ctx(seconds_since_last_order=None), _env(cooldown_s=30))
    assert r.decision is PolicyDecision.APPROVED


def test_not_eligible_denies() -> None:
    r = evaluate(_ctx(agent_eligible=False), _env())
    assert r.decision is PolicyDecision.DENIED and "agent_not_eligible" in r.reason_codes


def test_over_threshold_requires_human() -> None:
    # max_stake raised above the stake so the ONLY relevant gate is the human threshold;
    # otherwise stake_over_max (a hard reason) would correctly deny first.
    r = evaluate(_ctx(stake=2000.0), _env(human_approval_threshold=1000.0, max_stake=5000.0))
    assert r.decision is PolicyDecision.REQUIRES_HUMAN


def test_hard_reason_wins_over_threshold() -> None:
    # A stake at/above threshold that ALSO trips a hard rule must DENY, not escalate.
    r = evaluate(
        _ctx(stake=2000.0, venue="polymarket"),
        _env(human_approval_threshold=1000.0, max_stake=5000.0),
    )
    assert r.decision is PolicyDecision.DENIED and "venue_not_allowed" in r.reason_codes


def test_deny_by_default_collects_all_reasons() -> None:
    r = evaluate(_ctx(recomputed_edge_bps=10, quote_age_s=99, venue="x"), _env())
    assert r.decision is PolicyDecision.DENIED and {
        "edge_below_min",
        "quote_stale",
        "venue_not_allowed",
    } <= set(r.reason_codes)


def test_policy_hash_deterministic() -> None:
    assert _env().policy_hash() == _env().policy_hash()


def test_result_carries_policy_hash() -> None:
    env = _env()
    r = evaluate(_ctx(), env)
    assert r.policy_hash == env.policy_hash()


def test_policy_import_audit_clean() -> None:
    assert_no_llm_imports(Path("veridex/policy"))
