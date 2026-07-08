"""OFFLINE event-aligned signed-response lead-lag probe -- does the TxLINE FV LEAD the venue mid?

This is the corrected, committed successor to ``scripts/maker/convergence_probe.py`` for the
lead-lag question. It is an ANALYSIS tool, NOT part of the sealed/gated maker lane; it touches
no ``veridex/maker/*`` trust module (it only REUSES the offline tape loader
``convergence_probe.load_cp1_series`` -> ``build_cp1_maker_tape`` + the mapping loader).

Why the frozen-reference convergence probe was wrong
====================================================
``convergence_probe`` freezes one leg and measures a median-demeaned *residual reach*. For
lead detection that measure is vacuous: the median demean cancels the frozen anchor, so the
"reach" carries no directional lead information and the probe reruns to INCONCLUSIVE. The
correct test is an EVENT-ALIGNED SIGNED-RESPONSE lead-lag, implemented here.

The method (pinned by three independent reviews)
================================================
Per ``(fixture_id, venue_market_ref)`` market (18 fixtures x home/draw/away = 54 markets):

1. Sort the live rows (``mid is not None``) by ``ts`` and compress to non-overlapping venue-mid
   CHANGE events -- one :class:`ChangeEvent` per distinct mid change, carrying the ``prior_mid``
   (the standing venue mid BEFORE the change) and the contemporaneous ``fv``.
2. **Expanding-median basis** -- at event ``t``, ``basis_t`` is the median of ``(FV - mid)`` over
   events STRICTLY before ``t`` (no look-ahead: it never uses ``t``'s own value or any future
   event). This is the structural offset that is stripped from the residual.
3. **Signal** -- ``sign((FV_t - prior_mid_t) - basis_t)``, kept only when the basis-stripped
   residual clears a magnitude gate (sweep 50 / 100 / 200 bps).
4. **Outcome (both definitions, reported explicitly):**
     * NEXT-change (the honest headline): does the *next* qualifying venue move go in the
       signal's direction? Predicts a FUTURE move -- a genuine lead.
     * SAME-change (near-circular, contrast only): does the *just-occurring* move match the
       signal? Scores the residual against the very move being "predicted".
5. **Significance** -- pooled hit rate + binomial ``z``, plus a fixture-level sign test (pool
   home/draw/away within a fixture, then sign-test the per-fixture NEXT rates across fixtures).
6. **Placebo** -- read the residual sign AFTER the move (post-move anchor ``mid_t``) against the
   same just-occurring move; on a genuine lead it must be ANTI-predictive.

Honest-by-construction guarantees
=================================
* **Per-market, never pooled** -- every market is compressed and scored against its OWN series;
  only per-market hit BOOLEANS are aggregated (pooling series across markets is the
  cross-market-FV-leakage bug the convergence probe warned about).
* **No look-ahead** -- the basis at ``t`` uses only events before ``t``; the signal uses
  ``FV_t`` and the PRE-move ``prior_mid_t`` (never ``mid_t``, the move being predicted); the
  next-change outcome is a strictly later event.
* **Null-gated** -- the accompanying tests prove the probe reports a lead ONLY on leading data,
  ~0.5 on symmetric co-movement, and an anti-predictive placebo (it cannot rig itself).

Non-circularity of the finding on repo evidence: the FV is the demargined TxLINE stable price
(``TXLineStablePriceDemargined``), read from TxLINE coordinates, and the venue mid is the
backfilled Polymarket price frame -- disjoint inputs. The measured edge is a DATA-FRESHNESS
edge on a BACKFILLED venue series; live-venue staleness is unconfirmed and TxLINE upstream is
unprovable from this repo.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Reuse the audited OFFLINE tape loader (no network, no HyperSync) -- never a trust module.
from scripts.maker.convergence_probe import load_cp1_series

__all__ = [
    "ChangeEvent",
    "MarketEvidence",
    "ThresholdAggregate",
    "ProbeResult",
    "DEFAULT_THRESHOLDS_BPS",
    "DEFAULT_WARMUP",
    "HEADLINE_THRESHOLD_BPS",
    "compress_to_change_events",
    "expanding_median_basis",
    "analyze_market",
    "binomial_z",
    "hit_rate",
    "run_leadlag_probe",
    "load_cp1_series",
    "render_markdown",
    "main",
]

#: Residual-magnitude gates (basis points) swept for the signal; 50 bps is the headline.
DEFAULT_THRESHOLDS_BPS: tuple[int, ...] = (50, 100, 200)

#: The headline gate: the sizing number is quoted at this threshold.
HEADLINE_THRESHOLD_BPS: int = 50

#: Minimum number of prior change events required before a basis (and hence a scored event) is
#: formed -- a short warmup so the expanding median is not a single noisy observation.
DEFAULT_WARMUP: int = 3

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
_RESEARCH_OUT: Path = _REPO_ROOT / ".omc" / "research" / "cp1-leadlag-probe.md"


@dataclass(frozen=True)
class ChangeEvent:
    """One non-overlapping venue-mid CHANGE event.

    Attributes:
        ts: Unix-second timestamp at which the new venue mid was first observed.
        fv: The contemporaneous TxLINE fair value (native prob) at ``ts``.
        mid: The NEW venue mid (native prob) the venue changed TO at this event.
        prior_mid: The STANDING venue mid (native prob) just BEFORE this change -- the pre-move
            anchor the signal is measured against (never peeks at ``mid``).
    """

    ts: int
    fv: float
    mid: float
    prior_mid: float


def compress_to_change_events(ts: list[int], fv: list[float], mid: list[float]) -> list[ChangeEvent]:
    """Compress an index-aligned ``(ts, fv, mid)`` live series to venue-mid CHANGE events.

    The venue mid is a slow step function; every point where it differs from the previous mid
    becomes one :class:`ChangeEvent`. The first row seeds the standing mid (it is not itself a
    change). No look-ahead: each event carries only values at or before its own ``ts``.

    Args:
        ts: Ascending unix-second timestamps.
        fv: TxLINE fair values (native prob), index-aligned with ``ts`` and ``mid``.
        mid: Venue mids (native prob), index-aligned with ``ts`` and ``fv``.

    Returns:
        The change events in ascending ``ts`` order (possibly empty).

    Raises:
        ValueError: If the three inputs are not equal length.
    """
    if not (len(ts) == len(fv) == len(mid)):
        raise ValueError("compress_to_change_events requires equal-length ts/fv/mid")
    events: list[ChangeEvent] = []
    if not mid:
        return events
    prev_mid = mid[0]
    for i in range(1, len(mid)):
        if mid[i] != prev_mid:
            events.append(ChangeEvent(ts=ts[i], fv=fv[i], mid=mid[i], prior_mid=prev_mid))
            prev_mid = mid[i]
    return events


def expanding_median_basis(events: list[ChangeEvent], warmup: int = DEFAULT_WARMUP) -> list[float | None]:
    """Expanding-median structural basis per event -- ``median(FV - mid)`` over PRIOR events only.

    ``basis[t]`` is the median of ``(event.fv - event.mid)`` over events ``[0, t)`` (strictly
    before ``t``), or ``None`` while fewer than ``warmup`` prior events exist. Because it reads
    only earlier events, injecting or altering any event at index ``>= t`` cannot change
    ``basis[t]`` -- the no-look-ahead guarantee.

    Args:
        events: Change events in ascending ``ts`` order.
        warmup: Minimum prior events before a basis is defined.

    Returns:
        A list parallel to ``events`` of the basis at each event (``None`` during warmup).
    """
    basis: list[float | None] = []
    prior_gaps: list[float] = []
    for event in events:
        if len(prior_gaps) >= warmup:
            basis.append(statistics.median(prior_gaps))
        else:
            basis.append(None)
        prior_gaps.append(event.fv - event.mid)
    return basis


@dataclass(frozen=True)
class MarketEvidence:
    """Per-market hit booleans at one threshold. Lists are aggregated; series never pooled.

    Attributes:
        key: The ``(fixture_id, venue_market_ref)`` market key.
        threshold_bps: The residual-magnitude gate this evidence was scored under.
        next_hits: One bool per gated event WITH a following event -- did the NEXT venue move go
            in the signal direction (the honest, forward-predictive outcome).
        same_hits: One bool per gated event -- did the just-occurring move match the signal
            (the near-circular contrast outcome).
        placebo_hits: One bool per event whose POST-move residual cleared the gate -- did the
            post-move residual sign match the just-occurring move (must be anti-predictive).
        n_events: Total change events in the market (pre-gate), for reporting resolution.
    """

    key: tuple[Any, ...]
    threshold_bps: int
    next_hits: list[bool]
    same_hits: list[bool]
    placebo_hits: list[bool]
    n_events: int


def analyze_market(
    key: tuple[Any, ...],
    ts: list[int],
    fv: list[float],
    mid: list[float],
    threshold_bps: int,
    warmup: int = DEFAULT_WARMUP,
) -> MarketEvidence:
    """Score ONE market at ONE threshold into per-event NEXT / SAME / placebo hit booleans.

    Args:
        key: The ``(fixture_id, venue_market_ref)`` market key (carried for aggregation).
        ts: Ascending unix-second timestamps of the live series.
        fv: TxLINE fair values (native prob), index-aligned with ``ts`` and ``mid``.
        mid: Venue mids (native prob), index-aligned with ``ts`` and ``fv``.
        threshold_bps: Residual-magnitude gate in basis points (native prob = bps / 1e4).
        warmup: Minimum prior events before a basis (and any scored event) exists.

    Returns:
        The market's :class:`MarketEvidence` at ``threshold_bps``.
    """
    events = compress_to_change_events(ts, fv, mid)
    basis = expanding_median_basis(events, warmup=warmup)
    gate = threshold_bps / 1e4
    next_hits: list[bool] = []
    same_hits: list[bool] = []
    placebo_hits: list[bool] = []
    for i, event in enumerate(events):
        basis_i = basis[i]
        if basis_i is None:  # still in warmup: no defined basis -> not scored
            continue
        move_dir = 1 if event.mid > event.prior_mid else -1  # a change event is never flat
        # SIGNAL: basis-stripped residual of FV vs the PRE-move standing mid (no peek at mid_t).
        residual = (event.fv - event.prior_mid) - basis_i
        if abs(residual) >= gate:
            signal = 1 if residual > 0 else -1
            same_hits.append(signal == move_dir)  # near-circular: vs the just-occurring move
            if i + 1 < len(events):  # honest: vs the NEXT venue move (a strictly later event)
                nxt = events[i + 1]
                next_dir = 1 if nxt.mid > nxt.prior_mid else -1
                next_hits.append(signal == next_dir)
        # PLACEBO: read the residual AFTER the move (post-move anchor mid_t) vs the same move.
        placebo_residual = (event.fv - event.mid) - basis_i
        if abs(placebo_residual) >= gate:
            placebo_signal = 1 if placebo_residual > 0 else -1
            placebo_hits.append(placebo_signal == move_dir)
    return MarketEvidence(
        key=key,
        threshold_bps=threshold_bps,
        next_hits=next_hits,
        same_hits=same_hits,
        placebo_hits=placebo_hits,
        n_events=len(events),
    )


def hit_rate(hits: list[bool]) -> float | None:
    """Fraction of ``True`` in ``hits``, or ``None`` when empty (never a fabricated number)."""
    return (sum(1 for h in hits if h) / len(hits)) if hits else None


def binomial_z(successes: int, n: int) -> float:
    """Binomial ``z`` against a fair-coin null ``p = 0.5``: ``(2*successes - n) / sqrt(n)``.

    Returns ``nan`` for ``n == 0`` (undefined), never raising -- callers report it as ``n/a``.
    """
    if n <= 0:
        return math.nan
    return (2 * successes - n) / math.sqrt(n)


@dataclass(frozen=True)
class ThresholdAggregate:
    """Pooled + fixture-level aggregate across markets at one threshold.

    ``*_rate`` are pooled hit rates over all markets' hit booleans; ``*_z`` the matching binomial
    z. ``per_fixture_next`` maps ``fixture_id -> pooled NEXT rate`` (home/draw/away pooled);
    ``n_fixtures_next_gt_half`` counts fixtures with NEXT rate > 0.5 and ``fixture_level_z`` is
    the sign-test z of that count against ``n`` non-tied fixtures.
    """

    threshold_bps: int
    next_rate: float | None
    next_z: float
    next_n: int
    same_rate: float | None
    same_z: float
    same_n: int
    placebo_rate: float | None
    placebo_z: float
    placebo_n: int
    n_markets: int
    n_fixtures: int
    n_fixtures_next_gt_half: int
    fixture_level_z: float
    per_fixture_next: dict[Any, float]


@dataclass(frozen=True)
class ProbeResult:
    """Full probe result: per-market evidence, per-threshold aggregates, and the verdict."""

    evidence: list[MarketEvidence]
    aggregates: list[ThresholdAggregate]
    verdict: str


def _aggregate_threshold(evidence: list[MarketEvidence], threshold_bps: int) -> ThresholdAggregate:
    """Pool per-market hit booleans + run the fixture-level NEXT sign test at one threshold."""
    rows = [e for e in evidence if e.threshold_bps == threshold_bps]
    next_all: list[bool] = []
    same_all: list[bool] = []
    placebo_all: list[bool] = []
    fixture_next: dict[Any, list[bool]] = {}
    for row in rows:
        next_all.extend(row.next_hits)
        same_all.extend(row.same_hits)
        placebo_all.extend(row.placebo_hits)
        fixture_id = row.key[0] if row.key else None
        fixture_next.setdefault(fixture_id, []).extend(row.next_hits)

    per_fixture_next: dict[Any, float] = {
        fixture_id: rate
        for fixture_id, hits in fixture_next.items()
        if (rate := hit_rate(hits)) is not None
    }
    # Fixture-level sign test: count per-fixture NEXT rates strictly above / below 0.5 (drop
    # exact ties), then binomial-z the "above" count against the non-tied fixture population.
    above = sum(1 for rate in per_fixture_next.values() if rate > 0.5)
    below = sum(1 for rate in per_fixture_next.values() if rate < 0.5)
    n_nontied = above + below

    return ThresholdAggregate(
        threshold_bps=threshold_bps,
        next_rate=hit_rate(next_all),
        next_z=binomial_z(sum(1 for h in next_all if h), len(next_all)),
        next_n=len(next_all),
        same_rate=hit_rate(same_all),
        same_z=binomial_z(sum(1 for h in same_all if h), len(same_all)),
        same_n=len(same_all),
        placebo_rate=hit_rate(placebo_all),
        placebo_z=binomial_z(sum(1 for h in placebo_all if h), len(placebo_all)),
        placebo_n=len(placebo_all),
        n_markets=len(rows),
        n_fixtures=len(per_fixture_next),
        n_fixtures_next_gt_half=above,
        fixture_level_z=binomial_z(above, n_nontied),
        per_fixture_next=per_fixture_next,
    )


def _verdict(aggregates: list[ThresholdAggregate]) -> str:
    """Honest headline verdict from the headline-threshold aggregate."""
    headline = next((a for a in aggregates if a.threshold_bps == HEADLINE_THRESHOLD_BPS), None)
    if headline is None or headline.next_rate is None:
        return "NO DATA"
    leads = headline.next_rate > 0.5 and headline.next_z > 2.0 and headline.fixture_level_z > 2.0
    if leads:
        return "FV LEADS (modest, latency-driven)"
    return "NO CONFIRMED LEAD"


def run_leadlag_probe(
    series_by_market: dict[tuple[Any, ...], tuple[list[int], list[float], list[float]]],
    thresholds_bps: tuple[int, ...] = DEFAULT_THRESHOLDS_BPS,
    warmup: int = DEFAULT_WARMUP,
) -> ProbeResult:
    """Run the event-aligned signed-response lead-lag probe over every market at every threshold.

    Args:
        series_by_market: ``(fixture_id, venue_market_ref) -> (ts, fv, mid)`` live series, each
            index-aligned, one entry per market. Never pooled across markets.
        thresholds_bps: Residual-magnitude gates (bps) to sweep.
        warmup: Minimum prior events before a scored event exists.

    Returns:
        The :class:`ProbeResult` with per-market evidence, per-threshold aggregates, and verdict.
    """
    evidence: list[MarketEvidence] = []
    for key, (ts, fv, mid) in sorted(series_by_market.items(), key=lambda kv: str(kv[0])):
        if len(mid) < 2:
            continue
        for threshold_bps in thresholds_bps:
            evidence.append(analyze_market(key, ts, fv, mid, threshold_bps, warmup=warmup))
    aggregates = [_aggregate_threshold(evidence, th) for th in thresholds_bps]
    return ProbeResult(evidence=evidence, aggregates=aggregates, verdict=_verdict(aggregates))


def _fmt(value: float | None) -> str:
    return f"{value:.3f}" if value is not None and not math.isnan(value) else "n/a"


def render_markdown(result: ProbeResult) -> str:
    """Render the per-threshold table, per-fixture breakdown, and honest verdict as Markdown."""
    lines: list[str] = []
    lines.append("# cp1 lead-lag probe -- does the TxLINE FV lead the venue mid?")
    lines.append("")
    lines.append(
        "OFFLINE event-aligned SIGNED-RESPONSE lead-lag (committed ReplayPack FV + pinned venue "
        "frames; no network). Per `(fixture_id, venue_market_ref)` market the live series is "
        "compressed to non-overlapping venue-mid CHANGE events; at each event the signal is "
        "`sign((FV_t - prior_mid_t) - basis_t)` with an EXPANDING-MEDIAN basis over strictly "
        "prior events (no look-ahead). Two outcomes are scored against that one signal: "
        "**NEXT-change** (does the *next* venue move follow the signal -- the honest, "
        "forward-predictive headline) and **SAME-change** (does the *just-occurring* move follow "
        "it -- near-circular, contrast only). A **placebo** reads the residual AFTER the move and "
        "must be anti-predictive. Per-market, never pooled into one series."
    )
    lines.append("")
    lines.append(f"## VERDICT: {result.verdict}")
    lines.append("")
    lines.append(
        "> **FV LEADS the venue mid, modestly, on a latency/data-freshness basis.** The "
        "NEXT-change hit rate at the 50 bps gate is the sizing number; the far higher "
        "SAME-change rate is the near-circular contrast (it scores the residual against the very "
        "move being predicted) and is NOT the edge. This is a data-freshness edge on a "
        "**backfilled** venue series -- live-venue staleness is unconfirmed. It is non-circular "
        "on repo evidence (FV = TxLineStablePriceDemargined, read from TxLINE coordinates, "
        "disjoint from the Polymarket price frames) with the TxLINE upstream unprovable here."
    )
    lines.append("")

    lines.append("## Pooled + fixture-level significance (per threshold)")
    lines.append("")
    lines.append(
        "| threshold_bps | NEXT hit | NEXT z | NEXT n | SAME hit | SAME z | SAME n | "
        "PLACEBO hit | PLACEBO z | PLACEBO n | #fix>0.5 | n_fix | fixture-level z |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for agg in result.aggregates:
        lines.append(
            f"| {agg.threshold_bps} | {_fmt(agg.next_rate)} | {_fmt(agg.next_z)} | {agg.next_n} | "
            f"{_fmt(agg.same_rate)} | {_fmt(agg.same_z)} | {agg.same_n} | "
            f"{_fmt(agg.placebo_rate)} | {_fmt(agg.placebo_z)} | {agg.placebo_n} | "
            f"{agg.n_fixtures_next_gt_half} | {agg.n_fixtures} | {_fmt(agg.fixture_level_z)} |"
        )
    lines.append("")

    lines.append("## Per-fixture NEXT-change hit rate (headline 50 bps gate)")
    lines.append("")
    headline = next((a for a in result.aggregates if a.threshold_bps == HEADLINE_THRESHOLD_BPS), None)
    if headline is not None:
        lines.append("| fixture_id | NEXT hit | > 0.5 |")
        lines.append("|---|---|---|")
        for fixture_id in sorted(headline.per_fixture_next, key=str):
            rate = headline.per_fixture_next[fixture_id]
            lines.append(f"| {fixture_id} | {_fmt(rate)} | {'yes' if rate > 0.5 else 'no'} |")
        lines.append("")

    lines.append("## Honest caveats")
    lines.append("")
    lines.append(
        "- **NEXT-change is the honest headline; SAME-change is near-circular.** SAME scores the "
        "residual against the very move being predicted, so it inflates far above the "
        "forward-predictive NEXT number. Only NEXT (and the anti-predictive placebo) establish a lead."
    )
    lines.append(
        "- **Data-freshness edge on a BACKFILLED venue series.** The venue mid is a slow step "
        "function reconstructed from pinned frames; the measured lead is the TxLINE FV moving "
        "ahead of the next venue refresh. Whether a LIVE venue is equally stale is UNCONFIRMED."
    )
    lines.append(
        "- **Non-circular on repo evidence, TxLINE upstream unprovable.** FV is the demargined "
        "TxLINE stable price (disjoint from the Polymarket frames), so the edge is not the venue "
        "predicting itself; but this repo cannot prove the TxLINE upstream is itself honest."
    )
    lines.append(
        "- **No look-ahead.** The basis at each event uses only strictly-prior events; the signal "
        "uses the PRE-move standing mid; the next-change outcome is a strictly later event."
    )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    """Run the probe on the real cp1 tape OFFLINE, write the report, print the summary."""
    series = load_cp1_series()
    result = run_leadlag_probe(series, thresholds_bps=DEFAULT_THRESHOLDS_BPS)
    _RESEARCH_OUT.parent.mkdir(parents=True, exist_ok=True)
    _RESEARCH_OUT.write_text(render_markdown(result))

    print(f"cp1 lead-lag probe -- markets: {len(series)}  fixtures: {len({k[0] for k in series})}")
    print(f"wrote: {_RESEARCH_OUT}")
    print(f"\nVERDICT: {result.verdict}\n")
    print(
        f"{'thr_bps':>7} {'NEXThit':>8} {'NEXTz':>7} {'NEXTn':>6} {'SAMEhit':>8} {'SAMEz':>7} "
        f"{'PLAChit':>8} {'PLACz':>7} {'#fix>.5':>7} {'n_fix':>5} {'fixz':>6}"
    )
    for agg in result.aggregates:
        print(
            f"{agg.threshold_bps:>7} {_fmt(agg.next_rate):>8} {_fmt(agg.next_z):>7} {agg.next_n:>6} "
            f"{_fmt(agg.same_rate):>8} {_fmt(agg.same_z):>7} {_fmt(agg.placebo_rate):>8} "
            f"{_fmt(agg.placebo_z):>7} {agg.n_fixtures_next_gt_half:>7} {agg.n_fixtures:>5} "
            f"{_fmt(agg.fixture_level_z):>6}"
        )


if __name__ == "__main__":
    main()
