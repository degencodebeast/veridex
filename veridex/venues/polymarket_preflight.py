"""Polymarket write-path preflight (REQ-2D-403, AC-2D-403).

Before a real mainnet order, every REQ-2D-403 precondition is checked as a NAMED item with an
``ok`` verdict and a human-readable ``detail``, so an operator (or the T20 execution lane) can see
exactly what is/ isn't ready:

* ``market_mapped`` — the market resolved to concrete Polymarket identifiers.
* ``liquidity`` — the book fills the capped order size at acceptable slippage (quote-size coupled).
* ``wallet_funded_usdc`` / ``usdc_allowance`` — funded wallet + USDC allowance to the exchange.
* ``ctf_approval_exchange`` / ``ctf_approval_neg_risk`` — CTF (ERC-1155) approvals on BOTH exchanges.
* ``sig_type`` — the signer's signature type matches the wallet kind.
* ``egress_reachable`` — the Cloudflare-fronted CLOB egress is reachable.
* ``kill_switch_ready`` — the policy kill switch is NOT engaged.
* ``dry_run_state`` — records the DRY_RUN / write-enabled posture (informational).
* ``operator_fak_smoke`` — the live 1-share FAK smoke is an OPERATOR step, recorded PENDING
  (``ok=None``); it is NEVER auto-run here.

PURE orchestration over INJECTED clients — tests inject fakes, no network. Trust ordering
(AC-2D-403): the cheap deterministic checks run first; the single quote-dependent check
(``liquidity``) is SKIPPED (``ok=None``) when any cheap check fails, so a cheap failure issues ZERO
quote I/O. The report's overall ``ok`` is the AND of every check with a boolean verdict — the
operator-pending smoke (``ok=None``) does not fail the report.

The two-phase policy gate (:mod:`veridex.policy.gate`) remains the SINGLE execution authority: this
preflight is an operator readiness report over the SAME facts, not a second gate. Wiring it (and the
circuit breaker) into the live runner is the T20 seam.

Offline-safe import: no vendored/httpx/aiohttp imports here — the injected clients own any I/O.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel

from veridex.policy.envelope import PolicyEnvelope
from veridex.venues.base import Quote
from veridex.venues.polymarket_resolver import ResolvedMarket

# Polymarket USDC collateral is 6-decimal; the balance-allowance endpoint returns raw values in the
# same unit the caller states ``required_usdc`` in, so comparisons are unit-consistent by contract.

# ---------------------------------------------------------------------------
# Injected client protocols (tests inject fakes; the live path injects the vendored client)
# ---------------------------------------------------------------------------


class QuoteCapable(Protocol):
    """A quote source that supports quote-size coupling (``for_size``)."""

    async def quote_market(self, market_ref: str, for_size: float | None = None) -> Quote:
        """Return a depth-aware quote priced for ``for_size`` shares."""
        ...


class BalanceAllowanceClient(Protocol):
    """The vendored-shaped balance/allowance surface (``get_balance_allowance``)."""

    async def get_balance_allowance(
        self,
        asset_type: str,
        token_id: str | None = None,
        signature_type: int = -1,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Return ``{"balance": ..., "allowance": ...}`` for the asset (COLLATERAL / CONDITIONAL)."""
        ...


class EgressProbe(Protocol):
    """A reachability probe for the Cloudflare-fronted CLOB egress."""

    async def reachable(self) -> bool:
        """Return ``True`` if the production egress is reachable."""
        ...


# ---------------------------------------------------------------------------
# Report value types
# ---------------------------------------------------------------------------


class PreflightCheck(BaseModel):
    """One NAMED precondition verdict.

    Attributes:
        name: Stable check identifier (e.g. ``"liquidity"``).
        ok: ``True`` pass, ``False`` fail, or ``None`` for an operator-pending / skipped check that
            does not contribute to the report verdict.
        detail: Human-readable explanation of the verdict.
    """

    name: str
    ok: bool | None
    detail: str


