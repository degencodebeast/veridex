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

import pytest

from veridex.mm_strategy.config import StrategyConfig
from veridex.mm_strategy.contracts import (
    GuardFairValue,
    GuardStateWatermark,
    InventoryProjection,
    StrategyDecision,
    StrategyObservation,
    StrategyState,
)
from veridex.mm_strategy.core import _classify_row, decide


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
    book_status: str = "ok",
    tick_regime_changed: bool = False,
    level_count_in_band: int = 5,
    book_recv_ts: int | None = None,
    match_state_recv_ts: int | None = None,
    order_stream_ok: bool = True,
    projection_fresh: bool = True,
    bid: float | None = 0.49,
    ask: float | None = 0.51,
    bid_size: float | None = 100.0,
    ask_size: float | None = 120.0,
    net_position: float = 0.0,
) -> StrategyObservation:
    """A healthy per-tick observation; every ``recv_ts`` is derived ≤ ``as_of_ts`` so construction
    never trips the REQ-022 future-dating guard. Ordering/epoch/clock fields are the knobs, plus the
    reducer-classification knobs (E2-T4): ``book_status`` / ``tick_regime_changed`` / freshness +
    skew clocks / stream flags / raw top-of-book so each S/R/E/D/C/F/W/H row is constructible."""
    recv = as_of_ts - 10
    book_recv = recv if book_recv_ts is None else book_recv_ts
    match_recv = recv if match_state_recv_ts is None else match_state_recv_ts
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
        book_recv_ts=book_recv,
        level_count_in_band=level_count_in_band,
        tick_regime_changed=tick_regime_changed,
        phase=1,
        suspended=False,
        match_state_recv_ts=match_recv,
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
    # A reconnect is a REQ-033/row-R RESET: NO_QUOTE with the reset reason (Codex Gate#1 MAJOR-2 —
    # the pre-fix path returned a bare HOLD that cleared the smoother to None and anchored NO
    # cooldown, so quoting could resume mid-dwell; corrected here to the row-R transition).
    assert decision.kind == "NO_QUOTE"
    assert decision.reason_codes == ("tick_regime_changed",)
    # Watermark re-baselines to the reconnect frame.
    assert next_state.last_book_source_epoch == 2
    assert next_state.last_observation_sequence == 5
    assert next_state.last_as_of_ts == 1_100
    # Full REQ-033 reset: basis window AND rolling refs are cleared; the smoother RE-SEEDS from this
    # frame's own ok-book mid (0.5) and an event cooldown is anchored at its ``as_of_ts`` (row R).
    assert next_state.basis_samples == ()
    assert next_state.smoother_mid == 0.5
    assert next_state.smoother_mid_ts == 1_100
    assert next_state.spread_ref_samples == ()
    assert next_state.depth_ref_samples == ()
    assert next_state.event_cooldown_until_ts == 1_100 + 5_000


