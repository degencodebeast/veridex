"""Pure-tier E4 decision-policy layer — venue-mid anchor + zones + precedence (E4-T1).

These tests pin the FOUNDATION of the E4 policy spine that sits inside the row-H (HEALTHY) leg of
the REAL ``veridex.mm_strategy.core.decide`` reducer (no re-implementation): the venue-MID anchor
and the boundary / two-sided-liquidity zones, plus the load-bearing anchor-honesty invariants.

Pinned invariants (REQ-023/050/051/053/054/060/082/097; AC-001/002/004/009/036/037/038):

- ``test_anchor_is_venue_mid_never_raw_fv`` (RED-01/AC-004) — the anchor is the VENUE mid
  ``(bid+ask)/2``; the guard FV can NEVER center a quote, whether the guard is on or off.
- ``test_determinism_no_wall_clock`` (AC-002/RED-05) — patching the wall clock leaves the decision
  and next state byte-identical: ``decide`` reads no clock/rng/global.
- ``test_thin_or_crossed_or_gap_book_no_quote`` (AC-009/037/RED-11) — a ``gap`` / ``excluded`` book
  and a thin (below ``min_top_depth``) two-sided book each yield NO_QUOTE, never a place.
- ``test_boundary_and_two_sided_zones`` (AC-038) — an anchor outside the boundary zone is NO_QUOTE
  (``boundary_zone``); inside the boundary but outside the two-sided band it is at most one-sided
  (net-flat → the ``two_sided_zone_exit`` abstention); inside both it is QUOTE_TWO_SIDED.
- ``test_stream_degraded_no_quote`` (AC-036) — order-stream / projection degradation downgrades an
  otherwise placement-eligible frame to NO_QUOTE (``stream_degraded`` / ``projection_stale``).
- ``test_mid_never_imputed_on_empty_side`` (REQ-023) — a two-sided-"ok" book with ONE side absent
  yields NO anchor: the mid is never synthesized from the single present side; the decision
  downgrades to NO_QUOTE.
- ``test_no_microprice_config_path`` (REQ-051) — ``anchor_mode`` is the mono-valued ``mid`` and no
  config field or ``core`` code path selects a microprice / smoothed anchor.
"""

from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from veridex.mm_strategy import core
from veridex.mm_strategy.config import StrategyConfig
from veridex.mm_strategy.contracts import (
    GuardFairValue,
    GuardStateWatermark,
    InventoryProjection,
    StrategyObservation,
    StrategyState,
)
from veridex.mm_strategy.core import _classify_row, _venue_anchor, decide


def _config(*, guard_enabled: bool = False, **overrides: object) -> StrategyConfig:
    """A valid :class:`StrategyConfig` with ``guard_enabled`` explicit (it is REQUIRED)."""
    return StrategyConfig(guard_enabled=guard_enabled, **overrides)  # type: ignore[arg-type]


def _guard_fv(*, fv: float = 0.20, fv_source_epoch: int = 1) -> GuardFairValue:
    """A healthy guard FV leg whose ``fv`` deliberately DIFFERS from the venue mid (RED-01)."""
    return GuardFairValue(
        fv=fv,
        fv_source_ts=1,
        fv_recv_ts=990,
        fv_source_epoch=fv_source_epoch,
        message_id="msg-1",
        proof_status="proven",
    )


def _obs(
    *,
    observation_sequence: int = 2,
    book_source_epoch: int = 1,
    as_of_ts: int = 100_000,
    guard_fv: GuardFairValue | None = None,
    market_status: str = "ACTIVE",
    market_status_epoch: int | None = 5,
    book_status: str = "ok",
    tick_regime_changed: bool = False,
    level_count_in_band: int = 5,
    order_stream_ok: bool = True,
    projection_fresh: bool = True,
    bid: float | None = 0.49,
    ask: float | None = 0.51,
    bid_size: float | None = 100.0,
    ask_size: float | None = 120.0,
    net_position: float = 0.0,
) -> StrategyObservation:
    """A healthy per-tick observation; every ``recv_ts`` is derived ≤ ``as_of_ts`` so construction
    never trips the REQ-022 future-dating guard. Raw top-of-book + zone knobs are exposed so each
    anchor / zone / stream branch is constructible."""
    recv = as_of_ts - 10
    status_recv = None if market_status == "UNKNOWN" else recv
    status_epoch = None if market_status == "UNKNOWN" else market_status_epoch
    return StrategyObservation(
        fixture_id=1,
        market_ref="TEAM-A/YES",
        side="YES",
        token_id="TOKEN-YES",
        venue_market_ref="0xmarket",
        tick_size=0.01,
        observation_sequence=observation_sequence,
        book_source_epoch=book_source_epoch,
        bid=bid,
        ask=ask,
        bid_size=bid_size,
        ask_size=ask_size,
        book_status=book_status,  # type: ignore[arg-type]
        status_reason=None,
        book_recv_ts=recv,
        level_count_in_band=level_count_in_band,
        tick_regime_changed=tick_regime_changed,
        phase=1,
        suspended=False,
        match_state_recv_ts=recv,
        guard_fv=guard_fv,
        market_status=market_status,  # type: ignore[arg-type]
        market_status_recv_ts=status_recv,
        market_status_epoch=status_epoch,
        order_stream_ok=order_stream_ok,
        projection_fresh=projection_fresh,
        inventory=InventoryProjection(
            net_position=net_position, resting=(), projection_as_of_ts=as_of_ts, fresh=True
        ),
        as_of_ts=as_of_ts,
    )


