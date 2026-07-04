"""C-5 — VenueBehaviorReport: a report-only hypothesis-discovery layer over VvV estimated edge.

This module is a SEPARATE report object — NOT scoring, NOT law, NOT sealed evidence and NEVER an
``AgentAction``. That separation is the whole point: venue numbers (mean estimated edge, staleness)
live here legitimately because nothing here enters the sealed/scored path. The layer answers *"where
might a time-aligned Polymarket mid have been away from TxLINE fair value?"* as a set of ledgered
QUESTIONS for a future run (CON-008), never as an edge claim.

Three self-falsifying instruments are pinned here (spec REQ-005/REQ-006, CON-012):

  * **``cost_survival``** — for each haircut ladder level (bps), does the HEADLINE mean estimated edge
    stay ``> 0``? So "survives only at 0 bps" is an explicit, assertable field, not a hidden default.
  * **``freshness_artifact_warning``** — ``True`` iff positive HEADLINE edge appears ONLY in the
    stalest freshness bucket (a likely staleness artifact, not a real dislocation).
  * **``decision_quote_coverage``** — computed over ALL VvV decision opportunities (not only fired
    picks). A mostly-``None`` run then reads as "could not price most decisions under the freshness
    bound" (a COVERAGE statement), NOT "measured, no edge" (a false MEASUREMENT claim). Fixture
    coverage != decision coverage.

Trust boundaries:
  * ``hypothesis_only=True`` on EVERY slice; ``n`` disclosed on every slice (GUD-002).
  * ``headline`` and ``diagnostic-partial`` coverage are NEVER mixed into a headline metric — the
    survival/artifact instruments read HEADLINE rows only; diagnostic-partial rows are shown, never
    promoted.
  * The haircut ladder is applied ONLY to ``estimated_edge_bps`` for reporting (CON-007) — it changes
    no upstream decision.
  * No realized / executable / fillability / spread / profit wording anywhere (CON-009): rung-2
    ESTIMATED edge from historical MIDS, raw-edge basis.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel

#: The canonical coverage classes. Headline metrics are computed over ``"headline"`` rows ONLY;
#: ``"diagnostic-partial"`` rows are disclosed as their own slices but never promoted (CON-001).
HEADLINE = "headline"
DIAGNOSTIC_PARTIAL = "diagnostic-partial"

#: Placeholder liquidity dimension. C/P1 prices against historical MIDS with no bid/ask/depth
#: (CON-009), so there is no real liquidity bucket — the dimension is kept for spec-shape parity and
#: labeled a proxy, never a depth claim.
_LIQUIDITY_PROXY = "unknown"


class VenueBehaviorRow(BaseModel):
    """One fired-pick row: TxLINE fair prob priced against a time-aligned venue mid.

    Carries the raw estimated edge (bps) — the haircut ladder is applied at report time, never here.
    """

    side: str
    fair_prob: float  # [0, 1]
    venue_decimal_price: float | None = None  # > 1.0 when present; not used by the aggregation
    staleness_s: int  # age (s) of the used mid at the decision tick
    time_to_close_s: int  # seconds from the decision tick to market close
    estimated_edge_bps: int  # RAW estimated edge (bps); haircut applied at report time only
    coverage_class: str  # "headline" | "diagnostic-partial"


class VenueDecision(BaseModel):
    """One VvV decision opportunity — INCLUDING opportunities where the venue source returned ``None``.

    ``quote_matched`` is ``False`` exactly when the time-aligned source had no quote at/under the
    freshness bound (so the tick could not be priced). ``staleness_s`` is present only for matched
    quotes; it is the age of the mid that was actually used.
    """

    fired: bool
    quote_matched: bool
    staleness_s: int | None = None


class VenueBehaviorSlice(BaseModel):
    """A descriptive slice of estimated dislocation — a hypothesis, never an edge claim (CON-008)."""

    dimensions: dict[str, str]  # side / prob_band / time_to_close / liquidity / freshness / haircut_bps
    n: int
    mean_estimated_edge_bps: float | None
    coverage_class: str  # "headline" | "diagnostic-partial" — never mixed within a slice
    provenance: str = "backfilled-price-history"
    hypothesis_only: bool = True


class DecisionQuoteCoverage(BaseModel):
    """The self-falsifying instrument (CON-012): quote coverage over ALL VvV decisions, not just fired.

    A low ``quote_matched_pct`` means "could not price most decisions under the freshness bound," a
    COVERAGE statement — NOT "measured, no edge." ``freshness_bucket_counts_for_used_quotes`` buckets
    ONLY the matched-quote staleness, so its values sum to ``quote_matched_count``.
    """

    decision_count: int
    quote_matched_count: int
    quote_none_count: int
    quote_matched_pct: float
    fired_pick_count: int
    fired_pick_quote_matched_count: int
    fired_pick_quote_none_count: int
    freshness_bucket_counts_for_used_quotes: dict[str, int]


class VenueBehaviorReport(BaseModel):
    """Report-only hypothesis-discovery layer: descriptive slices + three self-falsifying instruments."""

    slices: list[VenueBehaviorSlice]
    cost_survival: dict[int, bool]  # haircut level (bps) -> HEADLINE mean edge stays > 0 there
    freshness_artifact_warning: bool
    decision_quote_coverage: DecisionQuoteCoverage


class HypothesisLedgerEntry(BaseModel):
    """A per-run ledger record (CON-008): the run's slices are QUESTIONS for a future run, not claims."""

    run_id: str
    hypothesis_only: bool = True
    slice_count: int
    headline_slice_count: int
    cost_survival: dict[int, bool]
    freshness_artifact_warning: bool
    quote_matched_pct: float
    decision_count: int


