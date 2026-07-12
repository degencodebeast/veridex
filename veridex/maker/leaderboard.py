"""Role-specific MAKER leaderboard (SEC-005 isolation).

This lane ranks market-maker agents on adverse-selection **toxicity** (the falsification
axis), NOT on directional edge. It is structurally isolated from the directional scorer: this
module MUST NOT import that scorer or blend any directional-edge metric into the maker rank
axis. ``avg_markout_bps`` is kept only as a labeled diagnostic, never the rank axis.
"""

from __future__ import annotations

from typing import Any

from veridex.rank_guards import assert_no_r3r4_in_rank  # neutral SEC-006 guard — imports no lane

__all__ = [
    "assert_bracket_not_ranked",
    "maker_rank_key",
    "rank_makers",
    "window_clv_analog",
]

# Static rank-denylist (SEC-005): EVERY R2-specific field name of
# ``R2SensitivityBracket`` ∪ ``R2ProtectionAblation`` (authored by hand so
# ``leaderboard.py`` imports NO R2 model -- module hygiene), plus the
# result-attach overlay key and the legacy aliases. ``real_executable_edge_bps``
# is DELIBERATELY EXCLUDED: it is a legitimate maker-row field emitted as
# ``None`` by ``aggregate_agent_metrics`` -- denylisting it would reject every
# normal maker row and break ``rank_makers``. The derivation test in
# ``test_maker_r2_not_ranked.py`` asserts this set covers every R2 field.
_R2_BRACKET_KEYS = frozenset(
    {
        # --- legacy aliases ---
        "bracket",
        "sensitivity",
        "r2",
        "r2_bracket",
        # --- R2SensitivityBracket declared-overlay data ---
        "simulated_expected_inventory_path",
        "simulated_expected_exposure",
        "simulated_spread_capture_range",
        "simulated_adverse_selection_haircut",
        "assumption_sensitivity",
        # --- R2SensitivityBracket honesty tombstones / guards ---
        "realized_pnl",
        "fill_proof",
        "uses_real_orderbook",
        "uses_own_fills",
        "queue_modeled",
        "ranked",
        "fill_rule_source",
        "forbidden_trigger_assertion",
        "label",
        # --- R2ProtectionAblation ---
        "protection_on",
        "protection_off",
        "event_gate_cost",
        "delta_note",
        # --- result-attach overlay key ---
        "protection_on_off_ablation",
    }
)


def assert_bracket_not_ranked(agent_metrics: list[dict[str, Any]]) -> None:
    """Revert-proof guard: reject rank input carrying an R2 sensitivity bracket.

    The R2 bracket is a declared model overlay, never a ranked measurement (HB-12). This guard
    ensures a future refactor cannot silently smuggle a bracket/sensitivity/r2 key into the
    maker rank axis.

    Args:
        agent_metrics: One metric-stack dict per maker agent, as passed to ``rank_makers``.

    Raises:
        AssertionError: If any row contains a key in ``_R2_BRACKET_KEYS`` (the
            full ``R2SensitivityBracket`` ∪ ``R2ProtectionAblation`` field set,
            minus ``real_executable_edge_bps``).
    """
    for row in agent_metrics:
        offending = _R2_BRACKET_KEYS & row.keys()
        if offending:
            raise AssertionError(
                f"R2 bracket key(s) {sorted(offending)} must never enter the maker rank input"
            )


def maker_rank_key(metrics: dict[str, Any]) -> tuple[Any, ...]:
    """Ascending sort key encoding the maker rank order (best maker sorts first).

    Rank by adverse-selection toxicity (lower loss = better) -- the SAME axis the
    falsification bootstrap uses. ``avg_markout_bps`` is a DIAGNOSTIC, not the rank axis
    (raw two-sided mean is dominated by half_spread/ref_now geometry, not quote quality).

    Order: toxicity loss asc (``None`` last) -> abstained asc -> quote_count desc -> agent_id asc
    (deterministic final tiebreak). The directional price-view metric never enters this key.

    Args:
        metrics: One maker's metric-stack dict.

    Returns:
        A tuple suitable for ``list.sort``/``sorted`` (ascending, best maker first).
    """
    assert_no_r3r4_in_rank(metrics)  # SEC-006: guard the KEY itself — closes the direct-sort bypass
    loss = metrics.get("avg_toxicity_loss_bps")
    loss_key = (1, 0.0) if loss is None else (0, loss)  # None last; lower loss first
    return (
        loss_key,
        metrics.get("abstained", 0),  # fewer abstentions first
        -metrics.get("quote_count", 0),  # more quotes first
        metrics.get("agent_id", ""),  # deterministic final tiebreak
    )


def window_clv_analog(avg_markout_bps: int | None, scored: int) -> dict[str, Any]:
    """Build the maker's window-CLV analog: a report-only labeled aggregate.

    Mirrors the shape of the directional ``avg_window_clv_bps`` supporting aggregate so the
    maker proof card can connect to the same evidence grammar, but this is explicitly labeled
    as NOT a rank axis. It must never be blended into ``maker_rank_key``/``rank_makers``.

    Args:
        avg_markout_bps: The maker's average markout vs future TxLINE FV, in bps (``None`` if
            unscored).
        scored: Count of scored actions contributing to the aggregate.

    Returns:
        A labeled aggregate dict: ``window_markout_bps``, ``window_action_count``, and a
        ``note`` explaining it is not a CLV rank axis.
    """
    return {
        "window_markout_bps": avg_markout_bps,
        "window_action_count": scored,
        "note": "maker markout vs future TxLINE FV; labeled aggregate, NOT a CLV rank axis",
    }


def rank_makers(agent_metrics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank makers best-first, assigning a 1-based ``maker_rank`` to each row.

    Args:
        agent_metrics: One metric-stack dict per maker agent.

    Returns:
        Copies of the input rows sorted by the maker key, each with ``maker_rank`` (1..N) added.
        Inputs are not mutated.
    """
    for row in agent_metrics:  # SEC-006: no R3/R4 execution field may enter the maker rank input
        assert_no_r3r4_in_rank(row)
    assert_bracket_not_ranked(agent_metrics)
    ranked = sorted((dict(row) for row in agent_metrics), key=maker_rank_key)
    for position, row in enumerate(ranked, start=1):
        row["maker_rank"] = position
    return ranked
