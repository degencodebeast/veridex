"""Gap-episode END is a REQ-070 row-R RESET (Gate #4 F-IMPORTANT-1).

These tests pin the END of a book ``gap|excluded`` episode as a RESET-class (row R) frame in the REAL
``veridex.mm_strategy.core.decide`` (no re-implementation). Fable's whole-lane review proved the
episode END was NOT a reset: after a ``gap|excluded`` outage the pre-gap smoother, rolling references
and — in the guarded arm — the pre-gap BASIS survived the outage and governed the post-gap decision
with NO re-warmup (``basis_count`` still ≥ ``basis_min_samples`` ⇒ the residual guard acted IMMEDIATELY
on a basis estimated under the pre-disruption regime). RED-53 covers episode ENTRY (row E) only; the
suite never saw the END.

Pinned invariants (REQ-033 / REQ-070 row R / AC-050 / AC-057):

- ``test_gap_episode_end_is_row_r_reset`` — the FIRST ``ok`` frame AFTER a ``gap|excluded`` episode
  (state.last_book_in_gap=True) classifies as row R: venue accumulators CLEARED, smoother RE-SEEDS
  from THIS frame's own ok mid, basis CLEARED, NO_QUOTE(``event_ref_warmup``) + cooldown anchored —
  IDENTICAL across both arms (the book-status watermark is FV-independent), and the residual guard
  never acts on the pre-gap basis.
- ``test_gap_end_accumulator_survival_teeth`` (RED-53-sibling teeth) — over a warm→gap→ok tape the
  pre-gap basis / smoother / rolling refs do NOT survive into the post-gap decision (fails if they do).
- ``test_gap_end_totality_ongoing_vs_end`` (AC-057) — totality is preserved: an ONGOING gap frame
  stays row E while the gap-END ok frame is row R; the two never collide (mutually exclusive by the
  current frame's ``book_status``).
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
    """A per-tick observation with the ``book_status`` knob exposed; every ``recv_ts`` is derived
    ≤ ``as_of_ts`` so construction never trips the REQ-022 future-dating guard."""
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


def _warm_state(*, last_book_in_gap: bool | None = None) -> StrategyState:
    """A guard-OFF warm-reference state (smoother seeded + both rolling refs past ``ref_min_samples``)
    so a healthy in-window frame reaches row H. ``last_phase=1`` matches the ``_obs`` default (no
    spurious phase reset); ``last_book_in_gap`` is the book-status watermark knob (was-the-prior-frame-
    in-a-``gap|excluded``-episode)."""
    return StrategyState(
        last_observation_sequence=1,
        last_book_source_epoch=1,
        last_as_of_ts=99_000,
        last_market_status_epoch=1,
        last_market_status_recv_ts=1,
        last_phase=1,
        last_suspended=False,
        last_book_in_gap=last_book_in_gap,
        guard_watermark=None,
        smoother_mid=0.5,
        smoother_mid_ts=99_000,
        spread_ref_samples=tuple(0.02 for _ in range(25)),
        depth_ref_samples=tuple(100.0 for _ in range(25)),
    )


def _guarded_warm_state(
    *, basis_gap: float = 0.05, basis_count: int = 38, last_book_in_gap: bool | None = None
) -> StrategyState:
    """A warm state whose ``rolling_median`` basis is ALSO warm (``basis_count`` samples ==
    ``basis_gap`` ≥ ``basis_min_samples`` default 30) with the guard watermark seeded at epoch 1, so a
    fresh-FV ok frame WOULD reach the residual guard block and act on the pre-gap basis absent the
    reset. ``last_book_in_gap`` is the book-status watermark knob."""
    return StrategyState(
        last_observation_sequence=1,
        last_book_source_epoch=1,
        last_as_of_ts=99_000,
        last_market_status_epoch=1,
        last_market_status_recv_ts=1,
        last_phase=1,
        last_suspended=False,
        last_book_in_gap=last_book_in_gap,
        guard_watermark=GuardStateWatermark(fv_source_epoch=1),
        smoother_mid=0.5,
        smoother_mid_ts=99_000,
        spread_ref_samples=tuple(0.02 for _ in range(25)),
        depth_ref_samples=tuple(100.0 for _ in range(25)),
        basis_samples=tuple((900 + i, basis_gap) for i in range(basis_count)),
        basis_sample_count=basis_count,
    )


# --- gap-episode END is a row-R RESET, byte-identical across arms (REQ-033/070 row R) --------


def test_gap_episode_end_is_row_r_reset() -> None:
    # A book that WAS in a gap|excluded episode (state.last_book_in_gap=True) resumes on a healthy ok
    # book: the gap-episode END is a REQ-070 row-R RESET (REQ-033), driven by the universal book-status
    # watermark. The ok mid is 0.35 (bid 0.30 / ask 0.40) — DISTINCT from the pre-gap smoother 0.50 —
    # so the RE-SEED is provable, not a coincidence.
    seed = _warm_state(last_book_in_gap=True)
    end = _obs(book_status="ok", bid=0.30, ask=0.40, guard_fv=None)

    # It classifies as row R in BOTH arms (book-status is universal — never the guard leg).
    assert _classify_row(end, seed, _config(guard_enabled=False)) == "R"
    assert _classify_row(end, seed, _config(guard_enabled=True)) == "R"

    d_base, s_base = decide(end, seed, _config(guard_enabled=False))
    d_guard, s_guard = decide(end, seed, _config(guard_enabled=True))

    dwell = _config().book_state_dwell_before_quote_ms
    for decision, state in ((d_base, s_base), (d_guard, s_guard)):
        # Row-R reset shape: NO_QUOTE with the reset/warmup reason — the residual guard NEVER runs on
        # the pre-gap basis (a reset pre-empts the whole row-H guard block).
        assert decision.kind == "NO_QUOTE"
        assert decision.reason_codes == ("event_ref_warmup",)
        # Venue accumulators CLEARED; the smoother RE-SEEDS from THIS frame's own ok mid (0.35).
        assert state.spread_ref_samples == ()
        assert state.depth_ref_samples == ()
        assert state.smoother_mid == 0.35
        # Basis CLEARED (the pre-gap basis does not carry into the post-gap decision).
        assert state.basis_samples == ()
        assert state.basis_sample_count == 0
        # A cooldown is anchored at the gap-END frame (as_of_ts + dwell).
        assert state.event_cooldown_until_ts == end.as_of_ts + dwell
        # The book-status watermark re-baselines to this ok frame (no re-trigger next frame).
        assert state.last_book_in_gap is False

    # ARM-SYMMETRIC: the RESET is byte-identical across arms — FV absent on the END frame, so the
    # next-state carries no guard element and ``state_hash`` matches exactly.
    assert s_base.state_hash() == s_guard.state_hash()
    assert (d_base.kind, d_base.reason_codes, d_base.intent_plan) == (
        d_guard.kind,
        d_guard.reason_codes,
        d_guard.intent_plan,
    )


# --- The teeth: pre-gap accumulators do NOT survive the outage (RED-53-sibling) --------------


def test_gap_end_accumulator_survival_teeth() -> None:
    # Reproduce Fable's executed control: a guarded warm arm (basis_count 38 ≥ 30, distinctive pre-gap
    # basis 0.05, smoother 0.50), a gap outage, then a fresh-FV ok frame. The TEETH: the pre-gap
    # basis / smoother / rolling refs must NOT survive into the post-gap decision (this FAILS on the
    # pre-fix code, where the gap-END frame is row C/H and the accumulators carry through untouched, so
    # the residual guard acts immediately on a basis estimated under the pre-disruption regime).
    guarded = _config(guard_enabled=True)
    seed = _guarded_warm_state(basis_gap=0.05, basis_count=38, last_book_in_gap=False)

    # Frame 1: a gap outage (degraded book) — row E ENTRY (RED-53); admits nothing, so the pre-gap
    # accumulators are untouched THROUGH the gap and the book-status watermark flips in-gap.
    gap = _obs(
        book_status="gap",
        bid=None,
        ask=None,
        bid_size=None,
        ask_size=None,
        guard_fv=_fresh_fv(fv=0.55),
        observation_sequence=2,
        as_of_ts=100_000,
    )
    assert _classify_row(gap, seed, guarded) == "E"
    _, s_gap = decide(gap, seed, guarded)
    assert s_gap.last_book_in_gap is True
    # The gap outage admitted nothing — the pre-gap accumulators survived the outage itself...
    assert s_gap.basis_sample_count == 38
    assert s_gap.smoother_mid == 0.50

    # Frame 2: the FIRST ok frame after the outage — the gap-episode END. Distinct ok mid 0.35 so a
    # re-seed is provable vs the surviving 0.50. A fresh FV so the pre-fix guard WOULD act on the
    # stale basis (basis_count 38 ≥ 30) — the exact defect Fable executed.
    end = _obs(
        book_status="ok",
        bid=0.30,
        ask=0.40,
        guard_fv=_fresh_fv(fv=0.55),
        observation_sequence=3,
        as_of_ts=100_100,
    )
    assert _classify_row(end, s_gap, guarded) == "R"
    d_end, s_end = decide(end, s_gap, guarded)

    # TEETH — the pre-gap basis / smoother / rolling refs did NOT survive into the post-gap decision:
    assert s_end.basis_sample_count == 0, "pre-gap basis must NOT survive the gap episode"
    assert s_end.basis_samples == ()
    assert s_end.spread_ref_samples == (), "pre-gap rolling refs must NOT survive the gap episode"
    assert s_end.depth_ref_samples == ()
    assert s_end.smoother_mid == 0.35, "smoother must RE-SEED from the post-gap ok mid, not survive"
    # The decision is the row-R reset, NOT a residual-guard action on the stale basis.
    assert d_end.kind == "NO_QUOTE"
    assert d_end.reason_codes == ("event_ref_warmup",)


# --- Totality: ongoing gap = row E, gap-END ok = row R, never colliding (AC-057) ------------


def test_gap_end_totality_ongoing_vs_end() -> None:
    # AC-057 totality: the gap-END row-R trigger must NOT collide with the row-E ONGOING gap. They are
    # mutually exclusive by the CURRENT frame's book_status — gap-END requires ``ok``, ongoing requires
    # ``gap|excluded`` — so the same in-gap watermark routes each to exactly one row.
    seed_in_gap = _warm_state(last_book_in_gap=True)
    config = _config()

    # An ONGOING gap frame (still in the episode) stays row E — never row R.
    ongoing = _obs(book_status="gap", bid=None, ask=None, bid_size=None, ask_size=None)
    assert _classify_row(ongoing, seed_in_gap, config) == "E"

    # An ONGOING excluded frame likewise stays row E.
    ongoing_excluded = _obs(book_status="excluded", bid=None, ask=None, bid_size=None, ask_size=None)
    assert _classify_row(ongoing_excluded, seed_in_gap, config) == "E"

    # The gap-END ok frame is row R (the in-gap watermark makes the ok frame a reset).
    end = _obs(book_status="ok", bid=0.30, ask=0.40)
    assert _classify_row(end, seed_in_gap, config) == "R"

    # With NO prior gap episode (fresh/cold-start watermark None) the SAME ok frame is NOT a spurious
    # reset — the gap-END trigger requires ``last_book_in_gap is True``, so it is inert here.
    seed_clean = _warm_state(last_book_in_gap=None)
    assert _classify_row(end, seed_clean, config) != "R"
    # A quiescent ok frame in-window (mid 0.50 matching the seeded smoother, no mid-jump) is plain
    # row H from a clean state — the in-gap watermark never fabricates a reset.
    quiescent = _obs(book_status="ok", bid=0.49, ask=0.51)
    assert _classify_row(quiescent, seed_clean, config) == "H"
    # ...and a newly-entered gap (ENTRY, RED-53) from a clean state is still row E.
    assert _classify_row(ongoing, seed_clean, config) == "E"
