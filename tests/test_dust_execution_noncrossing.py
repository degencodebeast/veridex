"""E5-T1 tests: post-tick same-token non-crossing over the full POSSIBLY-LIVE union (SAF-009).

Trust boundary proven here: YOUR own orders must never cross YOUR own orders (self-cross) on a
(market, outcome token). The check runs over the UNION of four possibly-live legs — proposed
(about to place), open (confirmed resting), uncertain-submit (AMBIGUOUS = possibly-live), and
uncertain-cancel (cancel requested/ACKed but NOT reconciled-absent = possibly-STILL-live). A
possibly-live order counts at its worst case (it IS live); dropping ANY leg — especially the
uncertain-cancel leg — lets a crossing slip through (the mutation check proves exactly that).

The check is PURE: it takes the legs + ``tick_size`` and returns ADMIT/REJECT, rounding prices to
the tick and validating each price is tick-aligned AND within ``[tick_size, 1 - tick_size]`` (an
outcome-token price cannot rest at exactly 0 or 1). It touches no network and no venue.
"""

from __future__ import annotations

import pytest

from veridex.dust_execution.feesnapshot import FeeSnapshot
from veridex.dust_execution.noncrossing import (
    CanonicalLeg,
    Leg,
    LegKind,
    OwnOrderLeg,
    RawOrder,
    check_non_crossing,
    complementary_lock_check,
    normalize,
)
from veridex.dust_execution.risk import FailClosed
from veridex.venues.polymarket_resolver import ResolvedMarket

_TICK = 0.01
_TOKEN = "token-yes"


def _leg(
    side: str,
    price: float,
    kind: LegKind,
    *,
    token_id: str = _TOKEN,
    uncertain_state: str | None = None,
) -> OwnOrderLeg:
    return OwnOrderLeg(
        token_id=token_id,
        side=side,
        price=price,
        kind=kind,
        uncertain_state=uncertain_state,
    )


def test_noncrossing_over_full_union() -> None:
    # A proposed BUY bid at 0.60 crosses an uncertain-CANCEL SELL ask at 0.55 that is possibly
    # STILL live (cancel requested/ACKed but NOT reconciled-absent) on the SAME token -> REJECT.
    # This is the mutation-catcher: dropping the uncertain-cancel leg would wrongly ADMIT this.
    crossing = [
        _leg("BUY", 0.60, LegKind.PROPOSED),
        _leg("SELL", 0.55, LegKind.UNCERTAIN_CANCEL, uncertain_state="AMBIGUOUS"),
    ]
    verdict = check_non_crossing(crossing, tick_size=_TICK)
    assert verdict.admitted is False

    # A genuinely non-crossing set (highest bid 0.40 strictly below lowest ask 0.60) -> ADMIT.
    clean = [
        _leg("BUY", 0.40, LegKind.PROPOSED),
        _leg("SELL", 0.60, LegKind.OPEN),
        _leg("BUY", 0.39, LegKind.UNCERTAIN_SUBMIT, uncertain_state="AMBIGUOUS"),
        _leg("SELL", 0.61, LegKind.UNCERTAIN_CANCEL, uncertain_state="AMBIGUOUS"),
    ]
    assert check_non_crossing(clean, tick_size=_TICK).admitted is True


def test_uncertain_submit_ambiguous_counts_as_possibly_live() -> None:
    # Second mutation-catcher: a proposed BUY bid at 0.60 crosses an AMBIGUOUS uncertain-SUBMIT
    # SELL ask at 0.55. The uncertain-submit leg is possibly-live and MUST count in the union.
    legs = [
        _leg("BUY", 0.60, LegKind.PROPOSED),
        _leg("SELL", 0.55, LegKind.UNCERTAIN_SUBMIT, uncertain_state="AMBIGUOUS"),
    ]
    assert check_non_crossing(legs, tick_size=_TICK).admitted is False