def _warm_state(
    *,
    guard_watermark: GuardStateWatermark | None = None,
    last_as_of_ts: int = 99_000,
) -> StrategyState:
    """A mid-stream state with WARM references (smoother seeded + both rolling refs past
    ``ref_min_samples``) so a healthy in-window frame reaches row H and is placement-eligible."""
    return StrategyState(
        last_observation_sequence=1,
        last_book_source_epoch=1,
        last_as_of_ts=last_as_of_ts,
        last_market_status_epoch=1,
        last_market_status_recv_ts=1,
        guard_watermark=guard_watermark,
        smoother_mid=0.5,
        smoother_mid_ts=last_as_of_ts,
        spread_ref_samples=tuple(0.02 for _ in range(25)),
        depth_ref_samples=tuple(100.0 for _ in range(25)),
    )


# --- RED-01 / AC-004: the anchor is the venue mid, never raw FV -----------------------------


def test_anchor_is_venue_mid_never_raw_fv() -> None:
    # The venue book mid is (0.49 + 0.51) / 2 = 0.50; the guard FV is deliberately 0.20.
    obs = _obs(guard_fv=_guard_fv(fv=0.20))
    venue_mid = 0.50

    # Guard ON: the anchor is the VENUE mid — raw FV (0.20) can never center the quote (RED-01).
    assert _venue_anchor(obs, _config(guard_enabled=True)) == venue_mid
    # Guard OFF: identical venue-derived anchor — the anchor never depends on the guard leg.
    assert _venue_anchor(_obs(), _config(guard_enabled=False)) == venue_mid
    # The anchor is provably NOT the FV under any config.
    assert _venue_anchor(obs, _config(guard_enabled=True)) != 0.20


# --- AC-002 / RED-05: determinism — no wall clock ------------------------------------------


def test_determinism_no_wall_clock() -> None:
    import time

    config = _config()
    state = _warm_state()
    obs = _obs()

    first_decision, first_state = decide(obs, state, config)

    # Mutate the wall clock between evaluations; a pure/total ``decide`` is unaffected (AC-002).
    original = time.time
    try:
        time.time = lambda: 4_242_424.2  # type: ignore[assignment]
        second_decision, second_state = decide(obs, state, config)
    finally:
        time.time = original  # type: ignore[assignment]

    assert first_decision == second_decision
    assert first_state == second_state
    assert first_state.state_hash() == second_state.state_hash()
    assert first_decision.kind == "QUOTE_TWO_SIDED"


# --- AC-009 / AC-037 / RED-11: thin / crossed / gap book → no quote -------------------------


def test_thin_or_crossed_or_gap_book_no_quote() -> None:
    config = _config()
    state = _warm_state()

    # A disconnected (gap) book yields no anchor and no new quote intent (row E, book_gap).
    d_gap, _ = decide(_obs(book_status="gap"), state, config)
    assert d_gap.kind == "NO_QUOTE"
    assert d_gap.reason_codes == ("book_gap",)

    # A crossed/locked (excluded) book likewise (row E, book_excluded).
    d_excluded, _ = decide(_obs(book_status="excluded"), state, config)
    assert d_excluded.kind == "NO_QUOTE"
    assert d_excluded.reason_codes == ("book_excluded",)

    # A two-sided "ok" book that is THIN (top depth below min_top_depth=50) passes status=="ok" but
    # its mid is fiction (REQ-082) → NO_QUOTE, never a place.
    d_thin, _ = decide(_obs(bid_size=10.0, ask_size=10.0), state, config)
    assert d_thin.kind == "NO_QUOTE"
    assert d_thin.reason_codes == ("book_thin",)


# --- AC-038: boundary zone + two-sided-liquidity band --------------------------------------


