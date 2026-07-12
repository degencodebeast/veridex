"""E4-T3: basis-adjusted ConvergenceReachReport — reach is read from the RESIDUAL only.

The raw TxLINE-vs-venue gap still contains the structural basis (a pricing convention,
not alpha), so a raw gap that looks "converging" can be pure basis. The builder reports
the structural ``basis_bps`` SEPARATELY and computes reach from the RESIDUAL alone via
``reach_from_residual`` — never from the raw gap, and no ``edge_from_gap`` helper exists.
"""

from pathlib import Path

from veridex.maker.basis import reach_from_residual
from veridex.maker.diagnostic import build_convergence_reach


def test_convergence_uses_residual_not_raw_gap():
    # raw gap [1000,900,800,700] bps looks monotonically "converging"; but median basis=850
    # leaves residual [150,50,-50,-150] whose |.|=[150,50,50,150] does NOT monotonically shrink.
    rep = build_convergence_reach(
        txline_fv=[0.50, 0.52, 0.54, 0.56],
        venue_native=[0.40, 0.43, 0.46, 0.49],
        reach_horizon_s=60,
    )
    assert rep.basis_bps == 850  # structural basis reported separately
    # reach reads the RESIDUAL only: |residual| shrinks 1 of 3 steps -> 1/3, NOT the
    # raw-gap "full convergence".
    assert rep.residual_reach_fraction == reach_from_residual([150, 50, -50, -150])
    assert rep.reach_horizon_s == 60 and rep.n == 4


def test_diagnostic_module_has_no_raw_gap_to_edge_helper():
    # No ``edge_from_gap`` / raw-gap->edge helper may exist: the raw gap still carries the
    # structural basis and can never be called edge.
    src = Path("veridex/maker/diagnostic.py").read_text()
    assert "edge_from_gap" not in src
