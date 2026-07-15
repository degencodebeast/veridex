"""Universal match-state ``suspended`` gate + suspension→reopen row-R reset (Gate #4 F-CRITICAL-1).

These tests pin the UNIVERSAL (arm-symmetric) handling of the REQ-020(d) match-state ``suspended``
leg in the REAL ``veridex.mm_strategy.core.decide`` (no re-implementation). Fable's whole-lane review
proved the leg was guard-SCOPED, not universal: the baseline (guard-off) arm QUOTED two-sided INTO a
suspended match, and a suspension→reopen reset nothing in either arm — arm-ASYMMETRIC streams that
violate REQ-080 / REQ-020(d) / AC-051.

Pinned invariants (REQ-080 / REQ-020(d) / REQ-033 / REQ-070 row R / AC-051):

- ``test_baseline_arm_no_quote_under_suspension`` — guard-OFF, ``suspended=True`` on a healthy ACTIVE
  book → NO_QUOTE(``txline_suspended``), NEVER a two-sided quote into the suspension; arm-symmetric
  with the guarded arm (both NO_QUOTE on the suspended frame). The universal quote-gate.
- ``test_suspension_reopen_is_row_r_reset_arm_symmetric`` (AC-051) — a suspension→reopen frame is a
  REQ-070 row-R RESET (accumulators cleared, smoother re-seeds from the reopen ok mid, cooldown
  anchored) that is BYTE-IDENTICAL across both arms (match state is a universal leg).
- ``test_baseline_arm_suspension_reset_teeth`` (RED-43-style) — the baseline arm's whole suspension
  handling is FV-INDEPENDENT: byte-identical decisions + next state across healthy / stale / absent FV.
"""

from __future__ import annotations

from veridex.mm_strategy.config import StrategyConfig
from veridex.mm_strategy.contracts import (
    GuardFairValue,
    GuardStateWatermark,
    InventoryProjection,
    StrategyObservation,
    StrategyState,
)
from veridex.mm_strategy.core import _classify_row, decide


def _config(*, guard_enabled: bool = False, **overrides: object) -> StrategyConfig:
    """A valid :class:`StrategyConfig` with ``guard_enabled`` explicit (it is REQUIRED)."""
    return StrategyConfig(guard_enabled=guard_enabled, **overrides)  # type: ignore[arg-type]


def _fresh_fv(*, fv: float = 0.50) -> GuardFairValue:
    """A healthy (transport- and content-fresh) guard FV leg at epoch 1."""
    return GuardFairValue(
        fv=fv,
        fv_source_ts=99,
        fv_recv_ts=99_990,
        fv_source_epoch=1,
        message_id="msg-1",
        proof_status="proven",
    )


def _stale_fv(*, fv: float = 0.55) -> GuardFairValue:
    """A TRANSPORT-stale guard FV leg (``as_of_ts − fv_recv_ts`` = 20 s > ``fv_freshness_ms`` 10 s)."""
    return GuardFairValue(
        fv=fv,
        fv_source_ts=79,
        fv_recv_ts=80_000,
        fv_source_epoch=1,
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
    market_status_epoch: int | None = 1,
    book_status: str = "ok",
    phase: int = 1,
    suspended: bool = False,
    bid: float | None = 0.49,
    ask: float | None = 0.51,
    bid_size: float | None = 100.0,
    ask_size: float | None = 120.0,
    net_position: float = 0.0,
) -> StrategyObservation:
    """A healthy per-tick observation with the ``suspended`` / ``phase`` match-state knobs exposed;
    every ``recv_ts`` is derived ≤ ``as_of_ts`` so construction never trips the REQ-022 guard."""
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
        level_count_in_band=5,
        tick_regime_changed=False,
        phase=phase,
        suspended=suspended,
        match_state_recv_ts=recv,
        guard_fv=guard_fv,
        market_status=market_status,  # type: ignore[arg-type]
        market_status_recv_ts=status_recv,
        market_status_epoch=status_epoch,
        order_stream_ok=True,
        projection_fresh=True,
        inventory=InventoryProjection(
            net_position=net_position, resting=(), projection_as_of_ts=as_of_ts, fresh=True
        ),
        as_of_ts=as_of_ts,
    )


