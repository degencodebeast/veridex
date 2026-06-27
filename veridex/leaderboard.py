"""B10 — leaderboard (REQ-114 / AC-114, gate CON-006, CON-008).

Aggregates per-run per-agent score rows (from :func:`~veridex.scoring.score_run`) across
multiple runs into a single ranked leaderboard row per agent.

**CLV is the primary score** across runs; proof completeness (``eligibility_badge``) is an
eligibility label only and is **intentionally absent from the sort key** — see CON-006/CON-008
and the spec note "proof completeness is an eligibility badge, NOT a performance score."

TRUST PATH (CON-007): this module imports NO LLM SDK (agno/anthropic/openai/litellm). The
import audit (``veridex.verifier.import_audit``) covers this file.

Judgment calls surfaced for the codex gate (see the module-level constants below):

  * **avg_clv_bps** is **pooled** across runs: ``sum(total_clv_bps) / sum(action_count)``.
    This is the true mean over all scored actions, NOT a mean-of-run-means.  When total
    ``action_count == 0`` the result is ``None`` (same semantics as
    :func:`~veridex.scoring.score_run`).
  * **valid_pct** is the **unweighted mean** of per-run ``valid_pct`` values.  A true pool
    (``Σ valid_count / Σ total_decisions × 100``) would require per-run ``total_decisions``,
    which is not present in ``score_run`` output.  Unweighted mean is the best available
    approximation with the data on hand.
  * **brier** is the **mean** of per-run brier values that are not ``None``; ``None`` when no
    run contributes a brier.  This weights each run equally (not action-count-weighted) for
    the same reason: per-run action counts for the brier subset are not exposed.
  * **max_drawdown** is the **min** (worst/most-negative) across runs — the worst single-run
    drawdown episode.  Rationale: preserves the ``≤ 0`` invariant; the worst episode is the
    conservative cross-run risk bound.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _eligibility_badge(anchor_statuses: list[str | None]) -> str:
    """Derive a proof-completeness badge from per-run anchor_status values.

    The badge reflects how many of the agent's runs are on-chain confirmed.
    It MUST NOT be used in the sort key — see CON-006/CON-008.

    Args:
        anchor_statuses: The ``anchor_status`` field for each run the agent appeared
            in.  ``None`` means the field was absent (treated as unanchored).

    Returns:
        ``"fully-proven"`` when every run is confirmed, ``"partially-proven"`` when
        at least one but not all runs are confirmed, ``"unproven"`` otherwise.
    """
    confirmed = sum(1 for s in anchor_statuses if s == "anchored")
    total = len(anchor_statuses)
    if confirmed == total and confirmed > 0:
        return "fully-proven"
    if confirmed > 0:
        return "partially-proven"
    return "unproven"


def _summarize_anchor_status(anchor_statuses: list[str | None]) -> str:
    """Summarize anchor_status across all runs for one agent.

    Args:
        anchor_statuses: Per-run ``anchor_status`` values (``None`` when absent).

    Returns:
        ``"all-anchored"`` if every run is confirmed, ``"some-pending"`` if mixed,
        ``"none-anchored"`` if none are confirmed.
    """
    confirmed = sum(1 for s in anchor_statuses if s == "anchored")
    total = len(anchor_statuses)
    if confirmed == total and confirmed > 0:
        return "all-anchored"
    if confirmed > 0:
        return "some-pending"
    return "none-anchored"


def _summarize_source_mode(modes: list[str | None]) -> str:
    """Summarize source_mode across all runs for one agent.

    Args:
        modes: Per-run ``source_mode`` values (``None`` when the field is absent).

    Returns:
        ``"all-replay"`` when every run used replay, ``"all-live"`` when every run
        used live, ``"mixed"`` when both appear, ``"unknown"`` when the field was
        absent on all runs.
    """
    present = [m for m in modes if m is not None]
    if not present:
        return "unknown"
    unique = set(present)
    if unique == {"replay"}:
        return "all-replay"
    if unique == {"live"}:
        return "all-live"
    return "mixed"


def _summarize_proof_mode(modes: list[str]) -> str:
    """Summarize proof_mode across all runs for one agent.

    Args:
        modes: Per-run ``proof_mode`` values.

    Returns:
        The common value when all runs share the same proof_mode, ``"mixed"``
        otherwise.
    """
    unique = set(modes)
    if len(unique) == 1:
        return unique.pop()
    return "mixed"


def _aggregate(agent_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate all per-run rows for one agent into a single leaderboard row.

    Applies the pooling / aggregation formulas described in the module docstring.
    The returned dict does not include ``rank`` — that is assigned by the caller
    after sorting.

    Args:
        agent_id: The agent identifier.
        rows: Every input record for this agent (one entry per run).

    Returns:
        Aggregated leaderboard row without ``rank``.
    """
    runs = len(rows)

    # Summed fields.
    total_clv_bps: int = sum(r["total_clv_bps"] for r in rows)
    sim_pnl: int = sum(r["sim_pnl"] for r in rows)
    action_count: int = sum(r["action_count"] for r in rows)

    # POOLED avg — true mean over all scored actions, NOT mean-of-run-means.
    avg_clv_bps: float | None = (total_clv_bps / action_count) if action_count > 0 else None

    # JUDGMENT: max_drawdown = min (worst / most-negative) across runs.
    max_drawdown: float = min(r["max_drawdown"] for r in rows)

    # JUDGMENT: valid_pct = unweighted mean of per-run valid_pct values.
    valid_pct: float = sum(r["valid_pct"] for r in rows) / runs

    # JUDGMENT: brier = mean of non-None per-run brier values.
    brier_vals: list[float] = [r["brier"] for r in rows if r.get("brier") is not None]
    brier: float | None = (sum(brier_vals) / len(brier_vals)) if brier_vals else None

    # Summarize per-run categorical fields.
    anchor_statuses: list[str | None] = [r.get("anchor_status") for r in rows]
    source_modes: list[str | None] = [r.get("source_mode") for r in rows]
    proof_modes: list[str] = [r["proof_mode"] for r in rows]

    return {
        "agent_id": agent_id,
        "runs": runs,
        "avg_clv_bps": avg_clv_bps,
        "total_clv_bps": total_clv_bps,
        "sim_pnl": sim_pnl,
        "brier": brier,
        "max_drawdown": max_drawdown,
        "action_count": action_count,
        "valid_pct": valid_pct,
        "proof_mode": _summarize_proof_mode(proof_modes),
        # eligibility_badge derived from anchor_status — NEVER used in rank key.
        "eligibility_badge": _eligibility_badge(anchor_statuses),
        "anchor_status": _summarize_anchor_status(anchor_statuses),
        "source_mode": _summarize_source_mode(source_modes),
    }