def test_boundary_and_two_sided_zones() -> None:
    config = _config()  # boundary_zone (0.04, 0.96); two_sided_band (0.30, 0.70)
    state = _warm_state()

    # Anchor 0.50 is inside both zones → QUOTE_TWO_SIDED (control).
    d_in, _ = decide(_obs(bid=0.49, ask=0.51), state, config)
    assert d_in.kind == "QUOTE_TWO_SIDED"

    # Each off-mid zone case aligns the state's smoothed prior mid with the frame mid so the E4-T2
    # REQ-080 MID-JUMP event trigger (|raw mid − smoothed prior mid| > threshold, a warm-reference
    # row-E pre-emption) does not fire — isolating the row-H boundary / two-sided ZONE logic under test
    # (a real stream's smoother tracks the mid, so a settled book has no jump).

    # Anchor 0.02 is OUTSIDE the boundary zone → NO_QUOTE (boundary_zone).
    boundary_state = state.model_copy(update={"smoother_mid": 0.02})
    d_boundary, _ = decide(_obs(bid=0.01, ask=0.03), boundary_state, config)
    assert d_boundary.kind == "NO_QUOTE"
    assert d_boundary.reason_codes == ("boundary_zone",)

    # Anchor 0.80 is inside the boundary zone but OUTSIDE the two-sided band → at most one-sided.
    # Net-flat (net_position == 0) is the pinned abstention (two_sided_zone_exit; REQ-054).
    band_state = state.model_copy(update={"smoother_mid": 0.80})
    d_band, _ = decide(_obs(bid=0.79, ask=0.81, net_position=0.0), band_state, config)
    assert d_band.kind == "NO_QUOTE"
    assert d_band.reason_codes == ("two_sided_zone_exit",)


# --- AC-036: stream / projection degradation → no quote ------------------------------------


def test_stream_degraded_no_quote() -> None:
    config = _config()
    state = _warm_state()

    # An otherwise placement-eligible frame with a degraded order stream → NO_QUOTE stream_degraded.
    d_stream, _ = decide(_obs(order_stream_ok=False), state, config)
    assert d_stream.kind == "NO_QUOTE"
    assert d_stream.reason_codes == ("stream_degraded",)

    # A stale inventory/order projection → NO_QUOTE projection_stale.
    d_proj, _ = decide(_obs(projection_fresh=False), state, config)
    assert d_proj.kind == "NO_QUOTE"
    assert d_proj.reason_codes == ("projection_stale",)


# --- REQ-023: the mid is NEVER imputed from a single present side ---------------------------


def test_mid_never_imputed_on_empty_side() -> None:
    config = _config()
    state = _warm_state()

    # A book claiming status=="ok" with the ASK side absent (price None) but ample present-side
    # depth: the mid must NOT be synthesized from the bid alone. No anchor → NO_QUOTE, never a
    # fabricated two-sided quote (REQ-023).
    obs = _obs(bid=0.49, ask=None, bid_size=100.0, ask_size=120.0)
    assert _venue_anchor(obs, config) is None
    decision, _ = decide(obs, state, config)
    assert decision.kind == "NO_QUOTE"
    assert decision.kind != "QUOTE_TWO_SIDED"


# --- REQ-051: mono-valued anchor mode — no microprice / smoothed-anchor path ----------------


def test_no_microprice_config_path() -> None:
    # anchor_mode is the mono-valued "mid": any other value is unconstructible (no future mode in v0).
    assert StrategyConfig.model_fields["anchor_mode"].default == "mid"
    with pytest.raises(ValidationError):
        _config(anchor_mode="microprice")

    # No config field selects a microprice / smoothed anchor.
    field_names = set(StrategyConfig.model_fields)
    assert not any("microprice" in name.lower() for name in field_names)

    # No ``core`` code path references a microprice / smoothed-anchor selection.
    source = inspect.getsource(core).lower()
    assert "microprice" not in source


# --- E4-T2: venue-book event detection using state references (REQ-080/AC-010; RED-09/RED-10) ---
# These pin the REQ-080 venue-book EVENT triggers that pull quotes (NO_QUOTE + cooldown) inside the
# REAL ``decide`` reducer — ORTHOGONAL to E4-T1's row-H quote-disposition spine. The row-E ratio/jump
# gates (depth-vanish, spread-blowout, mid-jump) compare the RAW book against the STATE-CARRIED
# rolling references / smoothed prior mid and are inadmissible while the references warm; the
# reset-class ``phase`` transition lands in row R (re-seed). ``market_status`` / stream stay OUT of
# the trigger set — they are admission-QUOTE-only blockers (REQ-070/026/097), never triggers.


