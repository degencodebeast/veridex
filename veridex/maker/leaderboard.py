"""Role-specific MAKER leaderboard (SEC-005 isolation).

This lane ranks market-maker agents on quote **markout**, NOT on directional edge. It is
structurally isolated from the directional scorer: this module MUST NOT import that scorer or
blend any directional-edge metric into the maker rank axis. The rank axis here is markout only.
"""

from __future__ import annotations

from typing import Any

__all__ = ["maker_rank_key", "rank_makers"]


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


def rank_makers(agent_metrics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank makers best-first, assigning a 1-based ``maker_rank`` to each row.

    Args:
        agent_metrics: One metric-stack dict per maker agent.

    Returns:
        Copies of the input rows sorted by the maker key, each with ``maker_rank`` (1..N) added.
        Inputs are not mutated.
    """
    ranked = sorted((dict(row) for row in agent_metrics), key=maker_rank_key)
    for position, row in enumerate(ranked, start=1):
        row["maker_rank"] = position
    return ranked
