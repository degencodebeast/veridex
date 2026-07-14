"""Pure-tier watermark precondition layer (REQ-020(b)/022/030/031/033/034/035).

These tests pin the NO-LOOKAHEAD precondition layer of the REAL ``veridex.mm_strategy.core.decide``
(no re-implementation) — the gate that runs BEFORE any quoting logic. On a frame that passes every
watermark ``decide`` returns ``HOLD`` and threads the advanced state through (E2-T4 replaces that
pass-through with the full reducer); a frame that fails a watermark is HELD with the unchanged prior
state so the state can never double-advance or run cooldown/half-life arithmetic on negative time.

Pinned invariants:

- ``test_stale_sequence_holds_no_double_advance`` (RED-06/AC-012/039) — a sequence at/below the
  watermark (a duplicate is stale) → ``HOLD``/``stale_observation`` with the prior state unchanged.
- ``test_clock_regression_holds`` (RED-36/REQ-022) — ``as_of_ts < last_as_of_ts`` →
  ``HOLD``/``clock_regression`` with the unchanged state.
- ``test_epoch_regression_never_resets`` (RED-37/AC-044) — an epoch below the last-seen value →
  ``HOLD``/``epoch_regression`` with the unchanged state; it is NEVER a reset (accumulators intact).
- ``test_book_epoch_increment_resets_and_rebaselines_sequence`` (AC-040) — a ``book_source_epoch``
  increment fires the full REQ-033 reset and RE-BASELINES the sequence watermark, so a re-based
  (possibly ≤ old-watermark) post-reconnect sequence is accepted, NOT rejected as stale. The epoch
  check is evaluated BEFORE sequence-staleness — this is the load-bearing ordering.
- ``test_fv_epoch_increment_resets_basis_only`` (AC-040/Codex-R5 MAJOR-1) — a guard-leg
  ``fv_source_epoch`` increment resets the basis window ONLY; the FV-independent venue accumulators
  (smoother, rolling references) are byte-identical after the bump.
- ``test_restart_from_snapshot_reproduces_or_fail_closed`` (AC-013/RED-07) — a valid state snapshot
  round-trips and reproduces the uninterrupted decision stream; a missing snapshot (fresh state)
  yields a fail-closed-safe ``HOLD`` (never a spontaneous quote).
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
from veridex.mm_strategy.core import decide


def _config(*, guard_enabled: bool = False, **overrides: object) -> StrategyConfig:
    """A valid :class:`StrategyConfig` with ``guard_enabled`` explicit (it is REQUIRED)."""
    return StrategyConfig(guard_enabled=guard_enabled, **overrides)  # type: ignore[arg-type]


def _guard_fv(*, fv_source_epoch: int, fv_recv_ts: int = 990) -> GuardFairValue:
    """A healthy guard FV leg at ``fv_source_epoch`` (``fv_recv_ts`` kept ≤ ``as_of_ts``)."""
    return GuardFairValue(
        fv=0.5,
        fv_source_ts=1,
        fv_recv_ts=fv_recv_ts,
        fv_source_epoch=fv_source_epoch,
        message_id="msg-1",
        proof_status="proven",
    )


def _obs(
    *,
    observation_sequence: int = 1,
    book_source_epoch: int = 1,
    as_of_ts: int = 1_000,
    guard_fv: GuardFairValue | None = None,
    market_status: str = "ACTIVE",
    market_status_epoch: int | None = 1,
) -> StrategyObservation:
    """A healthy per-tick observation; every ``recv_ts`` is derived ≤ ``as_of_ts`` so construction
    never trips the REQ-022 future-dating guard. Ordering/epoch/clock fields are the knobs."""
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
        bid=0.49,
        ask=0.51,
        bid_size=100.0,
        ask_size=120.0,
        book_status="ok",
        status_reason=None,
        book_recv_ts=recv,
        level_count_in_band=5,
        tick_regime_changed=False,
        phase=1,
        suspended=False,
        match_state_recv_ts=recv,
        guard_fv=guard_fv,
        market_status=market_status,  # type: ignore[arg-type]
        market_status_recv_ts=status_recv,
        market_status_epoch=status_epoch,
        order_stream_ok=True,
        projection_fresh=True,
        inventory=InventoryProjection(
            net_position=0.0, resting=(), projection_as_of_ts=as_of_ts, fresh=True
        ),
        as_of_ts=as_of_ts,
    )


def _seeded_state(
    *,
    last_observation_sequence: int,
    last_book_source_epoch: int,
    last_as_of_ts: int,
    guard_watermark: GuardStateWatermark | None = None,
) -> StrategyState:
    """A mid-stream state carrying a watermark AND populated accumulators, so a reset (or the
    absence of one) is observable on every accumulator field."""
    return StrategyState(
        last_observation_sequence=last_observation_sequence,
        last_book_source_epoch=last_book_source_epoch,
        last_as_of_ts=last_as_of_ts,
        last_market_status_epoch=1,
        last_market_status_recv_ts=last_as_of_ts - 10,
        guard_watermark=guard_watermark,
        smoother_mid=0.5,
        smoother_mid_ts=last_as_of_ts,
        spread_ref_samples=(0.02, 0.02),
        depth_ref_samples=(100.0, 120.0),
        basis_samples=((900, 0.01), (950, 0.011)),
    )


# --- Sequence staleness (REQ-034 / RED-06) -------------------------------------------------


def test_stale_sequence_holds_no_double_advance() -> None:
    # A first frame from a fresh state seeds the watermark at sequence 10.
    _, state = decide(_obs(observation_sequence=10, as_of_ts=1_000), StrategyState(), _config())
    assert state.last_observation_sequence == 10

    # A DUPLICATE sequence (10) is stale, and a lower sequence (9) is stale — both HOLD with the
    # prior state byte-identical: the watermark never double-advances (RED-06).
    for stale_seq in (10, 9):
        decision, next_state = decide(
            _obs(observation_sequence=stale_seq, as_of_ts=1_050), state, _config()
        )
        assert decision.kind == "HOLD"
        assert decision.reason_codes == ("stale_observation",)
        assert next_state == state
        assert next_state.state_hash() == state.state_hash()


# --- Clock regression (REQ-022 / RED-36) ---------------------------------------------------


def test_clock_regression_holds() -> None:
    state = _seeded_state(
        last_observation_sequence=10, last_book_source_epoch=1, last_as_of_ts=1_000
    )
    # as_of_ts regresses below last_as_of_ts even though the sequence advances → HOLD, unchanged.
    decision, next_state = decide(
        _obs(observation_sequence=11, book_source_epoch=1, as_of_ts=999), state, _config()
    )
    assert decision.kind == "HOLD"
    assert decision.reason_codes == ("clock_regression",)
    assert next_state == state


# --- Epoch regression (REQ-033 / RED-37 / AC-044) ------------------------------------------


def test_epoch_regression_never_resets() -> None:
    state = _seeded_state(
        last_observation_sequence=10, last_book_source_epoch=3, last_as_of_ts=1_000
    )
    # book_source_epoch regresses (2 < 3); the sequence and clock advance. It is HELD unchanged and
    # is NEVER treated as a reset — every accumulator survives intact.
    decision, next_state = decide(
        _obs(observation_sequence=11, book_source_epoch=2, as_of_ts=1_100), state, _config()
    )
    assert decision.kind == "HOLD"
    assert decision.reason_codes == ("epoch_regression",)
    assert next_state == state
    assert next_state.smoother_mid == 0.5
    assert next_state.basis_samples == ((900, 0.01), (950, 0.011))


# --- book_source_epoch increment: full reset + sequence re-baseline (AC-040) ----------------


def test_book_epoch_increment_resets_and_rebaselines_sequence() -> None:
    state = _seeded_state(
        last_observation_sequence=100, last_book_source_epoch=1, last_as_of_ts=1_000
    )
    # A reconnect: book_source_epoch increments to 2 and the source sequence RE-BASELINES to 5 —
    # BELOW the old watermark (100). Because the epoch check precedes sequence-staleness, the frame
    # is accepted as the post-reset baseline, NOT rejected as stale (the load-bearing ordering).
    decision, next_state = decide(
        _obs(observation_sequence=5, book_source_epoch=2, as_of_ts=1_100), state, _config()
    )
    assert decision.kind == "HOLD"
    # Watermark re-baselines to the reconnect frame.
    assert next_state.last_book_source_epoch == 2
    assert next_state.last_observation_sequence == 5
    assert next_state.last_as_of_ts == 1_100
    # Full REQ-033 reset: basis window AND venue accumulators are cleared.
    assert next_state.basis_samples == ()
    assert next_state.smoother_mid is None
    assert next_state.smoother_mid_ts is None
    assert next_state.spread_ref_samples == ()
    assert next_state.depth_ref_samples == ()


# --- fv_source_epoch increment: basis-only reset (AC-040 / Codex-R5 MAJOR-1) ----------------


def test_fv_epoch_increment_resets_basis_only() -> None:
    state = _seeded_state(
        last_observation_sequence=100,
        last_book_source_epoch=1,
        last_as_of_ts=1_000,
        guard_watermark=GuardStateWatermark(fv_source_epoch=1),
    )
    # Guard-arm FV reconnect: fv_source_epoch increments (1 → 2) while the book epoch is unchanged
    # and the observation sequence advances normally (101 > 100 — sequence is book-epoch-scoped).
    decision, next_state = decide(
        _obs(
            observation_sequence=101,
            book_source_epoch=1,
            as_of_ts=1_100,
            guard_fv=_guard_fv(fv_source_epoch=2, fv_recv_ts=1_090),
        ),
        state,
        _config(guard_enabled=True),
    )
    assert decision.kind == "HOLD"
    # Basis window reset...
    assert next_state.basis_samples == ()
    # ...but the FV-independent venue accumulators are byte-identical (UNTOUCHED).
    assert next_state.smoother_mid == 0.5
    assert next_state.smoother_mid_ts == 1_000
    assert next_state.spread_ref_samples == (0.02, 0.02)
    assert next_state.depth_ref_samples == (100.0, 120.0)
    # Guard watermark advanced; sequence advanced within the unchanged book epoch.
    assert next_state.guard_watermark == GuardStateWatermark(fv_source_epoch=2)
    assert next_state.last_observation_sequence == 101


# --- Restart / snapshot (REQ-035 / AC-013 / RED-07) ----------------------------------------


def test_restart_from_snapshot_reproduces_or_fail_closed() -> None:
    config = _config()
    # Uninterrupted stream: frame 1 seeds, frame 2 advances.
    _, state1 = decide(_obs(observation_sequence=10, as_of_ts=1_000), StrategyState(), config)
    obs2 = _obs(observation_sequence=11, book_source_epoch=1, as_of_ts=1_001)
    decision2, state2 = decide(obs2, state1, config)

    # A valid snapshot round-trips (serialize → reconstruct) byte-identically...
    reconstructed = StrategyState.model_validate_json(state1.model_dump_json())
    assert reconstructed == state1
    assert reconstructed.state_hash() == state1.state_hash()
    # ...and reproduces the uninterrupted decision stream exactly.
    replayed_decision, replayed_state = decide(obs2, reconstructed, config)
    assert replayed_decision == decision2
    assert replayed_state == state2

    # A MISSING snapshot (fresh state) under the fail_closed default never optimistically quotes —
    # the watermark layer HOLDs and seeds a fresh baseline, so the next frame can be ordered.
    fail_closed = _config(restart_policy="fail_closed")
    decision, seeded = decide(
        _obs(observation_sequence=50, book_source_epoch=3, as_of_ts=2_000), StrategyState(), fail_closed
    )
    assert decision.kind == "HOLD"
    assert seeded.last_observation_sequence == 50
    assert seeded.last_book_source_epoch == 3


# --- Clean frame pass-through (REQ-030 — the E2-T4 reducer seam) ----------------------------


def test_clean_frame_advances_watermark_without_training_accumulators() -> None:
    state = _seeded_state(
        last_observation_sequence=10, last_book_source_epoch=1, last_as_of_ts=1_000
    )
    decision, next_state = decide(
        _obs(observation_sequence=11, book_source_epoch=1, as_of_ts=1_010), state, _config()
    )
    assert decision.kind == "HOLD"
    # Watermark advances...
    assert next_state.last_observation_sequence == 11
    assert next_state.last_as_of_ts == 1_010
    # ...but the watermark layer does NOT train accumulators (that is the E2-T4 reducer's job).
    assert next_state.smoother_mid == 0.5
    assert next_state.spread_ref_samples == (0.02, 0.02)
    assert next_state.basis_samples == ((900, 0.01), (950, 0.011))
