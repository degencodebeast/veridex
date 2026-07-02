"""Forward executable edge — distinct from backward-looking CLV."""

from __future__ import annotations

from veridex.law.edge import executable_edge_bps


def test_positive_edge_when_price_beats_fair() -> None:
    # fair prob 0.50 (5000 bps); price 2.20 → EV = 0.5*2.2 - 1 = 0.10 → 1000 bps
    assert executable_edge_bps(5000, 2.20) == 1000


def test_zero_edge_at_fair_price() -> None:
    # fair prob 0.50; price 2.00 → EV = 0 → 0 bps
    assert executable_edge_bps(5000, 2.00) == 0


def test_negative_edge_when_price_worse_than_fair() -> None:
    # fair prob 0.50; price 1.80 → EV = -0.10 → -1000 bps
    assert executable_edge_bps(5000, 1.80) == -1000


def test_non_positive_price_is_zero() -> None:
    assert executable_edge_bps(5000, 0.0) == 0
    assert executable_edge_bps(5000, -1.0) == 0


def test_prob_bps_is_demargined_consensus_fair_probability() -> None:
    """Strategy doctrine: ``prob_bps`` is the TxLINE DE-MARGINED consensus FAIR probability.

    TxLINE already de-vigs the consensus, so at the fair decimal price (``1/p``) the edge is
    exactly 0, and a price LONGER than fair is +EV. p=0.40 (4000 bps) → fair price 2.5 → 0 edge;
    price 3.0 → 0.40*3.0-1 = 0.20 → +2000 bps.
    """
    assert executable_edge_bps(4000, 2.5) == 0  # priced at fair → no edge (we never re-de-vig)
    assert executable_edge_bps(4000, 3.0) == 2000  # longer than fair → +EV


def test_capped_fractional_kelly_is_policy_sizing_not_a_metric() -> None:
    """Strategy doctrine (SEC-005): capped fractional Kelly is POLICY execution sizing ONLY.

    It must NEVER surface as a leaderboard or proof metric — absent from ``score_run`` rows,
    leaderboard rows, the Performance-Metrics block, and the proof-check block.
    """
    from tests._arena_fixtures import finished_run_result
    from veridex.checks.build import (
        build_check_results,
        build_performance_metrics,
        check_results_to_proof_block,
    )
    from veridex.leaderboard import leaderboard
    from veridex.scoring import score_run

    run = finished_run_result()
    scores = score_run(run)
    board = leaderboard([dict(r) for r in scores])
    metrics = build_performance_metrics(scores)
    checks = check_results_to_proof_block(build_check_results(scores=scores, run=run))

    for row in scores:
        assert "kelly_fraction" not in row and "stake" not in row
    for row in board:
        assert "kelly_fraction" not in row and "stake" not in row
    assert "kelly_fraction" not in metrics and "stake" not in metrics
    assert "kelly_fraction" not in checks  # checks are keyed by CheckId — no kelly/stake check exists
