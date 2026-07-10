"""B6 — scoring + the metric stack (REQ-106 / AC-106, gate CON-002, CON-007).

Turns one :class:`~veridex.runtime.orchestrator.RunResult` into a ranked, per-agent metric stack.
**CLV is the primary score**; the secondary metrics (Sim PnL, Brier, max drawdown, action count,
valid %) are reported but never outrank avg CLV.

TRUST PATH (CON-007): this module imports NO LLM SDK (agno/anthropic/openai/litellm). All metrics
are recomputed from the deterministic evidence already sealed on the ``RunResult`` — never from an
LLM-claimed value. The import audit (``veridex.verifier.import_audit``) covers this file.

The load-bearing rule (codex B3 carry-forward): an action is *scored* IFF ``valid is True`` AND
``clv_bps`` is a real ``int`` (NOT the ``"pending"`` sentinel). WAIT (``wait_unscored``) and
live-pending (``pending_closing``) are valid abstentions — excluded from the CLV means, NEVER
counted as 0. Invalid actions (``valid is False``) are excluded even though their ``clv_bps`` is the
int ``0``. Scoring keys on ``valid`` + numeric ``clv_bps`` and NEVER pattern-matches ``reason``.

Judgment calls surfaced for the codex gate (see the module-level constants and ``_agent_metrics``):

  * **Sim PnL** is a closing-referenced, flat-stake PnL *proxy*: each scored action contributes its
    ``clv_bps`` as the per-unit-stake return, accumulated in ``tick_seq`` order. Phase 1 has NO match
    outcomes, so the closing line IS the truth proxy — ``sim_pnl`` is the final cumulative value,
    which therefore equals ``total_clv_bps``. It is reported as an honest CLV-referenced proxy, not
    settled PnL; its real job is to define the series ``max_drawdown`` is read from.
  * **max_drawdown** is the largest peak-to-trough drop of that cumulative series, stored as a value
    ``<= 0`` (``0.0`` when the series is monotonic non-decreasing; negative for any decline). Ranking
    prefers the less-severe (greater, closer-to-zero) drawdown.
  * **Brier** is a calibration proxy, present ONLY when the agent emitted a numeric ``confidence`` in
    [0, 1] on its scored actions. With no Phase-1 outcomes, the outcome indicator is the closing-line
    direction: ``1`` if the action's ``clv_bps > 0`` (the closing line validated the pick) else ``0``.
    ``brier = mean((confidence - indicator) ** 2)`` over the agent's scored, confidence-bearing
    actions; ``None`` when the agent emitted no usable confidence.
  * **valid_pct** = law-accepted (``valid is True``) / total decisions for the agent (×100) — per
    spec §2 this is *law-acceptance*, a metric DISTINCT from scored coverage: a WAIT
    (``wait_unscored``) and a live-pending (``pending_closing``) are valid abstentions that count
    toward valid_pct but NOT toward ``action_count`` (scored). Only ``valid is False`` rows are
    excluded. Keyed on ``valid``, never on ``reason``.
  * **avg_clv_bps** is ``None`` (not ``0``) when the agent has no scored actions; such agents rank
    last on the primary axis.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from veridex.rank_guards import assert_no_r3r4_in_rank  # neutral SEC-006 guard — imports no lane
from veridex.runtime.window import CLV_FIELD_WINDOW  # pure model (pydantic only) — trust-path clean

if TYPE_CHECKING:  # type-only import keeps the trust path free of a runtime dependency / cycle.
    from veridex.runtime.orchestrator import RunResult


def is_scored(row: dict[str, Any]) -> bool:
    """An action is scored IFF ``valid is True`` AND ``clv_bps`` is a real ``int``.

    The ``bool`` guard matters: ``isinstance(True, int)`` is ``True`` in Python, so a stray boolean
    ``clv_bps`` must not masquerade as a numeric score. The ``"pending"`` sentinel (WAIT and
    live-pending) is a ``str`` and is excluded here without ever inspecting ``reason``.

    This is the SINGLE SOURCE OF TRUTH for the "is this row scored?" predicate: the orchestrator's
    windowed DEC-2D-1/2 overrides (``finalize(window=...)``) import THIS function to decide which
    rows they may touch, so the override set can never silently desync from the scored set that
    ranks the leaderboard.

    Args:
        row: A single ``RunResult.score_rows`` entry.

    Returns:
        ``True`` if the row contributes to the CLV/PnL metrics.
    """
    return row.get("valid") is True and isinstance(row.get("clv_bps"), int) and not isinstance(row.get("clv_bps"), bool)


def is_window_scored(row: dict[str, Any]) -> bool:
    """An action carries WINDOW CLV IFF ``window_clv_bps`` is a real ``int`` (DEC-2D-1).

    The SINGLE SOURCE OF TRUTH for the "is this a scored WINDOW row?" predicate — mirrors
    :func:`is_scored` (same ``bool`` guard, since ``isinstance(True, int)`` is ``True``) but keys on
    the DISTINCT ``window_clv_bps`` field a ``fixed_duration``/``manual_stop`` window uses. Downstream
    (``competition.events``) imports THIS rather than re-deriving the check, so the window aggregate
    can never silently desync from the leaderboard-facing scored set.

    Mutually exclusive with :func:`is_scored` per row BY CONSTRUCTION: finalize renames a scored
    window row's value with ``row[window_clv_bps] = row.pop("clv_bps")``, so a window row physically
    carries NO ``clv_bps`` (``is_scored`` is ``False``) and a true-CLV row carries NO
    ``window_clv_bps`` (``is_window_scored`` is ``False``). A pending_horizon/WAIT row carries the
    ``"pending"`` ``str`` sentinel under ``clv_bps`` and no ``window_clv_bps`` → neither predicate
    fires (honest abstention, excluded from BOTH means).

    Args:
        row: A single ``RunResult.score_rows`` entry.

    Returns:
        ``True`` if the row contributes to the WINDOW-CLV aggregate (never to the true-CLV metrics).
    """
    value = row.get(CLV_FIELD_WINDOW)
    return isinstance(value, int) and not isinstance(value, bool)


def _confidence(row: dict[str, Any]) -> float | None:
    """Extract a usable predicted probability from the (untrusted) action params, or ``None``.

    Reads ``raw_prescore.raw_action.params.confidence``. Only a numeric value in [0, 1] (and not a
    ``bool``) counts as a probability; anything else yields ``None`` (no calibration signal).

    Args:
        row: A single ``RunResult.score_rows`` entry.

    Returns:
        The confidence as a ``float`` in [0, 1], or ``None`` when absent/unusable.
    """
    params = row.get("raw_prescore", {}).get("raw_action", {}).get("params", {})
    value = params.get("confidence")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    confidence = float(value)
    if 0.0 <= confidence <= 1.0:
        return confidence
    return None


def _max_drawdown(scored_clv: list[int]) -> float:
    """Largest peak-to-trough drop of the cumulative ``clv`` series (value ``<= 0``).

    The input is already ordered by ``tick_seq``. Returns ``0.0`` for an empty or monotonic
    non-decreasing series; a negative value for any decline (the steepest peak-to-trough fall).

    Args:
        scored_clv: Per-action ``clv_bps`` of the scored actions, in ``tick_seq`` order.

    Returns:
        The maximum drawdown as a ``float`` ``<= 0.0``.
    """
    cumulative = 0
    peak = 0
    max_dd = 0.0
    for clv in scored_clv:
        cumulative += clv
        peak = max(peak, cumulative)
        max_dd = min(max_dd, float(cumulative - peak))
    return max_dd


def _brier(scored_rows: list[dict[str, Any]]) -> float | None:
    """Mean Brier score over scored actions that carry a usable confidence, else ``None``.

    Outcome indicator is the closing-line direction: ``1`` if ``clv_bps > 0`` else ``0`` (Phase 1
    has no settled outcomes, so the closing line is the truth proxy). See the module docstring.

    Args:
        scored_rows: The agent's scored rows.

    Returns:
        The mean ``(confidence - indicator) ** 2``, or ``None`` when no scored row has a confidence.
    """
    errors: list[float] = []
    for row in scored_rows:
        confidence = _confidence(row)
        if confidence is None:
            continue
        indicator = 1.0 if row["clv_bps"] > 0 else 0.0
        errors.append((confidence - indicator) ** 2)
    if not errors:
        return None
    return sum(errors) / len(errors)


def _agent_metrics(agent_id: str, proof_mode: str, agent_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute one agent's full metric stack from its score rows (rank assigned later).

    Args:
        agent_id: The agent identifier.
        proof_mode: The agent's eligibility label (carried through verbatim, never scored).
        agent_rows: Every ``score_rows`` entry for this agent (decisions, including abstentions).

    Returns:
        A metric-stack dict for this agent, without the ``rank`` field.
    """
    scored = sorted((r for r in agent_rows if is_scored(r)), key=lambda r: r["tick_seq"])
    scored_clv: list[int] = [r["clv_bps"] for r in scored]

    action_count = len(scored)
    total_clv_bps = sum(scored_clv)
    avg_clv_bps = (total_clv_bps / action_count) if action_count else None

    # DEC-2D-1: WINDOW CLV (fixed_duration/manual_stop rows) is a DISTINCT, LABELED aggregate — it is
    # NEVER blended into avg_clv_bps (the leaderboard rank axis) and NEVER silently dropped. A run has
    # ONE end_rule, so its scored rows are all one kind: this populates the window fields OR the true
    # fields, never both. window_scored ∩ scored is empty by construction (see is_window_scored).
    window_scored = [r[CLV_FIELD_WINDOW] for r in agent_rows if is_window_scored(r)]
    window_action_count = len(window_scored)
    total_window_clv_bps = sum(window_scored)
    avg_window_clv_bps = (total_window_clv_bps / window_action_count) if window_action_count else None
    total_decisions = len(agent_rows)
    # valid_pct is law-acceptance, NOT scored coverage: WAIT/live-pending are valid abstentions
    # (valid is True) and count here even though they are NOT scored. Reuses the `valid is True`
    # predicate — consistent with the never-pattern-match-`reason` rule.
    valid_count = sum(1 for r in agent_rows if r.get("valid") is True)
    valid_pct = (valid_count / total_decisions * 100.0) if total_decisions else 0.0

    return {
        "agent_id": agent_id,
        "avg_clv_bps": avg_clv_bps,
        "total_clv_bps": total_clv_bps,
        "sim_pnl": total_clv_bps,  # final cumulative value of the closing-referenced flat-stake series
        "brier": _brier(scored),
        "max_drawdown": _max_drawdown(scored_clv),
        "action_count": action_count,
        "valid_pct": valid_pct,
        "valid_count": valid_count,  # WD-7: CLV sample-size source (law-valid decisions)
        # DEC-2D-1 window CLV — a labeled SUPPORTING metric, never the rank axis (see _rank_key).
        "avg_window_clv_bps": avg_window_clv_bps,
        "total_window_clv_bps": total_window_clv_bps,
        "window_action_count": window_action_count,
        "proof_mode": proof_mode,
    }