def test_book_epoch_increment_anchors_cooldown_no_early_quote() -> None:
    # Codex Gate#1 MAJOR-2: a ``book_source_epoch`` INCREMENT (reconnect) is a row-R RESET — it must
    # re-seed the smoother from THIS frame's own ok-book mid, ANCHOR an event cooldown at its
    # ``as_of_ts``, and produce NO_QUOTE (row R) — never a bare HOLD that lets quoting resume with
    # dwell still owed. A tiny ``ref_min_samples`` makes the pre-fix "~2 ms after reset it quoted"
    # bug reachable (REQ-036/070 row R/081).
    config = _config(ref_min_samples=1, book_state_dwell_before_quote_ms=5_000)
    dwell = config.book_state_dwell_before_quote_ms
    state = _seeded_state(
        last_observation_sequence=100, last_book_source_epoch=1, last_as_of_ts=1_000
    )

    # The epoch-increment frame: NO_QUOTE, cooldown anchored at ``as_of_ts``, smoother re-seeded to
    # this frame's ok-book mid (0.5) — the full REQ-033 epoch/sequence re-baseline still occurs.
    reset_obs = _obs(observation_sequence=5, book_source_epoch=2, as_of_ts=2_000)
    reset_decision, state = decide(reset_obs, state, config)
    assert reset_decision.kind == "NO_QUOTE"
    assert state.last_book_source_epoch == 2
    assert state.last_observation_sequence == 5
    assert state.event_cooldown_until_ts == 2_000 + dwell
    assert state.smoother_mid == 0.5  # re-seeded from this frame's ok mid, never cleared to None
    assert state.spread_ref_samples == ()  # rolling refs cleared by the reset

    # Every frame inside the dwell window is row C (cooldown) — NONE may place, even with
    # ``ref_min_samples=1`` (the pre-fix path resumed ``QUOTE_TWO_SIDED`` ~2 ms after the reset).
    for offset in (1, 2, 3):
        obs = _obs(observation_sequence=5 + offset, book_source_epoch=2, as_of_ts=2_000 + offset)
        assert _classify_row(obs, state, config) == "C"
        decision, state = decide(obs, state, config)
        assert decision.kind == "NO_QUOTE"
        assert decision.reason_codes == ("event_cooldown",)


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
    # E2-T4 row F (fv-epoch increment; frame otherwise W/H): the guard is inert until the basis
    # re-warms, so the disposition is NO_QUOTE(basis_warmup).
    assert decision.kind == "NO_QUOTE"
    assert decision.reason_codes == ("basis_warmup",)
    # Basis window CLEARED-then-ADMIT (Codex R6 MAJOR-1.2): exactly the current valid FV sample is
    # re-admitted as the first sample (raw_gap = fv 0.5 − mid 0.5 = 0.0 at this frame's as_of_ts).
    assert next_state.basis_samples == ((1_100, 0.0),)
    # The FV-INDEPENDENT venue accumulators are NOT cleared by the fv-epoch reset (Codex-R5 MAJOR-1):
    # the prior samples survive as a prefix AND this healthy frame ADMITS per the underlying W/H row
    # (row F trains the venue accumulators), so the spread/depth series grow rather than reset.
    assert next_state.spread_ref_samples[:2] == (0.02, 0.02)
    assert next_state.spread_ref_samples == pytest.approx((0.02, 0.02, 0.02))
    assert next_state.depth_ref_samples == (100.0, 120.0, 100.0)
    assert next_state.smoother_mid == 0.5  # ema of 0.5 toward mid 0.5 stays 0.5
    assert next_state.smoother_mid_ts == 1_100  # admission advances the smoother clock
    # Guard watermark advanced; sequence advanced within the unchanged book epoch.
    assert next_state.guard_watermark == GuardStateWatermark(fv_source_epoch=2)
    assert next_state.last_observation_sequence == 101


# --- Guard FV-epoch watermark preservation (Codex Gate#1 MAJOR-1 / REQ-031/033) -------------


def test_fv_absent_frame_preserves_epoch_then_older_fv_regresses() -> None:
    # Codex Gate#1 MAJOR-1: while the guard is ENABLED, an accepted frame that merely LACKS an FV
    # leg (``guard_fv=None``) must PRESERVE the prior guard FV-epoch watermark — never erase it — so
    # a later frame carrying an OLDER FV generation is still caught by ``epoch_regression``.
    config = _config(guard_enabled=True)
    state = _warm_state(guard_watermark=GuardStateWatermark(fv_source_epoch=5))

    # (1) An accepted, FV-absent frame while the guard is on carries the watermark forward UNCHANGED
    # (the prior epoch-5 watermark survives; only a guard-OFF projection drops FV state).
    fv_absent = _obs(observation_sequence=101, as_of_ts=1_010, guard_fv=None)
    _, after_absent = decide(fv_absent, state, config)
    assert after_absent.guard_watermark == GuardStateWatermark(fv_source_epoch=5)

    # (2) A guarded frame at an OLDER FV epoch (4 < 5) is now HELD for ``epoch_regression`` with the
    # state unchanged — only reachable because the FV-absent frame preserved the epoch-5 watermark.
    older_fv = _obs(
        observation_sequence=102,
        as_of_ts=1_020,
        guard_fv=_guard_fv(fv_source_epoch=4, fv_recv_ts=1_010),
    )
    decision, after_older = decide(older_fv, after_absent, config)
    assert decision.kind == "HOLD"
    assert decision.reason_codes == ("epoch_regression",)
    assert after_older == after_absent


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


# --- Clean healthy frame → the E2-T4 reducer (row W admission) ------------------------------


