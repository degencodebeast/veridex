"""Aegis two-phase policy gate — pre-quote (no I/O) then post-quote (price-dependent)."""
from __future__ import annotations

from veridex.policy.engine import PolicyDecision
from veridex.policy.envelope import PolicyEnvelope
from veridex.policy.gate import (
    PostQuoteContext,
    PreQuoteContext,
    evaluate_post_quote,
    evaluate_pre_quote,
)


def _env(**kw):
    base = dict(
        max_stake=100.0, max_orders_per_run=5, max_orders_per_session=20, max_orders_per_day=50,
        venue_allowlist=["sx_bet"], market_allowlist=["OU|2.5|full"], min_edge_bps=50,
        max_slippage_bps=100, max_price=3.0, max_quote_age_s=10, cooldown_s=0,
        human_approval_threshold=1000.0, kill_switch=False,
    )
    base.update(kw)
    return PolicyEnvelope(**base)


def _pre(**kw):
    base = dict(recomputed_edge_bps=120, stake=50.0, venue="sx_bet", market_key="OU|2.5|full",
                orders_this_run=0, seconds_since_last_order=None, agent_eligible=True)
    base.update(kw)
    return PreQuoteContext(**base)


def _post(**kw):
    base = dict(executable_edge_bps=120, price=2.0, slippage_bps=10, quote_age_s=1, stake=50.0)
    base.update(kw)
    return PostQuoteContext(**base)


def test_pre_quote_clean_approves() -> None:
    assert evaluate_pre_quote(_pre(), _env()).decision is PolicyDecision.APPROVED


def test_pre_quote_kill_switch_denies() -> None:
    r = evaluate_pre_quote(_pre(), _env(kill_switch=True))
    assert r.decision is PolicyDecision.DENIED and "kill_switch_on" in r.reason_codes


def test_pre_quote_collects_all_cheap_reasons() -> None:
    r = evaluate_pre_quote(_pre(venue="x", recomputed_edge_bps=10, agent_eligible=False), _env())
    assert {"venue_not_allowed", "edge_below_min", "agent_not_eligible"} <= set(r.reason_codes)


def test_post_quote_stale_quote_denies() -> None:
    r = evaluate_post_quote(_post(quote_age_s=60), _env(max_quote_age_s=10))
    assert r.decision is PolicyDecision.DENIED and "quote_stale" in r.reason_codes


def test_post_quote_slippage_denies() -> None:
    r = evaluate_post_quote(_post(slippage_bps=500), _env(max_slippage_bps=100))
    assert r.decision is PolicyDecision.DENIED and "slippage_over_max" in r.reason_codes


def test_post_quote_executable_edge_decayed_denies() -> None:
    # edge at the actual price fell below min — the inert-gate fix in action.
    r = evaluate_post_quote(_post(executable_edge_bps=10), _env(min_edge_bps=50))
    assert r.decision is PolicyDecision.DENIED and "edge_below_min" in r.reason_codes


def test_post_quote_over_threshold_requires_human() -> None:
    r = evaluate_post_quote(_post(stake=2000.0), _env(human_approval_threshold=1000.0, max_stake=5000.0))
    assert r.decision is PolicyDecision.REQUIRES_HUMAN


def test_gate_import_audit_clean() -> None:
    from pathlib import Path
    from veridex.verifier.import_audit import assert_no_llm_imports
    assert_no_llm_imports(Path("veridex/policy"))
