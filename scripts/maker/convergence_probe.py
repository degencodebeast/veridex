"""OFFLINE convergence probe -- does the TxLINE fair value LEAD the venue native mid?

SUPERSEDED for lead-lag by scripts/maker/leadlag_probe.py -- the frozen-reference
residual-reach here is vacuous for lead detection (median-demean cancels the anchor); kept as
a documented negative example.

This is an ANALYSIS tool, NOT part of the sealed/gated maker lane. It touches no
``veridex/maker/*`` trust module beyond REUSING the audited, basis-adjusted reach
machinery (``build_convergence_reach`` -> ``decompose_gap`` + ``reach_from_residual``):
reach is always read from the structural-basis-stripped RESIDUAL, never the raw gap.

The empirical question, per market and BOTH directions
=======================================================
For each ``(fixture_id, venue_market_ref)`` market it asks, over a forward horizon:

  * **FV -> venue** ("FV leads"): freeze the TxLINE FV at the window start and measure
    whether the venue mid's basis-adjusted residual gap TIGHTENS toward it over the
    horizon (venue reaches to FV).
  * **venue -> FV** ("venue leads"): freeze the venue mid and measure whether FV reaches
    to it.

The SIGNAL is the ASYMMETRY ``fv_to_venue_reach - venue_to_fv_reach``. Equal reaches ==
co-movement == NO tradeable lead. FV leads only if FV->venue is MATERIALLY greater.

Honest-by-construction guarantees
=================================
* **Per-market, never pooled** -- each market's reach is computed against its OWN FV/mid
  series; only per-market reach FRACTIONS are aggregated (pooling series across markets is
  the cross-market-FV-leakage bug).
* **Forward-horizon, no look-ahead** -- the frozen reference uses only the value at the
  window anchor ``t``; every other point in the window has ``ts > t`` (a legitimate future
  measurement). No settled/final probability is ever used as FV.
* **Residual-only** -- reach comes from ``build_convergence_reach`` (basis stripped), so a
  persistent structural offset can never be read as convergence.
* **Paired-window gate** -- a window is scored only when BOTH series actually move in it,
  so a frozen (stepped) leg cannot make a one-sided asymmetry.
* **Move-deduplicated reach** -- reach is read over a mover's DISTINCT moves (flat runs
  collapsed), so a stepped series' long flats do not dilute its reach toward zero.

The anti-self-fooling core: the venue-resolution trust gate
===========================================================
``reach_from_residual`` counts step-to-step residual shrinkage. Empirically it has NO power
to separate lead from co-movement when both series are well sampled -- it sits at ~0.5. Its
only apparent "signal" comes from a RESOLUTION MISMATCH: freezing FV and watching a coarse,
stepped venue yields a near-zero reach purely because the venue barely moves within a
window (a 2-point residual is forced to reach 0 by the median demean), which masquerades as
"venue leads". To refuse that trap, a directional verdict (FV LEADS / VENUE LEADS) is
emitted ONLY when the venue mover is adequately resolved (``median_venue_moves >=
MIN_MOVES_FOR_TRUST``); otherwise the market is INCONCLUSIVE. A genuine FV lead shows up as
a substantively HIGH FV->venue reach on a well-resolved (moving) venue -- the opposite of
the coarse-venue artifact.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from veridex.maker.basis import decompose_gap
from veridex.maker.diagnostic import build_convergence_reach

__all__ = [
    "MarketReachRow",
    "DirectionalReach",
    "ProbeResult",
    "HorizonAggregate",
    "DEFAULT_HORIZONS_S",
    "MATERIAL_ASYMMETRY",
    "MIN_MOVES_FOR_TRUST",
    "directional_reach",
    "probe_market",
    "run_probe",
    "load_cp1_series",
    "render_markdown",
    "main",
]

#: Forward reach horizons (seconds) swept over the tape's ACTUAL sampling. The cp1 venue
#: mid refreshes on a ~40-minute median interval (p25 ~10 min), so at 30/60/120s the venue
#: is frozen and "does venue converge to FV" is unmeasurable. The horizons below are the
#: ones at which BOTH series actually move within a window (both-vary >=50% only from ~600s).
DEFAULT_HORIZONS_S: tuple[int, ...] = (600, 1800, 3600)

#: Reach-fraction gap that counts as a MATERIAL directional asymmetry (below this the two
#: directions are treated as equal -> co-movement -> no lead).
MATERIAL_ASYMMETRY: float = 0.05

#: Minimum median distinct VENUE moves per window for a directional verdict to be trusted.
#: Below this the venue is too coarsely sampled and any asymmetry is a resolution artifact,
#: not a lead. (A residual reach over <~8 mover points is degenerate: a 2-point residual is
#: forced to 0 by the median demean.)
MIN_MOVES_FOR_TRUST: int = 8

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
_RESEARCH_OUT: Path = _REPO_ROOT / ".omc" / "research" / "cp1-convergence-probe.md"


def _dedupe(series: list[float]) -> list[float]:
    """Collapse consecutive equal values so reach is read over DISTINCT moves, not flats."""
    out = [series[0]]
    for value in series[1:]:
        if value != out[-1]:
            out.append(value)
    return out


def _is_constant(series: list[float]) -> bool:
    """True when a series has effectively zero variance (nothing to converge)."""
    return len({round(x, 9) for x in series}) <= 1


def _window_residual_reach(
    frozen_value: float, mover: list[float], horizon_s: int, *, frozen_is_fv: bool
) -> float | None:
    """Residual reach of one forward window: does ``mover`` reach toward ``frozen_value``?

    Reuses the audited ``build_convergence_reach`` on a paired series whose reference leg is
    the frozen anchor value repeated, so ``decompose_gap`` strips the window's structural
    offset and reach is read from the residual only. The mover's flat runs are collapsed
    first so a stepped series is scored over its real moves. ``frozen_is_fv`` selects which
    argument the frozen leg occupies. Returns ``None`` when fewer than two distinct moves.
    """
    moves = _dedupe(mover)
    if len(moves) < 2:
        return None
    frozen = [frozen_value] * len(moves)
    if frozen_is_fv:
        report = build_convergence_reach(frozen, moves, horizon_s)
    else:
        report = build_convergence_reach(moves, frozen, horizon_s)
    return report.residual_reach_fraction


def _forward_window_end(ts: list[int], anchor: int, horizon_s: int) -> int:
    """Last index ``j >= anchor`` whose ``ts`` is within ``horizon_s`` of ``ts[anchor]``.

    A pure-time forward window (no look-ahead: every ``ts[k] > ts[anchor]`` for ``k>anchor``
    is a legitimate future measurement). Bursty sampling is respected -- a window simply
    holds whatever real points fall inside the horizon.
    """
    n = len(ts)
    j = anchor
    while j + 1 < n and ts[j + 1] - ts[anchor] <= horizon_s:
        j += 1
    return j


@dataclass(frozen=True)
class DirectionalReach:
    """Both-directions held-reference residual reach for one market at one horizon.

    ``median_venue_moves`` / ``median_fv_moves`` are the median distinct MOVER points per
    scored window (the resolution of the venue and FV movers) -- a directional verdict is
    trustworthy only when the venue mover is adequately resolved.
    """

    fv_to_venue_reach: float | None
    venue_to_fv_reach: float | None
    n_windows: int
    median_venue_moves: float | None
    median_fv_moves: float | None


def directional_reach(fv: list[float], venue: list[float], ts: list[int], horizon_s: int) -> DirectionalReach:
    """Both-directions held-reference residual reach for ONE market at one horizon.

    Args:
        fv: TxLINE fair-value native probabilities, index-aligned with ``venue`` and ``ts``.
        venue: Venue native-mid probabilities.
        ts: Ascending unix-second timestamps for each (fv, venue) pair.
        horizon_s: Forward reach horizon in seconds.
    """
    if not (len(fv) == len(venue) == len(ts)):
        raise ValueError("directional_reach requires equal-length fv/venue/ts")
    n = len(ts)
    f2v: list[float] = []
    v2f: list[float] = []
    venue_moves: list[int] = []
    fv_moves: list[int] = []
    n_windows = 0
    for i in range(n):
        j = _forward_window_end(ts, i, horizon_s)
        if j <= i:
            continue
        venue_window = venue[i : j + 1]
        fv_window = fv[i : j + 1]
        # PAIRED-WINDOW GATE: only score a window when BOTH movers actually move in it, so
        # the two directions sample the SAME windows and a frozen (stepped) leg can never
        # manufacture a one-sided asymmetry.
        if _is_constant(venue_window) or _is_constant(fv_window):
            continue
        n_windows += 1
        venue_moves.append(len(_dedupe(venue_window)))
        fv_moves.append(len(_dedupe(fv_window)))
        # FV -> venue: FV frozen at anchor, venue moves toward it.
        rf = _window_residual_reach(fv[i], venue_window, horizon_s, frozen_is_fv=True)
        # venue -> FV: venue frozen at anchor, FV moves toward it.
        rv = _window_residual_reach(venue[i], fv_window, horizon_s, frozen_is_fv=False)
        if rf is not None:
            f2v.append(rf)
        if rv is not None:
            v2f.append(rv)
    return DirectionalReach(
        fv_to_venue_reach=statistics.mean(f2v) if f2v else None,
        venue_to_fv_reach=statistics.mean(v2f) if v2f else None,
        n_windows=n_windows,
        median_venue_moves=statistics.median(venue_moves) if venue_moves else None,
        median_fv_moves=statistics.median(fv_moves) if fv_moves else None,
    )


def _naive_raw_gap_directional(
    fv: list[float], venue: list[float], ts: list[int], horizon_s: int
) -> tuple[float, float]:
    """DELIBERATELY-WRONG control: the same held-reference measure on the RAW gap (no basis
    strip, no dedupe). It is fooled by a monotone basis closure (the raw gap shrinks) and
    falsely reports a lead where the residual probe does not. Exists only to demonstrate
    that contrast in the test suite -- never used by the real probe.
    """

    def _raw_reach(frozen: float, mover: list[float]) -> float | None:
        if len(mover) < 2:
            return None
        gaps = [abs(frozen - m) for m in mover]
        shrinks = sum(1 for k in range(len(gaps) - 1) if gaps[k + 1] < gaps[k])
        return shrinks / (len(gaps) - 1)

    n = len(ts)
    f2v: list[float] = []
    v2f: list[float] = []
    for i in range(n):
        j = _forward_window_end(ts, i, horizon_s)
        if j <= i:
            continue
        rf = _raw_reach(fv[i], venue[i : j + 1])
        rv = _raw_reach(venue[i], fv[i : j + 1])
        if rf is not None:
            f2v.append(rf)
        if rv is not None:
            v2f.append(rv)
    return (
        statistics.mean(f2v) if f2v else 0.0,
        statistics.mean(v2f) if v2f else 0.0,
    )


@dataclass(frozen=True)
class MarketReachRow:
    """One market's both-directions reach at one horizon.

    ``basis_bps`` (the structural offset) is surfaced SEPARATELY and is NEVER a lead signal.
    ``asymmetry = fv_to_venue_reach - venue_to_fv_reach`` (``None`` if either direction did
    not resolve). ``resolution_ok`` is ``True`` only when the venue mover is adequately
    sampled (``median_venue_moves >= MIN_MOVES_FOR_TRUST``); a directional verdict on a
    market with ``resolution_ok is False`` is an artifact, not a lead.
    """

    key: tuple[Any, ...]
    horizon_s: int
    basis_bps: int
    fv_to_venue_reach: float | None
    venue_to_fv_reach: float | None
    asymmetry: float | None
    median_venue_moves: float | None
    median_fv_moves: float | None
    resolution_ok: bool
    n: int
    n_windows: int


def probe_market(
    key: tuple[Any, ...],
    fv: list[float],
    venue: list[float],
    ts: list[int],
    horizon_s: int,
) -> MarketReachRow:
    """Probe ONE market at ONE horizon. ``basis_bps`` is the contemporaneous structural
    offset (``decompose_gap``); the reach is the forward both-directions asymmetry."""
    basis_bps = decompose_gap(fv, venue).basis_bps
    dr = directional_reach(fv, venue, ts, horizon_s)
    asymmetry = (
        dr.fv_to_venue_reach - dr.venue_to_fv_reach
        if dr.fv_to_venue_reach is not None and dr.venue_to_fv_reach is not None
        else None
    )
    resolution_ok = dr.median_venue_moves is not None and dr.median_venue_moves >= MIN_MOVES_FOR_TRUST
    return MarketReachRow(
        key=key,
        horizon_s=horizon_s,
        basis_bps=basis_bps,
        fv_to_venue_reach=dr.fv_to_venue_reach,
        venue_to_fv_reach=dr.venue_to_fv_reach,
        asymmetry=asymmetry,
        median_venue_moves=dr.median_venue_moves,
        median_fv_moves=dr.median_fv_moves,
        resolution_ok=resolution_ok,
        n=len(fv),
        n_windows=dr.n_windows,
    )


@dataclass(frozen=True)
class HorizonAggregate:
    """Per-horizon aggregate across markets -- medians of the per-market reach FRACTIONS."""

    horizon_s: int
    n_markets: int
    median_fv_to_venue: float | None
    median_venue_to_fv: float | None
    median_asymmetry: float | None
    mean_asymmetry: float | None
    median_venue_moves: float | None
    n_fv_leads: int
    n_venue_leads: int
    n_co_move: int
    n_inconclusive: int
    verdict: str


@dataclass(frozen=True)
class ProbeResult:
    """Full probe result: every per-market row, per-horizon aggregates, and the verdict."""

    rows: list[MarketReachRow]
    aggregates: list[HorizonAggregate]
    verdict: str


def _classify(row: MarketReachRow) -> str:
    """FV leads / venue leads / co-move / inconclusive for one market at one horizon."""
    if row.asymmetry is None:
        return "inconclusive"
    # A directional (leads) call requires the venue mover to be adequately resolved; else
    # the asymmetry is a coarse-venue artifact, not a lead.
    if not row.resolution_ok:
        if abs(row.asymmetry) < MATERIAL_ASYMMETRY:
            return "co_move"
        return "inconclusive"
    if row.asymmetry >= MATERIAL_ASYMMETRY:
        return "fv_leads"
    if row.asymmetry <= -MATERIAL_ASYMMETRY:
        return "venue_leads"
    return "co_move"


def _horizon_verdict(median_asym: float | None, n_fv: int, n_ven: int, n_co: int, n_incon: int) -> str:
    total = n_fv + n_ven + n_co + n_incon
    if total == 0 or median_asym is None:
        return "NO DATA"
    # Inconclusive dominates when most markets cannot be trusted (the cp1 coarse-venue case).
    if n_incon >= max(n_fv, n_ven, n_co) and n_incon > 0:
        return "INCONCLUSIVE"
    trusted_fv = n_fv > (n_ven + n_co)
    trusted_ven = n_ven > (n_fv + n_co)
    if median_asym >= MATERIAL_ASYMMETRY and trusted_fv:
        return "FV LEADS"
    if median_asym <= -MATERIAL_ASYMMETRY and trusted_ven:
        return "VENUE LEADS"
    if abs(median_asym) < MATERIAL_ASYMMETRY and n_co >= max(n_fv, n_ven):
        return "NO LEAD"
    return "MIXED"


def _aggregate_horizon(rows: list[MarketReachRow], horizon_s: int) -> HorizonAggregate:
    hz = [r for r in rows if r.horizon_s == horizon_s]
    asyms = [r.asymmetry for r in hz if r.asymmetry is not None]
    f2vs = [r.fv_to_venue_reach for r in hz if r.fv_to_venue_reach is not None]
    v2fs = [r.venue_to_fv_reach for r in hz if r.venue_to_fv_reach is not None]
    vmoves = [r.median_venue_moves for r in hz if r.median_venue_moves is not None]
    classes = [_classify(r) for r in hz]
    n_fv = classes.count("fv_leads")
    n_ven = classes.count("venue_leads")
    n_co = classes.count("co_move")
    n_incon = classes.count("inconclusive")
    median_asym = statistics.median(asyms) if asyms else None
    return HorizonAggregate(
        horizon_s=horizon_s,
        n_markets=len(hz),
        median_fv_to_venue=statistics.median(f2vs) if f2vs else None,
        median_venue_to_fv=statistics.median(v2fs) if v2fs else None,
        median_asymmetry=median_asym,
        mean_asymmetry=statistics.mean(asyms) if asyms else None,
        median_venue_moves=statistics.median(vmoves) if vmoves else None,
        n_fv_leads=n_fv,
        n_venue_leads=n_ven,
        n_co_move=n_co,
        n_inconclusive=n_incon,
        verdict=_horizon_verdict(median_asym, n_fv, n_ven, n_co, n_incon),
    )


def _overall_verdict(aggregates: list[HorizonAggregate]) -> str:
    verdicts = {a.verdict for a in aggregates}
    for single in ("INCONCLUSIVE", "NO LEAD", "FV LEADS", "VENUE LEADS", "NO DATA"):
        if verdicts == {single}:
            return single
    if verdicts <= {"INCONCLUSIVE", "NO LEAD"}:
        return "INCONCLUSIVE"
    return "MIXED"


def run_probe(
    series_by_market: dict[tuple[Any, ...], tuple[list[int], list[float], list[float]]],
    horizons_s: tuple[int, ...] = DEFAULT_HORIZONS_S,
) -> ProbeResult:
    """Probe every market at every horizon; aggregate per-market reach FRACTIONS only.

    Args:
        series_by_market: ``key -> (ts_list, fv_list, mid_list)``, each index-aligned, one
            entry per ``(fixture_id, venue_market_ref)`` market. Never pooled.
        horizons_s: Forward reach horizons (seconds) to sweep.
    """
    rows: list[MarketReachRow] = []
    for key, (ts, fv, mid) in sorted(series_by_market.items(), key=lambda kv: str(kv[0])):
        if len(fv) < 2:
            continue
        for horizon_s in horizons_s:
            rows.append(probe_market(key, fv, mid, ts, horizon_s))
    aggregates = [_aggregate_horizon(rows, h) for h in horizons_s]
    return ProbeResult(rows=rows, aggregates=aggregates, verdict=_overall_verdict(aggregates))


def load_cp1_series() -> dict[tuple[int, str], tuple[list[int], list[float], list[float]]]:
    """Build the cp1 tape OFFLINE and extract per-market ``(ts, fv, mid)`` live series.

    Reuses ``runner``'s committed inputs (ReplayPack + resolved-market-lookup) and
    ``build_cp1_maker_tape``. Only rows with a FRESH venue mid (``mid is not None``) are
    kept, sorted by ``ts``, grouped per ``(fixture_id, venue_market_ref)`` -- never merged
    across markets. No network, no HyperSync.
    """
    from veridex.maker.mapping import DEFAULT_MAPPING_PATH, load_resolved_market_lookup
    from veridex.maker.tape import build_cp1_maker_tape

    pack_root = _REPO_ROOT / "scripts" / "txline_live" / "packs"
    frames_root = _REPO_ROOT / "scripts" / "txline_live" / "cp1" / "frames"
    records, _ = load_resolved_market_lookup(DEFAULT_MAPPING_PATH)
    tape = build_cp1_maker_tape(records, pack_root=pack_root, cp1_frames_root=frames_root)

    groups: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for row in tape:
        groups.setdefault((row["fixture_id"], row["venue_market_ref"]), []).append(row)

    series: dict[tuple[int, str], tuple[list[int], list[float], list[float]]] = {}
    for key, market_rows in groups.items():
        live = sorted((r for r in market_rows if r["mid"] is not None), key=lambda r: r["ts"])
        if len(live) < 2:
            continue
        series[key] = (
            [r["ts"] for r in live],
            [r["fv"] for r in live],
            [r["mid"] for r in live],
        )
    return series


def _fmt(x: float | None) -> str:
    return f"{x:.3f}" if x is not None else "n/a"


def render_markdown(result: ProbeResult) -> str:
    """Render the per-market tables, aggregates, and honest verdict as Markdown."""
    lines: list[str] = []
    lines.append("# cp1 convergence probe -- does the TxLINE FV lead the venue mid?")
    lines.append("")
    lines.append(
        "OFFLINE probe (committed ReplayPack + pinned venue frames; no network). Reach is "
        "read from the basis-adjusted RESIDUAL only via the audited `build_convergence_reach`. "
        "The lead signal is the ASYMMETRY = (FV->venue reach) - (venue->FV reach): a POSITIVE "
        f"value beyond {MATERIAL_ASYMMETRY:.2f} on a well-resolved venue == FV leads; ~0 == "
        "co-movement. Per-market, never pooled; forward horizon, no look-ahead; no settled "
        "value used as FV."
    )
    lines.append("")
    lines.append(f"## VERDICT: {result.verdict}")
    lines.append("")
    if result.verdict == "INCONCLUSIVE":
        lines.append(
            "> The venue mid is too coarsely sampled to answer the lead question. Within any "
            "hour-scale window the venue makes only a handful of distinct moves (median "
            "`venue_moves` below), while the TxLINE FV moves continuously. Freezing FV and "
            "watching the near-flat venue forces the FV->venue reach toward 0, which reads as "
            "a spurious 'venue leads' -- a SAMPLING-CADENCE ARTIFACT, not price leadership. We "
            "therefore refuse a directional verdict: **no FV lead is established, and the "
            "apparent venue-leads asymmetry is an artifact, not a finding.**"
        )
        lines.append("")

    lines.append("## Aggregate across markets (per horizon)")
    lines.append("")
    lines.append(
        "| horizon_s | n_markets | med FV->venue | med venue->FV | med asym | mean asym | "
        "med venue_moves | #FV-leads | #venue-leads | #co-move | #inconclusive | verdict |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for a in result.aggregates:
        lines.append(
            f"| {a.horizon_s} | {a.n_markets} | {_fmt(a.median_fv_to_venue)} | "
            f"{_fmt(a.median_venue_to_fv)} | {_fmt(a.median_asymmetry)} | "
            f"{_fmt(a.mean_asymmetry)} | {_fmt(a.median_venue_moves)} | {a.n_fv_leads} | "
            f"{a.n_venue_leads} | {a.n_co_move} | {a.n_inconclusive} | {a.verdict} |"
        )
    lines.append("")

    for horizon in sorted({r.horizon_s for r in result.rows}):
        lines.append(f"## Per-market reach -- horizon {horizon}s")
        lines.append("")
        lines.append(
            "| fixture_id | venue_market_ref | basis_bps | FV->venue | venue->FV | asymmetry | "
            "venue_moves | fv_moves | resolution_ok | n | n_windows |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
        hz = sorted(
            (r for r in result.rows if r.horizon_s == horizon),
            key=lambda r: r.asymmetry if r.asymmetry is not None else -99.0,
            reverse=True,
        )
        for r in hz:
            fixture = r.key[0] if len(r.key) > 0 else ""
            # Escape pipes so a venue_market_ref like "1X2|home|full" does not break the cell.
            ref = str(r.key[1]).replace("|", "\\|") if len(r.key) > 1 else ""
            lines.append(
                f"| {fixture} | {ref} | {r.basis_bps} | {_fmt(r.fv_to_venue_reach)} | "
                f"{_fmt(r.venue_to_fv_reach)} | {_fmt(r.asymmetry)} | "
                f"{_fmt(r.median_venue_moves)} | {_fmt(r.median_fv_moves)} | "
                f"{r.resolution_ok} | {r.n} | {r.n_windows} |"
            )
        lines.append("")

    lines.append("## Honest caveats")
    lines.append("")
    lines.append(
        "- **`reach_from_residual` has no power on well-sampled series.** It counts "
        "step-to-step residual shrinkage and sits at ~0.5 for any noisy stationary gap; it "
        "only departs from 0.5 for a CLEAN decaying-amplitude convergence. A near-0.5 or "
        "near-zero-asymmetry reading is a TRUTHFUL 'no tradeable lead', not a detector failure."
    )
    lines.append(
        "- **The venue-resolution trust gate is load-bearing.** A directional verdict is "
        f"emitted only when the venue mover is adequately sampled (median venue_moves >= "
        f"{MIN_MOVES_FOR_TRUST}). The cp1 venue mid is a slow step function (~40-min median "
        "refresh), so FV->venue reach collapses toward 0 for lack of venue movement, not lack "
        "of convergence; that asymmetry is an artifact and is reported as INCONCLUSIVE."
    )
    lines.append(
        "- `basis_bps` is the structural (median) offset, reported SEPARATELY and never counted as a lead signal."
    )
    lines.append(
        "- cp1 venue frames are bursty (dense ~3s FV ticks vs ~40-min venue refresh). At "
        "30/60/120s the venue does not move at all, so only 600/1800/3600s horizons are "
        "swept -- and even there the venue makes only a few distinct moves per window."
    )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    """Run the probe on the real cp1 tape OFFLINE, write the report, print the verdict."""
    series = load_cp1_series()
    result = run_probe(series, horizons_s=DEFAULT_HORIZONS_S)
    _RESEARCH_OUT.parent.mkdir(parents=True, exist_ok=True)
    _RESEARCH_OUT.write_text(render_markdown(result))

    print(f"cp1 convergence probe -- markets probed: {len(series)}")
    print(f"wrote: {_RESEARCH_OUT}")
    print(f"\nVERDICT: {result.verdict}\n")
    print(
        f"{'horizon_s':>9} {'n_mkt':>5} {'medFV->ven':>10} {'medven->FV':>10} "
        f"{'medasym':>8} {'vmoves':>6} {'#FV':>4} {'#ven':>4} {'#co':>4} {'#inc':>4}  verdict"
    )
    for a in result.aggregates:
        print(
            f"{a.horizon_s:>9} {a.n_markets:>5} {_fmt(a.median_fv_to_venue):>10} "
            f"{_fmt(a.median_venue_to_fv):>10} {_fmt(a.median_asymmetry):>8} "
            f"{_fmt(a.median_venue_moves):>6} {a.n_fv_leads:>4} {a.n_venue_leads:>4} "
            f"{a.n_co_move:>4} {a.n_inconclusive:>4}  {a.verdict}"
        )


if __name__ == "__main__":
    main()