def test_clean_healthy_warmup_frame_admits_venue_accumulators() -> None:
    # E2-T4 fills the E2-T3 seam: a clean, healthy, non-trigger frame whose rolling references are
    # still below ``ref_min_samples`` (here 2 < 20) is row W (WARMUP) — it ADMITS (trains the venue
    # accumulators, liveness) yet withholds the quote with ``event_ref_warmup``.
    state = _seeded_state(
        last_observation_sequence=10, last_book_source_epoch=1, last_as_of_ts=1_000
    )
    decision, next_state = decide(
        _obs(observation_sequence=11, book_source_epoch=1, as_of_ts=1_010), state, _config()
    )
    assert _classify_row(
        _obs(observation_sequence=11, book_source_epoch=1, as_of_ts=1_010), state, _config()
    ) == "W"
    assert decision.kind == "NO_QUOTE"
    assert decision.reason_codes == ("event_ref_warmup",)
    # Watermark advances...
    assert next_state.last_observation_sequence == 11
    assert next_state.last_as_of_ts == 1_010
    # ...and the venue accumulators now TRAIN (row W admits): the raw spread (0.02) and top depth
    # (min(100, 120) = 100) append, and the smoother folds mid 0.5 toward mid 0.5 (unchanged value,
    # advanced clock).
    assert next_state.spread_ref_samples == pytest.approx((0.02, 0.02, 0.02))
    assert next_state.depth_ref_samples == (100.0, 120.0, 100.0)
    assert next_state.smoother_mid == 0.5
    assert next_state.smoother_mid_ts == 1_010
    # Baseline arm (guard off) never trains the basis window.
    assert next_state.basis_samples == ((900, 0.01), (950, 0.011))
    # WARMUP never anchors a cooldown (liveness — E2-T5).
    assert next_state.event_cooldown_until_ts is None


# --- E2-T4: the total S/R/E/D/C/F/W/H transition reducer (REQ-070/081/AC-045/050/052/057) -----


def _warm_state(
    *,
    last_observation_sequence: int = 100,
    last_book_source_epoch: int = 1,
    last_as_of_ts: int = 1_000,
    guard_watermark: GuardStateWatermark | None = None,
    event_cooldown_until_ts: int | None = None,
    ref_samples: int = 24,
) -> StrategyState:
    """A mid-stream state carrying a seeded smoother + ``ref_samples`` rolling reference samples
    (≥ ``ref_min_samples`` default 20 ⇒ references WARM) and a basis window. ``ref_samples`` and the
    optional ``event_cooldown_until_ts`` are the knobs that separate rows W/H/C."""
    return StrategyState(
        last_observation_sequence=last_observation_sequence,
        last_book_source_epoch=last_book_source_epoch,
        last_as_of_ts=last_as_of_ts,
        last_market_status_epoch=1,
        last_market_status_recv_ts=last_as_of_ts - 10,
        guard_watermark=guard_watermark,
        event_cooldown_until_ts=event_cooldown_until_ts,
        smoother_mid=0.5,
        smoother_mid_ts=last_as_of_ts,
        spread_ref_samples=tuple(0.02 for _ in range(ref_samples)),
        depth_ref_samples=tuple(100.0 for _ in range(ref_samples)),
        basis_samples=((900, 0.01), (950, 0.011)),
    )


