"""R4-A mechanical fixed-fraction dust sizing (GUD-001, AC-024; Codex-M5).

The executable dust ``size`` R4-A puts on the wire is a DETERMINISTIC, mechanical
fixed-fraction of the pinned wallet equity, then clamped by the manifest/policy caps:

    unit_size = fixed_fraction * wallet_equity_at_decision
    size      = min(unit_size, max_notional, max_per_order)

It is NEVER raw Kelly, and NEVER a discretionary/confidence-weighted term (GUD-001).
``fixed_fraction`` and ``wallet_equity_at_decision`` are PINNED inputs — bound into the
manifest/session hash (E1-T2), not agent-supplied. The signature is deliberately the four
keyword-only pinned parameters and NOTHING else: there is no ``confidence`` / ``size`` /
``requested_size`` parameter, so an R4-B/agent-supplied size or confidence cannot reach this
computation. An agent value can only cap the executable size further downstream — never
raise it. (The runner-binding proof, that the runner sources its wire size ONLY from this
function, is owned by E6-T4.)

This module imports only the standard library — no ranked-lane and no ``live_recorder``
dependency (SEC-003).
"""

from __future__ import annotations

import math

__all__ = ["resolve_dust_size"]


def resolve_dust_size(
    *,
    fixed_fraction: float,
    wallet_equity_at_decision: float,
    max_notional: float,
    max_per_order: float,
) -> float:
    """Return the deterministic mechanical fixed-fraction dust size (GUD-001).

    Computes ``fixed_fraction * wallet_equity_at_decision`` and clamps it by both the
    manifest ``max_notional`` and the per-order ``max_per_order`` cap. Pure and
    deterministic: identical inputs always yield an identical size, with no randomness,
    no Kelly, and no confidence/discretionary term.

    Args:
        fixed_fraction: Pinned fraction of wallet equity to risk per unit (``>= 0``);
            bound into the manifest/session hash (E1-T2), not agent-supplied.
        wallet_equity_at_decision: Pinned wallet equity at decision time (``>= 0``).
        max_notional: Manifest notional cap for the size (``>= 0``).
        max_per_order: Per-order cap for the size (``>= 0``).

    Returns:
        The executable dust size: ``min(fixed_fraction * wallet_equity_at_decision,
        max_notional, max_per_order)``.

    Raises:
        ValueError: Any input is non-finite (``nan``/``inf``) or negative — fail closed
            rather than emit a nonsensical size.
    """
    for name, value in (
        ("fixed_fraction", fixed_fraction),
        ("wallet_equity_at_decision", wallet_equity_at_decision),
        ("max_notional", max_notional),
        ("max_per_order", max_per_order),
    ):
        if not math.isfinite(value) or value < 0.0:
            raise ValueError(f"{name} must be a finite non-negative number, got {value!r}")

    unit_size = fixed_fraction * wallet_equity_at_decision
    return min(unit_size, max_notional, max_per_order)