def _parse_seconds(label: str) -> int:
    """Parse a ``"<=Nm"`` / ``"<=Ns"`` freshness-bucket label into a seconds threshold."""
    body = label.replace("<=", "").strip()
    unit = body[-1]
    value = int(body[:-1])
    return value * 60 if unit == "m" else value


def _freshness_bucket(staleness_s: int, freshness_buckets: Sequence[str]) -> str:
    """Assign a staleness age to the first (freshest) bucket whose threshold covers it."""
    ordered = sorted(freshness_buckets, key=_parse_seconds)
    for label in ordered:
        if staleness_s <= _parse_seconds(label):
            return label
    return ordered[-1]  # beyond the widest bound (should not occur under the source's freshness bound)


def _prob_band(fair_prob: float, prob_bands: Sequence[tuple[int, int]]) -> str:
    """Map a fair probability in [0, 1] to a ``"lo-hi"`` percentage band label."""
    pct = fair_prob * 100.0
    for lo, hi in prob_bands:
        if lo <= pct < hi or (pct >= hi and (lo, hi) == prob_bands[-1]):
            return f"{lo}-{hi}"
    return f"{prob_bands[-1][0]}-{prob_bands[-1][1]}"


def _ttc_bucket(ttc_s: int) -> str:
    """Map seconds-to-close to a canonical time-to-close bucket label (spec §4 pinned boundaries)."""
    if ttc_s >= 24 * 3600:
        return ">24h"
    if ttc_s >= 6 * 3600:
        return "6-24h"
    if ttc_s >= 3600:
        return "1-6h"
    return "<1h"


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def build_venue_behavior_report(
    rows: Sequence[VenueBehaviorRow],
    decisions: Sequence[VenueDecision],
    *,
    haircut_ladder_bps: Sequence[int],
    prob_bands: Sequence[tuple[int, int]],
    ttc_buckets: Sequence[str],
    freshness_buckets: Sequence[str],
) -> VenueBehaviorReport:
    """Aggregate fired-pick rows into hypothesis slices + self-falsifying instruments (pure, offline).

    Slices are cut per ``(side, prob_band, time_to_close, freshness, coverage_class) × haircut level``;
    each carries ``n``, ``hypothesis_only=True`` and its own ``coverage_class`` (headline and
    diagnostic-partial never share a slice). The haircut ladder is applied to ``estimated_edge_bps``
    for REPORTING only (CON-007). ``cost_survival`` / ``freshness_artifact_warning`` read HEADLINE
    rows only; ``decision_quote_coverage`` is computed over ALL ``decisions`` (CON-012).
    """
    # --- Slices: group rows by descriptive dimension, then fan out over the haircut ladder ---------
    grouped: dict[tuple[str, str, str, str, str], list[VenueBehaviorRow]] = defaultdict(list)
    for row in rows:
        key = (
            row.side,
            _prob_band(row.fair_prob, prob_bands),
            _ttc_bucket(row.time_to_close_s),
            _freshness_bucket(row.staleness_s, freshness_buckets),
            row.coverage_class,
        )
        grouped[key].append(row)

    slices: list[VenueBehaviorSlice] = []
    for (side, prob_band, ttc, freshness, coverage), group in grouped.items():
        for haircut in haircut_ladder_bps:
            adjusted = [float(r.estimated_edge_bps - haircut) for r in group]
            slices.append(
                VenueBehaviorSlice(
                    dimensions={
                        "side": side,
                        "prob_band": prob_band,
                        "time_to_close": ttc,
                        "liquidity": _LIQUIDITY_PROXY,
                        "freshness": freshness,
                        "haircut_bps": str(haircut),
                    },
                    n=len(group),
                    mean_estimated_edge_bps=_mean(adjusted),
                    coverage_class=coverage,
                )
            )

    # --- cost_survival: HEADLINE-only mean edge per ladder level (diagnostic rows never dragged in) -
    headline_edges = [float(r.estimated_edge_bps) for r in rows if r.coverage_class == HEADLINE]
    cost_survival: dict[int, bool] = {}
    for haircut in haircut_ladder_bps:
        headline_mean = _mean([e - haircut for e in headline_edges])
        cost_survival[haircut] = headline_mean is not None and headline_mean > 0

    # --- freshness_artifact_warning: positive HEADLINE edge ONLY in the stalest freshness bucket ----
    stalest = sorted(freshness_buckets, key=_parse_seconds)[-1]
    bucket_edges: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        if row.coverage_class == HEADLINE:
            bucket_edges[_freshness_bucket(row.staleness_s, freshness_buckets)].append(
                float(row.estimated_edge_bps)
            )
    positive_buckets = {b for b, edges in bucket_edges.items() if (_mean(edges) or 0.0) > 0}
    freshness_artifact_warning = positive_buckets == {stalest}

    decision_coverage = _decision_quote_coverage(decisions, freshness_buckets)

    return VenueBehaviorReport(
        slices=slices,
        cost_survival=cost_survival,
        freshness_artifact_warning=freshness_artifact_warning,
        decision_quote_coverage=decision_coverage,
    )


