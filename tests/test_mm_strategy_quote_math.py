"""Pure-tier E4-T7 QUOTE-math layer — join-or-behind targets + directional rounding + post-clamp.

These tests pin the DETERMINISTIC quote materialization that lives in the QUOTE-math slot (#6) of the
row-H disposition spine inside the REAL ``veridex.mm_strategy.core.decide`` reducer (no
re-implementation): it builds the actual leg PRICES for every quoting class and applies the
post-clamp + cardinality downgrade.

Pinned invariants (REQ-052/055/056; AC-046/047; RED-35/40/42):

- ``test_wide_book_join_or_behind_not_improved`` (RED-35) — the targets JOIN or rest BEHIND the
  current best (``bid = floor(min(anchor − h, best_bid))`` / ``ask = ceil(max(anchor + h,
  best_ask))``); improving past the book is impossible. A wide book never yields the naive
  ``anchor ± h`` (``0.48/0.52``) legs.
- ``test_directional_rounding_bid_floor_ask_ceil`` — the maker-safety invariant: a BID rounds DOWN
  (floor), an ASK rounds UP (ceil). A side-agnostic round-to-nearest (the rust_mm_bot/smm
  anti-pattern the mutation reproduces) is a VIOLATION.
- ``test_post_clamp_leg_out_of_zone`` (AC-046/RED-40) — a half-spread that pushes BOTH legs out of
  the boundary zone omits both → ``NO_QUOTE(leg_out_of_zone)``.
- ``test_one_leg_survival_downgrades_to_one_sided`` (AC-047/RED-42) — one leg valid, one clamped out
  → ``QUOTE_ONE_SIDED`` on the surviving side.
- ``test_invalid_reducing_leg_no_quote_never_flips`` — a ``ONE_SIDED_REDUCE`` whose reducing leg
  clamps out fails to ``NO_QUOTE`` and NEVER side-flips to the opposite (adding) leg.
"""

from __future__ import annotations

import pytest

from veridex.mm_strategy.config import StrategyConfig
from veridex.mm_strategy.contracts import (
    InventoryProjection,
    StrategyObservation,
    StrategyState,
)
from veridex.mm_strategy.core import decide


def _config(*, guard_enabled: bool = False, **overrides: object) -> StrategyConfig:
    """A valid :class:`StrategyConfig` with ``guard_enabled`` explicit (it is REQUIRED)."""
    return StrategyConfig(guard_enabled=guard_enabled, **overrides)  # type: ignore[arg-type]


def _obs(
    *,
    as_of_ts: int = 100_000,
    bid: float | None,
    ask: float | None,
    bid_size: float | None = 100.0,
    ask_size: float | None = 120.0,
    tick_size: float = 0.01,
    net_position: float = 0.0,
    level_count_in_band: int = 5,
) -> StrategyObservation:
    """A healthy, guard-off per-tick observation reaching row H; every ``recv_ts`` is ≤ ``as_of_ts``
    so construction never trips the REQ-022 future-dating guard. The raw top-of-book is exposed so
    each join-or-behind / post-clamp branch is constructible."""
    recv = as_of_ts - 10
    return StrategyObservation(
        fixture_id=1,
        market_ref="TEAM-A/YES",
        side="YES",
        token_id="TOKEN-YES",
        venue_market_ref="0xmarket",
        tick_size=tick_size,
        observation_sequence=2,
        book_source_epoch=1,
        bid=bid,
        ask=ask,
        bid_size=bid_size,
        ask_size=ask_size,
        book_status="ok",
        status_reason=None,
        book_recv_ts=recv,
        level_count_in_band=level_count_in_band,
        tick_regime_changed=False,
        phase=1,
        suspended=False,
        match_state_recv_ts=recv,
        guard_fv=None,
        market_status="ACTIVE",
        market_status_recv_ts=recv,
        market_status_epoch=5,
        order_stream_ok=True,
        projection_fresh=True,
        inventory=InventoryProjection(
            net_position=net_position, resting=(), projection_as_of_ts=as_of_ts, fresh=True
        ),
        as_of_ts=as_of_ts,
    )


