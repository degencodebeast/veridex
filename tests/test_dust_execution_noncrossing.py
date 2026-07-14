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

from veridex.dust_execution.feesnapshot import FeeSnapshot, round5
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


# ===========================================================================
# E5-T3 — §4.6 EXHAUSTIVE complement / fee / tick table (AC-041, §6 group 14).
#
# This block pins the FULL cross-product of §4.6 behavior over the existing
# E5-T1/T2 seams (TEST-ONLY — noncrossing.py is NOT modified). It proves:
#   * every BUY/SELL × YES/NO form canonicalizes to the right BUY rung;
#   * a SELL-YES + SELL-NO set NORMALIZES into the same lock as the canonical
#     BUY case -> the check is side-symmetric;
#   * multi-level ladders, unequal sizes (locked overlap + one-sided residual),
#     and partial (q=min) rung consumption walk correctly;
#   * maker (0) and taker (round5(feeRate·p·(1−p))) fees are summed per slice,
#     and BOTH round5 boundaries hold — a sub-threshold fee DROPS to 0 (it is
#     NOT clamped up to 0.00001), with a slice whose verdict FLIPS on exactly
#     that distinction;
#   * one-tick boundaries (tick / 1−tick valid; off-tick / <tick / >1−tick fail
#     closed) and the SAF-009 possibly-live union (uncertain-submit worst-case).
#
# NOTE ON off-tick THROUGH normalize: normalize's step (1) VENUE-TICK-ROUNDS the
# raw price BEFORE validating, so an off-tick raw price is snapped to the grid
# (0.015 @ tick 0.01 -> 0.02), NOT rejected. Off-tick FAIL-CLOSED therefore lives
# on check_non_crossing's path, which validates alignment on the ORIGINAL price.
# Both are pinned below. (This is the documented order of operations, not a bug.)
# ===========================================================================

#: Near-zero-rate snapshot: at p≈0.5 the raw taker fee (rate·0.25) lands BELOW the
#: round5 drop-to-zero threshold (<5e-6), so the pinned fee is exactly 0.0. Used to
#: place a slice at the integer-$1 boundary where a single 0.00001 would be decisive.
_SNAP_TINY = FeeSnapshot(condition_id="cond-1", fee_rate=1.6e-5, fee_exponent=1, taker_only=True)


def _lock_from_orders(
    orders: list[RawOrder],
    snapshot: FeeSnapshot,
    *,
    tick_size: float = 0.01,
) -> str:
    """Normalize raw orders to canonical rungs, split by outcome, return the lock verdict.

    Mirrors the real §4.6 pipeline: every raw BUY/SELL × YES/NO order goes through
    :func:`normalize` (four-form map + per-rung fee from the pinned snapshot), then the two
    canonical ladders feed :func:`complementary_lock_check`.
    """
    legs = [
        normalize(order, resolved=_RESOLVED, fee_snapshot=snapshot, tick_size=tick_size)
        for order in orders
    ]
    yes_ladder = [leg for leg in legs if leg.outcome == "YES"]
    no_ladder = [leg for leg in legs if leg.outcome == "NO"]
    return complementary_lock_check(yes_ladder, no_ladder)


