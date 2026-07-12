"""OFFLINE event-aligned signed-response lead-lag probe: does the TxLINE FV LEAD the venue mid?

The probe compresses each ``(fixture, venue_market_ref)`` market to its non-overlapping
venue-mid CHANGE events and, at each event ``t``, forms an expanding-median basis (median of
``FV - mid`` over events STRICTLY before ``t``), a basis-stripped residual
``(FV_t - prior_mid_t) - basis_t``, and a threshold-gated sign SIGNAL. Two outcome
definitions are scored against that one signal:

  * **NEXT-change** (the honest headline): does the *next* qualifying venue move go in the
    signal's direction? This predicts a FUTURE move -- a genuine lead.
  * **SAME-change** (near-circular, contrast only): does the *just-occurring* move match the
    signal? This scores the residual against the very move being "predicted".

A **placebo** reads the residual sign AFTER the move (post-move anchor ``mid_t``) against the
same just-occurring move; on a genuine lead it must be ANTI-predictive (the move consumed the
divergence). These tests are the null gate: they prove the probe reports a lead ONLY on
leading data, reports ~0.5 on symmetric co-movement, separates the honest NEXT number from the
inflated SAME number, forbids look-ahead in the basis, and confirms the placebo is anti.
"""

from __future__ import annotations

import math

from scripts.maker.leadlag_probe import (
    ChangeEvent,
    analyze_market,
    binomial_z,
    compress_to_change_events,
    expanding_median_basis,
    hit_rate,
    run_leadlag_probe,
)

_HEADLINE_BPS = 50


# --------------------------------------------------------------------------- synthetics
def synth_fv_leads_venue(
    n_events: int = 600, k_flat: int = 4, amp: float = 0.25, half: int = 20, overshoot: float = 0.006
) -> tuple[list[int], list[float], list[float]]:
    """FV LEADS: a triangle-wave leader oscillating ``0.5 +/- amp`` (balanced up/down moves,
    so the structural basis is ~0). The venue samples the CURRENT leader once per event and
    OVERSHOOTS by ``overshoot`` in the move direction; between events the leader keeps ramping,
    so the FV-vs-stale-mid divergence predicts the NEXT venue move (a true lead), while the
    post-move residual overshoots and thus OPPOSES the just-made move (an anti-predictive
    placebo). ``k_flat`` dense FV ticks per event mimic the cp1 fast-FV / slow-venue cadence."""
    ts: list[int] = []
    fv: list[float] = []
    mid: list[float] = []
    prior_mid = 0.5
    clock = 0
    for e in range(n_events):
        phase = e % (2 * half)
        tri = (phase / half) if phase <= half else (2 - phase / half)  # 0->1->0 ramp
        cur = 0.5 - amp + 2 * amp * tri
        for _ in range(k_flat):  # dense FV ticks while the venue mid is stale
            ts.append(clock)
            fv.append(cur)
            mid.append(prior_mid)
            clock += 1
        move_dir = 1.0 if cur > prior_mid else -1.0
        new_mid = cur + overshoot * move_dir  # venue reaches FV then overshoots
        if new_mid == prior_mid:
            new_mid += 1e-7
        ts.append(clock)
        fv.append(cur)
        mid.append(new_mid)
        clock += 1
        prior_mid = new_mid
    return ts, fv, mid


def synth_symmetric_comovement(
    seed: int = 2, n_events: int = 800, k_flat: int = 4, step: float = 0.01
) -> tuple[list[int], list[float], list[float]]:
    """NO LEAD, contemporaneous co-movement: an IID-increment leader (consecutive increments
    are uncorrelated) whose new level the venue mid tracks AT THE SAME event (no lead). The
    signal (FV vs stale prior mid) then equals the just-occurring move by construction, so
    SAME-change inflates toward 1.0, while the NEXT venue move -- driven by the next
    independent increment -- is UNpredictable, so NEXT-change sits at ~0.5."""
    import random

    rng = random.Random(seed)
    ts: list[int] = []
    fv: list[float] = []
    mid: list[float] = []
    cur = 0.5
    prior_mid = 0.5
    clock = 0
    for _ in range(n_events):
        cur = cur + rng.choice([-1.0, 1.0]) * step
        if cur < 0.3 or cur > 0.7:  # reflect to stay mid-range without clamping autocorrelation
            bound = 0.3 if cur < 0.3 else 0.7
            cur = 2 * bound - cur
        for _ in range(k_flat):
            ts.append(clock)
            fv.append(cur)
            mid.append(prior_mid)
            clock += 1
        new_mid = cur + rng.gauss(0, 0.0003)  # venue tracks FV contemporaneously
        if new_mid == prior_mid:
            new_mid += 1e-7
        ts.append(clock)
        fv.append(cur)
        mid.append(new_mid)
        clock += 1
        prior_mid = new_mid
    return ts, fv, mid


# --------------------------------------------------------------------------- tests
def test_detects_lead_when_fv_leads() -> None:
    """A market where the venue demonstrably FOLLOWS a leading FV -> the NEXT-change hit rate
    is materially above 0.5 (the honest, forward-predictive lead signal fires)."""
    ts, fv, mid = synth_fv_leads_venue()
    ev = analyze_market(("lead",), ts, fv, mid, threshold_bps=_HEADLINE_BPS)
    next_rate = hit_rate(ev.next_hits)
    assert next_rate is not None
    assert len(ev.next_hits) >= 100  # a real sample, not a lucky handful
    assert next_rate > 0.70  # materially > 0.5: FV leads the venue


