"""OFFLINE convergence probe: does TxLINE FV LEAD the venue native mid, or co-move?

The probe answers ONE empirical question per market and both directions:
  * FV->venue reach: over a forward horizon, does the venue mid converge toward a
    FROZEN TxLINE fair value (FV leads)?
  * venue->FV reach: does FV converge toward a frozen venue mid (venue leads)?

The load-bearing anti-self-fooling property is the BOTH-DIRECTIONS null: the signal is
the ASYMMETRY (FV->venue minus venue->FV). Equal reaches == co-movement == NO tradeable
lead. Reach is read from the basis-adjusted RESIDUAL only, via the audited
``build_convergence_reach`` (never the raw gap). These tests prove the probe reports lead
ONLY when a lead exists and reports no-lead on symmetric co-movement -- it cannot rig
itself toward "leads".
"""

from __future__ import annotations

from scripts.maker.convergence_probe import (
    MarketReachRow,
    _naive_raw_gap_directional,
    directional_reach,
    probe_market,
    run_probe,
)


# --------------------------------------------------------------------------- helpers
def _clean_ring_lead(level: float, n: int = 200) -> tuple[list[int], list[float], list[float]]:
    """FV LEADS: FV is a piecewise-constant leader; venue rings toward it with a CLEAN
    geometric-decaying overshoot, so the FROZEN-FV residual gap tightens (venue reaches to
    FV) while the reverse does not. ``level`` sets the price level so two markets can live
    at different levels without any shared series."""
    fv: list[float] = []
    lvl = level
    for t in range(n):
        if t % 40 == 0 and t > 0:
            lvl = level + 0.08 * ((t // 40) % 2 * 2 - 1)
        fv.append(min(0.9, max(0.1, lvl)))
    venue = [min(0.95, max(0.05, fv[t] + 0.12 * ((-0.6) ** (t % 40)))) for t in range(n)]
    ts = list(range(n))
    return ts, fv, venue


def _symmetric_comovement(n: int = 240, seed: int = 3) -> tuple[list[int], list[float], list[float]]:
    """NO LEAD: FV and venue are the SAME latent signal plus independent noise -- neither
    leads. Asymmetry must be ~0."""
    import random

    rng = random.Random(seed)
    latent: list[float] = []
    x = 0.5
    for _ in range(n):
        x += rng.gauss(0, 0.006)
        x = min(0.85, max(0.15, x))
        latent.append(x)
    fv = [v + rng.gauss(0, 0.004) for v in latent]
    venue = [v + rng.gauss(0, 0.004) for v in latent]
    return list(range(n)), fv, venue


def _noisy_basis_closure(n: int = 120, seed: int = 7) -> tuple[list[int], list[float], list[float]]:
    """A structural trap: venue monotonically drifts UP to FV's level (a one-time basis
    closure that ends AT the FV level, no crossing), NOT an active lead. The raw gap simply
    shrinks, so a NAIVE raw-gap directional measure falsely shouts 'FV leads'; the
    basis-adjusted RESIDUAL probe (which demeans the monotone drift) must not."""
    import random

    rng = random.Random(seed)
    fv = [0.55 + rng.gauss(0, 0.0008) for _ in range(n)]
    venue = [0.45 + 0.10 * (t / (n - 1)) + rng.gauss(0, 0.0008) for t in range(n)]
    return list(range(n)), fv, venue


# --------------------------------------------------------------------------- tests
def test_probe_reports_lead_when_fv_leads() -> None:
    """Two markets where the venue demonstrably converges to a leading FV -> the probe
    reports FV->venue reach MATERIALLY greater than venue->FV reach (asymmetry positive)
    in BOTH markets."""
    horizon = 10
    for level in (0.25, 0.75):
        ts, fv, venue = _clean_ring_lead(level)
        row = probe_market(("mkt", level), fv, venue, ts, horizon)
        assert row.fv_to_venue_reach is not None and row.venue_to_fv_reach is not None
        # FV->venue reaches materially more than venue->FV: FV leads.
        assert row.fv_to_venue_reach > row.venue_to_fv_reach
        assert row.asymmetry is not None and row.asymmetry > 0.15


def test_probe_reports_no_lead_on_symmetric_comovement() -> None:
    """CRITICAL null-validity: symmetric co-movement -> FV->venue reach ~= venue->FV reach
    (NO asymmetry). Proves the probe does NOT always find a lead.

    Plus the anti-fooling contrast on a monotone BASIS-closure trap: the NAIVE raw-gap
    directional measure falsely reports a large FV->venue lead, while the basis-adjusted
    RESIDUAL probe (this probe) does not."""
    horizon = 10

    # 1) symmetric co-movement -> asymmetry ~ 0
    ts, fv, venue = _symmetric_comovement()
    row = probe_market(("sym",), fv, venue, ts, horizon)
    assert row.asymmetry is not None
    assert abs(row.asymmetry) < 0.05

    # 2) monotone basis-closure trap: naive raw-gap FALSELY leads, residual probe does not.
    #    Use a longer horizon so the (slow) monotone drift dominates noise within a window.
    trap_horizon = 20
    ts, fv, venue = _noisy_basis_closure()
    naive_f, naive_v = _naive_raw_gap_directional(fv, venue, ts, trap_horizon)
    residual_row = probe_market(("trap",), fv, venue, ts, trap_horizon)
    naive_asym = naive_f - naive_v
    assert naive_asym > 0.15  # naive raw-gap is fooled: shouts "FV leads"
    assert residual_row.asymmetry is not None
    assert abs(residual_row.asymmetry) < 0.06  # residual probe is NOT fooled
    assert naive_asym - residual_row.asymmetry > 0.20  # residual strips the fooling


def test_probe_is_per_market_not_pooled() -> None:
    """Two markets at DIFFERENT price levels aggregate as two separate reach rows with
    their OWN basis, never one pooled series. Pooling the two would compute a single basis
    that misrepresents both (the cross-market-FV-leakage bug)."""
    horizon = 10
    # Market A near 0.15 with ~0 basis; market B near 0.85 with a large structural basis.
    ts_a, fv_a, ven_a = _clean_ring_lead(0.15)
    ts_b = list(range(200))
    fv_b = [0.85] * 200
    ven_b = [0.80] * 200  # venue persistently 5pp below FV -> basis ~ +500 bps

    series = {
        ("A",): (ts_a, fv_a, ven_a),
        ("B",): (ts_b, fv_b, ven_b),
    }
    result = run_probe(series, horizons_s=(horizon,))
    rows = [r for r in result.rows if r.horizon_s == horizon]
    assert len(rows) == 2  # two markets -> two rows, not one pooled row
    by_key = {r.key: r for r in rows}
    # Each market carries its OWN structural basis, reported separately.
    assert abs(by_key[("A",)].basis_bps) < 100
    assert by_key[("B",)].basis_bps > 300
    # Pooling would compute ONE basis over concatenated series that misrepresents at least
    # one market -- here it takes market B's ~500 bps and wrongly imposes it on market A
    # (true basis ~0), the cross-market-FV-leakage the per-market design forbids.
    from veridex.maker.basis import decompose_gap

    pooled = decompose_gap(fv_a + fv_b, ven_a + ven_b).basis_bps
    assert pooled != by_key[("A",)].basis_bps
    assert not (pooled == by_key[("A",)].basis_bps and pooled == by_key[("B",)].basis_bps)


def test_directional_reach_returns_none_when_series_too_short() -> None:
    """A degenerate (too-short) market yields ``None`` reaches, never a fabricated number."""
    dr = directional_reach([0.5], [0.5], [0], horizon_s=10)
    assert dr.fv_to_venue_reach is None and dr.venue_to_fv_reach is None
    assert dr.n_windows == 0


def test_row_is_frozen_and_reports_basis_separately() -> None:
    """The row surfaces basis_bps SEPARATELY from the reach signal and is immutable."""
    ts, fv, venue = _clean_ring_lead(0.5)
    row = probe_market(("x",), fv, venue, ts, 10)
    assert isinstance(row, MarketReachRow)
    assert isinstance(row.basis_bps, int)
    import dataclasses

    try:
        row.basis_bps = 0  # type: ignore[misc]
        raise AssertionError("MarketReachRow must be frozen")
    except dataclasses.FrozenInstanceError:
        pass
