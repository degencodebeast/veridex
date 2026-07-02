"""Polymarket preflight + two-phase gate tests (REQ-2D-403, AC-2D-403) — TDD.

The preflight is PURE orchestration over INJECTED fake clients (no network): each REQ-2D-403
precondition is a NAMED check with ``ok`` / ``detail`` — market exists + resolver maps it,
liquidity at acceptable slippage for the capped size, funded wallet + USDC + CTF approvals
(exchange and neg-risk exchange), correct sig_type, egress reachable, kill switch not engaged,
DRY_RUN state. The 1-share FAK smoke is NOT run here — it is recorded as an operator-pending
check (``ok=None``).

Two safety properties are call-counted against fakes (AC-2D-403):

* a failing CHEAP precondition short-circuits BEFORE the liquidity quote (zero quote I/O);
* driven through the real two-phase gate, a cheap deny happens PRE-quote (zero quote calls) and a
  liquidity deny happens POST-quote (zero submit calls).
"""

from __future__ import annotations

from typing import Any

from veridex.policy.engine import PolicyDecision
from veridex.policy.envelope import PolicyEnvelope
from veridex.policy.gate import (
    PostQuoteContext,
    PreQuoteContext,
    evaluate_post_quote,
    evaluate_pre_quote,
)
from veridex.venues.base import Quote
from veridex.venues.polymarket_preflight import PreflightReport, run_preflight
from veridex.venues.polymarket_resolver import ResolvedMarket

# ---------------------------------------------------------------------------
# Fakes (no network)
# ---------------------------------------------------------------------------

_RESOLVED = ResolvedMarket(condition_id="0xcond", token_id_yes="111", token_id_no="222", tick_size=0.01)


class _FakeQuoteAdapter:
    """Records the ``for_size`` it was quoted for (quote-size coupling) and returns a fixed quote."""

    def __init__(self, quote: Quote) -> None:
        self._quote = quote
        self.calls: list[str] = []
        self.for_sizes: list[float | None] = []

    async def quote_market(self, market_ref: str, for_size: float | None = None) -> Quote:
        self.calls.append(market_ref)
        self.for_sizes.append(for_size)
        return self._quote