def _warm_state(*, last_suspended: bool | None = None) -> StrategyState:
    """A guard-OFF warm-reference state (smoother seeded + both rolling refs past ``ref_min_samples``)
    so a healthy in-window frame reaches row H and is placement-eligible. ``last_phase=1`` matches the
    ``_obs`` default (no spurious phase reset); ``last_suspended`` is the match-state watermark knob."""
    return StrategyState(
        last_observation_sequence=1,
        last_book_source_epoch=1,
        last_as_of_ts=99_000,
        last_market_status_epoch=1,
        last_market_status_recv_ts=1,
        last_phase=1,
        last_suspended=last_suspended,
        guard_watermark=None,
        smoother_mid=0.5,
        smoother_mid_ts=99_000,
        spread_ref_samples=tuple(0.02 for _ in range(25)),
        depth_ref_samples=tuple(100.0 for _ in range(25)),
    )


def _guarded_warm_state(*, basis_gap: float = 0.0, basis_count: int = 40) -> StrategyState:
    """A warm state whose ``rolling_median`` basis is ALSO warm (``basis_count`` samples == ``basis_gap``)
    with the guard watermark seeded at epoch 1, so a fresh-FV frame reaches the guard block."""
    return StrategyState(
        last_observation_sequence=1,
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
        basis_samples=tuple((900 + i, basis_gap) for i in range(basis_count)),
        basis_sample_count=basis_count,
    )


# --- The universal suspension quote-gate (F-CRITICAL-1 / REQ-080 / REQ-020(d)) --------------


def test_baseline_arm_no_quote_under_suspension() -> None:
    baseline = _config(guard_enabled=False)
    guarded = _config(guard_enabled=True)

    # Non-vacuous control: the SAME healthy ACTIVE book QUOTES two-sided when the match is LIVE.
    d_control, _ = decide(_obs(suspended=False), _warm_state(), baseline)
    assert d_control.kind == "QUOTE_TWO_SIDED"

    # Suspend the match on an otherwise-identical healthy ACTIVE book.
    suspended = _obs(suspended=True)

    # Baseline arm (guard OFF): the UNIVERSAL match-state leg gates quoting — NO_QUOTE(txline_suspended),
    # NEVER a live two-sided quote resting INTO a suspended match (the F-CRITICAL-1 defect).
    d_base, _ = decide(suspended, _warm_state(), baseline)
    assert d_base.kind == "NO_QUOTE"
    assert d_base.reason_codes == ("txline_suspended",)
    assert d_base.kind != "QUOTE_TWO_SIDED"
    assert all(intent.kind != "place_quote" for intent in d_base.intent_plan)

    # Guarded arm (guard ON, basis warm, fresh FV): the SAME suspended frame is ALSO
    # NO_QUOTE(txline_suspended) — the two arms are ARM-SYMMETRIC on the universal leg (REQ-020(d)).
    guarded_suspended = suspended.model_copy(update={"guard_fv": _fresh_fv(fv=0.50)})
    d_guard, _ = decide(guarded_suspended, _guarded_warm_state(basis_gap=0.0), guarded)
    assert d_guard.kind == "NO_QUOTE"
    assert d_guard.reason_codes == ("txline_suspended",)

    # Arm-symmetric decision on the suspended frame (kind + reason identical across arms).
    assert (d_base.kind, d_base.reason_codes) == (d_guard.kind, d_guard.reason_codes)


# --- suspension→reopen is a row-R RESET, byte-identical across arms (AC-051) -----------------