# One case per spec REQ-070 row (glossary VERBATIM, :143-152). ``classify`` is the label
# ``core._classify_row`` must return for the ACCEPTED-frame rows R/E/D/C/F/W/H; row S is rejected
# UPSTREAM at the watermark layer (HOLD), so its classify label is None and only ``decide`` is
# asserted. ``dwell`` is ``config.book_state_dwell_before_quote_ms`` default (5_000).
def _row_cases() -> list[tuple[str, StrategyObservation, StrategyState, StrategyConfig, str | None, str, tuple[str, ...]]]:
    guarded = _config(guard_enabled=True)
    baseline = _config()
    return [
        # S — STALE: clock regression is rejected at the watermark layer → HOLD (row S).
        (
            "S",
            _obs(observation_sequence=11, as_of_ts=999),
            _seeded_state(
                last_observation_sequence=10, last_book_source_epoch=1, last_as_of_ts=1_000
            ),
            baseline,
            None,
            "HOLD",
            ("clock_regression",),
        ),
        # R — RESET-class: a ``tick_regime_changed`` frame (self-contained reset signal).
        (
            "R",
            _obs(observation_sequence=101, as_of_ts=2_000, tick_regime_changed=True),
            _warm_state(),
            baseline,
            "R",
            "NO_QUOTE",
            ("tick_regime_changed",),
        ),
        # E — EVENT-TRIGGER: a newly-entered ``gap`` book (RED-53 canonical object; AC-057:342).
        (
            "E",
            _obs(
                observation_sequence=101,
                as_of_ts=2_000,
                book_status="gap",
                bid=None,
                ask=None,
                bid_size=None,
                ask_size=None,
            ),
            _warm_state(),
            baseline,
            "E",
            "NO_QUOTE",
            ("book_gap",),
        ),
        # D — DATA-DEGRADED: book read too old (as_of − book_recv = 10_000 > book_freshness_ms 5_000).
        (
            "D",
            _obs(
                observation_sequence=101,
                as_of_ts=20_000,
                book_recv_ts=10_000,
                match_state_recv_ts=10_000,
            ),
            _warm_state(last_as_of_ts=15_000),
            baseline,
            "D",
            "NO_QUOTE",
            ("book_stale",),
        ),
        # C — COOLDOWN-active: prior-state cooldown deadline not yet elapsed (as_of 50_000 < 60_000).
        (
            "C",
            _obs(observation_sequence=101, as_of_ts=50_000),
            _warm_state(last_as_of_ts=49_000, event_cooldown_until_ts=60_000),
            baseline,
            "C",
            "NO_QUOTE",
            ("event_cooldown",),
        ),
        # F — FV-EPOCH increment (guarded arm): basis cleared-then-admit, guard inert (basis_warmup).
        (
            "F",
            _obs(
                observation_sequence=101,
                as_of_ts=1_100,
                guard_fv=_guard_fv(fv_source_epoch=2, fv_recv_ts=1_090),
            ),
            _warm_state(guard_watermark=GuardStateWatermark(fv_source_epoch=1)),
            guarded,
            "F",
            "NO_QUOTE",
            ("basis_warmup",),
        ),
        # W — WARMUP: healthy non-trigger frame with references below ref_min_samples (2 < 20).
        (
            "W",
            _obs(observation_sequence=101, as_of_ts=1_010),
            _warm_state(ref_samples=2),
            baseline,
            "W",
            "NO_QUOTE",
            ("event_ref_warmup",),
        ),
        # H — HEALTHY: warm references, ACTIVE status, guard off → quote-eligible disposition.
        (
            "H",
            _obs(observation_sequence=101, as_of_ts=1_010),
            _warm_state(),
            baseline,
            "H",
            "QUOTE_TWO_SIDED",
            (),
        ),
    ]


@pytest.mark.parametrize(
    "label, observation, state, config, classify, kind, reason",
    _row_cases(),
    ids=[case[0] for case in _row_cases()],
)
def test_every_frame_class_matches_exactly_one_row(
    label: str,
    observation: StrategyObservation,
    state: StrategyState,
    config: StrategyConfig,
    classify: str | None,
    kind: str,
    reason: tuple[str, ...],
) -> None:
    # Each constructed observation matches EXACTLY ONE spec row → one (kind, reason) transition
    # (AC-057/RED-53). For the seven accepted-frame rows the pure classifier returns the row's label;
    # row S is rejected upstream (HOLD), so only its decision is asserted.
    if classify is not None:
        assert _classify_row(observation, state, config) == classify, (
            f"row {label} must classify as {classify!r}"
        )
    decision, _ = decide(observation, state, config)
    assert decision.kind == kind, f"row {label}: kind"
    assert decision.reason_codes == reason, f"row {label}: reason"