def _decision_quote_coverage(
    decisions: Sequence[VenueDecision], freshness_buckets: Sequence[str]
) -> DecisionQuoteCoverage:
    """Coverage over ALL decision opportunities (CON-012), plus a separate fired-pick split."""
    decision_count = len(decisions)
    matched = [d for d in decisions if d.quote_matched]
    quote_matched_count = len(matched)
    quote_none_count = decision_count - quote_matched_count
    quote_matched_pct = round(quote_matched_count / decision_count * 100.0, 4) if decision_count else 0.0

    fired = [d for d in decisions if d.fired]
    fired_pick_count = len(fired)
    fired_pick_quote_matched_count = sum(1 for d in fired if d.quote_matched)

    # Bucket ONLY matched-quote staleness (unmatched quotes have no age) -> values sum to matched count.
    bucket_counts: dict[str, int] = dict.fromkeys(freshness_buckets, 0)
    for d in matched:
        if d.staleness_s is not None:
            bucket_counts[_freshness_bucket(d.staleness_s, freshness_buckets)] += 1

    return DecisionQuoteCoverage(
        decision_count=decision_count,
        quote_matched_count=quote_matched_count,
        quote_none_count=quote_none_count,
        quote_matched_pct=quote_matched_pct,
        fired_pick_count=fired_pick_count,
        fired_pick_quote_matched_count=fired_pick_quote_matched_count,
        fired_pick_quote_none_count=fired_pick_count - fired_pick_quote_matched_count,
        freshness_bucket_counts_for_used_quotes=bucket_counts,
    )


def register_hypothesis_ledger_entry(
    report: VenueBehaviorReport, *, ledger_path: Path, run_id: str
) -> HypothesisLedgerEntry:
    """Append ONE hypothesis-ledger entry for this run (CON-008) and return it.

    The report's slices are registered as a single ledgered hypothesis — a question for a future run,
    never an edge claim. The entry is appended as one JSON line (JSONL); the ledger accumulates one
    line per run.
    """
    entry = HypothesisLedgerEntry(
        run_id=run_id,
        slice_count=len(report.slices),
        headline_slice_count=sum(1 for s in report.slices if s.coverage_class == HEADLINE),
        cost_survival=report.cost_survival,
        freshness_artifact_warning=report.freshness_artifact_warning,
        quote_matched_pct=report.decision_quote_coverage.quote_matched_pct,
        decision_count=report.decision_quote_coverage.decision_count,
    )
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_ledger_payload(entry), sort_keys=True) + "\n")
    return entry


def _ledger_payload(entry: HypothesisLedgerEntry) -> dict[str, Any]:
    """Serialize a ledger entry with string haircut keys (JSON object keys are strings)."""
    payload = entry.model_dump()
    payload["cost_survival"] = {str(k): v for k, v in entry.cost_survival.items()}
    return payload
