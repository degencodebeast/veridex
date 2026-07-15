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


# =====================================================================================
# E4-T3: basis/residual guard + quiescence + extreme + stale-FV fail-safe
# (REQ-032 / REQ-070 guard block / REQ-071 / REQ-074 / REQ-075 / REQ-076 / REQ-077;
#  AC-005 / AC-007 / AC-008 / AC-011; RED-02 / RED-08)
#
# These pin the GUARD slot (#5) inside the row-H disposition spine: FV freshness/suspension/
# presence gates (guard-scoped, REQ-022/076), the REQ-032 basis warmup, the REQ-075 extreme-
# residual abstention, the REQ-071 ABSOLUTE (never spread-relative) band, the strict REQ-074
# event-gate precedence, and the STAGE-1 fail-safe: freshness gates the basis-EWMA UPDATE itself
# (a stale FV never trains the accumulator), not merely the quote. The default estimator is
# ``rolling_median``; the fail-safe test uses ``halflife_ewma`` to inspect the decay anchor.
# =====================================================================================


def _fresh_fv(
    *, fv: float, as_of_ts: int = 100_000, fv_source_epoch: int = 1
) -> GuardFairValue:
    """A guard FV leg that is FRESH under the default config: transport age
    ``as_of_ts − fv_recv_ts == 10 ms ≤ fv_freshness_ms`` (10 000) AND content lag
    ``fv_recv_ts/1000 − fv_source_ts ≈ 6 s ≤ fv_source_lag_s`` (10). ``fv`` deliberately differs
    from the venue mid (the anchor is never the FV — RED-01)."""
    recv = as_of_ts - 10
    return GuardFairValue(
        fv=fv,
        fv_source_ts=recv // 1000 - 5,
        fv_recv_ts=recv,
        fv_source_epoch=fv_source_epoch,
        message_id="msg-1",
        proof_status="proven",
    )


def _stale_fv(
    *, fv: float, as_of_ts: int = 100_000, fv_source_epoch: int = 1
) -> GuardFairValue:
    """A TRANSPORT-stale guard FV: ``as_of_ts − fv_recv_ts == 20 000 ms > fv_freshness_ms`` (10 000).
    The content clock is kept fresh so the staleness is unambiguously the transport gate (REQ-022);
    ``fv_recv_ts`` stays ≤ ``as_of_ts`` so construction never trips the future-dating guard."""
    recv = as_of_ts - 20_000
    return GuardFairValue(
        fv=fv,
        fv_source_ts=recv // 1000 - 5,
        fv_recv_ts=recv,
        fv_source_epoch=fv_source_epoch,
        message_id="msg-1",
        proof_status="proven",
    )


def _guarded_warm_state(
    *,
    basis_gap: float = 0.0,
    basis_count: int = 40,
    fv_source_epoch: int = 1,
    event_cooldown_until_ts: int | None = None,
) -> StrategyState:
    """A warm-reference state whose ``rolling_median`` basis is ALSO warm: ``basis_count`` accepted
    samples all equal to ``basis_gap`` (so the median basis is exactly ``basis_gap``) with a matching
    ``basis_sample_count``. ``last_phase=1`` matches the ``_obs`` default phase (no row-R transition)
    and the guard watermark is seeded at ``fv_source_epoch`` (no row-F reset) so a fresh-FV frame at
    the same epoch reaches row H and the guard block."""
    return StrategyState(
        last_observation_sequence=1,
        last_book_source_epoch=1,
        last_as_of_ts=99_000,
        last_market_status_epoch=1,
        last_market_status_recv_ts=1,
        last_phase=1,
        guard_watermark=GuardStateWatermark(fv_source_epoch=fv_source_epoch),
        smoother_mid=0.5,
        smoother_mid_ts=99_000,
        spread_ref_samples=tuple(0.02 for _ in range(25)),
        depth_ref_samples=tuple(100.0 for _ in range(25)),
        basis_samples=tuple((900 + i, basis_gap) for i in range(basis_count)),
        basis_sample_count=basis_count,
        event_cooldown_until_ts=event_cooldown_until_ts,
    )


# --- AC-005 / RED-02: a persistent basis is not edge ---------------------------------------