# --- The exhaustive normalize -> complementary_lock_check table -------------------
#
# Each row: (id, [raw orders], snapshot, expected verdict). Verified by hand against
# the E3-T0 §8 fee rule and the §4.6 worst-price-first slice walk.
_LOCK_TABLE: list[tuple[str, list[RawOrder], FeeSnapshot, str]] = [
    # (1) Canonical ladder REJECT: BUY YES {0.60×1, 0.30×99} + BUY NO {0.45×100}. Averaging
    #     YES to ~0.303 would admit; the top 1-share slice 0.60+0.45=1.05 LOCKS (re-pin of E5-T2).
    (
        "canonical_buy_ladder_1.05_reject",
        [
            RawOrder(side="yes", action="BUY", price=0.60, size=1, role="MAKER"),
            RawOrder(side="yes", action="BUY", price=0.30, size=99, role="MAKER"),
            RawOrder(side="no", action="BUY", price=0.45, size=100, role="MAKER"),
        ],
        _SNAP_ZERO,
        "REJECT",
    ),
    # (2) Single-rung 1.05 REJECT: one BUY YES @0.60 vs one BUY NO @0.45 -> 1.05 >= 1.
    (
        "single_rung_1.05_reject",
        [
            RawOrder(side="yes", action="BUY", price=0.60, size=1, role="MAKER"),
            RawOrder(side="no", action="BUY", price=0.45, size=1, role="MAKER"),
        ],
        _SNAP_ZERO,
        "REJECT",
    ),
    # (3) Safe multi-level ADMIT: every slice strictly < 1 (re-pin of E5-T2).
    (
        "safe_multilevel_admit",
        [
            RawOrder(side="yes", action="BUY", price=0.55, size=2, role="MAKER"),
            RawOrder(side="yes", action="BUY", price=0.50, size=8, role="MAKER"),
            RawOrder(side="no", action="BUY", price=0.40, size=5, role="MAKER"),
            RawOrder(side="no", action="BUY", price=0.35, size=5, role="MAKER"),
        ],
        _SNAP_ZERO,
        "ADMIT",
    ),
    # (4) SELL YES + SELL NO SYMMETRIC lock: reconstruct the canonical (1) case purely from
    #     SELLs via YES+NO=$1 — SELL NO @p -> BUY YES @(1−p); SELL YES @p -> BUY NO @(1−p):
    #       SELL NO  @0.40 ×1   -> BUY YES @0.60 ×1
    #       SELL NO  @0.70 ×99  -> BUY YES @0.30 ×99
    #       SELL YES @0.55 ×100 -> BUY NO  @0.45 ×100
    #     Once normalized this IS case (1) -> same REJECT. Proves normalization side-symmetry.
    (
        "sell_yes_sell_no_symmetric_reject",
        [
            RawOrder(side="no", action="SELL", price=0.40, size=1, role="MAKER"),
            RawOrder(side="no", action="SELL", price=0.70, size=99, role="MAKER"),
            RawOrder(side="yes", action="SELL", price=0.55, size=100, role="MAKER"),
        ],
        _SNAP_ZERO,
        "REJECT",
    ),
    # (5) ALL FOUR FORMS mixed into one locking set:
    #       BUY  YES @0.58 ×1  -> YES 0.58 ×1
    #       SELL NO  @0.35 ×1  -> BUY YES @0.65 ×1
    #       BUY  NO  @0.45 ×2  -> NO  0.45 ×2
    #       SELL YES @0.60 ×1  -> BUY NO  @0.40 ×1
    #     worst-first YES 0.65 vs NO 0.45 -> 1.10 >= 1 -> REJECT.
    (
        "all_four_forms_mixed_reject",
        [
            RawOrder(side="yes", action="BUY", price=0.58, size=1, role="MAKER"),
            RawOrder(side="no", action="SELL", price=0.35, size=1, role="MAKER"),
            RawOrder(side="no", action="BUY", price=0.45, size=2, role="MAKER"),
            RawOrder(side="yes", action="SELL", price=0.60, size=1, role="MAKER"),
        ],
        _SNAP_ZERO,
        "REJECT",
    ),
    # (6) Multi-level ladders, UNEQUAL sizes: locked-overlap portion is safe and a large YES-only
    #     residual remains one-sided -> ADMIT. YES {0.55×100} vs NO {0.40×3}: 0.95 < 1, 97 YES residual.
    (
        "multilevel_unequal_residual_admit",
        [
            RawOrder(side="yes", action="BUY", price=0.55, size=100, role="MAKER"),
            RawOrder(side="no", action="BUY", price=0.40, size=3, role="MAKER"),
        ],
        _SNAP_ZERO,
        "ADMIT",
    ),
    # (7) PARTIAL level consumption (q=min slices): YES {0.55×10} vs NO {0.44×4, 0.30×3}. Slice1
    #     q=4 (0.55+0.44=0.99<1), slice2 q=3 (0.55+0.30=0.85<1), 3 YES residual -> ADMIT. The single
    #     YES rung is consumed in PARTS across two NO rungs.
    (
        "partial_consumption_admit",
        [
            RawOrder(side="yes", action="BUY", price=0.55, size=10, role="MAKER"),
            RawOrder(side="no", action="BUY", price=0.44, size=4, role="MAKER"),
            RawOrder(side="no", action="BUY", price=0.30, size=3, role="MAKER"),
        ],
        _SNAP_ZERO,
        "ADMIT",
    ),
    # (8) Zero-fee baseline: prices ALONE sum to 0.99 < 1 -> ADMIT. Paired with (9) to prove the
    #     nonzero taker fee (not the price) is what flips the verdict.
    (
        "taker_prices_only_admit_zero_fee",
        [
            RawOrder(side="yes", action="BUY", price=0.50, size=5, role="TAKER"),
            RawOrder(side="no", action="BUY", price=0.49, size=5, role="TAKER"),
        ],
        _SNAP_ZERO,
        "ADMIT",
    ),
    # (9) NONZERO taker fee pushes the SAME 0.99 slice into a lock: round5(0.05·p·(1−p)) ≈ 0.0125
    #     per rung at the p≈0.5 peak -> 0.50+0.49+0.0125+0.0125 = 1.015 >= 1 -> REJECT.
    (
        "taker_fee_pushes_into_lock",
        [
            RawOrder(side="yes", action="BUY", price=0.50, size=5, role="TAKER"),
            RawOrder(side="no", action="BUY", price=0.49, size=5, role="TAKER"),
        ],
        _SNAP_5PCT,
        "REJECT",
    ),
]