def _warm_state(*, smoother_mid: float, spread_ref: float = 0.02) -> StrategyState:
    """A mid-stream WARM state (smoother seeded + both rolling refs past ``ref_min_samples``) so a
    healthy in-window frame reaches row H. ``smoother_mid`` is aligned to the book mid so the REQ-080
    mid-jump event trigger stays quiescent; ``spread_ref`` is sized to the book so the spread-blowout
    event trigger never pre-empts the row-H quote logic under test."""
    return StrategyState(
        last_observation_sequence=1,
        last_book_source_epoch=1,
        last_as_of_ts=99_000,
        last_market_status_epoch=1,
        last_market_status_recv_ts=1,
        guard_watermark=None,
        smoother_mid=smoother_mid,
        smoother_mid_ts=99_000,
        spread_ref_samples=tuple(spread_ref for _ in range(25)),
        depth_ref_samples=tuple(100.0 for _ in range(25)),
    )


# --- RED-35: wide-book join-or-behind — improving past the book is impossible ----------------


def test_wide_book_join_or_behind_not_improved() -> None:
    # A WIDE book (best_bid 0.406 / best_ask 0.594, mid 0.50) with half_spread 0.02: the naive
    # anchor ± h targets would be 0.48 / 0.52 — INSIDE the book, improving past both touches. The
    # join-or-behind formula caps at the book (``min``/``max``) and rounds AWAY from the mid (bid
    # floor, ask ceil), so the legs rest AT or BEHIND the best — never improving. The book prices are
    # deliberately OFF-tick so a round-to-nearest (the mutation) would round the bid UP past 0.406
    # and the ask DOWN past 0.594 (the anti-pattern), which the directional floor/ceil forbids.
    config = _config(half_spread=0.02)
    state = _warm_state(smoother_mid=0.50, spread_ref=0.19)
    obs = _obs(bid=0.406, ask=0.594)

    decision, _ = decide(obs, state, config)
    assert decision.kind == "QUOTE_TWO_SIDED"
    legs = {leg.leg_role: leg.price for leg in decision.intent_plan}

    # Rest AT/behind the book: bid floors to 0.40, ask ceils to 0.60 — the naive 0.48/0.52 is impossible.
    assert legs["bid"] == pytest.approx(0.40)
    assert legs["ask"] == pytest.approx(0.60)
    assert legs["bid"] != pytest.approx(0.48)
    assert legs["ask"] != pytest.approx(0.52)
    # The load-bearing invariant: NEVER improve past the current best (bid ≤ best_bid, ask ≥ best_ask).
    assert legs["bid"] <= obs.bid  # type: ignore[operator]
    assert legs["ask"] >= obs.ask  # type: ignore[operator]


# --- REQ-055: directional rounding — BID floors DOWN, ASK ceils UP (maker safety) ------------


def test_directional_rounding_bid_floor_ask_ceil() -> None:
    # The binding join-or-behind targets are OFF the tick grid (best_bid 0.478 binds the bid,
    # best_ask 0.522 binds the ask). The BID must round DOWN (floor → 0.47) and the ASK UP (ceil →
    # 0.53): a side-agnostic round-to-nearest (the rust_mm_bot/smm anti-pattern) would round the bid
    # to 0.48 (UP, improving past 0.478) and the ask to 0.52 (DOWN, improving past 0.522). The
    # asymmetric result (0.47 ≠ 0.53) is only reachable when the side chooses floor-vs-ceil.
    config = _config(half_spread=0.02)
    state = _warm_state(smoother_mid=0.50)
    obs = _obs(bid=0.478, ask=0.522)

    decision, _ = decide(obs, state, config)
    assert decision.kind == "QUOTE_TWO_SIDED"
    legs = {leg.leg_role: leg.price for leg in decision.intent_plan}

    # BID floors DOWN, ASK ceils UP — never the round-to-nearest 0.48 / 0.52.
    assert legs["bid"] == pytest.approx(0.47)
    assert legs["ask"] == pytest.approx(0.53)
    # Directional: the bid never rounds above its raw target, the ask never below its raw target.
    assert legs["bid"] <= 0.478
    assert legs["ask"] >= 0.522
    # post_only is set on every maker leg (REQ-056).
    assert all(leg.post_only is True for leg in decision.intent_plan)


