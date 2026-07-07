"""E4 predeclared aggregate verdict for the lag-vs-overreaction fork probe.

Consumes the per-event ``EventRecord`` stream (E3) and produces the sealed,
fail-closed ``ProbeResult`` -- the primary deliverable of the probe (CON-010).

**The directional set is load-bearing (CON-009/010).** It is EXACTLY the eligible
events' classifier ratio ``R``: records whose ``event_class in {LAG, OVERSHOOT,
REVERSAL}`` *and* ``R is not None``. NO-SIGNAL / excluded records are COUNTED
(``class_counts`` / ``excluded_by_reason``) but never enter a directional CI --
the class gate, not ``R is not None`` alone, is the honest boundary, because a
below-epsilon event may carry a large small-denominator ``grid`` artifact that
must never reach the finding (GUD-001, the Run-002 lesson). The descriptive
``grid`` is likewise never aggregated here.

Verdict rule (CON-010), all thresholds read from ``AggConfig`` (E5's sealed
``ProbeConfig`` will be the superset -- no literal is inlined below):

* **Global:** directional ``n < n_min_global`` -> ``INCONCLUSIVE`` (no CI). Else
  median ``R`` + a **percentile bootstrap CI** (``_bootstrap_ci``: seeded
  ``random.Random`` -> deterministic; 5th/95th percentile of resampled medians).
  CI wholly in ``(0, 1)`` -> ``FOLLOW``; CI wholly ``> 1`` or wholly ``< 0`` ->
  ``FADE``; CI straddling the ``[0, 1]`` boundary -> ``INCONCLUSIVE``.
* **Per-slice (CON-009):** for each slice-tag key, group eligible records by tag
  value; a group with ``n >= n_min_slice`` gets its own directional verdict via
  the same CI rule; a group below the floor is ``DESCRIPTIVE_ONLY`` (reported,
  never directional).
* **SPLIT-BY-SLICE (CON-010):** the overall verdict is ``SPLIT-BY-SLICE`` ONLY if
  >=2 slices each clear ``n_min_slice`` AND hold decisive OPPOSITE verdicts (one
  ``FOLLOW`` and one ``FADE``). A single decisive slice does not trigger it; the
  global verdict otherwise stands.

``INCONCLUSIVE`` is a valid, first-class success (CON-011) -- the fail-closed
result when the data cannot resolve the fork -- and is never dressed up.
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass, field

from veridex.backtest.event_probe.compute import EventRecord

# The three directional event classes (CON-005). NO-SIGNAL is excluded by class.
_DIRECTIONAL_CLASSES: frozenset[str] = frozenset({"LAG", "OVERSHOOT", "REVERSAL"})

# Decisive slice verdicts that can drive SPLIT-BY-SLICE (CON-010). INCONCLUSIVE
# and DESCRIPTIVE_ONLY are not decisive.
_DECISIVE: frozenset[str] = frozenset({"FOLLOW", "FADE"})


@dataclass(frozen=True)
class AggConfig:
    """E4-local aggregation params (CON-009/010 pinned defaults).

    E5's ``ProbeConfig`` becomes the sealed superset / single source of truth;
    every threshold here is read from ``cfg`` -- no literal is inlined in the
    logic below.
    """

    n_min_global: int = 30
    n_min_slice: int = 15
    bootstrap_n: int = 10000
    ci_level: float = 0.90
    seed: int = 20260705


@dataclass(frozen=True)
class SliceVerdict:
    """One context-slice's directional verdict (or DESCRIPTIVE_ONLY below floor).

    ``dimension`` names the CON-007 partition (e.g. ``half``, ``match_timing``) and
    ``slice`` is the bucket value within it (e.g. ``unknown``). Both are carried so
    the summary is self-describing and two dimensions sharing a bucket value (both
    ``unknown``) never collide into one indistinguishable row.
    """

    dimension: str
    slice: str
    n: int
    median_R: float | None
    ci_low: float | None
    ci_high: float | None
    verdict: str  # "FOLLOW" | "FADE" | "INCONCLUSIVE" | "DESCRIPTIVE_ONLY"


@dataclass(frozen=True)
class ProbeResult:
    """The sealed aggregate: global verdict + gated per-slice + full tallies."""

    overall_verdict: str  # "FOLLOW" | "FADE" | "SPLIT-BY-SLICE" | "INCONCLUSIVE"
    global_n: int
    global_median_R: float | None
    global_ci_low: float | None
    global_ci_high: float | None
    per_slice: list[SliceVerdict] = field(default_factory=list)
    class_counts: dict[str, int] = field(default_factory=dict)
    excluded_by_reason: dict[str, int] = field(default_factory=dict)


def _bootstrap_ci(
    values: list[float], *, n_resamples: int, level: float, seed: int
) -> tuple[float, float]:
    """Percentile bootstrap CI of the **median** (CON-010) -- deterministic.

    Draws ``n_resamples`` with-replacement resamples (each of size ``len(values)``)
    from a seeded ``random.Random(seed)``, takes each resample's median, and
    returns the ``(alpha, 1 - alpha)`` percentiles of those resampled medians,
    where ``alpha = (1 - level) / 2``. For ``level = 0.90`` this is the 5th/95th
    percentile. Seeded -> identical output for identical input (Section 6
    determinism requirement).

    Precondition: ``values`` is non-empty (callers gate on the sample floor).
    """
    rng = random.Random(seed)
    n = len(values)
    medians = [statistics.median(rng.choices(values, k=n)) for _ in range(n_resamples)]
    medians.sort()
    alpha = (1.0 - level) / 2.0
    return (_percentile(medians, alpha), _percentile(medians, 1.0 - alpha))


def _percentile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolated ``q``-quantile (``q`` in ``[0, 1]``) of a sorted list.

    numpy-style interpolation between the two nearest ranks, so the CI is a
    well-defined deterministic function of the resampled medians.
    """
    if len(sorted_values) == 1:
        return sorted_values[0]
    idx = q * (len(sorted_values) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = idx - lo
    return sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac


def _verdict_from_ci(ci_low: float, ci_high: float) -> str:
    """Map a directional CI to a verdict (CON-010).

    CI wholly inside ``(0, 1)`` -> ``FOLLOW``; wholly ``> 1`` or wholly ``< 0`` ->
    ``FADE``; anything straddling the ``[0, 1]`` boundary -> ``INCONCLUSIVE``.
    """
    if ci_low > 0.0 and ci_high < 1.0:
        return "FOLLOW"
    if ci_low > 1.0 or ci_high < 0.0:
        return "FADE"
    return "INCONCLUSIVE"


def _directional_R(records: list[EventRecord]) -> list[float]:
    """The directional set: eligible events' ``R`` (class gate, CON-009/010).

    A record is directional iff its ``event_class`` is one of LAG/OVERSHOOT/
    REVERSAL *and* it carries a non-None ``R``. This is the class gate -- not
    ``R is not None`` alone -- so a NO-SIGNAL row cannot leak in via a stray ratio.
    """
    return [
        rec.R
        for rec in records
        if rec.event_class in _DIRECTIONAL_CLASSES and rec.R is not None
    ]


def _directional_verdict(
    values: list[float], cfg: AggConfig
) -> tuple[float, float, float, str]:
    """Compute ``(median_R, ci_low, ci_high, verdict)`` for a directional sample.

    Callers guarantee ``values`` cleared the relevant sample floor, so it is
    non-empty and a CI is always produced.
    """
    median_r = statistics.median(values)
    ci_low, ci_high = _bootstrap_ci(
        values, n_resamples=cfg.bootstrap_n, level=cfg.ci_level, seed=cfg.seed
    )
    return median_r, ci_low, ci_high, _verdict_from_ci(ci_low, ci_high)


def _slice_verdicts(records: list[EventRecord], cfg: AggConfig) -> list[SliceVerdict]:
    """Per-slice directional verdicts, gated by ``n_min_slice`` (CON-009).

    Groups the directional records by every ``(slice_tag_key, tag_value)`` pair;
    a group at/above the slice floor gets its own CI verdict, one below is
    ``DESCRIPTIVE_ONLY`` (reported, never directional). Output is sorted by
    ``(dimension, value)`` for determinism; each ``SliceVerdict`` carries both the
    ``dimension`` (tag key) and ``slice`` (tag value) so rows are self-describing
    and never collide when two dimensions share a bucket value (Section 4).
    """
    groups: dict[tuple[str, str], list[float]] = {}
    for rec in records:
        if rec.event_class not in _DIRECTIONAL_CLASSES or rec.R is None:
            continue
        for key, value in rec.slice_tags.items():
            groups.setdefault((key, value), []).append(rec.R)

    verdicts: list[SliceVerdict] = []
    for (dimension, value), values in sorted(groups.items()):
        n = len(values)
        if n < cfg.n_min_slice:
            verdicts.append(
                SliceVerdict(
                    dimension=dimension,
                    slice=value,
                    n=n,
                    median_R=statistics.median(values),
                    ci_low=None,
                    ci_high=None,
                    verdict="DESCRIPTIVE_ONLY",
                )
            )
            continue
        median_r, ci_low, ci_high, verdict = _directional_verdict(values, cfg)
        verdicts.append(
            SliceVerdict(
                dimension=dimension,
                slice=value,
                n=n,
                median_R=median_r,
                ci_low=ci_low,
                ci_high=ci_high,
                verdict=verdict,
            )
        )
    return verdicts


def _is_split(per_slice: list[SliceVerdict]) -> bool:
    """True iff >=2 floored slices hold decisive OPPOSITE verdicts (CON-010).

    Requires at least one ``FOLLOW`` and at least one ``FADE`` among slices that
    cleared the floor (a below-floor slice is ``DESCRIPTIVE_ONLY`` and never
    decisive). A single decisive slice, or slices that all agree, is not a split.
    """
    decisive = {s.verdict for s in per_slice if s.verdict in _DECISIVE}
    return "FOLLOW" in decisive and "FADE" in decisive


def aggregate_verdict(records: list[EventRecord], cfg: AggConfig) -> ProbeResult:
    """Aggregate the predeclared, fail-closed probe verdict (CON-009/010/011).

    Tallies ``class_counts`` / ``excluded_by_reason`` over ALL records, then
    computes the global directional verdict, the gated per-slice verdicts, and the
    SPLIT-BY-SLICE override. ``INCONCLUSIVE`` (below the global floor or a
    straddling CI) is a valid success (CON-011), never forced into a direction.
    """
    # Tallies over ALL records (directional or not).
    class_counts: dict[str, int] = {}
    excluded_by_reason: dict[str, int] = {}
    for rec in records:
        class_counts[rec.event_class] = class_counts.get(rec.event_class, 0) + 1
        if rec.exclusion_reason is not None:
            excluded_by_reason[rec.exclusion_reason] = (
                excluded_by_reason.get(rec.exclusion_reason, 0) + 1
            )

    directional = _directional_R(records)
    global_n = len(directional)
    per_slice = _slice_verdicts(records, cfg)

    # Global verdict (CON-010): below the floor is INCONCLUSIVE with no CI.
    if global_n < cfg.n_min_global:
        global_verdict = "INCONCLUSIVE"
        global_median_r: float | None = None
        global_ci_low: float | None = None
        global_ci_high: float | None = None
    else:
        global_median_r, global_ci_low, global_ci_high, global_verdict = (
            _directional_verdict(directional, cfg)
        )

    # SPLIT overrides the global only when the global itself clears its floor AND
    # slices decisively disagree (CON-010). Slice dimensions overlap, so two
    # cross-dimension slices can each reach 15 while the DISTINCT global n < 30 --
    # an underpowered global must fail closed to INCONCLUSIVE (GUD-002 / AC-005),
    # never be rescued into a decisive SPLIT.
    overall_verdict = (
        "SPLIT-BY-SLICE"
        if global_n >= cfg.n_min_global and _is_split(per_slice)
        else global_verdict
    )

    return ProbeResult(
        overall_verdict=overall_verdict,
        global_n=global_n,
        global_median_R=global_median_r,
        global_ci_low=global_ci_low,
        global_ci_high=global_ci_high,
        per_slice=per_slice,
        class_counts=class_counts,
        excluded_by_reason=excluded_by_reason,
    )