def test_suspension_reopen_is_row_r_reset_arm_symmetric() -> None:
    # A match that WAS suspended (state.last_suspended=True) REOPENS (suspended=False) on a healthy ok
    # book: suspension→reopen is a REQ-070 row-R RESET (REQ-033/080), driven by the universal leg.
    seed = _warm_state(last_suspended=True)
    # FV absent on the reopen frame so the guarded-arm state carries NO FV element — the reset is then
    # byte-identical to the guard-off arm (venue accumulators + cooldown are FV-independent).
    reopen = _obs(suspended=False, guard_fv=None, observation_sequence=2, as_of_ts=100_000)

    # It classifies as row R in BOTH arms (match-state is universal — never the guard leg).
    assert _classify_row(reopen, seed, _config(guard_enabled=False)) == "R"
    assert _classify_row(reopen, seed, _config(guard_enabled=True)) == "R"

    d_base, s_base = decide(reopen, seed, _config(guard_enabled=False))
    d_guard, s_guard = decide(reopen, seed, _config(guard_enabled=True))

    dwell = _config().book_state_dwell_before_quote_ms
    for decision, state in ((d_base, s_base), (d_guard, s_guard)):
        # Row-R reset shape: NO_QUOTE with the reset/warmup reason.
        assert decision.kind == "NO_QUOTE"
        assert decision.reason_codes == ("event_ref_warmup",)
        # Accumulators cleared; the smoother RE-SEEDS from THIS reopen frame's own ok mid (0.50).
        assert state.spread_ref_samples == ()
        assert state.depth_ref_samples == ()
        assert state.basis_samples == ()
        assert state.basis_sample_count == 0
        assert state.smoother_mid == 0.50
        # A cooldown is anchored at the reopen frame (as_of_ts + dwell).
        assert state.event_cooldown_until_ts == reopen.as_of_ts + dwell
        # The suspension watermark re-baselines to the reopen frame's own value (no re-trigger next).
        assert state.last_suspended is False

    # ARM-SYMMETRIC (AC-051): the RESET is byte-identical across arms — the next-state venue
    # accumulators / basis / cooldown / suspension watermark carry no guard element (FV absent), so
    # ``state_hash`` matches exactly and the reset never depends on the guard leg. The decision's KIND
    # / reason_codes / intent_plan match too; only the config-derived provenance (``config_hash`` and
    # thus ``decision_id``) legitimately differs, since the two arms ARE two distinct configs.
    assert s_base.state_hash() == s_guard.state_hash()
    assert (d_base.kind, d_base.reason_codes, d_base.intent_plan) == (
        d_guard.kind,
        d_guard.reason_codes,
        d_guard.intent_plan,
    )
    assert d_base.config_hash != d_guard.config_hash  # arm identity IS exactly the config diff


# --- The baseline arm's suspension handling is FV-independent (RED-43-style; AC-049/051) -----


def test_baseline_arm_suspension_reset_teeth() -> None:
    baseline = _config(guard_enabled=False)

    def run(make_fv: object) -> tuple[object, object, StrategyState]:
        # Two-frame suspension tape: a suspended frame (quote-gated) then a reopen frame (row-R reset).
        seed = _warm_state(last_suspended=False)
        susp = _obs(
            suspended=True, observation_sequence=2, as_of_ts=100_000, guard_fv=make_fv()  # type: ignore[operator]
        )
        d1, s1 = decide(susp, seed, baseline)
        reopen = _obs(
            suspended=False, observation_sequence=3, as_of_ts=100_100, guard_fv=make_fv()  # type: ignore[operator]
        )
        d2, s2 = decide(reopen, s1, baseline)
        return (d1, d2, s2)

    healthy = run(lambda: _fresh_fv(fv=0.50))
    stale = run(lambda: _stale_fv(fv=0.55))
    absent = run(lambda: None)

    # The suspended frame is the universal NO_QUOTE gate and the reopen frame is the row-R reset —
    # in the BASELINE arm, both are FV-INDEPENDENT (the guard FV leg can never touch arm A).
    assert healthy[0].kind == "NO_QUOTE"
    assert healthy[0].reason_codes == ("txline_suspended",)
    assert healthy[1].kind == "NO_QUOTE"
    assert healthy[1].reason_codes == ("event_ref_warmup",)

    # Decision kind + reason and the FULL next state are byte-identical across FV health — the state
    # carries no FV element in guard-off, so a stale/absent FV can never perturb the baseline stream.
    for variant in (stale, absent):
        assert (variant[0].kind, variant[0].reason_codes) == (
            healthy[0].kind,
            healthy[0].reason_codes,
        )
        assert (variant[1].kind, variant[1].reason_codes) == (
            healthy[1].kind,
            healthy[1].reason_codes,
        )
        assert variant[2].state_hash() == healthy[2].state_hash()