def test_persistent_basis_not_edge() -> None:
    # A FULLY PERSISTENT basis (raw_gap == basis ⇒ residual == 0) is not tradable edge — a
    # median-demeaned persistent offset can never, by itself, pull a side (RED-02). The guarded
    # frame quotes normally (QUOTE_TWO_SIDED), identically to a zero-gap book: the guard is quiescent.
    config = _config(guard_enabled=True)
    state = _guarded_warm_state(basis_gap=0.035)
    # raw_gap = fv 0.535 − mid 0.50 = 0.035 == basis 0.035 ⇒ residual 0.0.
    obs = _obs(guard_fv=_fresh_fv(fv=0.535))
    decision, _ = decide(obs, state, config)
    assert decision.kind == "QUOTE_TWO_SIDED"
    assert decision.reason_codes == ()


# --- AC-007: extreme residual → NO_QUOTE, never a taker chase -------------------------------


def test_extreme_residual_no_quote_no_taker_chase() -> None:
    # |residual| ≥ extreme_multiple × band (3 × 0.02 = 0.06) ⇒ NO_QUOTE(residual_extreme) — never a
    # taker chase, never a "bigger signal" (REQ-075). The high-|residual| moment is an FV-lateness
    # (gap-tail) moment; the guard abstains rather than inverting its own toxicity logic.
    config = _config(guard_enabled=True)
    state = _guarded_warm_state(basis_gap=0.0)
    # residual = (fv 0.60 − mid 0.50) − basis 0.0 = 0.10 ≥ 0.06.
    obs = _obs(guard_fv=_fresh_fv(fv=0.60))
    decision, _ = decide(obs, state, config)
    assert decision.kind == "NO_QUOTE"
    assert decision.reason_codes == ("residual_extreme",)
    # No taker chase: v0 has no TAKE (REQ-061) and the extreme guard never escalates to a place.
    assert decision.kind not in ("QUOTE_TWO_SIDED", "QUOTE_ONE_SIDED")
    assert all(intent.kind != "place_quote" for intent in decision.intent_plan)


# --- AC-008 / RED-08: missing / stale / suspended FV → NO_QUOTE, never a submit -------------


def test_stale_or_suspended_fv_no_submit_quote() -> None:
    # A missing / stale / suspended TxLINE leg in the GUARDED arm fails closed to NO_QUOTE with the
    # exact reason — never a submit-capable quote; freshness/proof are never fabricated (REQ-076).
    config = _config(guard_enabled=True)
    state = _guarded_warm_state(basis_gap=0.0)

    # Missing FV leg (guard_fv is None) ⇒ txline_missing.
    d_missing, _ = decide(_obs(guard_fv=None), state, config)
    assert d_missing.kind == "NO_QUOTE"
    assert d_missing.reason_codes == ("txline_missing",)

    # Transport-stale FV (age 20 s > fv_freshness_ms 10 s) ⇒ txline_stale.
    d_stale, _ = decide(_obs(guard_fv=_stale_fv(fv=0.55)), state, config)
    assert d_stale.kind == "NO_QUOTE"
    assert d_stale.reason_codes == ("txline_stale",)

    # Suspended match-state (fresh FV present) ⇒ txline_suspended.
    suspended = _obs(guard_fv=_fresh_fv(fv=0.55)).model_copy(update={"suspended": True})
    d_susp, _ = decide(suspended, state, config)
    assert d_susp.kind == "NO_QUOTE"
    assert d_susp.reason_codes == ("txline_suspended",)


# --- AC-011 / REQ-032: pre-warmup the guard is inert (basis_warmup) -------------------------


def test_warmup_guard_inert() -> None:
    # Below ``basis_min_samples`` accepted samples the residual guard is INERT: the guarded frame is
    # NO_QUOTE(basis_warmup) and the venue-only core still governs — a guard-OFF frame on the SAME
    # book QUOTES (REQ-032). Warm venue references isolate the BASIS warmup from event warmup.
    guarded = _config(guard_enabled=True)
    baseline = _config(guard_enabled=False)
    # Venue references warm, but the basis holds only 5 < basis_min_samples (30) accepted samples.
    state = _guarded_warm_state(basis_gap=0.0, basis_count=5)

    d_guard, _ = decide(_obs(guard_fv=_fresh_fv(fv=0.55)), state, guarded)
    assert d_guard.kind == "NO_QUOTE"
    assert d_guard.reason_codes == ("basis_warmup",)

    # The venue-only core still applies: with the guard OFF the same book QUOTES.
    d_base, _ = decide(_obs(), state, baseline)
    assert d_base.kind == "QUOTE_TWO_SIDED"