def test_definitively_absent_leg_is_not_counted() -> None:
    # A DEFINITIVELY_ABSENT uncertain leg is provably gone (worst-case reserve released), so it
    # drops out of the union and the otherwise-crossing set is ADMITTED. (On real Polymarket
    # DEFINITIVELY_ABSENT is unreachable, so an uncertain-cancel stays possibly-live in practice —
    # this proves the gate keys on the reconciled state, not on the leg kind alone.)
    legs = [
        _leg("BUY", 0.60, LegKind.PROPOSED),
        _leg("SELL", 0.55, LegKind.UNCERTAIN_CANCEL, uncertain_state="DEFINITIVELY_ABSENT"),
    ]
    assert check_non_crossing(legs, tick_size=_TICK).admitted is True


def test_equal_price_is_a_cross_strict_inequality() -> None:
    # highest_own_bid < lowest_own_ask is STRICT: equal prices are a cross at the same price.
    legs = [
        _leg("BUY", 0.50, LegKind.PROPOSED),
        _leg("SELL", 0.50, LegKind.OPEN),
    ]
    assert check_non_crossing(legs, tick_size=_TICK).admitted is False


def test_cross_only_within_same_token() -> None:
    # A BUY 0.60 on token A and a SELL 0.55 on token B do NOT cross — the invariant is
    # per (market, outcome token), not across tokens.
    legs = [
        _leg("BUY", 0.60, LegKind.PROPOSED, token_id="token-a"),
        _leg("SELL", 0.55, LegKind.OPEN, token_id="token-b"),
    ]
    assert check_non_crossing(legs, tick_size=_TICK).admitted is True


def test_post_tick_rounding_snaps_float_noise() -> None:
    # Prices carrying float noise are rounded to the tick before comparison; 0.5999999999 and
    # 0.6000000001 both round to 0.60, so this BUY/SELL pair crosses (0.60 == 0.60, strict).
    legs = [
        _leg("BUY", 0.60 + 1e-12, LegKind.PROPOSED),
        _leg("SELL", 0.60 - 1e-12, LegKind.OPEN),
    ]
    assert check_non_crossing(legs, tick_size=_TICK).admitted is False


def test_price_at_or_beyond_unit_boundary_fails_closed() -> None:
    # An outcome-token price cannot rest at exactly 0 or 1, nor below tick / above 1 - tick.
    for bad in (0.0, 1.0):
        with pytest.raises(ValueError):
            check_non_crossing([_leg("BUY", bad, LegKind.PROPOSED)], tick_size=_TICK)
    # Below tick_size (0.005 < 0.01) and above 1 - tick_size (0.995 > 0.99) fail closed too.
    with pytest.raises(ValueError):
        check_non_crossing([_leg("BUY", 0.005, LegKind.PROPOSED)], tick_size=_TICK)
    with pytest.raises(ValueError):
        check_non_crossing([_leg("SELL", 0.995, LegKind.PROPOSED)], tick_size=_TICK)


def test_off_tick_price_fails_closed() -> None:
    # 0.055 is not a multiple of the 0.01 tick -> off-tick -> fail closed (raise).
    with pytest.raises(ValueError):
        check_non_crossing([_leg("BUY", 0.055, LegKind.PROPOSED)], tick_size=_TICK)


def test_invalid_tick_size_fails_closed() -> None:
    good = [_leg("BUY", 0.40, LegKind.PROPOSED)]
    for bad_tick in (0.0, -0.01, 0.6, float("nan")):
        with pytest.raises(ValueError):
            check_non_crossing(good, tick_size=bad_tick)


def test_unknown_side_fails_closed() -> None:
    with pytest.raises(ValueError):
        check_non_crossing([_leg("LONG", 0.40, LegKind.PROPOSED)], tick_size=_TICK)


# ---------------------------------------------------------------------------
# E5-T2 — §4.6 canonical complement-normalization + worst-price-first slice-wise
# ladder lock check (SAF-010, AC-028/037). YES/NO complementary-ECONOMIC lock:
# separate YES+NO bids can lock (pay >= $1 for a $1 payout) while each token's own
# book passes SAF-009. Averaging a multi-level ladder to one effective price HIDES
# a locked rung; the lock is per SLICE, not average.
# ---------------------------------------------------------------------------