@pytest.mark.parametrize(
    "orders, snapshot, expected",
    [(orders, snap, expected) for _id, orders, snap, expected in _LOCK_TABLE],
    ids=[row[0] for row in _LOCK_TABLE],
)
def test_complement_lock_table(
    orders: list[RawOrder], snapshot: FeeSnapshot, expected: str
) -> None:
    assert _lock_from_orders(orders, snapshot) == expected


def test_all_four_forms_canonical_buy_rung_mapping() -> None:
    """Each BUY/SELL × YES/NO form maps to the documented canonical BUY rung (via YES+NO=$1)."""
    cases = [
        # (side, action, raw_price) -> (canonical outcome, canonical price)
        (("yes", "BUY", 0.60), ("YES", 0.60)),
        (("yes", "SELL", 0.60), ("NO", 0.40)),  # SELL YES @p -> BUY NO  @(1−p)
        (("no", "BUY", 0.45), ("NO", 0.45)),
        (("no", "SELL", 0.45), ("YES", 0.55)),  # SELL NO  @p -> BUY YES @(1−p)
    ]
    for (side, action, price), (outcome, canon_price) in cases:
        leg = normalize(
            RawOrder(side=side, action=action, price=price, size=1, role="MAKER"),
            resolved=_RESOLVED,
            fee_snapshot=_SNAP_ZERO,
            tick_size=0.01,
        )
        assert leg.outcome == outcome
        assert leg.price == pytest.approx(canon_price)


# --- round5 fee-precision boundaries (E3-T0 §8): drop-to-0, NOT clamp-up ----------