# --- REQ-074: the event gate strictly precedes the residual guard ---------------------------


def test_event_gate_precedes_residual_guard() -> None:
    # The event gate STRICTLY precedes the residual guard (REQ-074). A frame under an ACTIVE event
    # cooldown (row C) whose residual WOULD be extreme is NO_QUOTE(event_cooldown) — the cooldown
    # pre-empts the guard entirely, so the extreme-residual verdict never runs. (Mutation: letting
    # the guard act while a cooldown is active surfaces residual_extreme here instead.)
    config = _config(guard_enabled=True)
    # Cooldown active: as_of_ts (100_000) < event_cooldown_until_ts (200_000).
    state = _guarded_warm_state(basis_gap=0.0, event_cooldown_until_ts=200_000)
    # residual = (fv 0.60 − mid 0.50) − basis 0.0 = 0.10 ≥ extreme 0.06 — WOULD be residual_extreme.
    obs = _obs(guard_fv=_fresh_fv(fv=0.60))
    assert _classify_row(obs, state, config) == "C"
    decision, _ = decide(obs, state, config)
    assert decision.kind == "NO_QUOTE"
    assert decision.reason_codes == ("event_cooldown",)
    assert decision.reason_codes != ("residual_extreme",)


# --- REQ-071 / MAJOR-5: the residual band is ABSOLUTE, never spread-relative ----------------


def test_residual_band_is_absolute_not_spread_relative() -> None:
    # The residual band is an ABSOLUTE probability-space width — NEVER scaled by the book spread
    # (REQ-071). Hold the residual FIXED (0.01) and VARY the spread wide↔narrow: the guard verdict
    # is IDENTICAL because the extreme threshold (extreme_multiple × band = 0.06) carries no spread
    # term. A spread-relative band (band × (best_ask − best_bid)) would read the NARROW book as
    # extreme while the WIDE book stays admissible — flipping the verdict — and this test kills it.
    # ``spread_blowout_multiple`` is raised so the wide spread does not trip the REQ-080 blowout
    # event (row E), isolating the band-absoluteness under test; both mids are 0.50 (no mid-jump).
    # The residual (0.01) is deliberately kept in the QUIESCENT zone (≤ residual_band 0.02, below the
    # E4-T4 side-pull band) so this test isolates the EXTREME threshold's absoluteness alone.
    config = _config(guard_enabled=True, spread_blowout_multiple=1000.0)
    state = _guarded_warm_state(basis_gap=0.0)

    # Narrow spread 0.02 and wide spread 0.90 — SAME mid 0.50 ⇒ SAME anchor ⇒ SAME raw_gap
    # (fv 0.51 − 0.50 = 0.01) ⇒ SAME residual 0.01 (≤ band 0.02 ⇒ quiescent, well below extreme 0.06).
    narrow = _obs(bid=0.49, ask=0.51, guard_fv=_fresh_fv(fv=0.51))
    wide = _obs(bid=0.05, ask=0.95, guard_fv=_fresh_fv(fv=0.51))

    d_narrow, _ = decide(narrow, state, config)
    d_wide, _ = decide(wide, state, config)

    # The verdict is spread-invariant: an absolute band admits BOTH identically. (Under a
    # spread-relative band the narrow book would flip to NO_QUOTE(residual_extreme).)
    assert d_narrow.kind == d_wide.kind == "QUOTE_TWO_SIDED"
    assert d_narrow.reason_codes == d_wide.reason_codes == ()


# --- STAGE-1 fail-safe (mandated): freshness gates the EWMA UPDATE, not only the quote ------