# --- AC-046 / RED-40: post-clamp — both legs out of zone → NO_QUOTE --------------------------


def test_post_clamp_leg_out_of_zone() -> None:
    # A half_spread of 0.49 pushes BOTH legs out of the boundary zone (0.04, 0.96): the bid target
    # ``floor(min(0.50 − 0.49, 0.49)) = 0.01`` is below the lower boundary and the ask target
    # ``ceil(max(0.50 + 0.49, 0.51)) = 0.99`` is above the upper boundary. Both are OMITTED → no valid
    # leg → NO_QUOTE(leg_out_of_zone), never a place with a leg outside the quoting zone (REQ-052).
    config = _config(half_spread=0.49)
    state = _warm_state(smoother_mid=0.50)
    obs = _obs(bid=0.49, ask=0.51)

    decision, _ = decide(obs, state, config)
    assert decision.kind == "NO_QUOTE"
    assert decision.reason_codes == ("leg_out_of_zone",)
    assert decision.intent_plan == ()


# --- AC-047 / RED-42: one-leg survival downgrades a two-sided quote to one-sided --------------


def test_one_leg_survival_downgrades_to_one_sided() -> None:
    # Near the upper boundary the ASK leg clamps out while the BID survives. Anchor 0.95 (mid of
    # 0.94/0.96) with half_spread 0.02: the ask target ``ceil(max(0.97, 0.96)) = 0.97`` is above the
    # 0.96 upper boundary → omitted; the bid target ``floor(min(0.93, 0.94)) = 0.93`` is in zone →
    # survives. Two-sided DOWNGRADES to QUOTE_ONE_SIDED on the surviving (bid) side (REQ-052). The
    # two-sided band is widened to the boundary so anchor 0.95 still reaches the two-sided quote math.
    config = _config(half_spread=0.02, two_sided_band=(0.04, 0.96))
    state = _warm_state(smoother_mid=0.95)
    obs = _obs(bid=0.94, ask=0.96)

    decision, _ = decide(obs, state, config)
    assert decision.kind == "QUOTE_ONE_SIDED"
    assert decision.reason_codes == ("leg_out_of_zone",)
    assert len(decision.intent_plan) == 1
    survivor = decision.intent_plan[0]
    assert survivor.leg_role == "bid"  # the surviving side — the out-of-zone ask is omitted
    assert survivor.price == pytest.approx(0.93)


# --- inventory safety: an invalid reducing leg is NO_QUOTE, never a side-flip -----------------


def test_invalid_reducing_leg_no_quote_never_flips() -> None:
    # A ONE_SIDED_REDUCE (net LONG +0.6 ≥ soft limit → reduce by SELLING ⇒ the ASK leg) whose ASK
    # target clamps out of zone. Anchor 0.95 (0.94/0.96), half_spread 0.02: the ask target
    # ``ceil(max(0.97, 0.96)) = 0.97`` is above the 0.96 boundary → out of zone. The reduce fails to
    # NO_QUOTE(leg_out_of_zone). INVENTORY SAFETY: it must NEVER side-flip to the BID (adding) leg to
    # salvage a quote — quoting the adding side would GROW the very position we are reducing.
    config = _config(half_spread=0.02)
    state = _warm_state(smoother_mid=0.95)
    obs = _obs(bid=0.94, ask=0.96, net_position=0.6)

    decision, _ = decide(obs, state, config)
    assert decision.kind == "NO_QUOTE"
    assert decision.reason_codes == ("leg_out_of_zone",)
    assert decision.intent_plan == ()
    # Never side-flip: no place-quote leg is emitted, and in particular NOT the adding (bid) side.
    assert all(leg.kind != "place_quote" for leg in decision.intent_plan)
    assert not any(leg.leg_role == "bid" for leg in decision.intent_plan)