def test_reconnect_disappearing_depth_pulls_quotes() -> None:
    # RED-09: after a reconnect the top-of-book depth collapses — a warm-reference DEPTH-VANISH
    # event. The venue-book trigger PULLS the quote (NO_QUOTE ``book_thin``) and anchors an event
    # cooldown (REQ-080/081), so no fresh placement occurs until the dwell elapses.
    config = _config()
    state = _warm_state()  # warm depth reference (median of 25 samples) == 100.0
    dwell = config.book_state_dwell_before_quote_ms

    # (a) The ABSOLUTE floor form: top depth min(10, 10) == 10 < ``min_top_depth`` (50).
    vanished = _obs(bid=0.49, ask=0.51, bid_size=10.0, ask_size=10.0)
    assert _classify_row(vanished, state, config) == "E"
    decision, next_state = decide(vanished, state, config)
    assert decision.kind == "NO_QUOTE"
    assert decision.reason_codes == ("book_thin",)
    # The event anchors a cooldown (cancel-plan + cooldown; REQ-081) and ADMITS NOTHING (row E).
    assert next_state.event_cooldown_until_ts == vanished.as_of_ts + dwell
    assert next_state.spread_ref_samples == state.spread_ref_samples
    assert next_state.depth_ref_samples == state.depth_ref_samples
    assert next_state.smoother_mid == state.smoother_mid

    # (b) The RATIO form INDEPENDENTLY of the absolute floor: with a high rolling-depth reference
    # (1000), a depth of 100 clears the absolute floor (100 >= 50) yet collapses below
    # ``depth_collapse_ratio`` (0.25) x 1000 == 250 — depth-vanish still fires.
    high_ref = state.model_copy(update={"depth_ref_samples": tuple(1000.0 for _ in range(25))})
    ratio_vanish = _obs(bid=0.49, ask=0.51, bid_size=100.0, ask_size=100.0)
    assert _classify_row(ratio_vanish, high_ref, config) == "E"
    d_ratio, _ = decide(ratio_vanish, high_ref, config)
    assert d_ratio.kind == "NO_QUOTE"
    assert d_ratio.reason_codes == ("book_thin",)


def test_tick_regime_change_invalidates_plan() -> None:
    # AC-010/RED-10: a ``tick_regime_changed`` frame is a RESET-class trigger (row R) — it pulls the
    # quote (NO_QUOTE ``tick_regime_changed``), anchors an event cooldown, and RE-SEEDS the smoother
    # from this frame's own ok-book mid (the reset transition E4-T2 leaves to E2-T4's row R).
    config = _config()
    state = _warm_state()
    dwell = config.book_state_dwell_before_quote_ms

    tick = _obs(bid=0.49, ask=0.51, tick_regime_changed=True)
    assert _classify_row(tick, state, config) == "R"
    decision, next_state = decide(tick, state, config)
    assert decision.kind == "NO_QUOTE"
    assert decision.reason_codes == ("tick_regime_changed",)
    # Row R invalidates the plan: cooldown anchored, rolling refs cleared, smoother re-seeded from
    # this frame's ok mid (0.50) so the references can re-warm.
    assert next_state.event_cooldown_until_ts == tick.as_of_ts + dwell
    assert next_state.spread_ref_samples == ()
    assert next_state.depth_ref_samples == ()
    assert next_state.smoother_mid == 0.50


def test_status_not_in_trigger_list() -> None:
    # REQ-070/080: ``market_status`` is admission-QUOTE-only, NEVER a venue-book EVENT trigger. A
    # market_status change ALONE — a fully-healthy, warm, in-band book — must NOT classify row E or
    # anchor an event cooldown; it stays row H (venue-healthy) and the status only downgrades the
    # WRITE to NO_QUOTE while admission keeps training. (Mutation: adding ``market_status != ACTIVE``
    # to the trigger set reclassifies this frame to row E and anchors a cooldown → this test fails.)
    config = _config()
    state = _warm_state()

    # A HALTED market over a fully-healthy book (mid 0.50 == smoothed prior mid, depth 100, spread
    # 0.02, level 5): the venue book is UNCHANGED, so no venue-book trigger fires.
    obs = _obs(market_status="HALTED", market_status_epoch=5)
    assert _classify_row(obs, state, config) == "H"
    decision, next_state = decide(obs, state, config)
    assert decision.kind == "NO_QUOTE"
    assert decision.reason_codes == ("market_halted",)
    # The load-bearing assertion: status is QUOTE-only, so NO event cooldown is created (a real
    # venue-book trigger WOULD anchor one), and admission is ungated — the venue accumulators train.
    assert next_state.event_cooldown_until_ts is None
    assert len(next_state.spread_ref_samples) == len(state.spread_ref_samples) + 1
    assert len(next_state.depth_ref_samples) == len(state.depth_ref_samples) + 1