def test_stale_fv_not_admitted_to_basis() -> None:
    # A stale/suspended FV must NOT train the basis accumulator — otherwise it silently corrupts the
    # basis (and its ``as_of_ts`` decay anchor) for every later fresh frame. The freshness check
    # PRECEDES the EWMA fold in the code path, so the accumulator stays FROZEN on a stale frame.
    config = _config(
        guard_enabled=True, basis_estimator="halflife_ewma", ewma_halflife_ms=1_000
    )
    # A warm EWMA basis: value 0.010 anchored at ts 90_000 (≥ basis_min_samples folded upstream).
    warm = StrategyState(
        last_observation_sequence=100,
        last_book_source_epoch=1,
        last_as_of_ts=99_000,
        last_market_status_epoch=1,
        last_market_status_recv_ts=1,
        last_phase=1,
        guard_watermark=GuardStateWatermark(fv_source_epoch=1),
        smoother_mid=0.5,
        smoother_mid_ts=99_000,
        spread_ref_samples=tuple(0.02 for _ in range(25)),
        depth_ref_samples=tuple(100.0 for _ in range(25)),
        basis_ewma_value=0.010,
        basis_ewma_ts=90_000,
        basis_sample_count=40,
    )

    # (1) A transport-stale FV frame FREEZES the accumulator (value, ts, count all unchanged) and
    # fails closed to NO_QUOTE(txline_stale) — the stale FV never trained the basis.
    stale = _obs(
        observation_sequence=101,
        as_of_ts=100_000,
        guard_fv=_stale_fv(fv=0.55, as_of_ts=100_000),
    )
    d_stale, after_stale = decide(stale, warm, config)
    assert d_stale.kind == "NO_QUOTE"
    assert d_stale.reason_codes == ("txline_stale",)
    assert after_stale.basis_ewma_value == 0.010  # UNCHANGED — no stale training
    assert after_stale.basis_ewma_ts == 90_000  # decay anchor UNCHANGED
    assert after_stale.basis_sample_count == 40  # sample count UNCHANGED

    # (2) No corruption: a subsequent FRESH frame yields the SAME basis it would have had if the
    # stale frame had never arrived — because the frozen decay anchor (90_000) is preserved, the
    # fresh fold decays over the GENUINE interval, not a stale-shortened one.
    fresh = _obs(
        observation_sequence=102,
        as_of_ts=101_000,
        guard_fv=_fresh_fv(fv=0.55, as_of_ts=101_000),
    )
    _, after_fresh_via_stale = decide(fresh, after_stale, config)
    # The counterfactual: the SAME fresh frame applied directly to the pre-stale state.
    _, after_fresh_direct = decide(fresh, warm, config)
    assert after_fresh_via_stale.basis_ewma_value == pytest.approx(
        after_fresh_direct.basis_ewma_value
    )
    assert after_fresh_via_stale.basis_ewma_ts == after_fresh_direct.basis_ewma_ts


# --- E4-T4 / REQ-073 / AC-006: directional residual side-pull (the worst-bug rule) ----------
# The residual axis inside the admissible band has THREE regimes by increasing |residual|:
#   quiescent  |residual| <= residual_band (0.02)                 -> QUOTE_TWO_SIDED (E4-T1 floor)
#   pull       residual_band < |residual| < extreme (0.06)        -> QUOTE_ONE_SIDED (THIS task)
#   extreme    |residual| >= extreme_multiple*residual_band (0.06)-> NO_QUOTE(residual_extreme) (T3)
# residual = (fv - anchor) - basis, anchor = venue mid. DIRECTION (REQ-073): fv ABOVE the anchor
# (positive residual) means the venue's YES ASK is too cheap vs fair value, so a taker will lift our
# resting ask adversely -> PULL THE ASK. Symmetric: fv BELOW the anchor (negative residual) means our
# YES BID is too high -> PULL THE BID. A flipped sign quotes INTO the adverse flow and loses money.


def test_positive_residual_pulls_ask() -> None:
    # residual > +residual_band ⇒ PULL the YES ASK (REQ-073/AC-006). basis is a persistent 0.0, so
    # residual = fv 0.54 − mid 0.50 = +0.04, which sits in the pull band (0.02 < 0.04 < 0.06). fair
    # value is ABOVE the anchor ⇒ our resting ask is too cheap ⇒ pull the ask; the bid may rest, so
    # the disposition is QUOTE_ONE_SIDED (never a two-sided quote, never NO_QUOTE — not extreme).
    config = _config(guard_enabled=True)
    state = _guarded_warm_state(basis_gap=0.0)
    obs = _obs(guard_fv=_fresh_fv(fv=0.54))  # residual = +0.04

    decision, _ = decide(obs, state, config)
    assert decision.kind == "QUOTE_ONE_SIDED"
    assert decision.reason_codes == ("residual_pull_ask",)