def _rank_key(metrics: dict[str, Any]) -> tuple[Any, ...]:
    """Ascending sort key encoding the documented rank order (best agent sorts first).

    Order: avg CLV desc (``None`` last) -> total CLV desc -> Brier asc (``None`` last) -> max
    drawdown desc (less-severe first) -> action count desc -> agent_id asc (stable final tiebreak).

    Args:
        metrics: One agent's metric-stack dict.

    Returns:
        A tuple suitable for ``list.sort``/``sorted`` (ascending).
    """
    assert_no_r3r4_in_rank(metrics)  # SEC-006: no R3/R4 execution field may enter the rank key
    avg = metrics["avg_clv_bps"]
    brier = metrics["brier"]
    return (
        (1, 0.0) if avg is None else (0, -avg),  # primary: avg CLV desc, None last
        -metrics["total_clv_bps"],  # total CLV desc
        (1, 0.0) if brier is None else (0, brier),  # Brier asc (lower better), None last
        -metrics["max_drawdown"],  # max drawdown desc (less-severe / closer-to-0 first)
        -metrics["action_count"],  # action count desc
        metrics["agent_id"],  # deterministic final tiebreak
    )


def score_run(run: RunResult) -> list[dict[str, Any]]:
    """Score one run into a ranked, per-agent metric stack (one row per agent).

    Every participating agent (``run.agent_ids``) gets exactly one row — including agents whose
    actions were all abstentions/invalid (``avg_clv_bps=None``, ``action_count=0``), which rank
    last. CLV/PnL metrics are computed over the *scored set* only (``valid is True`` AND numeric
    ``clv_bps``); WAIT/live-pending/invalid actions are excluded, never counted as 0. Deterministic:
    the same run yields the same rows and ranks.

    Args:
        run: The completed :class:`~veridex.runtime.orchestrator.RunResult` to score.

    Returns:
        Metric-stack rows sorted best-first, each with ``rank`` (1..N) assigned. Each row is
        ``{agent_id, avg_clv_bps, total_clv_bps, sim_pnl, brier, max_drawdown, action_count,
        valid_pct, valid_count, avg_window_clv_bps, total_window_clv_bps, window_action_count,
        rank, proof_mode}`` (``valid_count`` is the WD-7 CLV sample size; it is display-only and
        never enters the rank key — SEC-005). The ``*_window_clv_bps`` / ``window_action_count``
        fields are the DEC-2D-1 WINDOW-CLV aggregate for fixed_duration/manual_stop runs — a labeled
        supporting metric, NEVER blended into ``avg_clv_bps`` and NEVER entering ``_rank_key``.
    """
    rows_by_agent: dict[str, list[dict[str, Any]]] = {agent_id: [] for agent_id in run.agent_ids}
    for row in run.score_rows:
        rows_by_agent.setdefault(row["agent_id"], []).append(row)

    metrics = [
        _agent_metrics(agent_id, run.proof_mode_map.get(agent_id, ""), agent_rows)
        for agent_id, agent_rows in rows_by_agent.items()
    ]
    metrics.sort(key=_rank_key)
    for rank, row in enumerate(metrics, start=1):
        row["rank"] = rank
    return metrics