class _FakeBalances:
    """Fake matching the PRODUCTION ``get_balance_allowance`` surface exactly: it keys only on
    ``asset_type`` (COLLATERAL / CONDITIONAL). It has NO neg-risk selector — the vendored client has
    none either, so the fake must not invent a capability the real client lacks (that phantom is
    what would mask the neg-risk false-green)."""

    def __init__(self, *, collateral: dict[str, Any], conditional: dict[str, Any]) -> None:
        self._collateral = collateral
        self._conditional = conditional

    async def get_balance_allowance(
        self,
        asset_type: str,
        token_id: str | None = None,
        signature_type: int = -1,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return self._collateral if asset_type == "COLLATERAL" else self._conditional


class _FakeEgress:
    def __init__(self, ok: bool) -> None:
        self._ok = ok

    async def reachable(self) -> bool:
        return self._ok


class _CountingAdapter:
    """Counts quote_market / submit_order calls to prove the two-phase gate ordering."""

    def __init__(self, quote: Quote) -> None:
        self._quote = quote
        self.quote_calls = 0
        self.submit_calls = 0

    async def quote_market(self, market_ref: str, for_size: float | None = None) -> Quote:
        self.quote_calls += 1
        return self._quote

    async def submit_order(self, order: Any) -> Any:
        self.submit_calls += 1
        return None


def _quote(*, size: float, price: float, native: float) -> Quote:
    return Quote(market_ref="ref", price=price, native_price=native, size=size, for_size=size, ts=0)


def _envelope(*, kill_switch: bool = False) -> PolicyEnvelope:
    return PolicyEnvelope(
        max_stake=1000.0,
        max_orders_per_run=100,
        max_orders_per_session=100,
        max_orders_per_day=100,
        venue_allowlist=["polymarket"],
        market_allowlist=["ref"],
        min_edge_bps=0,
        max_slippage_bps=500,
        max_price=100.0,
        max_quote_age_s=60,
        cooldown_s=0,
        human_approval_threshold=1_000_000.0,
        kill_switch=kill_switch,
    )


def _healthy_balances() -> _FakeBalances:
    return _FakeBalances(
        collateral={"balance": 100.0, "allowance": 100.0},
        conditional={"balance": 5.0, "allowance": 5.0},
    )


async def _run(
    *,
    resolved: ResolvedMarket | None = _RESOLVED,
    quote_adapter: _FakeQuoteAdapter | None = None,
    balances: _FakeBalances | None = None,
    egress: _FakeEgress | None = None,
    envelope: PolicyEnvelope | None = None,
    order_size: float = 10.0,
    required_usdc: float = 10.0,
    actual_sig_type: int = 2,
    expected_sig_type: int = 2,
    max_slippage_bps: int = 500,
    reference_price: float | None = 1.90,
) -> tuple[PreflightReport, _FakeQuoteAdapter]:
    adapter = quote_adapter or _FakeQuoteAdapter(_quote(size=50.0, price=1.90, native=0.526))
    report = await run_preflight(
        market_ref="ref",
        order_size=order_size,
        required_usdc=required_usdc,
        resolved=resolved,
        quote_adapter=adapter,
        balances=balances or _healthy_balances(),
        egress=egress or _FakeEgress(True),
        envelope=envelope or _envelope(),
        actual_sig_type=actual_sig_type,
        expected_sig_type=expected_sig_type,
        max_slippage_bps=max_slippage_bps,
        reference_price=reference_price,
        dry_run=True,
    )
    return report, adapter


def _by_name(report: PreflightReport) -> dict[str, Any]:
    return {c.name: c for c in report.checks}


# ---------------------------------------------------------------------------
# Fully-passing preflight
# ---------------------------------------------------------------------------


async def test_preflight_all_pass_ok_true_operator_smoke_pending() -> None:
    report, adapter = await _run()

    assert report.ok is True
    checks = _by_name(report)
    assert checks["market_mapped"].ok is True
    assert checks["liquidity"].ok is True
    assert checks["wallet_funded_usdc"].ok is True
    assert checks["usdc_allowance"].ok is True
    assert checks["ctf_approval_exchange"].ok is True
    # neg-risk exchange approval is NOT offline-verifiable — operator-verify, never a boolean pass.
    assert checks["ctf_approval_neg_risk"].ok is None
    assert checks["sig_type"].ok is True
    assert checks["egress_reachable"].ok is True
    assert checks["kill_switch_ready"].ok is True
    # the live 1-share FAK smoke is an OPERATOR step — recorded pending, never auto-run.
    assert checks["operator_fak_smoke"].ok is None
    # quote-size coupling: the liquidity quote was priced for the ORDER size, not a default.
    assert adapter.for_sizes == [10.0]


# ---------------------------------------------------------------------------
# Each precondition fails NAMED with ok=False + detail
# ---------------------------------------------------------------------------


async def test_unmapped_market_fails_named() -> None:
    report, adapter = await _run(resolved=None)
    assert report.ok is False
    assert _by_name(report)["market_mapped"].ok is False
    assert _by_name(report)["market_mapped"].detail  # human-readable reason


async def test_unfunded_wallet_fails_named() -> None:
    balances = _FakeBalances(
        collateral={"balance": 0.0, "allowance": 100.0},
        conditional={"balance": 5.0, "allowance": 5.0},
    )
    report, _ = await _run(balances=balances)
    assert report.ok is False
    assert _by_name(report)["wallet_funded_usdc"].ok is False


async def test_missing_usdc_allowance_fails_named() -> None:
    balances = _FakeBalances(
        collateral={"balance": 100.0, "allowance": 0.0},
        conditional={"balance": 5.0, "allowance": 5.0},
    )
    report, _ = await _run(balances=balances)
    assert report.ok is False
    assert _by_name(report)["usdc_allowance"].ok is False


async def test_missing_ctf_approval_fails_named() -> None:
    balances = _FakeBalances(
        collateral={"balance": 100.0, "allowance": 100.0},
        conditional={"balance": 5.0, "allowance": 0.0},
    )
    report, _ = await _run(balances=balances)
    assert report.ok is False
    assert _by_name(report)["ctf_approval_exchange"].ok is False


async def test_neg_risk_approval_is_operator_verify_not_a_boolean_pass() -> None:
    """The neg-risk exchange approval CANNOT be verified offline (the vendored balance-allowance
    surface has no neg-risk selector), so it must be reported ok=None — never a fabricated boolean
    pass that would merely mirror the regular-exchange approval. Being ok=None, it neither fails the
    report on its own nor claims a pass the code can't back."""
    report, _ = await _run()  # everything else healthy

    check = _by_name(report)["ctf_approval_neg_risk"]
    assert check.ok is None  # operator-verify, not True and not False
    assert "operator-verify" in check.detail.lower()
    assert "neg-risk" in check.detail.lower()
    # ok=None does not drag the overall verdict down when every boolean check passed.
    assert report.ok is True


async def test_thin_liquidity_fails_named() -> None:
    thin = _FakeQuoteAdapter(_quote(size=3.0, price=1.90, native=0.526))  # only 3 of 10 fillable
    report, _ = await _run(quote_adapter=thin, order_size=10.0)
    assert report.ok is False
    assert _by_name(report)["liquidity"].ok is False


async def test_excess_slippage_fails_named() -> None:
    # quote decimal 3.0 vs reference 1.90 -> huge slippage, over a tight cap.
    wide = _FakeQuoteAdapter(_quote(size=50.0, price=3.0, native=0.333))
    report, _ = await _run(quote_adapter=wide, max_slippage_bps=100, reference_price=1.90)
    assert report.ok is False
    assert _by_name(report)["liquidity"].ok is False


async def test_wrong_sig_type_fails_named() -> None:
    report, _ = await _run(actual_sig_type=1, expected_sig_type=2)
    assert report.ok is False
    assert _by_name(report)["sig_type"].ok is False


async def test_egress_unreachable_fails_named() -> None:
    report, _ = await _run(egress=_FakeEgress(False))
    assert report.ok is False
    assert _by_name(report)["egress_reachable"].ok is False


async def test_kill_switch_engaged_fails_named() -> None:
    report, _ = await _run(envelope=_envelope(kill_switch=True))
    assert report.ok is False
    assert _by_name(report)["kill_switch_ready"].ok is False


# ---------------------------------------------------------------------------
# Cheap failure short-circuits the liquidity quote (zero quote I/O)
# ---------------------------------------------------------------------------


async def test_cheap_precondition_failure_skips_liquidity_quote() -> None:
    """A cheap deny (kill switch) must NOT issue the liquidity quote — zero quote I/O."""
    report, adapter = await _run(envelope=_envelope(kill_switch=True))

    assert adapter.calls == []  # the depth quote was never requested
    assert _by_name(report)["kill_switch_ready"].ok is False
    assert _by_name(report)["liquidity"].ok is None  # skipped, not evaluated


# ---------------------------------------------------------------------------
# Two-phase gate ordering, call-counted (AC-2D-403)
# ---------------------------------------------------------------------------


def _pre_ctx(*, stake: float = 10.0) -> PreQuoteContext:
    return PreQuoteContext(
        recomputed_edge_bps=100,
        stake=stake,
        venue="polymarket",
        market_key="ref",
        orders_this_run=0,
        seconds_since_last_order=None,
        agent_eligible=True,
    )


async def test_two_phase_cheap_fail_denies_pre_quote_zero_quote_calls() -> None:
    """A cheap precondition (kill switch) denies at the PRE-quote gate — zero quote/submit calls."""
    adapter = _CountingAdapter(_quote(size=50.0, price=1.90, native=0.526))
    envelope = _envelope(kill_switch=True)

    pre = evaluate_pre_quote(_pre_ctx(), envelope)
    # A correct orchestration only quotes when the pre-quote gate approves.
    if pre.decision == PolicyDecision.APPROVED:
        await adapter.quote_market("ref", for_size=10.0)

    assert pre.decision == PolicyDecision.DENIED
    assert adapter.quote_calls == 0  # denied BEFORE any quote I/O
    assert adapter.submit_calls == 0


async def test_two_phase_liquidity_fail_denies_post_quote_zero_submit_calls() -> None:
    """A thin-book liquidity fail denies at the POST-quote gate — the quote ran, submit never does."""
    thin = _quote(size=5.0, price=1.90, native=0.526)  # only 5 fillable, stake is 10
    adapter = _CountingAdapter(thin)
    envelope = _envelope()

    pre = evaluate_pre_quote(_pre_ctx(stake=10.0), envelope)
    assert pre.decision == PolicyDecision.APPROVED

    quote = await adapter.quote_market("ref", for_size=10.0)  # priced for the order size
    post = evaluate_post_quote(
        PostQuoteContext(
            executable_edge_bps=100,
            price=quote.price,
            slippage_bps=0,
            quote_age_s=0,
            stake=10.0,
            quoted_size=quote.size,  # 5 < 10 -> insufficient liquidity
        ),
        envelope,
    )
    if post.decision == PolicyDecision.APPROVED:
        await adapter.submit_order(object())

    assert post.decision == PolicyDecision.DENIED
    assert adapter.quote_calls == 1  # the quote DID run (quote-size coupled)
    assert adapter.submit_calls == 0  # but submit NEVER did