def test_negative_residual_pulls_bid() -> None:
    # residual < −residual_band ⇒ PULL the YES BID (REQ-073/AC-006). residual = fv 0.46 − mid 0.50 =
    # −0.04, in the pull band (−0.06 < −0.04 < −0.02). fair value is BELOW the anchor ⇒ our resting
    # bid is too high ⇒ pull the bid; the ask may rest ⇒ QUOTE_ONE_SIDED. This is the mirror image of
    # the positive case: the SIGN of the residual selects the side, and only that one side is pulled.
    config = _config(guard_enabled=True)
    state = _guarded_warm_state(basis_gap=0.0)
    obs = _obs(guard_fv=_fresh_fv(fv=0.46))  # residual = −0.04

    decision, _ = decide(obs, state, config)
    assert decision.kind == "QUOTE_ONE_SIDED"
    assert decision.reason_codes == ("residual_pull_bid",)


def test_sign_mutation_pulls_wrong_side_fails() -> None:
    # THE adversarial sign-flip guard (RED-03). This test pins the WHOLE sign→side mapping in one
    # place: a POSITIVE residual pulls the ASK and a NEGATIVE residual pulls the BID — never the
    # reverse. Flipping the `>`/`<` comparison in the pull logic swaps both branches, so a positive
    # residual would pull the BID and a negative residual the ASK — quoting INTO the adverse flow.
    # Both assertions below then fail, killing the money-bug mutation. (The direction is asserted for
    # a SPECIFIC residual sign so the failure is about DIRECTION, not mechanics.)
    config = _config(guard_enabled=True)
    state = _guarded_warm_state(basis_gap=0.0)

    # fair value ABOVE anchor (residual +0.04) ⇒ ask is too cheap ⇒ pull the ASK, NOT the bid.
    pos = _obs(guard_fv=_fresh_fv(fv=0.54))
    d_pos, _ = decide(pos, state, config)
    assert d_pos.reason_codes == ("residual_pull_ask",)
    assert d_pos.reason_codes != ("residual_pull_bid",)

    # fair value BELOW anchor (residual −0.04) ⇒ bid is too high ⇒ pull the BID, NOT the ask.
    neg = _obs(guard_fv=_fresh_fv(fv=0.46))
    d_neg, _ = decide(neg, state, config)
    assert d_neg.reason_codes == ("residual_pull_bid",)
    assert d_neg.reason_codes != ("residual_pull_ask",)


def test_pull_threshold_is_absolute_band() -> None:
    # The pull threshold is the ABSOLUTE ``residual_band`` (0.02) — NEVER scaled by the book spread
    # (REQ-071, consistent with E4-T3's extreme band). Held at a FIXED residual, varying the spread
    # wide↔narrow must not change the verdict. ``spread_blowout_multiple`` is raised so the wide book
    # does not trip the REQ-080 spread-blowout event (row E); both mids are 0.50 (no mid-jump), so
    # the ONLY thing that varies is the raw spread.
    config = _config(guard_enabled=True, spread_blowout_multiple=1000.0)
    state = _guarded_warm_state(basis_gap=0.0)

    # (1) residual JUST ABOVE the band pulls the ask under BOTH spreads (spread-invariant pull).
    # residual = fv 0.53 − mid 0.50 = +0.03 (> band 0.02, < extreme 0.06). Same mid 0.50 for both.
    narrow_pull = _obs(bid=0.49, ask=0.51, guard_fv=_fresh_fv(fv=0.53))  # spread 0.02
    wide_pull = _obs(bid=0.05, ask=0.95, guard_fv=_fresh_fv(fv=0.53))  # spread 0.90
    d_narrow_pull, _ = decide(narrow_pull, state, config)
    d_wide_pull, _ = decide(wide_pull, state, config)
    assert d_narrow_pull.kind == d_wide_pull.kind == "QUOTE_ONE_SIDED"
    assert d_narrow_pull.reason_codes == d_wide_pull.reason_codes == ("residual_pull_ask",)

    # (2) residual JUST BELOW the band does NOT pull under EITHER spread (stays QUOTE_TWO_SIDED).
    # residual = fv 0.515 − mid 0.50 = +0.015 (< band 0.02) ⇒ quiescent. A spread-RELATIVE threshold
    # (band × spread) would shrink to 0.0004 on the narrow book, so 0.015 would SPURIOUSLY exceed it
    # and pull the ask — this arm kills the spread-scaling mutation.
    narrow_quiet = _obs(bid=0.49, ask=0.51, guard_fv=_fresh_fv(fv=0.515))
    wide_quiet = _obs(bid=0.05, ask=0.95, guard_fv=_fresh_fv(fv=0.515))
    d_narrow_quiet, _ = decide(narrow_quiet, state, config)
    d_wide_quiet, _ = decide(wide_quiet, state, config)
    assert d_narrow_quiet.kind == d_wide_quiet.kind == "QUOTE_TWO_SIDED"
    assert d_narrow_quiet.reason_codes == d_wide_quiet.reason_codes == ()


