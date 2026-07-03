"""Two-phase policy gate — pre-quote (no I/O) then post-quote (price-dependent)."""

from __future__ import annotations

from pathlib import Path

from veridex.policy.circuit_breaker import CircuitBreaker, CircuitState
from veridex.policy.engine import PolicyDecision
from veridex.policy.envelope import PolicyEnvelope
from veridex.policy.gate import (
    PostQuoteContext,
    PreQuoteContext,
    evaluate_post_quote,
    evaluate_pre_quote,
)
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
    return PolicyEnvelope(**base)  # type: ignore[arg-type]


def _pre(**kw: object) -> PreQuoteContext:
    base: dict[str, object] = {
        "recomputed_edge_bps": 120,
        "stake": 50.0,
        "venue": "sx_bet",
        "market_key": "OU|2.5|full",
        "orders_this_run": 0,
        "seconds_since_last_order": None,
        "agent_eligible": True,
    }
    base.update(kw)
    return PreQuoteContext(**base)  # type: ignore[arg-type]


def _post(**kw: object) -> PostQuoteContext:
    base: dict[str, object] = {
        "executable_edge_bps": 120,
        "price": 2.0,
        "slippage_bps": 10,
        "quote_age_s": 1,
        "stake": 50.0,
    }
    base.update(kw)
    return PostQuoteContext(**base)  # type: ignore[arg-type]


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
    assert_no_llm_imports(Path("veridex/policy"))


# --- Guardrail lift (REQ-2D-404/405): circuit breaker + liquidity + live-guarded cap ---------


def test_pre_quote_open_breaker_denies_circuit_open() -> None:
    """A cheap PRE-quote precondition: an OPEN breaker denies BEFORE any venue I/O."""
    r = evaluate_pre_quote(_pre(breaker=CircuitBreaker(state=CircuitState.OPEN, opened_at=0.0)), _env())
    assert r.decision is PolicyDecision.DENIED
    assert "circuit_open" in r.reason_codes


def test_pre_quote_half_open_allows_single_probe() -> None:
    """HALF_OPEN admits exactly one probe: unused -> approved, dispatched -> denied."""
    half = CircuitBreaker(state=CircuitState.HALF_OPEN)
    assert evaluate_pre_quote(_pre(breaker=half), _env()).decision is PolicyDecision.APPROVED
    used = half.start_probe()
    denied = evaluate_pre_quote(_pre(breaker=used), _env())
    assert denied.decision is PolicyDecision.DENIED and "circuit_open" in denied.reason_codes


def test_pre_quote_closed_breaker_is_transparent() -> None:
    """A CLOSED breaker (the default) never contributes a reason code."""
    r = evaluate_pre_quote(_pre(breaker=CircuitBreaker()), _env())
    assert r.decision is PolicyDecision.APPROVED and "circuit_open" not in r.reason_codes


def test_pre_quote_live_guarded_cap_denies() -> None:
    """The tighter live-money cap denies only when the action is live-guarded."""
    over = _pre(stake=250.0, live_guarded=True)
    r = evaluate_pre_quote(over, _env(max_stake=1000.0, max_stake_live_guarded=200.0))
    assert r.decision is PolicyDecision.DENIED and "stake_over_live_guarded" in r.reason_codes


def test_pre_quote_live_guarded_cap_not_applied_off_live() -> None:
    """Off the live path (default), the live-guarded cap is inert."""
    ok = _pre(stake=250.0, live_guarded=False)
    r = evaluate_pre_quote(ok, _env(max_stake=1000.0, max_stake_live_guarded=200.0))
    assert "stake_over_live_guarded" not in r.reason_codes


def test_post_quote_thin_book_denies_insufficient_liquidity() -> None:
    """A quote-dependent precondition: quoted size below the intended fill denies POST-quote."""
    r = evaluate_post_quote(_post(stake=100.0, quoted_size=40.0), _env())
    assert r.decision is PolicyDecision.DENIED and "insufficient_liquidity" in r.reason_codes


def test_post_quote_deep_book_approves() -> None:
    """Ample book depth (quoted size >= intended fill) does not trip the liquidity rule."""
    r = evaluate_post_quote(_post(stake=100.0, quoted_size=500.0), _env())
    assert r.decision is PolicyDecision.APPROVED and "insufficient_liquidity" not in r.reason_codes


def test_runner_has_no_second_authority() -> None:
    """Single-authority invariant: the runner delegates ALL gating to the policy two-phase gate.

    The breaker's allow/deny decision belongs to the gate, not the runner: the runner never
    calls ``.allows()`` and never manufactures the ``circuit_open`` reason itself. It consults
    ONLY ``evaluate_pre_quote`` / ``evaluate_post_quote`` -- there is no parallel guardrail gate.
    """
    src = Path("veridex/execution/runner.py").read_text(encoding="utf-8")
    assert "evaluate_pre_quote" in src and "evaluate_post_quote" in src
    assert ".allows(" not in src  # the gate owns the breaker verdict, not the runner
    assert "circuit_open" not in src  # the deny reason is minted inside the gate only