class PreflightReport(BaseModel):
    """The full preflight verdict.

    Attributes:
        ok: ``True`` only when EVERY boolean-verdict check passed; operator-pending checks
            (``ok=None``) are excluded from the AND.
        checks: The ordered list of named checks.
    """

    ok: bool
    checks: list[PreflightCheck]


# ---------------------------------------------------------------------------
# Pure check helpers
# ---------------------------------------------------------------------------


def _amount(record: Any, key: str) -> float:
    """Parse a numeric ``balance`` / ``allowance`` field from a balance-allowance record; 0.0 if absent."""
    if not isinstance(record, dict):
        return 0.0
    try:
        return float(record.get(key, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _check_liquidity(
    quote: Quote,
    order_size: float,
    *,
    max_slippage_bps: int,
    reference_price: float | None,
) -> PreflightCheck:
    """Verify the book fills ``order_size`` at an executable price within the slippage cap."""
    if quote.size + 1e-9 < order_size:
        return PreflightCheck(
            name="liquidity",
            ok=False,
            detail=f"book fills only {quote.size:g} of {order_size:g} shares at the quoted depth",
        )
    if quote.price <= 1.0:
        return PreflightCheck(
            name="liquidity", ok=False, detail="no executable price at the quoted depth (price <= 1.0)"
        )
    if reference_price is not None and reference_price > 0.0:
        slippage_bps = abs(quote.price - reference_price) / reference_price * 10_000.0
        if slippage_bps > max_slippage_bps:
            return PreflightCheck(
                name="liquidity",
                ok=False,
                detail=f"slippage {slippage_bps:.0f}bps exceeds max {max_slippage_bps}bps "
                f"(quote {quote.price:g} vs reference {reference_price:g})",
            )
    return PreflightCheck(
        name="liquidity",
        ok=True,
        detail=f"fills {order_size:g} shares at decimal {quote.price:g} within {max_slippage_bps}bps",
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def run_preflight(
    *,
    market_ref: str,
    order_size: float,
    required_usdc: float,
    resolved: ResolvedMarket | None,
    quote_adapter: QuoteCapable,
    balances: BalanceAllowanceClient,
    egress: EgressProbe,
    envelope: PolicyEnvelope,
    actual_sig_type: int,
    expected_sig_type: int = 2,
    max_slippage_bps: int = 100,
    reference_price: float | None = None,
    dry_run: bool = True,
) -> PreflightReport:
    """Run every REQ-2D-403 precondition over the injected clients and return a named report.

    The cheap deterministic checks run first; the quote-dependent ``liquidity`` check is only
    issued when ALL cheap checks pass, so a cheap failure performs ZERO quote I/O (AC-2D-403).

    Args:
        market_ref: The market reference the order targets.
        order_size: Shares the order will submit; the liquidity quote is priced for this (gate B).
        required_usdc: USDC (collateral) the order needs; compared to balance/allowance in the same
            unit the balance-allowance client returns.
        resolved: The resolved market, or ``None`` if resolution failed (``market_mapped`` fails).
        quote_adapter: Injected quote source (supports ``for_size``).
        balances: Injected balance/allowance client (vendored-shaped).
        egress: Injected reachability probe for the CLOB egress.
        envelope: The policy envelope (kill-switch state is read here).
        actual_sig_type: The signer's configured signature type.
        expected_sig_type: The signature type the wallet kind requires (``2`` = browser wallet).
        max_slippage_bps: Slippage cap for the liquidity check.
        reference_price: Decimal-odds reference for slippage; ``None`` skips the slippage sub-check.
        dry_run: The DRY_RUN posture, recorded in ``dry_run_state`` (informational).

    Returns:
        A :class:`PreflightReport` whose ``ok`` is the AND of every boolean-verdict check.
    """
    cheap: list[PreflightCheck] = []

    # --- market mapping ---
    cheap.append(
        PreflightCheck(
            name="market_mapped",
            ok=resolved is not None,
            detail=(
                f"resolved condition_id={resolved.condition_id}" if resolved is not None
                else f"market_ref={market_ref!r} did not resolve to Polymarket identifiers"
            ),
        )
    )

    # --- wallet funding + USDC allowance ---
    collateral = await balances.get_balance_allowance(asset_type="COLLATERAL")
    usdc_balance = _amount(collateral, "balance")
    usdc_allowance = _amount(collateral, "allowance")
    cheap.append(
        PreflightCheck(
            name="wallet_funded_usdc",
            ok=usdc_balance + 1e-9 >= required_usdc,
            detail=f"USDC balance {usdc_balance:g} vs required {required_usdc:g}",
        )
    )
    cheap.append(
        PreflightCheck(
            name="usdc_allowance",
            ok=usdc_allowance + 1e-9 >= required_usdc,
            detail=f"USDC allowance {usdc_allowance:g} vs required {required_usdc:g}",
        )
    )

    # --- CTF (ERC-1155) approvals on BOTH the exchange and the neg-risk exchange ---
    if resolved is not None:
        conditional = await balances.get_balance_allowance(
            asset_type="CONDITIONAL", token_id=resolved.token_id_yes
        )
        neg_risk = await balances.get_balance_allowance(
            asset_type="CONDITIONAL", token_id=resolved.token_id_yes, neg_risk=True
        )
        cheap.append(
            PreflightCheck(
                name="ctf_approval_exchange",
                ok=_amount(conditional, "allowance") > 0.0,
                detail=f"CTF allowance (exchange) {_amount(conditional, 'allowance'):g}",
            )
        )
        cheap.append(
            PreflightCheck(
                name="ctf_approval_neg_risk",
                ok=_amount(neg_risk, "allowance") > 0.0,
                detail=f"CTF allowance (neg-risk exchange) {_amount(neg_risk, 'allowance'):g}",
            )
        )
    else:
        for name in ("ctf_approval_exchange", "ctf_approval_neg_risk"):
            cheap.append(
                PreflightCheck(
                    name=name, ok=False, detail="market unresolved — cannot check CTF token approval"
                )
            )

    # --- signature type ---
    cheap.append(
        PreflightCheck(
            name="sig_type",
            ok=actual_sig_type == expected_sig_type,
            detail=f"sig_type {actual_sig_type} vs expected {expected_sig_type}",
        )
    )

    # --- egress reachability ---
    egress_ok = await egress.reachable()
    cheap.append(
        PreflightCheck(
            name="egress_reachable",
            ok=egress_ok,
            detail="CLOB egress reachable" if egress_ok else "CLOB egress UNREACHABLE",
        )
    )

    # --- kill switch (must NOT be engaged) ---
    cheap.append(
        PreflightCheck(
            name="kill_switch_ready",
            ok=not envelope.kill_switch,
            detail="kill switch engaged — trading halted" if envelope.kill_switch else "kill switch clear",
        )
    )

    all_cheap_ok = all(check.ok for check in cheap)

    # --- liquidity: the ONLY quote-dependent check; skipped (zero quote I/O) on any cheap failure ---
    if all_cheap_ok:
        quote = await quote_adapter.quote_market(market_ref, for_size=order_size)
        liquidity = _check_liquidity(
            quote, order_size, max_slippage_bps=max_slippage_bps, reference_price=reference_price
        )
    else:
        liquidity = PreflightCheck(
            name="liquidity",
            ok=None,
            detail="skipped: a cheap precondition failed, so no quote was issued",
        )

    # --- informational posture + operator-pending smoke ---
    dry_run_state = PreflightCheck(
        name="dry_run_state",
        ok=True,
        detail=f"dry_run={dry_run} (a real submit needs polymarket_write_enabled=true AND dry_run=False)",
    )
    operator_smoke = PreflightCheck(
        name="operator_fak_smoke",
        ok=None,
        detail="pending OPERATOR: run scripts/polymarket_smoke.py with POLYMARKET_SMOKE=yes (not auto-run)",
    )

    checks = [*cheap, liquidity, dry_run_state, operator_smoke]
    overall_ok = all(check.ok for check in checks if check.ok is not None)
    return PreflightReport(ok=overall_ok, checks=checks)
