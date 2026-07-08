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

from pydantic import BaseModel, ConfigDict, Field, model_validator

from veridex.maker.basis import decompose_gap, reach_from_residual
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
    # E4-T1 extended report-only diagnostics (all ``_diagnostic``-suffixed, ``None`` on
    # no resolvable near-trade + fv). None of these is a fill / fill-rate / spread-capture
    # / PnL / executable-edge value; they are independent trade-reference metrics only.
    near_quote_trade_rate_diagnostic: float | None = None
    signed_flow_pressure_bps_diagnostic: int | None = None
    post_trade_fv_markout_bps_diagnostic: int | None = None
    picked_off_pressure_diagnostic: float | None = None
    candidate_vs_naive_toxicity_delta_bps_diagnostic: int | None = None
    # Tautology-breaker verdict: ``SEPARATED`` only when the real-trade reference AND the
    # falsification agree; ``INCONCLUSIVE`` when they disagree; ``INSUFFICIENT_DATA`` when
    # there is no resolvable near-trade + fv. Defaults to ``INSUFFICIENT_DATA`` so a
    # bare ``AdverseSelectionReport()`` constructs cleanly.
    independent_reference_verdict: str = "INSUFFICIENT_DATA"
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
    naive_quote_price: float | None = None,
    candidate_toxicity_loss_bps: int | None = None,
    naive_toxicity_loss_bps: int | None = None,
    falsification_verdict: str | None = None,
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

    # ``signs`` and ``contributions`` are aligned per RESOLVABLE near-trade (one whose
    # ``fv_now`` and ``fv_after`` both exist). ``.size`` is NEVER read: every metric below
    # is size-agnostic, so doubling every trade's observational size leaves the report
    # byte-identical (the size-independence invariant, AC-105).
    signs: list[float] = []
    contributions: list[float] = []
    for t in near_trades:
        fv_now = fv_at(t.ts)
        fv_after = fv_at(t.ts + window_s)
        if fv_now is None or fv_after is None:
            continue
        sign = 1.0 if t.aggressor_side is AggressorSide.BUY else -1.0
        signs.append(sign)
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

    # E4-T2 extended metrics. All abstain to ``None`` when there is no resolvable
    # near-trade + fv (``contributions`` empty), never to ``0`` (which would falsely
    # read as "measured, and neutral").
    total = len(trades)
    near_quote_trade_rate: float | None = (
        len(near_trades) / total if total > 0 else None
    )
    # Net aggressor imbalance in bps, size-agnostic: mean of the +1/-1 aggressor signs
    # only (never weighted by ``.size``). ``None`` when nothing resolves.
    signed_flow_pressure_bps: int | None = (
        round(mean(signs) * 1e4) if signs else None
    )
    # Average post-trade fair-value markout FROM THE MAKER'S PERSPECTIVE: how far fv moved
    # in the aggressor's favour after the trade (positive => informed flow => adverse
    # selection against a maker who took the other side). It is a DIAGNOSTIC, never a fill.
    post_trade_fv_markout_bps: int | None = (
        round(mean(contributions)) if contributions else None
    )
    # Fraction of resolvable near-trades where the aggressor was subsequently right
    # (fv moved their way) — the maker got "picked off". A rate in [0, 1], ``None`` when
    # nothing resolves.
    picked_off_pressure: float | None = (
        sum(1 for c in contributions if c > 0) / len(contributions)
        if contributions
        else None
    )
    # Candidate-vs-naive toxicity delta is a pure subtraction; ``None`` if EITHER operand
    # is absent (cannot fabricate a comparison from a missing side).
    candidate_vs_naive_toxicity_delta_bps: int | None
    if candidate_toxicity_loss_bps is not None and naive_toxicity_loss_bps is not None:
        candidate_vs_naive_toxicity_delta_bps = (
            candidate_toxicity_loss_bps - naive_toxicity_loss_bps
        )
    else:
        candidate_vs_naive_toxicity_delta_bps = None

    # Tautology-breaker verdict. INSUFFICIENT_DATA when nothing resolves. Otherwise the
    # real-trade reference "separates the candidate" iff the post-trade markout is strictly
    # positive (there IS informed-flow adverse selection a fair-value maker would avoid);
    # SEPARATED only when that independent reference AND the falsification agree, else
    # INCONCLUSIVE (the two references disagree — never claim separation from one alone).
    if not contributions:
        independent_reference_verdict = "INSUFFICIENT_DATA"
    else:
        markout_separates = (
            post_trade_fv_markout_bps is not None and post_trade_fv_markout_bps > 0
        )
        if markout_separates and falsification_verdict == "SEPARATED":
            independent_reference_verdict = "SEPARATED"
        else:
            independent_reference_verdict = "INCONCLUSIVE"

    return AdverseSelectionReport(
        trade_flow_preceding_fv_move_bps_diagnostic=flow_bps,
        toxic_vs_benign_flow_ratio_diagnostic=ratio,
        trades_near_quote_count=len(near_trades),
        near_quote_trade_rate_diagnostic=near_quote_trade_rate,
        signed_flow_pressure_bps_diagnostic=signed_flow_pressure_bps,
        post_trade_fv_markout_bps_diagnostic=post_trade_fv_markout_bps,
        picked_off_pressure_diagnostic=picked_off_pressure,
        candidate_vs_naive_toxicity_delta_bps_diagnostic=candidate_vs_naive_toxicity_delta_bps,
        independent_reference_verdict=independent_reference_verdict,
    )