# --- E4-T5 / REQ-078 / AC-054 / RED-50: pre-match basis gate --------------------------------
# A pre-match-ONLY fail-closed gate inside the guard block, BETWEEN basis-warmup and the residual
# band. Pre-match (``phase == 0``) with the basis warm, a persistent basis WIDER than the venue's
# own top-of-book spread (``|basis| >= best_ask − best_bid``) means the pre-match edge estimate is
# unreliable -> NO_QUOTE(prematch_basis_exceeds_spread). This comparison is DELIBERATELY
# spread-relative (REQ-078) and is a DISTINCT quantity from the residual band below it, which is the
# ABSOLUTE ``residual_band`` config width NEVER scaled by the spread (REQ-071): basis-vs-spread here,
# residual-vs-absolute-band there. Precedence is load-bearing — the gate PRECEDES the residual wall,
# so a pre-match block is never mislabeled ``residual_extreme``.


def test_prematch_basis_exceeds_spread_no_quote() -> None:
    # AC-054: guarded, pre-match (``phase == 0``), warmup complete, ``|basis| >= (best_ask − best_bid)``
    # ⇒ NO_QUOTE(prematch_basis_exceeds_spread). The default book bid=0.49/ask=0.51 has spread 0.02;
    # a basis of 0.03 exceeds it (0.03 chosen over 0.02 to clear the IEEE-754 0.020000000000000018
    # float representation of the spread). ``last_phase=0`` matches the pre-match obs so the phase is
    # unchanged (no row-R reset pre-empts the guard block).
    config = _config(guard_enabled=True)
    prematch_state = _guarded_warm_state(basis_gap=0.03).model_copy(update={"last_phase": 0})
    prematch = _obs(guard_fv=_fresh_fv(fv=0.53)).model_copy(update={"phase": 0})

    decision, _ = decide(prematch, prematch_state, config)
    assert decision.kind == "NO_QUOTE"
    assert decision.reason_codes == ("prematch_basis_exceeds_spread",)

    # CONTROL 1 — ``|basis| < spread`` at ``phase == 0`` does NOT trip the gate: basis 0.01 < spread
    # 0.02, so the frame falls through to the residual band (residual ≈ 0 ⇒ quiescent ⇒ quote).
    narrow_basis_state = _guarded_warm_state(basis_gap=0.01).model_copy(
        update={"last_phase": 0}
    )
    narrow_basis = _obs(guard_fv=_fresh_fv(fv=0.51)).model_copy(update={"phase": 0})
    d_narrow, _ = decide(narrow_basis, narrow_basis_state, config)
    assert d_narrow.kind == "QUOTE_TWO_SIDED"
    assert d_narrow.reason_codes != ("prematch_basis_exceeds_spread",)

    # CONTROL 2 — ``phase != 0`` (post-match) with ``|basis| >= spread`` does NOT trip: the gate is
    # pre-match ONLY. Same basis 0.03 ≥ spread 0.02, but phase 1 skips the gate; the residual (0.0)
    # is quiescent so the frame quotes normally. (``last_phase=1`` default ⇒ no row-R transition.)
    postmatch_state = _guarded_warm_state(basis_gap=0.03)
    postmatch = _obs(guard_fv=_fresh_fv(fv=0.53))  # phase defaults to 1
    d_post, _ = decide(postmatch, postmatch_state, config)
    assert d_post.kind == "QUOTE_TWO_SIDED"
    assert d_post.reason_codes != ("prematch_basis_exceeds_spread",)