def test_round5_fee_boundaries_from_snapshot() -> None:
    """BOTH boundaries via a real snapshot taker fee: sub-threshold DROPS to 0; smallest nonzero 1e-5.

    (i) rate 1.6e-5 at p=0.5 -> raw 4e-6 < 0.00001 -> round5 collapses to 0.0 (NOT bumped up to the
        0.00001 minimum). (ii) rate 4e-5 at p=0.5 -> raw exactly 1e-5 -> the smallest nonzero fee.
    """
    below = FeeSnapshot(condition_id="c", fee_rate=1.6e-5, fee_exponent=1, taker_only=True)
    at_min = FeeSnapshot(condition_id="c", fee_rate=4e-5, fee_exponent=1, taker_only=True)
    # (i) drop-to-0: a sub-threshold computed fee is NOT charged and is NOT clamped up.
    assert below.taker_fee(shares=1, price=0.50) == 0.0
    # (ii) smallest nonzero fee is exactly 0.00001.
    assert at_min.taker_fee(shares=1, price=0.50) == pytest.approx(1e-5)
    # And the round5 primitive itself: a raw magnitude below 1e-5 DROPS to 0 (checked BEFORE rounding,
    # AC-041), and only a raw magnitude at/above 1e-5 rounds onto the 1e-5 grid.
    assert round5(4e-6) == 0.0
    assert round5(0.000009999) == 0.0  # just-below: a COMPUTED fee < 1e-5 is zero (AC-041 boundary)
    assert round5(9.75e-6) == 0.0  # 0.00000975 raw < 1e-5 → zero (NOT rounded UP to 1e-5)
    assert round5(1e-5) == pytest.approx(1e-5)  # the smallest nonzero fee (raw exactly at threshold)


def test_round5_dropzero_vs_clampup_flips_the_lock_verdict() -> None:
    """The round5 flip: a slice ADMITS because the tiny fee DROPS to 0 — it would REJECT if clamped up.

    Prices are placed so the slice sits exactly on the integer-$1 boundary (0.50 + 0.49999 = 0.99999,
    a deliberately sub-tick construction — complementary_lock_check bounds prices to [0,1], not the
    grid — so a single 0.00001 is decisive). With the pinned near-zero-rate snapshot the per-rung
    taker fee round5(rate·p·(1−p)) DROPS to 0, so the slice sums to 0.99999 < 1 -> ADMIT. Had round5
    (wrongly) CLAMPED the sub-threshold fee UP to 0.00001, the slice would reach 1.00001 -> REJECT.
    The verdict FLIP on exactly that distinction proves the drop-to-0 rule (no upward clamp).
    """
    fee_yes = _SNAP_TINY.taker_fee(shares=1, price=0.50)
    fee_no = _SNAP_TINY.taker_fee(shares=1, price=0.49999)
    assert fee_yes == 0.0 and fee_no == 0.0  # round5 dropped both sub-threshold fees to 0

    admit = complementary_lock_check(
        [Leg("YES", 0.50, 1, "TAKER", fee_yes)],
        [Leg("NO", 0.49999, 1, "TAKER", fee_no)],
    )
    assert admit == "ADMIT"  # 0.50 + 0.49999 + 0 + 0 = 0.99999 < 1

    # Counterfactual: had the tiny fee been CLAMPED UP to the 0.00001 minimum, the SAME slice locks.
    reject_if_clamped = complementary_lock_check(
        [Leg("YES", 0.50, 1, "TAKER", 1e-5)],
        [Leg("NO", 0.49999, 1, "TAKER", 1e-5)],
    )
    assert reject_if_clamped == "REJECT"  # 0.99999 + 0.00002 = 1.00001 >= 1 -> proves the flip


# --- one-tick boundaries + fail-closed through the real seams --------------------

@pytest.mark.parametrize("price", [0.01, 0.99])
def test_normalize_accepts_one_tick_boundary_prices(price: float) -> None:
    """A BUY YES at exactly tick (0.01) or 1−tick (0.99) is a valid restable price."""
    leg = normalize(
        RawOrder(side="yes", action="BUY", price=price, size=1, role="MAKER"),
        resolved=_RESOLVED,
        fee_snapshot=_SNAP_ZERO,
        tick_size=0.01,
    )
    assert leg.price == pytest.approx(price)


