"""II-10 — the Sharp-Momentum pre-registered ADMISSION GATE (performance-blind, honest-outcome).

The four load-bearing properties, one test each (+ a real-run honesty trap):

1. **Pre-registration** — the fixture set is a frozen constant pinned BEFORE any outcome is read;
   which fixtures are evaluated does not change with their results.
2. **Performance-blind** — admission reads ACTION COUNTS only; the ONLY clv-reader
   (:func:`is_scoreable`) is sign-blind, and :func:`evaluate_gate` has no CLV input at all.
3. **Synthetic-never-admits** — a non-genuine (synthetic-labeled) run, or ``< MIN_GENUINE_FIXTURES``
   genuine fixtures, is TEMPLATE-ONLY regardless of how many actions it produced.
4. **Admission path** — ``>= MIN_SCOREABLE_ACTIONS`` across ``>= MIN_GENUINE_FIXTURES`` genuine
   fixtures ⇒ ADMITTED (exercised with genuine-LABELED fakes so it needs no minted genuine pack).
"""

from __future__ import annotations

from pathlib import Path

from veridex.ingest.capture_chain import is_genuine_pack
from veridex.law.recompute import PENDING
from veridex.research.sharp_momentum_gate import (
    MIN_GENUINE_FIXTURES,
    MIN_SCOREABLE_ACTIONS,
    PINNED_FIXTURES,
    FixtureCandidate,
    FixtureGateResult,
    evaluate_gate,
    is_scoreable,
    pinned_fixtures,
    run_gate,
)


def _result(fid: int, *, genuine: bool, scoreable: int, total: int | None = None) -> FixtureGateResult:
    """A synthetic per-fixture result row for the pure-function admission tests."""
    return FixtureGateResult(
        pack_dir=Path(f"/nonexistent/{fid}"),
        fixture_id=fid,
        genuine=genuine,
        scoreable_actions=scoreable,
        total_non_wait=scoreable if total is None else total,
    )


# --- RED test 1: pre-registration -------------------------------------------------------------
def test_pin_is_pre_registered_and_independent_of_outcomes() -> None:
    # The pin is a frozen, non-empty constant surfaced verbatim by the accessor.
    assert isinstance(PINNED_FIXTURES, tuple)
    assert len(PINNED_FIXTURES) >= 1
    assert pinned_fixtures() == PINNED_FIXTURES

    # Pre-registration: the evaluated fixture set + order does NOT depend on the outcomes. Run the
    # gate twice with count_fns reporting OPPOSITE counts and assert the pinned candidates were
    # evaluated verbatim both times (fixtures pinned BEFORE any result is read).
    hi = run_gate(count_fn=lambda pd, fid: (99, 99), genuine_fn=lambda pd: True)
    lo = run_gate(count_fn=lambda pd, fid: (0, 0), genuine_fn=lambda pd: True)
    assert [r.fixture_id for r in hi.results] == [r.fixture_id for r in lo.results]
    assert [(r.pack_dir, r.fixture_id) for r in hi.results] == [(c.pack_dir, c.fixture_id) for c in PINNED_FIXTURES]


# --- RED test 2: performance-blind (counts only, never the CLV sign) ---------------------------
def test_admission_is_performance_blind_counts_only() -> None:
    # is_scoreable is the ONLY place clv_bps is inspected, and it reads validity + numeric-ness
    # ONLY — the SIGN is never consulted. A +CLV and a -CLV action of equal validity score alike.
    assert is_scoreable({"valid": True, "clv_bps": 500}) is True
    assert is_scoreable({"valid": True, "clv_bps": -500}) is True
    assert is_scoreable({"valid": True, "clv_bps": 500}) == is_scoreable({"valid": True, "clv_bps": -500})
    # PENDING (WAIT / awaiting-close) and invalid verdicts are never scoreable.
    assert is_scoreable({"valid": True, "clv_bps": PENDING}) is False
    assert is_scoreable({"valid": False, "clv_bps": 500}) is False

    # At the gate level, admission is a pure function of COUNTS + the genuine flag — evaluate_gate
    # takes no CLV, so a sign can never enter the decision. Identical counts ⇒ identical verdict.
    winners = [_result(1, genuine=True, scoreable=3), _result(2, genuine=True, scoreable=3)]
    assert evaluate_gate(winners).verdict == "ADMITTED"
    assert evaluate_gate(winners).admitted is True


