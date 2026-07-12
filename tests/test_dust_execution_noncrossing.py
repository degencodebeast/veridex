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

from veridex.dust_execution.noncrossing import (
    LegKind,
    OwnOrderLeg,
    check_non_crossing,
)

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
