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

__all__ = ["R3_R4_RANK_DENYLIST", "assert_no_r3r4_in_rank"]

#: Canonical, FROZEN, non-generic R3/R4 execution-field denylist (SEC-006). Every name
#: here is an R3 (queue-position fill simulation) or R4 (real own-fill reconciliation)
#: execution observation the recorder lane may carry but a rank input may not. Generic
#: legitimate maker-row names (``side``, ``spread``, ``size``,
#: ``real_executable_edge_bps`` — emitted as ``None``) are DELIBERATELY EXCLUDED, and
#: ``label``/``ranked`` are NOT listed here (they are already covered by
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
        # --- R4 real own-fill / inventory reconciliation ---
        "own_fill",
        "inventory",
        "filled_size",
        "fill_price",
        "realized_pnl",
        "real_fill_reconciliation",
    }
)


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
