"""MM-R1.5 adverse-selection diagnostics — the HARD no-fill boundary.

MM-R1.5 joins real on-chain trade prints to measure trade-aware adverse
selection. Critically, those trades are venue trades between OTHER parties —
they are NEVER Veridex fills. Therefore the R1.5 report MUST carry NO
fill / fill-rate / spread-capture / PnL / executable-edge field, and
``real_executable_edge_bps`` is typed literally ``None`` so it can never be
set to a value (AC-018).

Every trade-DERIVED metric is a diagnostic and MUST be ``_diagnostic``-suffixed
so it can never masquerade as a fill-based cost.
"""

from __future__ import annotations

from collections.abc import Callable
from statistics import mean
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from veridex.maker.trades import AggressorSide

if TYPE_CHECKING:
    from veridex.maker.trades import TradePrint

#: Fields that would (incorrectly) imply Veridex executed a fill at R1.5.
#: The report model must never carry any of these.
FORBIDDEN_FILL_FIELDS: frozenset[str] = frozenset(
    {
        "fill_price",
        "fill_rate",
        "spread_capture",
        "pnl",
        "realized_pnl",
        "executed_spread",
    }
)


class AdverseSelectionReport(BaseModel):
    """R1.5 trade-aware adverse-selection report — hard no-fill boundary.

    All fields default so ``AdverseSelectionReport()`` constructs cleanly.
    Trade-derived metrics are ``_diagnostic``-suffixed; ``real_executable_edge_bps``
    is typed literally ``None`` and can NEVER hold a value at R1.5. The model is
    ``frozen`` (post-construction mutation raises ``ValidationError``) and forbids
    extra fields (a smuggled fill kwarg is rejected loudly), matching
    :class:`~veridex.maker.trades.TradePrint`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    trade_flow_preceding_fv_move_bps_diagnostic: int | None = None
    toxic_vs_benign_flow_ratio_diagnostic: float | None = None
    trades_near_quote_count: int = 0
    # Typed literally ``None`` (NOT ``int | None``) AND frozen: structurally
    # impossible to set to a value, enforcing AC-018's no-executable-edge
    # boundary at R1.5.
    real_executable_edge_bps: None = None


def compute_trade_aware_diagnostic(
    trades: list["TradePrint"],
    fv_at: Callable[[int], float | None],
    quote_price: float,
    *,
    window_s: int = 120,
    near_band: float = 0.10,
) -> AdverseSelectionReport:
    """Measure trade-aware adverse selection from venue trades near a quote.

    Venue trades are trades between OTHER parties — NEVER Veridex fills. This
    diagnostic asks: when trade flow (aggressor buys/sells) PRECEDES a TxLINE
    fair-value move, did fair value move in the aggressor's favor? If so, the
    aggressors were informed (toxic to a maker who took the other side). It is a
    DIAGNOSTIC only: ``real_executable_edge_bps`` stays ``None`` and no
    fill / edge / PnL is claimed — trades are an independent reference, never
    evidence that our quote filled.

    Args:
        trades: Venue trade prints (never our fills).
        fv_at: TxLINE fair-value lookup by timestamp; returns ``None`` if the
            fair value is unavailable at that timestamp.
        quote_price: The maker quote price to measure trade flow around.
        window_s: Forward horizon (seconds) over which the post-trade fair-value
            move is measured.
        near_band: Half-width (native prob units) of the "near the quote" band.

    Returns:
        An :class:`AdverseSelectionReport` carrying only ``_diagnostic``-suffixed
        trade-derived metrics plus ``trades_near_quote_count``.
        ``real_executable_edge_bps`` is never set (stays ``None``).
    """
    near_trades = [t for t in trades if abs(t.price - quote_price) <= near_band]

    contributions: list[float] = []
    for t in near_trades:
        fv_now = fv_at(t.ts)
        fv_after = fv_at(t.ts + window_s)
        if fv_now is None or fv_after is None:
            continue
        sign = 1.0 if t.aggressor_side is AggressorSide.BUY else -1.0
        contributions.append(sign * (fv_after - fv_now) * 1e4)

    flow_bps: int | None = round(mean(contributions)) if contributions else None

    ratio: float | None
    if contributions:
        toxic = sum(1 for c in contributions if c > 0)
        benign = sum(1 for c in contributions if c <= 0)
        ratio = toxic / max(1, benign)
    else:
        # Abstain in lockstep with the flow diagnostic: when there are near
        # trades but NONE have a resolvable fv-after, the ratio is ``None``
        # (unknown) rather than ``0.0`` (falsely "all benign").
        ratio = None

    return AdverseSelectionReport(
        trade_flow_preceding_fv_move_bps_diagnostic=flow_bps,
        toxic_vs_benign_flow_ratio_diagnostic=ratio,
        trades_near_quote_count=len(near_trades),
    )