_RESOLVED = ResolvedMarket(
    condition_id="cond-1",
    token_id_yes="TID_YES",
    token_id_no="TID_NO",
    tick_size=0.01,
)
#: Zero-rate snapshot — isolates the four-form PRICE mapping from fee arithmetic.
_SNAP_ZERO = FeeSnapshot(condition_id="cond-1", fee_rate=0.0, fee_exponent=1, taker_only=True)
#: 5% category rate — exercises the symmetric taker fee round5(feeRate·p·(1−p)).
_SNAP_5PCT = FeeSnapshot(condition_id="cond-1", fee_rate=0.05, fee_exponent=1, taker_only=True)


# --- complementary_lock_check: slice-wise ladder lock (the load-bearing tests) ---

def test_ladder_counterexample_rejects_where_average_would_admit() -> None:
    yes = [Leg("YES", 0.60, 1, "MAKER", 0.0), Leg("YES", 0.30, 99, "MAKER", 0.0)]
    no = [Leg("NO", 0.45, 100, "MAKER", 0.0)]
    # avg YES ~0.303 -> 0.303+0.45=0.753<1 would WRONGLY admit; slice-wise top slice 0.60+0.45=1.05>=1
    assert complementary_lock_check(yes, no) == "REJECT"


def test_safe_multilevel_pair_admits_every_slice() -> None:
    yes = [Leg("YES", 0.55, 2, "MAKER", 0.0), Leg("YES", 0.50, 8, "MAKER", 0.0)]
    no = [Leg("NO", 0.40, 5, "MAKER", 0.0), Leg("NO", 0.35, 5, "MAKER", 0.0)]
    assert complementary_lock_check(yes, no) == "ADMIT"  # every slice <1


def test_missing_mapping_or_fee_snapshot_fails_closed() -> None:
    # An unknown/unavailable per-rung fee (fee is None) is a fund-touching input -> never admit.
    yes_with_unknown_fee = [Leg("YES", 0.55, 5, "TAKER", None)]
    no = [Leg("NO", 0.40, 5, "MAKER", 0.0)]
    with pytest.raises(FailClosed):
        complementary_lock_check(yes_with_unknown_fee, no)


def test_per_rung_fee_pushes_an_otherwise_safe_slice_into_a_lock() -> None:
    # Prices ALONE sum to 0.99 (< 1, would admit); the per-share taker fees at the p≈0.5 peak
    # (round5(0.05·p·(1−p)) ≈ 0.0125 each) push the slice total to >= 1 -> REJECT. This proves the
    # per-rung fee is summed into the slice test — the fee, not just the price, is load-bearing.
    yes = [Leg("YES", 0.50, 5, "TAKER", _SNAP_5PCT.taker_fee(shares=1, price=0.50))]
    no = [Leg("NO", 0.49, 5, "TAKER", _SNAP_5PCT.taker_fee(shares=1, price=0.49))]
    assert complementary_lock_check(yes, no) == "REJECT"


def test_walk_continues_past_admitting_slices_before_a_later_lock() -> None:
    # No early-ADMIT exit: the first (dearest) slice admits, and the walk MUST continue and test the
    # remaining slices. Here a cheap-but-large NO rung is fully consumed across several YES rungs, and
    # a deeper YES rung locks against it -> REJECT (a mid-walk lock is not skipped).
    yes = [Leg("YES", 0.20, 3, "MAKER", 0.0), Leg("YES", 0.58, 7, "MAKER", 0.0)]
    no = [Leg("NO", 0.45, 10, "MAKER", 0.0)]
    # worst-price-first: YES sorts to [0.58, 0.20]; slice1 0.58+0.45=1.03 >=1 -> REJECT.
    assert complementary_lock_check(yes, no) == "REJECT"


def test_partial_overlap_residual_one_sided_allowed() -> None:
    # After pairing, unmatched YES-only size is a one-sided residual (not a lock) -> ADMIT.
    yes = [Leg("YES", 0.55, 100, "MAKER", 0.0)]
    no = [Leg("NO", 0.40, 3, "MAKER", 0.0)]
    assert complementary_lock_check(yes, no) == "ADMIT"