def test_event_trigger_frame_updates_nothing_except_row_R_reseed() -> None:
    # AC-050/052: a row-E event frame and a row-C cooldown frame ADMIT NOTHING — every venue
    # accumulator is byte-identical to the prior state; only a row-R RESET re-seeds the smoother.
    warm = _warm_state()

    e_obs = _obs(
        observation_sequence=101,
        as_of_ts=2_000,
        book_status="gap",
        bid=None,
        ask=None,
        bid_size=None,
        ask_size=None,
    )
    assert _classify_row(e_obs, warm, _config()) == "E"
    _, e_state = decide(e_obs, warm, _config())
    assert e_state.smoother_mid == warm.smoother_mid
    assert e_state.smoother_mid_ts == warm.smoother_mid_ts
    assert e_state.spread_ref_samples == warm.spread_ref_samples
    assert e_state.depth_ref_samples == warm.depth_ref_samples
    assert e_state.basis_samples == warm.basis_samples
    # ...the watermark still advances and the event anchors a cooldown at this frame.
    assert e_state.last_observation_sequence == 101
    assert e_state.event_cooldown_until_ts == 2_000 + 5_000

    cd = _warm_state(event_cooldown_until_ts=9_000)
    c_obs = _obs(observation_sequence=101, as_of_ts=2_000)
    assert _classify_row(c_obs, cd, _config()) == "C"
    _, c_state = decide(c_obs, cd, _config())
    assert c_state.smoother_mid == cd.smoother_mid
    assert c_state.spread_ref_samples == cd.spread_ref_samples
    assert c_state.depth_ref_samples == cd.depth_ref_samples
    assert c_state.basis_samples == cd.basis_samples
    assert c_state.event_cooldown_until_ts == 9_000  # C never re-anchors the cooldown

    # Row R is the ONLY re-seed row: the smoother RE-SEEDS from THIS frame's ok-book mid
    # ((0.30 + 0.40) / 2 = 0.35) and the rolling refs + basis window are CLEARED.
    r_obs = _obs(
        observation_sequence=101,
        as_of_ts=2_000,
        tick_regime_changed=True,
        bid=0.30,
        ask=0.40,
    )
    assert _classify_row(r_obs, warm, _config()) == "R"
    _, r_state = decide(r_obs, warm, _config())
    assert r_state.smoother_mid == pytest.approx(0.35)
    assert r_state.smoother_mid_ts == 2_000
    assert r_state.spread_ref_samples == ()
    assert r_state.depth_ref_samples == ()
    assert r_state.basis_samples == ()
    assert r_state.event_cooldown_until_ts == 2_000 + 5_000


def test_row_R_preempts_req080_book_trigger() -> None:
    # A frame that is BOTH a reset (``tick_regime_changed``) AND a REQ-080/row-E book trigger
    # (``book_status == "gap"``): rows evaluate IN ORDER, so row R PRE-EMPTS row E — exactly one row,
    # one transition (row R wins). It must NOT ALSO run the row-E cancel-cooldown path twice.
    warm = _warm_state()
    obs = _obs(
        observation_sequence=101,
        as_of_ts=2_000,
        tick_regime_changed=True,
        book_status="gap",
        bid=None,
        ask=None,
        bid_size=None,
        ask_size=None,
    )
    assert _classify_row(obs, warm, _config()) == "R"
    decision, next_state = decide(obs, warm, _config())
    # The row-R reset reason, NOT the row-E book trigger reason.
    assert decision.kind == "NO_QUOTE"
    assert decision.reason_codes == ("tick_regime_changed",)
    assert "book_gap" not in decision.reason_codes
    # ONLY the row-R transition: refs+basis cleared, cooldown anchored once. The book is NOT ``ok``
    # (it is gap), so the smoother RE-SEED is withheld — cleared to None, never seeded from a bad book.
    assert next_state.smoother_mid is None
    assert next_state.spread_ref_samples == ()
    assert next_state.basis_samples == ()
    assert next_state.event_cooldown_until_ts == 2_000 + 5_000


def test_status_unknown_blocks_quote_not_admission() -> None:
    # Status is a QUOTE-ONLY blocker in every row (never an admission or cooldown gate): a healthy,
    # warm frame under UNKNOWN status classifies row H, the quote is blocked (market_status_unknown),
    # yet the venue accumulators STILL TRAIN — one exact post-recovery accumulator state (REQ-070).
    warm = _warm_state()
    obs = _obs(observation_sequence=101, as_of_ts=1_010, market_status="UNKNOWN")
    assert _classify_row(obs, warm, _config()) == "H"
    decision, next_state = decide(obs, warm, _config())
    assert decision.kind == "NO_QUOTE"
    assert decision.reason_codes == ("market_status_unknown",)
    # Admission is NOT gated by status: the spread/depth series each grew by one admitted sample and
    # the smoother clock advanced.
    assert len(next_state.spread_ref_samples) == len(warm.spread_ref_samples) + 1
    assert len(next_state.depth_ref_samples) == len(warm.depth_ref_samples) + 1
    assert next_state.smoother_mid_ts == 1_010


