"""Role-specific MAKER leaderboard (SEC-005 isolation).

This lane ranks market-maker agents on quote **markout**, NOT on directional edge. It is
structurally isolated from the directional scorer: this module MUST NOT import that scorer or
blend any directional-edge metric into the maker rank axis. The rank axis here is markout only.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "assert_bracket_not_ranked",
    "maker_rank_key",
    "rank_makers",
    "window_clv_analog",
]

_R2_BRACKET_KEYS = frozenset({"bracket", "sensitivity", "r2"})


def assert_bracket_not_ranked(agent_metrics: list[dict[str, Any]]) -> None:
    """Revert-proof guard: reject rank input carrying an R2 sensitivity bracket.

    The R2 bracket is a declared model overlay, never a ranked measurement (HB-12). This guard
    ensures a future refactor cannot silently smuggle a bracket/sensitivity/r2 key into the
    maker rank axis.

    Args:
        agent_metrics: One metric-stack dict per maker agent, as passed to ``rank_makers``.

    Raises:
        AssertionError: If any row contains a key in ``{"bracket", "sensitivity", "r2"}``.
    """
    for row in agent_metrics:
        offending = _R2_BRACKET_KEYS & row.keys()
        assert not offending, (
            f"R2 bracket key(s) {sorted(offending)} must never enter the maker rank input"
        )


def maker_rank_key(metrics: dict[str, Any]) -> tuple[Any, ...]:
    """Ascending sort key encoding the maker rank order (best maker sorts first).

    Order: avg markout desc (``None`` last) -> abstained asc -> quote_count desc -> agent_id asc
    (deterministic final tiebreak). Markout is the maker's honesty/quality signal; the directional
    price-view metric never enters this key.

    Args:
        metrics: One maker's metric-stack dict.

    Returns:
        A tuple suitable for ``list.sort``/``sorted`` (ascending, best maker first).
    """
    avg = metrics.get("avg_markout_bps")
    avg_key = (1, 0.0) if avg is None else (0, -avg)  # primary: avg markout desc, None last
    return (
        avg_key,
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
    assert_bracket_not_ranked(agent_metrics)
    ranked = sorted((dict(row) for row in agent_metrics), key=maker_rank_key)
    for position, row in enumerate(ranked, start=1):
        row["maker_rank"] = position
    return ranked
