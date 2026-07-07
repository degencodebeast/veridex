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

from pydantic import BaseModel

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
    is typed literally ``None`` and can NEVER hold a value at R1.5.
    """

    trade_flow_preceding_fv_move_bps_diagnostic: int | None = None
    toxic_vs_benign_flow_ratio_diagnostic: float | None = None
    trades_near_quote_count: int = 0
    # Typed literally ``None`` (NOT ``int | None``): structurally impossible to
    # set to a value, enforcing AC-018's no-executable-edge boundary at R1.5.
    real_executable_edge_bps: None = None