@pytest.mark.parametrize("price", [0.005, 0.995])
def test_normalize_price_below_tick_or_above_one_minus_tick_fails_closed(price: float) -> None:
    """After venue-tick rounding, <tick snaps toward 0 and >1−tick snaps toward 1 -> off-band raise."""
    with pytest.raises(ValueError):
        normalize(
            RawOrder(side="yes", action="BUY", price=price, size=1, role="MAKER"),
            resolved=_RESOLVED,
            fee_snapshot=_SNAP_ZERO,
            tick_size=0.01,
        )


@pytest.mark.parametrize(
    "legs",
    [
        pytest.param([_leg("BUY", 0.055, LegKind.PROPOSED)], id="off_tick"),
        pytest.param([_leg("BUY", 0.005, LegKind.PROPOSED)], id="below_tick"),
        pytest.param([_leg("SELL", 0.995, LegKind.PROPOSED)], id="above_one_minus_tick"),
        pytest.param([_leg("BUY", 0.0, LegKind.PROPOSED)], id="at_zero"),
        pytest.param([_leg("SELL", 1.0, LegKind.PROPOSED)], id="at_one"),
    ],
)
def test_noncrossing_price_band_fails_closed(legs: list[OwnOrderLeg]) -> None:
    """SAF-009 fails closed on off-tick / <tick / >1−tick / exactly 0 or 1 (validated on the raw price)."""
    with pytest.raises(ValueError):
        check_non_crossing(legs, tick_size=_TICK)


def test_noncrossing_union_counts_uncertain_submit_worst_case_rung() -> None:
    """An UNCERTAIN_SUBMIT ask at its worst-case (possibly-live) price crosses a proposed bid -> REJECT.

    The ambiguous submit rung is included at its worst case and counts in the SAF-009 union; here it
    ties the proposed bid at 0.60 (equal is a STRICT cross) -> REJECT. Dropping it would wrongly ADMIT.
    """
    legs = [
        _leg("BUY", 0.60, LegKind.PROPOSED),
        _leg("SELL", 0.60, LegKind.UNCERTAIN_SUBMIT, uncertain_state="AMBIGUOUS"),
    ]
    assert check_non_crossing(legs, tick_size=_TICK).verdict == "REJECT"


def test_normalize_off_tick_raw_price_is_venue_rounded_not_rejected() -> None:
    """DESIGN pin: normalize venue-tick-ROUNDS an off-tick raw price (step 1) before validating.

    0.015 @ tick 0.01 snaps to 0.02 (round-half-to-even) and is accepted — off-tick FAIL-CLOSED is
    check_non_crossing's job (it validates alignment on the ORIGINAL price). This documents the
    documented order of operations so the two seams are not conflated.
    """
    leg = normalize(
        RawOrder(side="yes", action="BUY", price=0.015, size=1, role="MAKER"),
        resolved=_RESOLVED,
        fee_snapshot=_SNAP_ZERO,
        tick_size=0.01,
    )
    assert leg.price == pytest.approx(0.02)


def test_complement_lock_missing_mapping_and_snapshot_fail_closed() -> None:
    """Fail-closed at the normalize seam: an ambiguous side↔token map or unavailable snapshot raises."""
    # Ambiguous mapping: 'draw' on a non-draw market has no unambiguous YES/NO token.
    with pytest.raises(FailClosed):
        normalize(
            RawOrder(side="draw", action="BUY", price=0.50, size=1, role="MAKER"),
            resolved=_RESOLVED,
            fee_snapshot=_SNAP_ZERO,
            tick_size=0.01,
        )
    # Unavailable fee snapshot: never invent a fund-touching fee.
    with pytest.raises(FailClosed):
        normalize(
            RawOrder(side="yes", action="BUY", price=0.50, size=1, role="TAKER"),
            resolved=_RESOLVED,
            fee_snapshot=None,
            tick_size=0.01,
        )