# --- E2-T5: WARMUP liveness + status quote-only recovery (REQ-070/AC-055/RED-39/RED-51) --------


def test_warmup_reaches_ref_min_samples_and_resumes() -> None:
    # RED-51 (LIVENESS): after a reset zeroes the rolling references and anchors a BOUNDED cooldown,
    # a stream of healthy frames drives the reference counts back up to ``ref_min_samples`` and
    # quoting RESUMES. WARMUP (row W) admits every frame and NEVER re-anchors a cooldown, so the
    # strategy can never deadlock in a perpetual warmup/cooldown re-trigger loop. A small
    # ``ref_min_samples`` keeps the proof bounded (the property is independent of the floor's value).
    config = _config(ref_min_samples=3)
    dwell = config.book_state_dwell_before_quote_ms  # 5_000
    reset_ts = 2_000

    # A tick-regime RESET (row R): references cleared, smoother re-seeded from this ok-book mid,
    # cooldown anchored at the reset frame — NO_QUOTE.
    reset_decision, state = decide(
        _obs(observation_sequence=101, as_of_ts=reset_ts, tick_regime_changed=True),
        _warm_state(last_observation_sequence=100, last_as_of_ts=1_000),
        config,
    )
    assert reset_decision.kind == "NO_QUOTE"
    assert reset_decision.reason_codes == ("tick_regime_changed",)
    assert state.spread_ref_samples == ()
    assert state.depth_ref_samples == ()
    assert state.smoother_mid == 0.5  # re-seeded from the ok book, so references can re-warm
    assert state.event_cooldown_until_ts == reset_ts + dwell

    # A frame DURING the cooldown is row C — it holds and admits nothing, but the cooldown is a
    # bounded deadline (never re-anchored by a passive frame), so it MUST elapse (no deadlock).
    seq = 102
    during = _obs(observation_sequence=seq, as_of_ts=reset_ts + dwell - 1)
    assert _classify_row(during, state, config) == "C"
    _, state = decide(during, state, config)
    assert state.spread_ref_samples == ()  # cooldown admits nothing
    assert state.event_cooldown_until_ts == reset_ts + dwell  # C never re-anchors

    # Post-cooldown healthy frames now ADMIT and climb toward the floor. Feed a BOUNDED stream and
    # assert quoting resumes exactly once the references warm — the first frame sits at the cooldown
    # deadline itself (``as_of_ts < until`` is strict, so the deadline frame already admits).
    resumed_at: int | None = None
    as_of = reset_ts + dwell - 1
    for step in range(config.ref_min_samples + 5):
        seq += 1
        as_of += 1
        decision, state = decide(_obs(observation_sequence=seq, as_of_ts=as_of), state, config)
        # WARMUP admits every frame and NEVER anchors a cooldown — the liveness guarantee.
        assert state.event_cooldown_until_ts is None
        if decision.kind == "QUOTE_TWO_SIDED":
            resumed_at = step
            break
        assert decision.reason_codes == ("event_ref_warmup",)

    assert resumed_at is not None, "quoting must resume once references warm — no warmup deadlock"
    # The references reached the floor: at least ``ref_min_samples`` admitted warmup frames stand.
    assert len(state.spread_ref_samples) >= config.ref_min_samples
    assert len(state.depth_ref_samples) >= config.ref_min_samples