class ConvergenceReachReport(BaseModel):
    """Basis-adjusted convergence ("Reach") report — residual-only, never raw gap.

    The raw TxLINE-vs-venue gap still contains the structural ``basis`` (a pricing
    convention, not alpha), so it can never itself be read as convergence toward fair
    value. This report therefore surfaces the structural ``basis_bps`` SEPARATELY and
    exposes the reach signal ONLY on the residual (the gap after the basis is removed),
    via :func:`~veridex.maker.basis.reach_from_residual`.

    Attributes:
        basis_bps: The structural (median) offset in basis points — reported separately
            and NEVER counted toward the reach signal.
        residual_reach_fraction: Fraction of consecutive steps whose RESIDUAL magnitude
            shrank; ``None`` when there are fewer than two observations.
        reach_horizon_s: The reach horizon (seconds) this report was built for.
        n: The number of paired observations.
        note: A short honest caveat that reach is measured on the residual only.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    basis_bps: int
    residual_reach_fraction: float | None
    reach_horizon_s: int
    n: int
    note: str = "reach measured on the basis-adjusted residual only, never the raw gap"


def build_convergence_reach(
    txline_fv: list[float],
    venue_native: list[float],
    reach_horizon_s: int,
) -> ConvergenceReachReport:
    """Build a :class:`ConvergenceReachReport` from paired TxLINE / venue series.

    Decomposes the gap into a structural ``basis_bps`` and a per-step residual via
    :func:`~veridex.maker.basis.decompose_gap`, then reads the reach signal from the
    RESIDUAL only via :func:`~veridex.maker.basis.reach_from_residual`. It deliberately
    NEVER derives reach (or any "edge") from the raw gap — no raw-gap-to-edge helper
    exists here or in :mod:`veridex.maker.basis`.

    Args:
        txline_fv: TxLINE fair-value native probabilities in ``[0, 1]``.
        venue_native: Venue native-price probabilities in ``[0, 1]`` (same length).
        reach_horizon_s: The reach horizon (seconds) recorded on the report.

    Returns:
        The basis-adjusted :class:`ConvergenceReachReport`.

    Raises:
        MarkoutError: If any operand is not a native probability in ``[0, 1]``.
        ValueError: If the two series differ in length or are empty.
    """
    decomposition = decompose_gap(txline_fv, venue_native)
    residual_reach_fraction = reach_from_residual(decomposition.residual_gap_bps)
    return ConvergenceReachReport(
        basis_bps=decomposition.basis_bps,
        residual_reach_fraction=residual_reach_fraction,
        reach_horizon_s=reach_horizon_s,
        n=decomposition.n,
    )


class TradeAwareDiagnostic(BaseModel):
    """R1.5 real-artifact join + trade-aware diagnostic container (no-fill boundary).

    Holds the pinned artifact identity, the FULL join accounting (every joined trade is
    grouped-or-unmatched — ``rows_total == rows_matched + rows_unmatched``, no silent
    drops), the per-agent :class:`AdverseSelectionReport`, and the basis-adjusted
    :class:`ConvergenceReachReport`. It carries NO fill / fill-rate / spread-capture /
    PnL / realized-PnL / executable-edge field: the trades are an independent diagnostic
    reference, never Veridex fills.

    ``data_state`` is ``"OK"`` when the join produced at least one usable observation and
    ``"INSUFFICIENT_DATA"`` when the verified artifact yields nothing separable for this
    fixture set (a real R1.5 outcome: the artifact earns the rung by data presence, but
    the diagnostic still has nothing to say).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    data_state: str
    artifact_hash: str | None = None
    rows_total: int = 0
    rows_matched: int = 0
    rows_unmatched: int = 0
    per_agent: dict[str, AdverseSelectionReport] = Field(default_factory=dict)
    convergence: ConvergenceReachReport | None = None
    excluded_by_reason: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _accounting_reconciles(self) -> "TradeAwareDiagnostic":
        """Every joined trade is grouped XOR unmatched — no silent drops (AC-102)."""
        if self.rows_total != self.rows_matched + self.rows_unmatched:
            raise ValueError(
                "trade-join accounting failed: rows_total="
                f"{self.rows_total} != rows_matched+rows_unmatched="
                f"{self.rows_matched + self.rows_unmatched}"
            )
        return self