def test_prematch_gate_not_residual_extreme_reason() -> None:
    # RED-50: when the pre-match gate fires, the reason is ``prematch_basis_exceeds_spread``, NEVER
    # ``residual_extreme`` — the gate PRECEDES the residual band. Construct a frame where BOTH would
    # fire: pre-match (``phase == 0``) with basis 0.03 ≥ spread 0.02 (gate), AND a residual that is
    # extreme (residual = (fv 0.62 − mid 0.50) − basis 0.03 = 0.09 ≥ extreme 0.06). Because the
    # pre-match gate runs first, the recorded reason is the pre-match reason — a pre-match block is
    # never mislabeled as an extreme-residual block. (Mutation: reusing ``residual_extreme`` as the
    # pre-match reason — or placing the gate AFTER the residual wall — surfaces ``residual_extreme``
    # here and fails this test.)
    config = _config(guard_enabled=True)
    state = _guarded_warm_state(basis_gap=0.03).model_copy(update={"last_phase": 0})
    obs = _obs(guard_fv=_fresh_fv(fv=0.62)).model_copy(update={"phase": 0})

    decision, _ = decide(obs, state, config)
    assert decision.kind == "NO_QUOTE"
    assert decision.reason_codes == ("prematch_basis_exceeds_spread",)
    assert decision.reason_codes != ("residual_extreme",)


# =====================================================================================
# E4-T6: inventory-reducing one-sided rule (REQ-054 which-side / REQ-083 / AC-023)
#
# The INVENTORY slot (#3) inside the row-H disposition spine, ABOVE the two-sided band and the
# guard: ``|net_position| >= inventory_soft_limit`` quotes ONLY the inventory-REDUCING leg
# (ONE_SIDED_REDUCE) — net LONG YES (> 0) reduces by SELLING YES (the ASK leg), net SHORT YES (< 0)
# reduces by BUYING YES (the BID leg). The reducing leg materialises as a size-free ``place_quote``
# intent whose ``leg_role`` IS the side (price=None — E4-T7 fills the surviving leg's price). The
# two-sided-band exit is EXTENDED: outside the band, net != 0 quotes the reducing leg, net-flat keeps
# the E4-T1 pinned ``two_sided_zone_exit`` abstention. reduce_conflict (the cross-slot coupling): when
# the reducing leg is ALSO the leg the E4-T4 guard would PULL as adverse (residual sign), reducing
# would quote INTO adverse flow, so it fails closed to NO_QUOTE(``reduce_conflict``) + cancel (E5-T3
# owns the cancel intent). NO size (R4-A owns sizing).
# =====================================================================================


def test_inventory_over_soft_limit_reduces_side() -> None:
    # AC-023: ``|net_position| >= inventory_soft_limit`` (0.5) ⇒ ONE_SIDED_REDUCE quoting ONLY the
    # REDUCING leg. Inventory pre-empts the two-sided band + the guard, so it fires even with the
    # anchor (0.50) squarely inside the two-sided band where an unencumbered book would quote both
    # sides. The SPECIFIC surviving leg is pinned via the intent's physical ``leg_role`` so the
    # adding-vs-reducing direction is load-bearing (the mutation quoting the ADDING leg flips it).
    config = _config()  # guard OFF — isolate the inventory rule from any residual/toxic signal
    state = _warm_state()

    # net LONG YES (+0.6): reduce by SELLING YES ⇒ the ASK leg survives (never the bid).
    d_long, _ = decide(_obs(bid=0.49, ask=0.51, net_position=0.6), state, config)
    assert d_long.kind == "ONE_SIDED_REDUCE"
    assert d_long.reason_codes == ("inventory_reduce",)
    assert len(d_long.intent_plan) == 1
    reduce_leg = d_long.intent_plan[0]
    assert reduce_leg.kind == "place_quote"
    assert reduce_leg.leg_role == "ask"  # the REDUCING leg for a long — NOT "bid" (the adding leg)
    # E4-T7 fills the surviving leg's price: ask = ceil(max(anchor 0.50 + h 0.02, best_ask 0.51)) = 0.52.
    assert reduce_leg.price == pytest.approx(0.52)

    # net SHORT YES (−0.6): the mirror — reduce by BUYING YES ⇒ the BID leg survives (never the ask).
    d_short, _ = decide(_obs(bid=0.49, ask=0.51, net_position=-0.6), state, config)
    assert d_short.kind == "ONE_SIDED_REDUCE"
    assert d_short.reason_codes == ("inventory_reduce",)
    assert d_short.intent_plan[0].leg_role == "bid"  # NOT "ask" (the adding leg for a short)