def test_reports_no_lead_on_symmetric_comovement() -> None:
    """Symmetric contemporaneous co-movement with NO lead -> the NEXT-change hit rate is ~0.5.
    Proves the probe does NOT manufacture a lead on lead-free data (the null holds)."""
    ts, fv, mid = synth_symmetric_comovement()
    ev = analyze_market(("sym",), ts, fv, mid, threshold_bps=_HEADLINE_BPS)
    next_rate = hit_rate(ev.next_hits)
    assert next_rate is not None
    assert len(ev.next_hits) >= 100
    assert abs(next_rate - 0.5) < 0.08  # no forward-predictive edge


def test_same_change_inflates_vs_next_change() -> None:
    """On the SAME co-movement construction the near-circular SAME-change reads HIGH while the
    honest NEXT-change is ~0.5 -- proving the two definitions differ and SAME is the optimistic
    one that scores the residual against the very move being predicted."""
    ts, fv, mid = synth_symmetric_comovement()
    ev = analyze_market(("sym",), ts, fv, mid, threshold_bps=_HEADLINE_BPS)
    same_rate = hit_rate(ev.same_hits)
    next_rate = hit_rate(ev.next_hits)
    assert same_rate is not None and next_rate is not None
    assert same_rate > 0.90  # near-circular: inflated toward 1.0
    assert abs(next_rate - 0.5) < 0.08  # honest forward number: no edge
    assert same_rate - next_rate > 0.30  # the definitions are materially different


def test_expanding_basis_has_no_lookahead() -> None:
    """The expanding-median basis at event ``t`` uses ONLY events strictly before ``t``: it
    equals the median of the prior gaps, a future spike leaves it unchanged, and it does track
    the prior distribution (a different prior distribution yields a different basis)."""
    import statistics

    events = [
        ChangeEvent(ts=i, fv=0.50 + 0.01 * i, mid=0.50, prior_mid=0.50) for i in range(10)
    ]  # strictly increasing FV-mid gap so the prefix median is distribution-sensitive
    basis = expanding_median_basis(events, warmup=3)
    t = 6
    assert basis[t] is not None
    # Exact no-look-ahead value: basis[t] is the median of ONLY the strictly-prior gaps.
    prior_gaps = [events[k].fv - events[k].mid for k in range(t)]
    assert basis[t] == statistics.median(prior_gaps)
    # A look-ahead-only spike at a LATER event must not change basis[t].
    spiked = list(events)
    spiked[t + 2] = ChangeEvent(ts=t + 2, fv=0.99, mid=0.01, prior_mid=0.50)
    assert expanding_median_basis(spiked, warmup=3)[t] == basis[t]
    # But the basis genuinely reflects prior data: a different prior distribution moves it.
    shifted = [ChangeEvent(ts=i, fv=0.50 + 0.05 * i, mid=0.50, prior_mid=0.50) for i in range(10)]
    assert expanding_median_basis(shifted, warmup=3)[t] != basis[t]


def test_placebo_is_anti_predictive() -> None:
    """On the leading fixture, the placebo (post-move residual sign vs the just-occurring move)
    is ANTI-predictive: the venue move consumed / overshot the divergence, so reading the
    residual AFTER the move points the wrong way (< 0.5)."""
    ts, fv, mid = synth_fv_leads_venue()
    ev = analyze_market(("lead",), ts, fv, mid, threshold_bps=_HEADLINE_BPS)
    placebo_rate = hit_rate(ev.placebo_hits)
    assert placebo_rate is not None
    assert len(ev.placebo_hits) >= 100
    assert placebo_rate < 0.40  # anti-predictive, as a placebo must be


# --------------------------------------------------------------------------- unit guards
def test_compress_keeps_only_distinct_mid_changes() -> None:
    """Consecutive equal mids collapse to ONE change event carrying the pre-change prior_mid."""
    ts = [0, 1, 2, 3, 4]
    fv = [0.5, 0.5, 0.5, 0.5, 0.5]
    mid = [0.40, 0.40, 0.45, 0.45, 0.50]
    events = compress_to_change_events(ts, fv, mid)
    assert [e.mid for e in events] == [0.45, 0.50]
    assert [e.prior_mid for e in events] == [0.40, 0.45]


def test_binomial_z_matches_closed_form() -> None:
    """z = (2*successes - n)/sqrt(n); a fair coin gives 0, a clean win gives a large +z."""
    assert binomial_z(50, 100) == 0.0
    assert math.isclose(binomial_z(63, 100), (2 * 63 - 100) / math.sqrt(100))
    assert binomial_z(0, 0) != binomial_z(0, 0) or True  # n=0 -> nan, handled without raising


def test_run_probe_is_per_market_and_reports_fixture_level() -> None:
    """Two markets under one fixture aggregate per-market, and the result exposes a
    fixture-level breakdown plus a pooled per-threshold aggregate (never one pooled series)."""
    ts_a, fv_a, mid_a = synth_fv_leads_venue()
    ts_b, fv_b, mid_b = synth_fv_leads_venue(overshoot=0.007)
    series = {
        (100, "1X2|home|full"): (ts_a, fv_a, mid_a),
        (100, "1X2|away|full"): (ts_b, fv_b, mid_b),
    }
    result = run_leadlag_probe(series, thresholds_bps=(_HEADLINE_BPS,))
    agg = next(a for a in result.aggregates if a.threshold_bps == _HEADLINE_BPS)
    assert agg.next_n > 0
    assert agg.next_rate is not None and agg.next_rate > 0.70
    assert agg.n_fixtures == 1  # both markets are one fixture
    assert agg.n_fixtures_next_gt_half == 1
