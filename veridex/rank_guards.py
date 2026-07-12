"""SEC-006: the canonical R3/R4 rank denylist, shared by BOTH ranked lanes.

The scored maker lane (:mod:`veridex.maker.leaderboard`) and the directional lane
(:mod:`veridex.scoring`) rank on quote-quality / CLV evidence only. R3/R4 execution
observations — queue position, outbidding, executability, own fills, inventory,
realized PnL — belong exclusively to the operator-gated recorder lane
(:mod:`veridex.live_recorder`) and must NEVER enter a rank input.

This module is deliberately NEUTRAL: it imports nothing from ``veridex.maker``,
``veridex.scoring``, or ``veridex.live_recorder``, so both ranked lanes can depend on
it without crossing the import boundary enforced by
``tests/test_no_r3_r4_code.py::test_maker_and_scoring_do_not_import_live_recorder``.

:func:`assert_no_r3r4_in_rank` is RAISE-ONLY: it raises iff a forbidden field name is
present on a rank row, and is a strict NO-OP otherwise. Wiring it as the first
statement of a rank path therefore leaves every clean (existing) output BYTE-IDENTICAL.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = [
    "R3_R4_RANK_DENYLIST",
    "R4A_EXECUTION_DENYLIST_FIELDS",
    "assert_no_r3r4_in_rank",
]

#: FROZEN canonical set of EVERY R4-A dust-execution outcome/diagnostic field name that a
#: rank input must never carry (SEC-006, AC-014/AC-015). These are the realized-execution
#: OUTCOMES of the ``veridex.dust_execution`` lane — own fills, fill size/price, realized
#: PnL, net inventory, real-fill reconciliation, and the post-trade markout diagnostic —
#: NOT order-INTENT / quote fields (``client_order_id``, ``decision_id``, ``tif``, ``price``,
#: ``side``, ``size``, ``spread``, ``label``), which legitimately describe a quote and are
#: DELIBERATELY absent here. Freezing the WHOLE set (not just ``post_trade_markout``) closes
#: Codex-M7: a set-equality test over this literal makes omitting ANY one field fail. The R4-A
#: contracts.py surfaces these outcomes across ``OwnFillEvent`` (``own_fill``),
#: ``OrderStatusEvent`` (``filled_size``/``fill_price``), the realized-PnL concept (recorded as
#: ``realized_loss_*`` on ``SessionRiskSnapshot``), ``InventoryEvent`` (``inventory``),
#: ``RealFillReconciliation`` (``real_fill_reconciliation``) and ``PostTradeMarkoutEvent``
#: (``post_trade_markout``); these are their canonical rank-input names.
#:
#: The set ALSO carries the ACTUAL contract attribute names that ``event.model_dump()`` yields
#: as rank-row keys, so a leaked dumped event is caught by exact key name (defense-in-depth, not
#: just the friendly canonical alias). Gate #1 MAJOR-4 (Codex) showed the earlier 13-field set
#: omitted whole outcome events: an ``OrderCancelEvent`` / ``OrderAckEvent`` dump, or a
#: ``PostTradeMarkoutEvent`` with ``markout_bps`` removed, passed all three rank guards. Every
#: realized-execution OUTCOME/DIAGNOSTIC attribute across ``contracts.py`` is now denied:
#: ``fill_size`` / ``fill_ts`` (``OwnFillEvent``), ``status`` / ``filled_size`` (``OrderStatusEvent``),
#: ``canceled`` (``OrderCancelEvent`` — its SOLE outcome field), ``ack_status`` (``OrderAckEvent``
#: — its sole outcome field), ``reconciled_state`` / ``reconciled_fill_size``
#: (``RealFillReconciliation``), ``net_inventory`` (``InventoryEvent``), ``reference_price`` /
#: ``markout_bps`` (``PostTradeMarkoutEvent`` — ``reference_price`` REVERSES E1-T5's earlier
#: exclusion: it leaks a markout diagnostic), and ``realized_loss_session`` /
#: ``realized_loss_daily`` (``SessionRiskSnapshot``). Pure operational/safety counters that do NOT
#: reveal alpha are DELIBERATELY excluded (``open_order_count``, ``breaker_open``,
#: ``kill_switch_engaged``, ``canceled_count``, ``trigger_cause``, ``reject_reason``,
#: ``reconciliation_path``, ``surfaces_queried``, ``uncertain_state``), as are envelope/join/intent/
#: config/label fields (``side``, ``venue_order_id``, ``horizon_ms``, ``token_id`` …) and
#: ``real_executable_edge_bps``.
R4A_EXECUTION_DENYLIST_FIELDS = frozenset(
    {
        # --- canonical rank-input concept names ---
        "own_fill",
        "filled_size",
        "fill_price",
        "realized_pnl",
        "inventory",
        "real_fill_reconciliation",
        "post_trade_markout",
        # --- ACTUAL contracts.py attribute names (model_dump() rank-row keys) ---
        "fill_size",
        "net_inventory",
        "markout_bps",
        "reconciled_fill_size",
        "realized_loss_session",
        "realized_loss_daily",
        # --- Gate #1 MAJOR-4: remaining per-event OUTCOME/DIAGNOSTIC attributes ---
        "canceled",          # OrderCancelEvent — sole outcome field
        "ack_status",        # OrderAckEvent — sole outcome field
        "status",            # OrderStatusEvent — fill-lifecycle outcome
        "reconciled_state",  # RealFillReconciliation — reconciliation outcome
        "fill_ts",           # OwnFillEvent — fill timestamp = evidence a fill occurred
        "reference_price",   # PostTradeMarkoutEvent — markout diagnostic (reverses E1-T5)
    }
)

#: Canonical, FROZEN, non-generic R3/R4 execution-field denylist (SEC-006). Every name here
#: is an R3 (queue-position fill simulation) or R4/R4-A (real own-fill reconciliation)
#: execution observation the recorder/dust lane may carry but a rank input may not. The full
#: R4-A execution-outcome set is folded in via :data:`R4A_EXECUTION_DENYLIST_FIELDS` so a
#: single canonical set feeds every rank surface. Generic legitimate maker-row names (``side``,
#: ``spread``, ``size``, ``real_executable_edge_bps`` — emitted as ``None``) are DELIBERATELY
#: EXCLUDED, and ``label``/``ranked`` are NOT listed here (they are already covered by
#: ``leaderboard._R2_BRACKET_KEYS``) so the two guards stay orthogonal.
R3_R4_RANK_DENYLIST = frozenset(
    {
        # --- R3 queue-position / executability observations ---
        "queue_ahead_size",
        "outbid_within_ms",
        "stepped_ahead_count",
        "executability",
        "available_size_at_price",
        "cumulative_size_to_clear",
        "cost_clearing_threshold",
        "fee_stress_multiplier",
    }
) | R4A_EXECUTION_DENYLIST_FIELDS  # --- full R4/R4-A real own-fill / inventory / markout set ---


def assert_no_r3r4_in_rank(metrics: Mapping[str, Any]) -> None:
    """Raise-only guard: reject a rank row carrying any R3/R4 execution field.

    A NO-OP on any clean row (no denylisted key present), so wiring it into a rank
    path leaves existing outputs byte-identical.

    Args:
        metrics: One rank-input row (maker metric stack or directional metric stack).

    Raises:
        AssertionError: If any key of ``metrics`` is in :data:`R3_R4_RANK_DENYLIST`.
            ``AssertionError`` mirrors ``leaderboard.assert_bracket_not_ranked`` so both
            revert-proof rank guards raise the same way (the two denylists overlap only on
            ``realized_pnl``, which either guard rejects identically).
    """
    offending = R3_R4_RANK_DENYLIST.intersection(metrics)
    if offending:
        raise AssertionError(
            f"R3/R4 execution field(s) {sorted(offending)} must never enter a rank input"
        )