# --- RED test 3: synthetic / < 2 genuine never admits -----------------------------------------
def test_synthetic_or_under_two_genuine_never_admits() -> None:
    # (a) Non-genuine (synthetic-labeled) fixtures can NEVER admit — even with a huge action count.
    syn = evaluate_gate([_result(1, genuine=False, scoreable=99), _result(2, genuine=False, scoreable=99)])
    assert syn.admitted is False
    assert syn.verdict == "TEMPLATE-ONLY"
    assert syn.genuine_fixture_count == 0

    # (b) A single genuine fixture (< MIN_GENUINE_FIXTURES) is TEMPLATE-ONLY, huge count notwithstanding.
    lone = evaluate_gate([_result(1, genuine=True, scoreable=99), _result(2, genuine=False, scoreable=99)])
    assert lone.admitted is False
    assert lone.verdict == "TEMPLATE-ONLY"
    assert lone.genuine_fixture_count == 1

    # (c) The REAL classifier reads the pinned SYNTHETIC control pack as non-genuine, so synthetic
    # data cannot reach the genuine-counting branch through the production path either.
    assert is_genuine_pack(PINNED_FIXTURES[-1].pack_dir) is False


# --- RED test 4: admission path (with genuine-labeled fakes) -----------------------------------
def test_admission_path_with_genuine_fakes() -> None:
    fixtures = (FixtureCandidate(Path("/fake/a"), 1), FixtureCandidate(Path("/fake/b"), 2))

    admitted = run_gate(
        candidates=fixtures,
        genuine_fn=lambda pd: True,  # genuine-LABELED fakes exercise the positive branch
        count_fn=lambda pd, fid: (MIN_SCOREABLE_ACTIONS, MIN_SCOREABLE_ACTIONS),
    )
    assert admitted.admitted is True
    assert admitted.verdict == "ADMITTED"
    assert admitted.genuine_fixture_count == MIN_GENUINE_FIXTURES
    assert admitted.qualifying_fixture_count == MIN_GENUINE_FIXTURES

    # One action short of the threshold on each fixture ⇒ no qualifying fixture ⇒ template-only.
    below = run_gate(
        candidates=fixtures,
        genuine_fn=lambda pd: True,
        count_fn=lambda pd, fid: (MIN_SCOREABLE_ACTIONS - 1, MIN_SCOREABLE_ACTIONS - 1),
    )
    assert below.admitted is False
    assert below.verdict == "TEMPLATE-ONLY"


# --- Honesty trap: the REAL pinned run over committed packs is the recorded data-determined outcome.
def test_real_pinned_run_is_honest_template_only() -> None:
    """Run momentum-sharp over the PINNED committed packs with the REAL classifier + replay.

    This asserts the ACTUAL, data-determined verdict — a regression trap: if the pinned data ever
    began producing >= MIN_SCOREABLE_ACTIONS on >= MIN_GENUINE_FIXTURES genuine fixtures this test
    would fail and force a fresh, honest admission review rather than silently flipping the roster.
    """
    verdict = run_gate()
    genuine = [r for r in verdict.results if r.genuine]
    # The banked genuine demo pack exposes >= MIN_GENUINE_FIXTURES real TxLINE fixtures.
    assert len(genuine) >= MIN_GENUINE_FIXTURES
    # Honest recorded outcome: the genuine 400-record prefixes are shorter than momentum-sharp's
    # warmup, so each yields ZERO scoreable actions — below the gate. TEMPLATE-ONLY, not admitted.
    assert all(r.scoreable_actions < MIN_SCOREABLE_ACTIONS for r in genuine)
    assert verdict.admitted is False
    assert verdict.verdict == "TEMPLATE-ONLY"