def _rank_key(row: dict[str, Any]) -> tuple[Any, ...]:
    """Ascending sort key encoding the documented cross-run rank order.

    Order: avg CLV desc (``None`` last) → total CLV desc → Brier asc (``None``
    last) → max drawdown desc (less-severe/closer-to-0 better) → action count desc
    → agent_id asc (deterministic final tiebreak).

    ``eligibility_badge`` is intentionally absent — proof completeness MUST NOT
    affect rank (CON-006/CON-008).

    Args:
        row: An aggregated leaderboard row (without ``rank``).

    Returns:
        A tuple suitable for ``list.sort`` (ascending) so that the best agent
        sorts first.
    """
    avg = row["avg_clv_bps"]
    brier = row["brier"]
    return (
        (1, 0.0) if avg is None else (0, -avg),  # avg CLV desc; None last
        -row["total_clv_bps"],  # total CLV desc
        (1, 0.0) if brier is None else (0, brier),  # Brier asc; None last
        -row["max_drawdown"],  # drawdown desc (closer-to-0 first)
        -row["action_count"],  # action count desc
        row["agent_id"],  # deterministic final tiebreak
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def leaderboard(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate per-run per-agent score rows into a ranked cross-run leaderboard.

    Takes a flat list of :func:`~veridex.scoring.score_run` output rows (optionally
    tagged with ``anchor_status`` and ``source_mode`` for that run) and produces
    one aggregated row per agent, sorted best-first by pooled avg CLV.

    The ``eligibility_badge`` field reflects proof completeness (``"fully-proven"``,
    ``"partially-proven"``, or ``"unproven"``) but is **never used in the sort key**
    — per CON-006/CON-008 and the spec: *"proof completeness is an eligibility badge,
    NOT a performance score."*

    Aggregation formulas (judgment calls — see module docstring for full rationale):

    * ``avg_clv_bps``: pooled = ``sum(total_clv_bps) / sum(action_count)``.  Not
      mean-of-run-means.  ``None`` when total action_count is 0.
    * ``total_clv_bps``, ``sim_pnl``, ``action_count``: summed across runs.
    * ``max_drawdown``: min (worst/most-negative) across runs.
    * ``valid_pct``: unweighted mean of per-run ``valid_pct`` values (per-run
      ``total_decisions`` is unavailable; see module docstring).
    * ``brier``: mean of per-run non-``None`` brier values; ``None`` when no run
      contributes a brier.

    Ranking: primary ``avg_clv_bps`` desc; tie-breakers ``total_clv_bps`` desc →
    ``brier`` asc (``None`` last) → ``max_drawdown`` desc (less-severe first) →
    ``action_count`` desc → ``agent_id`` asc.  Deterministic: same records → same
    board.

    Args:
        records: Flat list of per-run per-agent rows.  Each row MUST carry the
            ``score_run`` keys: ``agent_id``, ``avg_clv_bps``, ``total_clv_bps``,
            ``sim_pnl``, ``brier``, ``max_drawdown``, ``action_count``,
            ``valid_pct``, ``proof_mode``.  Optional extras per run: ``anchor_status``
            (canonical vocabulary ``"anchored"`` / ``"pending"`` / ``"not_anchored"``;
            only ``"anchored"`` counts toward proof completeness), ``source_mode``
            (``"replay"`` / ``"live"``).

    Returns:
        One row per agent, sorted best-first, each carrying ``rank`` (1..N).
        Row keys: ``agent_id``, ``runs``, ``avg_clv_bps``, ``total_clv_bps``,
        ``sim_pnl``, ``brier``, ``max_drawdown``, ``action_count``, ``valid_pct``,
        ``proof_mode``, ``eligibility_badge``, ``anchor_status``, ``source_mode``,
        ``rank``.
    """
    # Group records by agent_id, preserving first-seen order for determinism
    # (dict insertion order is guaranteed stable in Python ≥ 3.7).
    by_agent: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        agent_id: str = record["agent_id"]
        by_agent.setdefault(agent_id, []).append(record)

    aggregated = [_aggregate(aid, rows) for aid, rows in by_agent.items()]
    aggregated.sort(key=_rank_key)
    for rank, row in enumerate(aggregated, start=1):
        row["rank"] = rank
    return aggregated