def test_seed_and_1ms_post_seed_do_not_place() -> None:
    # RED-39: a smoother RE-SEED (row R reset) never quotes on the seed frame itself — it seeds the
    # smoother into the NEXT state ONLY and anchors a cooldown at the reset ``as_of_ts`` — and a
    # frame just 1ms later still sits INSIDE that cooldown (row C), so neither the seed frame nor its
    # immediate successor places an order.
    config = _config()
    dwell = config.book_state_dwell_before_quote_ms
    seed_ts = 2_000
    warm = _warm_state()

    # The seed frame is a tick-regime reset over an ok book: it RE-SEEDS the smoother from this
    # frame's mid ((0.30 + 0.40) / 2 = 0.35) into the next state and anchors the cooldown — no quote.
    seed_obs = _obs(
        observation_sequence=101, as_of_ts=seed_ts, tick_regime_changed=True, bid=0.30, ask=0.40
    )
    assert _classify_row(seed_obs, warm, config) == "R"
    seed_decision, seeded = decide(seed_obs, warm, config)
    assert seed_decision.kind == "NO_QUOTE"
    assert seed_decision.reason_codes == ("tick_regime_changed",)
    # The re-seed lands in the NEXT state ONLY — ``decide`` is pure, so the prior state is untouched.
    assert warm.smoother_mid == 0.5
    assert seeded.smoother_mid == pytest.approx(0.35)
    assert seeded.smoother_mid_ts == seed_ts
    assert seeded.event_cooldown_until_ts == seed_ts + dwell

    # A frame 1ms after the seed is still INSIDE the cooldown (row C): it does NOT place, it trains
    # no accumulator, and it does NOT re-anchor the cooldown.
    post_obs = _obs(observation_sequence=102, as_of_ts=seed_ts + 1)
    assert _classify_row(post_obs, seeded, config) == "C"
    post_decision, post_state = decide(post_obs, seeded, config)
    assert post_decision.kind == "NO_QUOTE"
    assert post_decision.reason_codes == ("event_cooldown",)
    assert post_state.smoother_mid == pytest.approx(0.35)  # seed untouched — no admission
    assert post_state.spread_ref_samples == ()
    assert post_state.event_cooldown_until_ts == seed_ts + dwell  # C never re-anchors


def test_unknown_status_healthy_book_one_recovery_state() -> None:
    # AC-055: a venue-healthy STRETCH under UNKNOWN market status keeps TRAINING the venue
    # accumulators (status is a QUOTE-ONLY blocker, never an admission or cooldown gate), so the
    # references never fall out of warm — and the FIRST ACTIVE recovery frame yields EXACTLY ONE
    # quote-eligible post-recovery state that is deterministically reproducible.
    config = _config()
    warm = _warm_state(last_observation_sequence=100, last_as_of_ts=1_000)

    def run() -> tuple[StrategyState, StrategyDecision]:
        state = warm
        seq, as_of = 100, 1_000
        # A stretch of healthy frames under UNKNOWN status: each is row H, quote-BLOCKED yet
        # ADMITTING — no cooldown is ever anchored and the references stay warm.
        for _ in range(5):
            seq += 1
            as_of += 1
            obs = _obs(observation_sequence=seq, as_of_ts=as_of, market_status="UNKNOWN")
            assert _classify_row(obs, state, config) == "H"
            decision, state = decide(obs, state, config)
            assert decision.kind == "NO_QUOTE"
            assert decision.reason_codes == ("market_status_unknown",)
            assert state.event_cooldown_until_ts is None
        # Status RECOVERS to ACTIVE: the very next frame quotes (the references never left warm).
        seq += 1
        as_of += 1
        recovery = _obs(observation_sequence=seq, as_of_ts=as_of, market_status="ACTIVE")
        assert _classify_row(recovery, state, config) == "H"
        rec_decision, rec_state = decide(recovery, state, config)
        return rec_state, rec_decision

    rec_state, rec_decision = run()
    assert rec_decision.kind == "QUOTE_TWO_SIDED"
    assert rec_decision.reason_codes == ()
    # The UNKNOWN stretch trained 5 samples + the recovery frame = 6 admitted samples on top of the
    # 24 warm-state samples — status never gated admission.
    assert len(rec_state.spread_ref_samples) == 24 + 6
    assert len(rec_state.depth_ref_samples) == 24 + 6

    # EXACTLY ONE post-recovery state: the whole UNKNOWN→ACTIVE sequence is deterministic — a re-run
    # yields a byte-identical recovered state (same ``state_hash``).
    rec_state2, _ = run()
    assert rec_state2 == rec_state
    assert rec_state2.state_hash() == rec_state.state_hash()