def test_spread_blowout_and_mid_jump_pull_quotes_only_when_warm() -> None:
    # REQ-080: the spread-blowout and mid-jump gates compare the RAW book against the STATE-carried
    # rolling-spread reference / smoothed prior mid, and are INADMISSIBLE while the references warm
    # (``event_ref_warmup``) — the liveness-preserving warmup gate. Both surface as ``book_thin``.
    config = _config()
    warm = _warm_state()  # rolling-spread ref (median 25x0.02) == 0.02; smoothed prior mid == 0.5

    # (a) SPREAD-BLOWOUT: raw spread 0.10 > ``spread_blowout_multiple`` (3.0) x 0.02 == 0.06. The mid
    # (bid 0.45 + ask 0.55)/2 == 0.50 equals the smoothed prior mid, so ONLY the spread gate fires.
    blowout = _obs(bid=0.45, ask=0.55)
    assert _classify_row(blowout, warm, config) == "E"
    d_spread, _ = decide(blowout, warm, config)
    assert d_spread.kind == "NO_QUOTE"
    assert d_spread.reason_codes == ("book_thin",)

    # (b) MID-JUMP: raw mid (0.55 + 0.57)/2 == 0.56 vs smoothed prior mid 0.5 — a jump of 0.06 >
    # ``mid_jump_threshold`` (0.02). Depth 100 and spread 0.02 are nominal, so ONLY mid-jump fires.
    jump = _obs(bid=0.55, ask=0.57)
    assert _classify_row(jump, warm, config) == "E"
    d_jump, _ = decide(jump, warm, config)
    assert d_jump.kind == "NO_QUOTE"
    assert d_jump.reason_codes == ("book_thin",)

    # (c) WARMUP INADMISSIBILITY: the SAME jumped/blown frames on a state whose references are BELOW
    # ``ref_min_samples`` are row W (WARMUP) — the ratio/jump gates cannot fire without a warm
    # reference, so the frame ADMITS (no cooldown) instead of pulling quotes (REQ-080 liveness).
    cold = StrategyState(
        last_observation_sequence=1,
        last_book_source_epoch=1,
        last_as_of_ts=99_000,
        last_market_status_epoch=1,
        last_market_status_recv_ts=1,
        smoother_mid=0.5,
        smoother_mid_ts=99_000,
        spread_ref_samples=(0.02, 0.02),  # 2 < ref_min_samples (20) => NOT warm
        depth_ref_samples=(100.0, 100.0),
    )
    assert _classify_row(jump, cold, config) == "W"
    d_cold, cold_next = decide(jump, cold, config)
    assert d_cold.kind == "NO_QUOTE"
    assert d_cold.reason_codes == ("event_ref_warmup",)
    assert cold_next.event_cooldown_until_ts is None  # warmup never creates a cooldown


def test_phase_transition_is_a_reset_trigger() -> None:
    # REQ-080: a match-state ``phase`` transition is a RESET-class trigger (row R) — it pulls the
    # quote (NO_QUOTE ``phase_transition``), anchors an event cooldown, and re-seeds the smoother.
    # It compares against the phase carried on the PRIOR state (``last_phase``); a state with no prior
    # phase merely SEEDS the watermark (no spurious reset).
    config = _config()
    dwell = config.book_state_dwell_before_quote_ms
    # A warm state whose LAST accepted phase was 0; the incoming frame is phase 1 → a transition.
    state = _warm_state().model_copy(update={"last_phase": 0})

    obs = _obs(bid=0.49, ask=0.51)  # phase == 1 (the _obs default)
    assert obs.phase == 1
    assert _classify_row(obs, state, config) == "R"
    decision, next_state = decide(obs, state, config)
    assert decision.kind == "NO_QUOTE"
    assert decision.reason_codes == ("phase_transition",)
    assert next_state.event_cooldown_until_ts == obs.as_of_ts + dwell
    assert next_state.smoother_mid == 0.50  # re-seeded from this ok-book mid
    assert next_state.last_phase == 1  # the phase watermark advances to the current frame

    # No prior phase (``last_phase is None``) merely SEEDS the watermark — never a spurious reset.
    seed_state = _warm_state()
    assert seed_state.last_phase is None
    assert _classify_row(obs, seed_state, config) == "H"
    _, seeded = decide(obs, seed_state, config)
    assert seeded.last_phase == 1