def test_empty_ladders_admit() -> None:
    assert complementary_lock_check([], []) == "ADMIT"
    assert complementary_lock_check([Leg("YES", 0.55, 5, "MAKER", 0.0)], []) == "ADMIT"


def test_boundary_exactly_one_is_a_lock_strict() -> None:
    # STRICT < 1 to admit: a slice summing to exactly 1 (0.55+0.45) is a lock -> REJECT.
    yes = [Leg("YES", 0.55, 5, "MAKER", 0.0)]
    no = [Leg("NO", 0.45, 5, "MAKER", 0.0)]
    assert complementary_lock_check(yes, no) == "REJECT"


# --- normalize: four-form BUY/SELL × YES/NO -> canonical BUY rung via YES+NO=$1 ---

def test_normalize_buy_yes_is_canonical_buy_yes() -> None:
    leg = normalize(
        RawOrder(side="yes", action="BUY", price=0.60, size=1, role="MAKER"),
        resolved=_RESOLVED,
        fee_snapshot=_SNAP_ZERO,
        tick_size=0.01,
    )
    assert leg == CanonicalLeg(outcome="YES", price=0.60, size=1, role="MAKER", fee=0.0)


def test_normalize_sell_yes_maps_to_buy_no_at_complement() -> None:
    leg = normalize(
        RawOrder(side="yes", action="SELL", price=0.60, size=1, role="MAKER"),
        resolved=_RESOLVED,
        fee_snapshot=_SNAP_ZERO,
        tick_size=0.01,
    )
    assert leg.outcome == "NO"
    assert leg.price == pytest.approx(0.40)


def test_normalize_buy_no_and_sell_no() -> None:
    buy_no = normalize(
        RawOrder(side="no", action="BUY", price=0.45, size=1, role="MAKER"),
        resolved=_RESOLVED,
        fee_snapshot=_SNAP_ZERO,
        tick_size=0.01,
    )
    assert buy_no.outcome == "NO" and buy_no.price == pytest.approx(0.45)
    sell_no = normalize(
        RawOrder(side="no", action="SELL", price=0.45, size=1, role="MAKER"),
        resolved=_RESOLVED,
        fee_snapshot=_SNAP_ZERO,
        tick_size=0.01,
    )
    assert sell_no.outcome == "YES" and sell_no.price == pytest.approx(0.55)


def test_normalize_taker_fee_from_snapshot_round5() -> None:
    leg = normalize(
        RawOrder(side="yes", action="BUY", price=0.50, size=1, role="TAKER"),
        resolved=_RESOLVED,
        fee_snapshot=_SNAP_5PCT,
        tick_size=0.01,
    )
    # round5(0.05 · 0.5 · 0.5) = round5(0.0125) = 0.0125 (peak at p=0.5).
    assert leg.fee == pytest.approx(0.0125)


def test_normalize_missing_mapping_fails_closed() -> None:
    with pytest.raises(FailClosed):
        normalize(
            RawOrder(side="bogus", action="BUY", price=0.50, size=1, role="MAKER"),
            resolved=_RESOLVED,
            fee_snapshot=_SNAP_ZERO,
            tick_size=0.01,
        )
    # 'draw' on a non-draw market is an ambiguous mapping -> fail closed (not routed to YES).
    with pytest.raises(FailClosed):
        normalize(
            RawOrder(side="draw", action="BUY", price=0.50, size=1, role="MAKER"),
            resolved=_RESOLVED,
            fee_snapshot=_SNAP_ZERO,
            tick_size=0.01,
        )


def test_normalize_unavailable_fee_snapshot_fails_closed() -> None:
    with pytest.raises(FailClosed):
        normalize(
            RawOrder(side="yes", action="BUY", price=0.50, size=1, role="TAKER"),
            resolved=_RESOLVED,
            fee_snapshot=None,
            tick_size=0.01,
        )
