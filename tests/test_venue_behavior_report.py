"""C-5 — VenueBehaviorReport: hypothesis-slice discovery + the decision_quote_coverage instrument.

This layer is a SEPARATE report object, NOT scoring / law / sealed evidence — so venue numbers
(mean estimated edge, staleness) live here legitimately (they never touch ``AgentAction.params`` or
``RunResult.evidence_hash``). Two trust invariants are pinned by these tests:

  * **Every slice is ``hypothesis_only=True`` (CON-008).** A favorable slice is a question for a
    future run, never an edge claim; ``n`` is disclosed on every slice; ``headline`` and
    ``diagnostic-partial`` coverage are never mixed into one headline metric.
  * **``decision_quote_coverage`` is computed over ALL decisions, not just fired picks (CON-012).**
    The self-falsifying instrument: a mostly-``None`` run reads as "could not price most decisions
    under the freshness bound," NOT "measured, no edge." Fixture coverage != decision coverage.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from veridex.backtest.venue_behavior_report import (
    DecisionQuoteCoverage,
    VenueBehaviorReport,
    VenueBehaviorRow,
    VenueBehaviorSlice,
    VenueDecision,
    build_venue_behavior_report,
    register_hypothesis_ledger_entry,
)


def _row(
    *,
    side: str,
    prob: float,
    edge: int,
    staleness: int,
    ttc_s: int,
    coverage: str,
) -> VenueBehaviorRow:
    """A fired-pick row: TxLINE fair prob priced against a time-aligned venue mid (raw estimated edge)."""
    return VenueBehaviorRow(
        side=side,
        fair_prob=prob,
        venue_decimal_price=(1.0 + prob),  # any > 1.0 decimal; not load-bearing for these tests
        staleness_s=staleness,
        time_to_close_s=ttc_s,
        estimated_edge_bps=edge,
        coverage_class=coverage,
    )


def _dec(*, fired: bool, quote_matched: bool, staleness: int | None = None) -> VenueDecision:
    """One VvV decision opportunity — including the ones where the venue source returned ``None``."""
    return VenueDecision(fired=fired, quote_matched=quote_matched, staleness_s=staleness)


def test_report_slices_and_named_survival_warnings() -> None:
    rows = [
        _row(side="home", prob=0.55, edge=450, staleness=120, ttc_s=3600, coverage="headline"),
        _row(side="away", prob=0.30, edge=-50, staleness=600, ttc_s=300, coverage="diagnostic-partial"),
    ]
    decisions = [
        _dec(fired=True, quote_matched=True, staleness=120),
        _dec(fired=True, quote_matched=False),
        _dec(fired=False, quote_matched=False),
        _dec(fired=False, quote_matched=True, staleness=600),
    ]
    rep = build_venue_behavior_report(
        rows,
        decisions,
        haircut_ladder_bps=[0, 100, 200, 300],
        prob_bands=[(0, 20), (20, 40), (40, 60), (60, 80), (80, 100)],
        ttc_buckets=[">24h", "6-24h", "1-6h", "<1h"],
        freshness_buckets=["<=2m", "<=5m", "<=15m"],
    )
    assert isinstance(rep, VenueBehaviorReport)
    assert rep.slices and all(isinstance(s, VenueBehaviorSlice) for s in rep.slices)
    assert all(s.hypothesis_only for s in rep.slices)
    assert all(s.n >= 1 for s in rep.slices)
    assert {s.coverage_class for s in rep.slices} <= {"headline", "diagnostic-partial"}
    assert set(rep.cost_survival) == {0, 100, 200, 300}  # every ladder level named
    assert isinstance(rep.freshness_artifact_warning, bool)  # explicit, assertable


def test_decision_quote_coverage_is_computed_over_all_decisions_not_just_fired() -> None:
    # DISCRIMINATING fixture: the all-decisions ratio (3/5=60%) DIVERGES from the fired-pick ratio
    # (1/2=50%), so a regression that computed quote_matched_pct over fired picks would read 50.0 and
    # FAIL this assertion. A degenerate 2/4-and-1/2 fixture (both 50%) could not catch that bug.
    decisions = [
        _dec(fired=True, quote_matched=True, staleness=120),  # fired + matched
        _dec(fired=True, quote_matched=False),  # fired + no quote
        _dec(fired=False, quote_matched=False),  # unfired + no quote
        _dec(fired=False, quote_matched=True, staleness=600),  # unfired + matched
        _dec(fired=False, quote_matched=True, staleness=200),  # unfired + matched (breaks the symmetry)
    ]
    rep = build_venue_behavior_report(
        [],
        decisions,
        haircut_ladder_bps=[0, 100, 200, 300],
        prob_bands=[(0, 100)],
        ttc_buckets=["<1h"],
        freshness_buckets=["<=2m", "<=5m", "<=15m"],
    )
    c = rep.decision_quote_coverage
    assert isinstance(c, DecisionQuoteCoverage)
    assert c.decision_count == 5 and c.quote_matched_count == 3 and c.quote_none_count == 2
    assert c.quote_matched_pct == 60.0  # 3/5 over ALL decisions — NOT 50.0 (1/2 over fired picks)
    # fired-pick split is reported INDEPENDENTLY and stays 1/2 — proving the two denominators differ:
    assert c.fired_pick_count == 2 and c.fired_pick_quote_matched_count == 1 and c.fired_pick_quote_none_count == 1
    assert sum(c.freshness_bucket_counts_for_used_quotes.values()) == c.quote_matched_count  # only matched bucketed


def test_headline_and_diagnostic_coverage_never_mixed_in_cost_survival() -> None:
    """cost_survival is a HEADLINE-only metric: a negative diagnostic-partial row must not drag it."""
    rows = [
        _row(side="home", prob=0.55, edge=450, staleness=120, ttc_s=3600, coverage="headline"),
        _row(side="away", prob=0.30, edge=-9000, staleness=600, ttc_s=300, coverage="diagnostic-partial"),
    ]
    rep = build_venue_behavior_report(
        rows,
        [_dec(fired=True, quote_matched=True, staleness=120)],
        haircut_ladder_bps=[0, 100, 200, 300],
        prob_bands=[(0, 20), (20, 40), (40, 60), (60, 80), (80, 100)],
        ttc_buckets=[">24h", "6-24h", "1-6h", "<1h"],
        freshness_buckets=["<=2m", "<=5m", "<=15m"],
    )
    # Headline mean edge is +450 (the diagnostic -9000 row is excluded); it survives every ladder level.
    assert rep.cost_survival == {0: True, 100: True, 200: True, 300: True}


def test_cost_survival_can_fail_at_higher_haircut_levels() -> None:
    """"survives only at 0bps" must be an explicit, assertable field, not a hidden default."""
    rows = [
        _row(side="home", prob=0.55, edge=50, staleness=120, ttc_s=3600, coverage="headline"),
    ]
    rep = build_venue_behavior_report(
        rows,
        [_dec(fired=True, quote_matched=True, staleness=120)],
        haircut_ladder_bps=[0, 100, 200, 300],
        prob_bands=[(0, 20), (20, 40), (40, 60), (60, 80), (80, 100)],
        ttc_buckets=[">24h", "6-24h", "1-6h", "<1h"],
        freshness_buckets=["<=2m", "<=5m", "<=15m"],
    )
    assert rep.cost_survival == {0: True, 100: False, 200: False, 300: False}


def test_freshness_artifact_warning_true_only_when_positive_edge_is_stalest_bucket_only() -> None:
    """Positive headline edge ONLY in the stalest freshness bucket ⇒ likely a staleness artifact."""
    rows = [
        # positive edge lives only in <=15m (stalest); the fresher <=2m bucket is negative
        _row(side="home", prob=0.55, edge=500, staleness=800, ttc_s=3600, coverage="headline"),
        _row(side="away", prob=0.45, edge=-200, staleness=100, ttc_s=3600, coverage="headline"),
    ]
    rep = build_venue_behavior_report(
        rows,
        [_dec(fired=True, quote_matched=True, staleness=800)],
        haircut_ladder_bps=[0, 100, 200, 300],
        prob_bands=[(0, 20), (20, 40), (40, 60), (60, 80), (80, 100)],
        ttc_buckets=[">24h", "6-24h", "1-6h", "<1h"],
        freshness_buckets=["<=2m", "<=5m", "<=15m"],
    )
    assert rep.freshness_artifact_warning is True


def test_freshness_artifact_warning_false_when_fresh_bucket_also_positive() -> None:
    rows = [
        _row(side="home", prob=0.55, edge=500, staleness=800, ttc_s=3600, coverage="headline"),
        _row(side="away", prob=0.55, edge=300, staleness=100, ttc_s=3600, coverage="headline"),
    ]
    rep = build_venue_behavior_report(
        rows,
        [_dec(fired=True, quote_matched=True, staleness=100)],
        haircut_ladder_bps=[0, 100, 200, 300],
        prob_bands=[(0, 20), (20, 40), (40, 60), (60, 80), (80, 100)],
        ttc_buckets=[">24h", "6-24h", "1-6h", "<1h"],
        freshness_buckets=["<=2m", "<=5m", "<=15m"],
    )
    assert rep.freshness_artifact_warning is False


def test_run_writes_one_hypothesis_ledger_entry(tmp_path: Path) -> None:
    """CON-008: every run registers its slices as ONE ledgered hypothesis (a question, not a claim)."""
    rep = build_venue_behavior_report(
        [_row(side="home", prob=0.55, edge=450, staleness=120, ttc_s=3600, coverage="headline")],
        [_dec(fired=True, quote_matched=True, staleness=120)],
        haircut_ladder_bps=[0, 100, 200, 300],
        prob_bands=[(0, 20), (20, 40), (40, 60), (60, 80), (80, 100)],
        ttc_buckets=[">24h", "6-24h", "1-6h", "<1h"],
        freshness_buckets=["<=2m", "<=5m", "<=15m"],
    )
    ledger_path = tmp_path / "hypothesis-ledger.jsonl"

    entry = register_hypothesis_ledger_entry(rep, ledger_path=ledger_path, run_id="run-002-vvv")

    assert entry.run_id == "run-002-vvv"
    assert entry.hypothesis_only is True
    assert entry.slice_count == len(rep.slices)
    assert ledger_path.exists()
    lines = [ln for ln in ledger_path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1  # exactly one hypothesis-ledger entry per run


def _ttc_of(rep: VenueBehaviorReport) -> set[str]:
    return {s.dimensions["time_to_close"] for s in rep.slices}


def test_custom_ttc_buckets_change_bucketing() -> None:
    """``ttc_buckets`` is a FUNCTIONAL param: a custom partition relabels a row's time-to-close slice."""
    row = _row(side="home", prob=0.55, edge=450, staleness=120, ttc_s=3600, coverage="headline")
    common = {
        "haircut_ladder_bps": [0],
        "prob_bands": [(0, 20), (20, 40), (40, 60), (60, 80), (80, 100)],
        "freshness_buckets": ["<=2m", "<=5m", "<=15m"],
    }
    default = build_venue_behavior_report([row], [], ttc_buckets=[">24h", "6-24h", "1-6h", "<1h"], **common)
    custom = build_venue_behavior_report([row], [], ttc_buckets=["<2h", "2-24h", ">24h"], **common)
    # 3600s (1h): the default partition calls it "1-6h"; the custom partition calls it "<2h".
    assert _ttc_of(default) == {"1-6h"}
    assert _ttc_of(custom) == {"<2h"}


def test_matched_decision_without_staleness_raises() -> None:
    """Producer contract (self-defense for the honesty instrument): a matched quote MUST carry an age.

    Otherwise it would be silently dropped from ``freshness_bucket_counts_for_used_quotes`` and the
    bucket sum would undercount matched quotes. Fail LOUD at construction, never silent-undercount.
    """
    with pytest.raises(ValidationError):
        VenueDecision(fired=True, quote_matched=True)  # matched but no staleness_s


def test_empty_freshness_buckets_yields_no_artifact_warning() -> None:
    """Degenerate empty freshness partition must not crash; there is no stalest bucket ⇒ warning False."""
    rep = build_venue_behavior_report(
        [],
        [],
        haircut_ladder_bps=[0, 100],
        prob_bands=[(0, 100)],
        ttc_buckets=["<1h", "1-6h"],
        freshness_buckets=[],
    )
    assert rep.freshness_artifact_warning is False