def test_two_sided_zone_exit_reducing_or_abstain() -> None:
    # REQ-054: outside the two-sided band (inside the boundary) is at most one-sided. This EXTENDS the
    # E4-T1 net-flat-only abstention: with net BELOW the soft limit (|0.2| < 0.5 ⇒ slot #3 does NOT
    # fire) the band-exit governs — net != 0 quotes the REDUCING leg, net-flat abstains. The anchor
    # 0.80 is inside the boundary (0.04, 0.96) but outside the two-sided band (0.30, 0.70); the
    # smoother is aligned to 0.80 so no REQ-080 mid-jump (row E) pre-empts the row-H zone logic.
    config = _config()  # guard OFF
    band_state = _warm_state().model_copy(update={"smoother_mid": 0.80})

    # net LONG != 0 (below soft limit) ⇒ the reducing (ask) leg, not the abstention.
    d_long, _ = decide(_obs(bid=0.79, ask=0.81, net_position=0.2), band_state, config)
    assert d_long.kind == "ONE_SIDED_REDUCE"
    assert d_long.reason_codes == ("inventory_reduce",)
    assert d_long.intent_plan[0].leg_role == "ask"

    # net SHORT != 0 ⇒ the reducing (bid) leg — the mirror.
    d_short, _ = decide(_obs(bid=0.79, ask=0.81, net_position=-0.2), band_state, config)
    assert d_short.kind == "ONE_SIDED_REDUCE"
    assert d_short.intent_plan[0].leg_role == "bid"

    # net-FLAT (net_position == 0) ⇒ the pinned abstention is preserved (E4-T1 ``two_sided_zone_exit``).
    d_flat, _ = decide(_obs(bid=0.79, ask=0.81, net_position=0.0), band_state, config)
    assert d_flat.kind == "NO_QUOTE"
    assert d_flat.reason_codes == ("two_sided_zone_exit",)
    assert d_flat.intent_plan == ()


def test_reduce_conflict_no_quote() -> None:
    # The reduce_conflict coupling (REQ-083, fail closed): when the inventory-reducing leg is ALSO the
    # leg the E4-T4 guard would PULL as adverse (by residual sign), quoting to reduce would quote INTO
    # the adverse flow ⇒ NO_QUOTE(reduce_conflict) + cancel (no place). net LONG (reduce = ASK) with a
    # POSITIVE residual (fv 0.54 − mid 0.50 − basis 0.0 = +0.04 > residual_band 0.02 ⇒ the guard pulls
    # the ASK) is the conflict: reducing leg == toxic leg.
    config = _config(guard_enabled=True)
    state = _guarded_warm_state(basis_gap=0.0)

    conflict = _obs(guard_fv=_fresh_fv(fv=0.54), net_position=0.6)  # reduce=ask, toxic=ask
    d_conflict, _ = decide(conflict, state, config)
    assert d_conflict.kind == "NO_QUOTE"
    assert d_conflict.reason_codes == ("reduce_conflict",)
    assert d_conflict.intent_plan == ()  # E5-T3 owns the cancel intent — none wired here (no place)

    # CONTROL — SAME long inventory (reduce = ASK) but a NEGATIVE residual (fv 0.46 ⇒ the guard would
    # pull the BID, toxic = bid) is NOT the reducing leg, so there is NO conflict and the reduce
    # proceeds on the ask. This pins that the conflict is DIRECTIONAL (reducing == toxic), not merely
    # "guard active + over-limit".
    no_conflict = _obs(guard_fv=_fresh_fv(fv=0.46), net_position=0.6)  # reduce=ask, toxic=bid
    d_ok, _ = decide(no_conflict, state, config)
    assert d_ok.kind == "ONE_SIDED_REDUCE"
    assert d_ok.reason_codes == ("inventory_reduce",)
    assert d_ok.intent_plan[0].leg_role == "ask"
