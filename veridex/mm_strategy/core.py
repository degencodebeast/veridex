"""Pure-tier strategy core — the watermark precondition layer + transition reducer (MM-R4-B).

``decide()`` is the deterministic, total decision function. It layers two pieces:

1. The NO-LOOKAHEAD WATERMARK PRECONDITION LAYER (E2-T3) that runs BEFORE any quoting logic:
   clock/epoch monotonicity vs the carried state, sequence-staleness, the epoch-driven resets, and
   the fail-closed restart guarantee. This is spec row S (STALE → HOLD).
2. The complete S/R/E/D/C/F/W/H transition reducer (E2-T4) over that precondition layer: every
   frame that passes the watermark is classified into exactly one of rows R/E/D/C/F/W/H
   (:func:`_classify_row`) and that row's ``(smoother, refs, basis, cooldown, quote)`` transition
   is applied. See the reducer scope comment above :func:`_mid` for what's wired here vs deferred
   to E4.

Load-bearing ordering (REQ-033/034, AC-040, RED-06/37): the ``book_source_epoch`` INCREMENT is
evaluated BEFORE sequence-staleness, so a healthy first frame after a reconnect — whose re-baselined
sequence may sit at or below the old watermark — is accepted as the post-reset baseline instead of
being wrongly rejected as stale.

Import whitelist (load-bearing): stdlib + pydantic + the pure ``mm_strategy`` siblings
(``config`` for the ``StrategyConfig`` type + ``guard_enabled`` / ``restart_policy`` knobs,
``contracts`` for the models, ``basis`` for the smoother/reference helpers) +
``veridex.runtime.evidence`` (transitively) ONLY. No network, no I/O, no wall clock, no randomness,
no module-level mutable state, no process-local cache.
"""

from __future__ import annotations

from typing import Any, Literal

from veridex.mm_strategy.basis import (
    basis_from_state,
    ceil_to_tick,
    event_smoother_update,
    floor_to_tick,
    halflife_ewma,
    reference_is_warm,
    rolling_depth_reference,
    rolling_spread_reference,
)
from veridex.mm_strategy.config import StrategyConfig
from veridex.mm_strategy.contracts import (
    STRATEGY_REVISION,
    DecisionKind,
    GuardFairValue,
    GuardStateWatermark,
    MarketStatus,
    NeutralIntent,
    ReasonCode,
    StrategyDecision,
    StrategyObservation,
    StrategyState,
    client_order_id,
    decision_id,
)


def _hold(reason_codes: tuple[ReasonCode, ...] = ()) -> StrategyDecision:
    """A ``HOLD`` decision with the given (closed-vocabulary) reason codes.

    The provenance hashes / ``decision_id`` are populated by E5-T1, not here; an ACCEPTED frame
    carries no reason yet (the E2-T4 reducer assigns it), so the watermark layer emits a reason
    ONLY on the reject rows (``clock_regression`` / ``epoch_regression`` / ``stale_observation``).
    """
    return StrategyDecision(kind="HOLD", reason_codes=reason_codes)


def _status_is_stale(observation: StrategyObservation, config: StrategyConfig) -> bool:
    """REQ-026 upper bound: a non-``UNKNOWN`` status older than ``market_status_max_age_ms`` is
    temporally invalid — provenance authenticates WHO said ``ACTIVE``, not that it is STILL current
    (Codex R2 MAJOR-2). The lower bound (``recv_ts > as_of_ts`` future-dating) is a REQ-022
    construction guard, so a constructed observation only needs this upper-bound check. ``UNKNOWN``
    carries no ``recv_ts`` to age (typed ``None`` sentinel) — it is handled directly as ``UNKNOWN``."""
    if observation.market_status_recv_ts is None:
        return False
    age = observation.as_of_ts - observation.market_status_recv_ts
    return age > config.market_status_max_age_ms


def _status_regressed(observation: StrategyObservation, state: StrategyState) -> bool:
    """REQ-026 regression: a non-``UNKNOWN`` status whose epoch OR recv_ts is below the DURABLE
    ``StrategyState`` watermark (``last_market_status_epoch`` / ``last_market_status_recv_ts``) is a
    rolled-back generation and is never accepted as fresher truth. Comparing against the state
    watermark — not just prior-frame history — makes the check durable across an assembler restart
    that replays an older ``ACTIVE`` generation (AC-048). ``UNKNOWN`` (typed ``None`` sentinels) has
    no generation to compare and is handled directly as ``UNKNOWN``."""
    if observation.market_status_epoch is None or observation.market_status_recv_ts is None:
        return False
    if (
        state.last_market_status_epoch is not None
        and observation.market_status_epoch < state.last_market_status_epoch
    ):
        return True
    return (
        state.last_market_status_recv_ts is not None
        and observation.market_status_recv_ts < state.last_market_status_recv_ts
    )


def _effective_market_status(
    observation: StrategyObservation, state: StrategyState, config: StrategyConfig
) -> MarketStatus:
    """The market status AFTER the core's OWN freshness + regression re-check (REQ-026) — the single
    source of truth both the watermark advance and the quote-blocker reason read from.

    A ``UNKNOWN`` status stays ``UNKNOWN``; a non-``UNKNOWN`` status (``ACTIVE`` / ``HALTED`` /
    ``CLOSED``) that is over-age or regressed below the durable watermark is DOWNGRADED to ``UNKNOWN``
    (fail closed — a stale/rolled-back status is never trusted as current). This is what makes the
    watermark advance ONLY on an accepted-current non-``UNKNOWN`` status, so a stale or regressed
    generation can never overwrite it."""
    if observation.market_status == "UNKNOWN":
        return "UNKNOWN"
    if _status_is_stale(observation, config) or _status_regressed(observation, state):
        return "UNKNOWN"
    return observation.market_status


def _status_watermark(
    observation: StrategyObservation, state: StrategyState, config: StrategyConfig
) -> tuple[int | None, int | None]:
    """The ``(market_status_epoch, market_status_recv_ts)`` to carry forward on an ACCEPTED frame.

    REQ-026/027 (Fable n-m6): the status watermark advances ONLY on an accepted observation whose
    EFFECTIVE status is not ``UNKNOWN`` — a raw ``UNKNOWN``, a stale non-``UNKNOWN``, or one regressed
    below the durable watermark all leave the prior watermark standing, so a rolled-back generation
    can never overwrite it (durable across an assembler restart, AC-048)."""
    if _effective_market_status(observation, state, config) == "UNKNOWN":
        return state.last_market_status_epoch, state.last_market_status_recv_ts
    return observation.market_status_epoch, observation.market_status_recv_ts


def _guard_watermark(
    observation: StrategyObservation, state: StrategyState, config: StrategyConfig
) -> GuardStateWatermark | None:
    """The guard-scoped watermark to seed/carry on an accepted frame.

    - Guard OFF (config): no FV element exists anywhere in state (Codex-R5 MAJOR-1) → ``None``.
    - Guard ON + FV present: (re)seed the watermark at this observation's FV generation.
    - Guard ON + FV ABSENT: CARRY THE PRIOR ``state.guard_watermark`` FORWARD UNCHANGED (Codex
      Gate#1 MAJOR-1). A frame that merely lacks an FV leg while the guard is enabled must NOT erase
      the last-seen generation, or a later OLDER FV epoch would slip past ``epoch_regression``
      (REQ-031 guarded last-seen epoch / REQ-033 regression).
    """
    if not config.guard_enabled:
        return None
    if observation.guard_fv is not None:
        return GuardStateWatermark(fv_source_epoch=observation.guard_fv.fv_source_epoch)
    return state.guard_watermark


def _accept(
    observation: StrategyObservation,
    state: StrategyState,
    config: StrategyConfig,
    *,
    full_reset: bool,
    basis_reset: bool,
) -> StrategyState:
    """Advance the watermark onto ``state`` for an accepted frame, applying the selected reset.

    - ``full_reset`` (a ``book_source_epoch`` increment or a cold-start seed) clears the basis
      window AND the venue accumulators (smoother + rolling references) — REQ-033.
    - ``basis_reset`` (an ``fv_source_epoch`` increment) clears the basis window ALONE; the
      FV-independent venue accumulators are UNTOUCHED (Codex-R5 MAJOR-1).
    - A plain clean frame advances the watermark only — the watermark layer never TRAINS the
      accumulators (that admission is the E2-T4 reducer's job).
    """
    status_epoch, status_recv_ts = _status_watermark(observation, state, config)
    update: dict[str, Any] = {
        "last_observation_sequence": observation.observation_sequence,
        "last_book_source_epoch": observation.book_source_epoch,
        "last_as_of_ts": observation.as_of_ts,
        "last_market_status_epoch": status_epoch,
        "last_market_status_recv_ts": status_recv_ts,
        "guard_watermark": _guard_watermark(observation, state, config),
        # Advance the REQ-080 phase watermark on EVERY accepted frame (like the clock/sequence
        # watermark): the row-R ``phase`` transition compares the NEXT frame against this value, and
        # a reset frame re-baselines it to its own phase so a reconnect never spuriously re-triggers.
        "last_phase": observation.phase,
    }
    if full_reset:
        update.update(
            basis_samples=(),
            basis_ewma_value=None,
            basis_ewma_ts=None,
            basis_sample_count=0,
            smoother_mid=None,
            smoother_mid_ts=None,
            spread_ref_samples=(),
            depth_ref_samples=(),
        )
    elif basis_reset:
        update.update(
            basis_samples=(),
            basis_ewma_value=None,
            basis_ewma_ts=None,
            basis_sample_count=0,
        )
    return state.model_copy(update=update)


def _guard_epoch_delta(
    observation: StrategyObservation, state: StrategyState, config: StrategyConfig
) -> int:
    """Sign of the guard ``fv_source_epoch`` change vs the state watermark: ``-1`` regressed, ``0``
    unchanged / not comparable, ``+1`` incremented.

    Not comparable (→ ``0``) when the guard is disabled, the observation carries no FV leg, or the
    state has no guard watermark yet (the first FV frame merely SEEDS the watermark — no reset)."""
    if not (
        config.guard_enabled
        and observation.guard_fv is not None
        and state.guard_watermark is not None
    ):
        return 0
    seen = state.guard_watermark.fv_source_epoch
    incoming = observation.guard_fv.fv_source_epoch
    if incoming < seen:
        return -1
    if incoming > seen:
        return 1
    return 0


# --- The total S/R/E/D/C/F/W/H transition reducer (REQ-070 / REQ-081 / AC-045/050/052/057) ---
# Every constructed observation that PASSES the watermark layer matches EXACTLY ONE spec REQ-070 row.
# Row S (STALE) is the watermark layer's reject rows (clock / epoch / sequence → HOLD) upstream; the
# reducer below classifies the seven ACCEPTED-frame rows in spec order R,E,D,C,F,W,H and applies each
# row's ``(smoother, refs, basis, cooldown, quote)`` transition. All gates read PRIOR-state
# accumulator values (compare-then-update, universal); training folds into the NEXT state.
#
# SCOPE (E2-T4 + E4-T2): the reset/event triggers are wired via ``_classify_row``. E2-T4 seeded the
# single-frame triggers — ``tick_regime_changed`` (row R), ``book_status ∈ {gap, excluded}`` (row E,
# the RED-53 canonical newly-entered gap book), and data-degraded book-stale / leg-skew (row D).
# E4-T2 completes the REQ-080 venue-book set that reads PRIOR-state references: the ``phase``
# transition (row R, via ``_phase_transition`` + the ``last_phase`` watermark) and the WARM-reference
# ratio/jump/floor events (depth-vanish, spread-blowout, mid-jump, level-count floor — row E, via
# ``_venue_event_reason``, gated behind ``_references_warm``). ``market_status`` / stream / projection
# stay OUT of the trigger set — they are QUOTE-ONLY blockers (REQ-070/026/097). The remaining REQ-080
# reset variants (gap-episode END, suspension→reopen) and the cancel/intent PLAN wiring stay for later
# E4 tasks; they slot into the SAME row-E / row-R branches without reshaping this reducer. The row-H
# quote here is the eligible DecisionKind CLASS only — E4 fills anchor/zones/prices and the intent plan.

# The seven ACCEPTED-frame rows (row S/STALE is the watermark layer's reject rows, upstream — it
# never reaches ``_classify_row``). Pinning this as a `Literal`, like every other closed vocabulary
# in this tier, makes `_classify_row`'s return and the `decide()` dispatch a mypy-checked
# discriminant instead of an open `str`.
Row = Literal["R", "E", "D", "C", "F", "W", "H"]


def _mid(observation: StrategyObservation) -> float | None:
    """The raw venue mid ``(bid + ask) / 2`` — ``None`` when either touch is absent (degraded book)."""
    if observation.bid is None or observation.ask is None:
        return None
    return (observation.bid + observation.ask) / 2.0


def _spread(observation: StrategyObservation) -> float | None:
    """The raw top-of-book spread ``ask − bid`` — ``None`` when either touch is absent."""
    if observation.bid is None or observation.ask is None:
        return None
    return observation.ask - observation.bid


def _top_depth(observation: StrategyObservation) -> float | None:
    """The raw top-of-book depth ``min(bid_size, ask_size)`` (the thinner side binds REQ-080/082) —
    ``None`` when either size is absent."""
    if observation.bid_size is None or observation.ask_size is None:
        return None
    return min(observation.bid_size, observation.ask_size)


def _book_is_stale(observation: StrategyObservation, config: StrategyConfig) -> bool:
    """Row D ``book_stale``: the venue read is older than ``book_freshness_ms`` (REQ-022 clocks)."""
    return observation.as_of_ts - observation.book_recv_ts > config.book_freshness_ms


def _leg_is_skewed(observation: StrategyObservation, config: StrategyConfig) -> bool:
    """Row D ``leg_skew``: the book vs match-state read clocks diverge beyond ``max_leg_skew_ms``."""
    return (
        abs(observation.book_recv_ts - observation.match_state_recv_ts)
        > config.max_leg_skew_ms
    )


def _cooldown_active(observation: StrategyObservation, state: StrategyState) -> bool:
    """Row C: a prior-anchored event cooldown whose dwell has NOT elapsed in observation time
    (``as_of_ts`` arithmetic, never wall clock — REQ-081)."""
    return (
        state.event_cooldown_until_ts is not None
        and observation.as_of_ts < state.event_cooldown_until_ts
    )


def _references_warm(state: StrategyState, config: StrategyConfig) -> bool:
    """True once the smoother is seeded AND both rolling references hold ``ref_min_samples`` accepted
    samples (REQ-080). Below this floor a frame is row W (WARMUP) — quote-blocked but ADMITTING."""
    return (
        state.smoother_mid is not None
        and reference_is_warm(len(state.spread_ref_samples), config)
        and reference_is_warm(len(state.depth_ref_samples), config)
    )


def _phase_transition(observation: StrategyObservation, state: StrategyState) -> bool:
    """REQ-080 RESET-class (row R) trigger: the match-state ``phase`` changed vs the PRIOR accepted
    frame's phase carried on ``state.last_phase``. A fresh / cold-start / reset state
    (``last_phase is None``) has no prior phase to compare, so the first accepted frame merely SEEDS
    the phase watermark — never a spurious reset."""
    return state.last_phase is not None and observation.phase != state.last_phase


def _reset_reason(observation: StrategyObservation, state: StrategyState) -> ReasonCode:
    """The truthful reset reason for an ACCEPTED-frame row-R trigger (REQ-033/036/080). The two
    reset-class triggers detectable from a single accepted frame + prior state are the in-stream
    ``tick_regime_changed`` signal and a match-state ``phase`` transition; ``tick_regime_changed``
    takes precedence when both hold. (The book-epoch RECONNECT reset carries its own truthful
    ``event_ref_warmup`` reason on the upstream path — this observation never saw a tick regime
    change, REQ-036 — so it is NEVER routed through here.)"""
    if observation.tick_regime_changed:
        return "tick_regime_changed"
    return "phase_transition"


def _venue_event_reason(
    observation: StrategyObservation, state: StrategyState, config: StrategyConfig
) -> ReasonCode | None:
    """The REQ-080 venue-book EVENT-trigger reason for a WARM-reference frame, or ``None`` (row E).

    These ratio/jump/floor gates are evaluable ONLY on warmed references (REQ-080 / REQ-070 row E),
    so the caller gates this behind :func:`_references_warm`; the reset-class triggers (tick-regime,
    ``phase``) are row R and the ``gap`` / ``excluded`` book ENTRY is handled separately. Each fired
    trigger routes the frame to row E (NO_QUOTE + cooldown, no admission). ``market_status`` / stream
    / projection are DELIBERATELY absent — they are QUOTE-ONLY blockers, never triggers
    (REQ-070/026/097; the ``market_status != ACTIVE`` mutation adds exactly that and MUST fail).

    Evaluated in a pinned deterministic order (first match wins). Each gate reads the PRIOR-state
    rolling references / smoothed prior mid (compare-then-update, universal):

    * depth-vanish — top depth ``< min_top_depth`` OR ``< depth_collapse_ratio ×`` the state-carried
      rolling-depth reference.
    * spread-blowout — raw spread ``> spread_blowout_multiple ×`` the rolling-spread reference.
    * mid-jump — ``|raw mid − the STATE-carried smoothed prior mid| > mid_jump_threshold`` (compares
      the RAW book mid against ``state.smoother_mid``, REQ-036 compare-then-update).
    * level-count floor — ``level_count_in_band < min_level_count``.

    The depth / spread / mid gates need BOTH raw touches — an absent side yields ``None`` and the gate
    is skipped (a degraded book is row E via ``book_status`` or row D, never a spurious ratio
    trigger). Closed §4.4 vocabulary (REQ-063): depth-vanish / spread-blowout / mid-jump all surface
    as ``book_thin`` — the sole 'venue book untrustworthy to anchor' reason beyond the status codes;
    a dedicated per-trigger code would be a spec revision, never task discretion.
    """
    depth = _top_depth(observation)
    if depth is not None:
        depth_ref = rolling_depth_reference(state.depth_ref_samples, config)
        if depth < config.min_top_depth or depth < config.depth_collapse_ratio * depth_ref:
            return "book_thin"
    spread = _spread(observation)
    if spread is not None:
        spread_ref = rolling_spread_reference(state.spread_ref_samples, config)
        if spread > config.spread_blowout_multiple * spread_ref:
            return "book_thin"
    mid = _mid(observation)
    if (
        mid is not None
        and state.smoother_mid is not None
        and abs(mid - state.smoother_mid) > config.mid_jump_threshold
    ):
        return "book_thin"
    if observation.level_count_in_band < config.min_level_count:
        return "level_count_low"
    return None


def _classify_row(
    observation: StrategyObservation, state: StrategyState, config: StrategyConfig
) -> Row:
    """Classify an ACCEPTED observation into EXACTLY ONE of the reducer rows R/E/D/C/F/W/H.

    Rows are evaluated in spec REQ-070 order, so an earlier row PRE-EMPTS a later one — most notably
    a RESET-class reset (row R: ``tick_regime_changed`` OR a ``phase`` transition) pre-empts the
    REQ-080 book triggers (row E) for the same frame (Fable-plan-review Minor-1). The row-E ratio /
    jump / floor triggers (:func:`_venue_event_reason`) are evaluable ONLY on WARM references (REQ-080
    / REQ-070 row E), so they are gated behind :func:`_references_warm`; below the warmup floor those
    gates are inadmissible and the frame falls through to row W (which never anchors a cooldown — the
    liveness guarantee). Purely a function of ``(observation, prior state, config)`` — no clock, no
    randomness — so decision identity reproduces. (Row S is handled upstream by the watermark layer
    and never reaches here.)
    """
    if observation.tick_regime_changed or _phase_transition(observation, state):
        return "R"
    if observation.book_status in ("gap", "excluded"):
        return "E"
    if _references_warm(state, config) and _venue_event_reason(observation, state, config):
        return "E"
    if _book_is_stale(observation, config) or _leg_is_skewed(observation, config):
        return "D"
    if _cooldown_active(observation, state):
        return "C"
    if _guard_epoch_delta(observation, state, config) > 0:
        return "F"
    if not _references_warm(state, config):
        return "W"
    return "H"


def _decide(kind: DecisionKind, reason_codes: tuple[ReasonCode, ...]) -> StrategyDecision:
    """A decision with the given kind + ordered closed-vocabulary reasons (provenance hashes are
    E5-T1's; the E4 taxonomy fills anchor/zones/prices + the intent plan for a row-H quote)."""
    return StrategyDecision(kind=kind, reason_codes=reason_codes)


def _status_stream_reason(
    observation: StrategyObservation, state: StrategyState, config: StrategyConfig
) -> ReasonCode | None:
    """The QUOTE-ONLY status/stream blocker reason, or ``None`` when the frame may quote (REQ-026/097).

    The status arm reads the EFFECTIVE status (:func:`_effective_market_status`), so a stale ``ACTIVE``
    or a status regressed below the durable watermark is blocked as ``market_status_unknown`` — never
    as a fresh write. ``market_status ≠ ACTIVE`` and projection/stream degradation block a fresh WRITE
    but NEVER admission or cooldown (REQ-070): this reason only downgrades an otherwise quote-eligible
    row-H disposition to ``NO_QUOTE``; the accumulator training already folded into the next state
    stands. The regression re-check reads ``state`` (the watermark-advanced base) — equivalent to the
    prior watermark, since :func:`_status_watermark` advances ONLY on an accepted-current status, so a
    just-accepted status equals its own generation (never below it) and a downgraded one leaves the
    prior watermark in place (the value this re-check compares against)."""
    effective = _effective_market_status(observation, state, config)
    if effective == "UNKNOWN":
        return "market_status_unknown"
    if effective == "HALTED":
        return "market_halted"
    if effective == "CLOSED":
        return "market_closed"
    if not observation.order_stream_ok:
        return "stream_degraded"
    if not observation.projection_fresh:
        return "projection_stale"
    return None


def _token_mapping_reason(observation: StrategyObservation) -> ReasonCode | None:
    """The token-mapping blocker reason (REQ-062/024), or ``None`` when this outcome's venue token
    identity is resolved.

    The resolved ``token_id`` arrives ON the observation — the assembler resolves the
    ``(market_ref, side) -> token_id`` mapping via the pinned maker mapping and the pure core NEVER
    imports that surface (E3-T5 import closure). A BLANK / whitespace-only ``token_id`` is the
    assembler's signal that the mapping was MISSING or AMBIGUOUS: without an unambiguous token this
    outcome cannot place an order on the venue, so it fails closed to
    NO_QUOTE(``token_mapping_missing``). The core NEVER fabricates or defaults a token to salvage a
    quote (REQ-062) — a missing identity blocks the outcome's order-placing actions, it does not
    invent one. This is a QUOTE-ONLY blocker (like the status/stream gate): it downgrades an
    otherwise placement-eligible row-H disposition to ``NO_QUOTE`` without touching admission or the
    watermark — the venue accumulators keyed on the present ``(market_ref, side)`` still train."""
    if not observation.token_id.strip():
        return "token_mapping_missing"
    return None


def _admit_venue_updates(
    observation: StrategyObservation, base: StrategyState, config: StrategyConfig
) -> dict[str, Any]:
    """The venue-accumulator (smoother + rolling refs) training deltas for an ADMITTING row (F/W/H).

    COMPARE-then-UPDATE: reads PRIOR values off ``base`` (the watermark-advanced, accumulator-
    untouched state) and folds THIS frame's raw ``ok``-book mid / spread / top-depth in. Both arms
    train identically on the UNIVERSAL gates — the guard never enters here (REQ-070). A smoother that
    is ``None`` (post-full-reset, unseeded) takes this mid as its first sample; otherwise the
    config-selected smoother step folds it in on the elapsed observation-clock interval.
    """
    updates: dict[str, Any] = {}
    mid = _mid(observation)
    if mid is not None:
        if base.smoother_mid is None:
            updates["smoother_mid"] = mid
        else:
            prior_ts = (
                base.smoother_mid_ts
                if base.smoother_mid_ts is not None
                else observation.as_of_ts
            )
            dt_ms = float(observation.as_of_ts - prior_ts)
            updates["smoother_mid"] = event_smoother_update(
                base.smoother_mid, mid, config, dt_ms
            )
        updates["smoother_mid_ts"] = observation.as_of_ts
    spread = _spread(observation)
    if spread is not None:
        # Bound the stored series to its configured window ON APPEND (Codex Gate#1 MAJOR-3): only the
        # last ``rolling_spread_window`` samples the estimator reads are retained, so ``StrategyState``
        # size + canonical hash never depend on data outside the declared window (REQ-031/072/080).
        updates["spread_ref_samples"] = (base.spread_ref_samples + (spread,))[
            -config.rolling_spread_window :
        ]
    depth = _top_depth(observation)
    if depth is not None:
        updates["depth_ref_samples"] = (base.depth_ref_samples + (depth,))[
            -config.rolling_depth_window :
        ]
    return updates


def _fv_is_stale(
    guard_fv: GuardFairValue, observation: StrategyObservation, config: StrategyConfig
) -> bool:
    """REQ-022 guard-scoped FV staleness — transport OR content staleness (either breach is stale).

    * transport freshness bounds ``as_of_ts − fv_recv_ts`` by ``fv_freshness_ms``;
    * content staleness bounds ``fv_recv_ts/1000 − fv_source_ts`` (both FV-side clocks — the ms
      recorder clock and the integer-second source clock) by ``fv_source_lag_s``.

    A transport-fresh but content-stale FV is stale (mirroring the R3 alignment pattern)."""
    transport_stale = observation.as_of_ts - guard_fv.fv_recv_ts > config.fv_freshness_ms
    content_stale = (
        guard_fv.fv_recv_ts / 1000.0 - guard_fv.fv_source_ts > config.fv_source_lag_s
    )
    return transport_stale or content_stale


def _guard_fv_block_reason(
    observation: StrategyObservation, config: StrategyConfig
) -> ReasonCode | None:
    """The guard-scoped FV blocker reason, or ``None`` when the FV leg is present, fresh, and the
    match-state is not suspended (the guard may proceed to warmup / residual).

    Guard-scoped (REQ-022/076): the CALLER gates this behind ``config.guard_enabled`` — the baseline
    arm never consults the FV leg, so a stale/missing FV can never make it abstain (Fable F1).
    Evaluated in the closed-vocabulary declared order (REQ-063): a MISSING leg (``guard_fv is None``)
    is ``txline_missing``; a present-but-stale leg is ``txline_stale``; a suspended match-state (the
    UNIVERSAL leg, REQ-020(d)) is ``txline_suspended``. REQ-076 unifies the former ``fv_missing`` away.
    """
    if observation.guard_fv is None:
        return "txline_missing"
    if _fv_is_stale(observation.guard_fv, observation, config):
        return "txline_stale"
    if observation.suspended:
        return "txline_suspended"
    return None


def _admit_basis_updates(
    observation: StrategyObservation, base: StrategyState, config: StrategyConfig
) -> dict[str, Any]:
    """The basis training delta for an ADMITTING row — GUARDED arm ONLY (baseline never trains a
    basis). Folds THIS frame's ``raw_gap = fv − mid`` into the config-selected basis state. For a
    row-F fv-epoch frame both bases were already cleared by ``_accept(basis_reset=True)``, so the
    fold yields exactly the current sample — the REQ-070 clear-then-admit (Codex R6 MAJOR-1.2).

    **STAGE-1 FAIL-SAFE (REQ-022/076):** the FV freshness / suspension / presence gate PRECEDES the
    fold, so a stale / missing / suspended FV frame NEVER trains the accumulator. Freezing the basis
    (and its ``as_of_ts`` decay anchor) here is load-bearing: a stale sample folded in would silently
    corrupt the basis — and shorten the EWMA decay interval — for EVERY subsequent fresh frame. This
    is strictly stronger than the quote-level FV gate: it protects the accumulator itself, not merely
    the quote. ``basis_sample_count`` advances in lockstep with the fold so REQ-032 warmup is honest.

    * ``halflife_ewma`` — fold into the BOUNDED SUFFICIENT ACCUMULATOR one sample at a time (Codex
      Gate#1-R2 MAJOR-1). The accumulator IS the online estimate, so it is independent of how much
      raw history is retained — never a finite last-``basis_window`` window. An unseeded accumulator
      (fresh / post-reset) takes this ``raw_gap`` as its first sample.
    * ``rolling_median`` — append to ``base.basis_samples`` and bound to ``basis_window`` ON APPEND
      (Codex Gate#1 MAJOR-3): the truncation is EXACT here (the estimator already reads
      ``[-basis_window:]``), so state size + canonical hash never depend on data outside the window.
    """
    if not (config.guard_enabled and observation.guard_fv is not None):
        return {}
    # STAGE-1 FAIL-SAFE: freshness/suspension gate BEFORE the fold — a stale/suspended FV is frozen
    # out of the accumulator, never trained (the guard_fv-None case already returned above).
    if _guard_fv_block_reason(observation, config) is not None:
        return {}
    mid = _mid(observation)
    if mid is None:
        return {}
    raw_gap = observation.guard_fv.fv - mid
    count = base.basis_sample_count + 1
    if config.basis_estimator == "halflife_ewma":
        prev = base.basis_ewma_value
        prev_ts = base.basis_ewma_ts
        if prev is None or prev_ts is None:
            return {
                "basis_ewma_value": raw_gap,
                "basis_ewma_ts": observation.as_of_ts,
                "basis_sample_count": count,
            }
        dt_ms = float(observation.as_of_ts - prev_ts)
        value = halflife_ewma(prev, raw_gap, dt_ms, float(config.ewma_halflife_ms))
        return {
            "basis_ewma_value": value,
            "basis_ewma_ts": observation.as_of_ts,
            "basis_sample_count": count,
        }
    samples = base.basis_samples + ((observation.as_of_ts, raw_gap),)
    return {
        "basis_samples": samples[-config.basis_window :],
        "basis_sample_count": count,
    }


def _cooldown_deadline(observation: StrategyObservation, config: StrategyConfig) -> int:
    """The event-cooldown expiry anchored at ``observation.as_of_ts`` — the common deadline
    computed by both reset-anchoring rows (R and E)."""
    return observation.as_of_ts + config.book_state_dwell_before_quote_ms


def _apply_reset(
    observation: StrategyObservation,
    base: StrategyState,
    config: StrategyConfig,
    *,
    reason: ReasonCode,
) -> tuple[StrategyDecision, StrategyState]:
    """Row R (RESET-class): clear the basis window + rolling refs; RE-SEED the smoother from THIS
    frame's mid IFF the book is ``ok`` (the ONLY re-seed row); anchor an event cooldown at this frame;
    NO_QUOTE (a cancel plan on exposure is E4's). The reset STATE transition is shared by two
    triggers, but the recorded ``reason`` is provenance-truthful per trigger (Codex Gate#1-R2
    MAJOR-2, REQ-033): the in-stream ``tick_regime_changed`` signal passes ``tick_regime_changed``,
    while a book-epoch RECONNECT (whose observation did NOT see a tick regime change) passes the
    truthful ``event_ref_warmup`` reset/warmup reason (REQ-036) — never a false tick-regime claim
    bound into ``decision_id``."""
    reseed_mid = _mid(observation) if observation.book_status == "ok" else None
    reseed_ts = observation.as_of_ts if reseed_mid is not None else None
    next_state = base.model_copy(
        update={
            "smoother_mid": reseed_mid,
            "smoother_mid_ts": reseed_ts,
            "spread_ref_samples": (),
            "depth_ref_samples": (),
            "basis_samples": (),
            "basis_ewma_value": None,
            "basis_ewma_ts": None,
            "basis_sample_count": 0,
            "event_cooldown_until_ts": _cooldown_deadline(observation, config),
        }
    )
    return _decide("NO_QUOTE", (reason,)), next_state


def _apply_event(
    observation: StrategyObservation, base: StrategyState, config: StrategyConfig
) -> tuple[StrategyDecision, StrategyState]:
    """Row E (EVENT-TRIGGER): NO admission (a trigger frame never trains accumulators); NO_QUOTE +
    an event cooldown anchored at this frame. Two trigger families reach here (row R pre-empts both):
    ENTRY into a ``gap`` / ``excluded`` book (E2-T4; RED-53), and the E4-T2 warm-reference venue
    events (depth-vanish / spread-blowout / mid-jump / level-count floor; REQ-080). ``base`` admitted
    nothing on a row-E frame, so its venue accumulators are the PRIOR-state values
    :func:`_venue_event_reason` recomputes the trigger reason from."""
    if observation.book_status == "gap":
        reason: ReasonCode = "book_gap"
    elif observation.book_status == "excluded":
        reason = "book_excluded"
    else:
        # A warm-reference venue event — row E was classified, so a reason is present; the ``or``
        # fallback is a mypy-total guard (never taken) that keeps the reason a closed ReasonCode.
        reason = _venue_event_reason(observation, base, config) or "book_thin"
    next_state = base.model_copy(
        update={"event_cooldown_until_ts": _cooldown_deadline(observation, config)}
    )
    return _decide("NO_QUOTE", (reason,)), next_state


def _apply_data_degraded(
    observation: StrategyObservation, base: StrategyState, config: StrategyConfig
) -> tuple[StrategyDecision, StrategyState]:
    """Row D (DATA-DEGRADED): stale facts never train accumulators and NO cooldown is created — the
    watermark-advanced ``base`` (cooldown untouched) is the next state; NO_QUOTE with the REQ-022
    reason(s) in closed-vocabulary order (``book_stale`` before ``leg_skew``)."""
    reasons: list[ReasonCode] = []
    if _book_is_stale(observation, config):
        reasons.append("book_stale")
    if _leg_is_skewed(observation, config):
        reasons.append("leg_skew")
    return _decide("NO_QUOTE", tuple(reasons)), base


def _apply_cooldown(base: StrategyState) -> tuple[StrategyDecision, StrategyState]:
    """Row C (COOLDOWN-active): NO admission; NO_QUOTE ``event_cooldown``; the cooldown is left
    UNCHANGED (row C never re-anchors it) — the watermark-advanced ``base`` is the next state."""
    return _decide("NO_QUOTE", ("event_cooldown",)), base


# --- The row-H quote-policy spine: venue-mid anchor + zones (E4-T1; REQ-050..054/060/082) ---
# Everything below is the HEALTHY-row disposition — the ordered precedence spine the rest of E4
# slots into. The upstream reducer already resolves data-validity / event / cooldown / warmup
# (rows S/R/E/D/C/W/F) and the QUOTE-ONLY status+stream blocker is applied by the caller BEFORE
# this spine, so here the frame is venue-healthy, in-window, warm, and status/stream-clean. The
# spine order is REQ-060 (highest first): anchor-validity / boundary zone → INVENTORY (E4-T6 slot)
# → two-sided band → GUARD (E4-T3/T4/T5 slot) → QUOTE math (E4-T7 slot) → QUOTE_TWO_SIDED.


def _venue_anchor(
    observation: StrategyObservation, config: StrategyConfig
) -> float | None:
    """The venue-MID anchor ``(bid + ask) / 2`` — the ONLY anchor mode (``anchor_mode == "mid"``;
    REQ-050/051). ``None`` (no anchor → NO_QUOTE class) unless ALL admissibility floors hold:

    * ``book_status == "ok"`` (two-sided, non-crossed — a ``gap`` / ``excluded`` book is row E and
      never reaches here, but the gate is defended locally too);
    * BOTH top touches present, so the mid is computed from real two-sided liquidity — an absent
      side yields ``None`` and the mid is NEVER imputed from the single present side (REQ-023);
    * the absolute depth floor ``min_top_depth`` holds (a thin two-sided book passes ``ok`` while
      its mid is fiction — REQ-082);
    * the in-band level count meets ``min_level_count``.

    The guard FV leg is NEVER read — raw TxLINE FV can never center a quote (RED-01/AC-004). There
    is NO alternate anchor branch (no Stoikov cross-weighted size mode, no EMA/median smoothed-mid
    mode): the venue mid is the SOLE v0 anchor mode (REQ-051). The absence of a config branch here
    is load-bearing — adding one is a spec revision, never task discretion.
    """
    if observation.book_status != "ok":
        return None
    mid = _mid(observation)
    depth = _top_depth(observation)
    if mid is None or depth is None:
        return None
    # Both floors below are already pre-empted by row E (REQ-080): _venue_event_reason pre-empts to
    # row E whenever depth < min_top_depth or level_count_in_band < min_level_count on a warm frame,
    # so a row-H frame never reaches here sub-floor. Retained as local anchor-honesty defense-in-depth
    # in case the row-E pre-emption changes.
    if depth < config.min_top_depth:
        return None
    if observation.level_count_in_band < config.min_level_count:
        return None
    return mid


def _no_anchor_reason(
    observation: StrategyObservation, config: StrategyConfig
) -> ReasonCode:
    """The closed-vocabulary reason for an inadmissible anchor, mirroring :func:`_venue_anchor`'s
    gate order so the FIRST failed floor names the block. An absent side, a non-``ok`` book, or a
    sub-floor depth is ``book_thin`` (insufficient real two-sided liquidity to anchor — the mid is
    never imputed); a two-sided book below the in-band level floor is ``level_count_low``."""
    depth = _top_depth(observation)
    if (
        observation.book_status != "ok"
        or _mid(observation) is None
        or depth is None
        or depth < config.min_top_depth
    ):
        return "book_thin"
    # Already pre-empted by row E (REQ-080): a warm sub-floor level count surfaces as level_count_low
    # via _venue_event_reason before row H, so this return is unreachable at row H. Retained as
    # defense-in-depth (mirrors _venue_anchor's gate order) if the row-E pre-emption changes.
    return "level_count_low"


def _basis_is_warm(state: StrategyState, config: StrategyConfig) -> bool:
    """REQ-032 basis warmup: at least ``basis_min_samples`` ACCEPTED basis samples have folded since
    the last basis reset. Reads the explicit ``basis_sample_count`` so warmup is HONEST for BOTH
    estimators — the ``halflife_ewma`` accumulator collapses its history into one scalar and cannot
    be counted from ``basis_samples``. Below this floor the residual guard is inert (``basis_warmup``)
    while the venue-only core still governs the quote (REQ-032)."""
    return state.basis_sample_count >= config.basis_min_samples


def _reducing_leg(net_position: float) -> Literal["ask", "bid"]:
    """The inventory-REDUCING leg for a non-flat net position (REQ-054/083). The reducing leg is the
    one that moves the net toward zero: a net LONG YES (``> 0``) reduces by SELLING YES — the ASK leg
    — and a net SHORT YES (``< 0``) reduces by BUYING YES — the BID leg. The caller guarantees
    ``net_position != 0`` (the flat case abstains, never reduces)."""
    return "ask" if net_position > 0 else "bid"


def _guard_residual(
    observation: StrategyObservation,
    base: StrategyState,
    config: StrategyConfig,
    anchor: float,
) -> float | None:
    """The E4-T4 guard residual ``(fv − anchor) − basis`` for the ``reduce_conflict`` coupling, or
    ``None`` when the guard exposes NO directional signal to compare against.

    Returns ``None`` — no toxic side — whenever the guard cannot speak: the guard is disabled, the FV
    leg is missing / stale / suspended (:func:`_guard_fv_block_reason`), or the basis is still warming
    (:func:`_basis_is_warm`). This mirrors the conditions under which the guard block itself computes
    a residual, and reads the SAME PRIOR-state basis (compare-then-update). It is DELIBERATELY the
    inventory rule's own read rather than a refactor of the guard block: the inventory slot pre-empts
    the guard (REQ-083 precedence), so it can never rely on the guard block having run. A guard that
    cannot speak yields no conflict, so the reduce proceeds — the fail direction here REDUCES exposure.
    """
    if not config.guard_enabled:
        return None
    if _guard_fv_block_reason(observation, config) is not None:
        return None
    if not _basis_is_warm(base, config):
        return None
    guard_fv = observation.guard_fv
    if guard_fv is None:  # unreachable once fv_block is None — narrows for mypy totality
        return None
    return (guard_fv.fv - anchor) - basis_from_state(base, config)


def _residual_side(
    residual: float, config: StrategyConfig
) -> Literal["ask", "bid"] | None:
    """The SOLE residual sign→side rule (REQ-073/071 — the worst-bug MAJOR-5 direction), shared by the
    inventory ``reduce_conflict`` detection (:func:`_toxic_leg`) and the guard's directional side-pull
    (:func:`_quote_disposition`) so the ``±residual_band`` threshold has ONE definition. A residual
    ABOVE ``+residual_band`` means fair value sits above the anchor → the ASK is adverse
    (``residual_pull_ask``); BELOW ``−residual_band`` the BID is adverse (``residual_pull_bid``). The
    band is the ABSOLUTE config width, NEVER scaled by ``(best_ask − best_bid)`` (REQ-071). A quiescent
    residual (``|residual| <= residual_band``) selects no side (``None``)."""
    if residual > config.residual_band:
        return "ask"
    if residual < -config.residual_band:
        return "bid"
    return None


def _toxic_leg(
    observation: StrategyObservation,
    base: StrategyState,
    config: StrategyConfig,
    anchor: float,
) -> Literal["ask", "bid"] | None:
    """The leg the E4-T4 guard would PULL as adverse (REQ-073), or ``None`` when quiescent / no guard
    signal. Applies the SAME sign + ABSOLUTE ``residual_band`` rule as the directional side-pull via the
    shared :func:`_residual_side`, so a residual ABOVE ``+residual_band`` yields the adverse ASK and
    BELOW ``−residual_band`` the adverse BID; a quiescent residual pulls nothing. Read here so the
    inventory rule can detect a reducing leg that is ALSO the toxic leg (``reduce_conflict``) WITHOUT
    reshaping the guard block below (REQ-071 — never spread-relative)."""
    residual = _guard_residual(observation, base, config, anchor)
    if residual is None:
        return None
    return _residual_side(residual, config)


# --- The E4-T7 QUOTE-math primitives: join-or-behind targets + directional round + post-clamp ---
# The deterministic leg-price materialization the row-H quoting classes all resolve through (REQ-052/
# 055/056). Join-or-behind (the ``min``/``max``) never posts BETTER than the current best — improving
# is impossible, so a resting maker quote never crosses. The directional tick round is the maker-safety
# invariant: a BID floors DOWN and an ASK ceils UP (never a side-agnostic round-to-nearest — the
# rust_mm_bot/smm anti-pattern). The post-clamp omits any final leg outside the native ``(0,1)`` unit
# interval, the boundary zone, or the tick grid (``leg_out_of_zone``). ``tick`` / ``best_bid`` /
# ``best_ask`` all come from RAW observation facts; an admissible anchor (:func:`_venue_anchor`)
# guarantees both touches are present, so the callers narrow ``bid`` / ``ask`` off ``None`` locally.


def _bid_leg_price(
    anchor: float, best_bid: float, tick: float, config: StrategyConfig
) -> float:
    """The join-or-behind BID leg price (REQ-052/055): ``floor_to_tick(min(anchor − half_spread,
    best_bid))``. The ``min`` is join-or-behind — NEVER post a bid better (higher) than the current
    best bid (join it or rest behind it; improving is impossible). The ``floor`` is the maker-safe
    directional round (DOWN, never up THROUGH the target) — the side is chosen by role, never
    defaulted (a round-to-nearest is the rust_mm_bot/smm anti-pattern; REQ-055)."""
    return floor_to_tick(min(anchor - config.half_spread, best_bid), tick)


def _ask_leg_price(
    anchor: float, best_ask: float, tick: float, config: StrategyConfig
) -> float:
    """The join-or-behind ASK leg price (REQ-052/055): ``ceil_to_tick(max(anchor + half_spread,
    best_ask))`` — the mirror of :func:`_bid_leg_price`. The ``max`` rests at or behind the best ask
    (never lower — improving down is impossible); the ``ceil`` is the maker-safe directional round
    (UP). BID→floor / ASK→ceil is role-driven; a defaulted side is a REQ-055 violation."""
    return ceil_to_tick(max(anchor + config.half_spread, best_ask), tick)


def _on_tick(price: float, tick: float) -> bool:
    """True when ``price`` sits on the current tick grid (REQ-052 post-clamp). The join-or-behind
    legs are on-tick BY CONSTRUCTION (:func:`_bid_leg_price` / :func:`_ask_leg_price` round to the
    grid), so this is a defensive re-check whose small tolerance absorbs binary-representation dust —
    it never fires on a rounded leg. The R4-A wire layer remains the authoritative tick check."""
    ratio = price / tick
    return abs(ratio - round(ratio)) <= 1e-9


def _leg_in_zone(price: float, tick: float, config: StrategyConfig) -> bool:
    """REQ-052 post-clamp: a final leg is admissible ONLY if it is strictly inside the native
    ``(0, 1)`` unit interval AND inside the quoting ``boundary_zone`` AND on the tick grid; otherwise
    the leg is OMITTED (``leg_out_of_zone``). Zone application is pinned per REQ-052 — the boundary
    zone gates the anchor AND the final targets (this predicate), while the two-sided band gates the
    anchor ONLY (upstream in :func:`_quote_disposition`)."""
    boundary_low, boundary_high = config.boundary_zone
    return (
        0.0 < price < 1.0
        and boundary_low <= price <= boundary_high
        and _on_tick(price, tick)
    )


def _place_leg(
    leg_role: Literal["bid", "ask"], price: float
) -> NeutralIntent:
    """A maker ``place_quote`` intent for one priced leg (REQ-056). ``post_only=True`` ALWAYS — a
    maker quote is rejected if marketable, so it never crosses. The ``tif`` is the config-level
    time-in-force the R4-A adapter applies when it maps this neutral intent onto the wire; the neutral
    intent carries NO ``tif`` (and no size) field — decision identity is over side + price only
    (REQ-057). ``leg_role`` is the physical side, authoritative for both quoting and reducing legs."""
    return NeutralIntent(kind="place_quote", leg_role=leg_role, price=price, post_only=True)


def _inventory_reduce(
    observation: StrategyObservation,
    base: StrategyState,
    config: StrategyConfig,
    anchor: float,
    net_position: float,
) -> StrategyDecision:
    """The inventory-reducing one-sided disposition for a non-flat net position (REQ-054/083; the
    caller guarantees ``net_position != 0``). Quotes ONLY the reducing leg (:func:`_reducing_leg`) —
    UNLESS that leg is ALSO the guard's toxic leg (:func:`_toxic_leg`), in which case quoting to
    reduce would quote INTO the adverse flow, so it fails closed to ``NO_QUOTE(reduce_conflict)`` +
    cancel (the cancel intent is E5-T3's; none is wired here).

    E4-T6 owns REQ-054 (WHICH side reduces — the ``leg_role`` is AUTHORITATIVE and never recomputed
    here); E4-T7 PRICES that leg (join-or-behind + directional round for ITS side). The reducing leg
    materialises as a size-free ``place_quote`` intent (NO size — R4-A owns sizing; REQ-057).
    INVENTORY SAFETY (REQ-052): if the reducing leg's computed price clamps OUT of zone we fail to
    ``NO_QUOTE(leg_out_of_zone)`` — we NEVER side-flip to the opposite (adding) leg to salvage a
    quote, since quoting the adding side would GROW the very position we are trying to reduce."""
    reducing = _reducing_leg(net_position)
    if reducing == _toxic_leg(observation, base, config, anchor):
        return _decide("NO_QUOTE", ("reduce_conflict",))
    best_bid = observation.bid
    best_ask = observation.ask
    if best_bid is None or best_ask is None:  # unreachable: admissible anchor ⇒ both touches present
        return _decide("NO_QUOTE", (_no_anchor_reason(observation, config),))
    tick = observation.tick_size
    price = (
        _bid_leg_price(anchor, best_bid, tick, config)
        if reducing == "bid"
        else _ask_leg_price(anchor, best_ask, tick, config)
    )
    if not _leg_in_zone(price, tick, config):
        return _decide("NO_QUOTE", ("leg_out_of_zone",))
    return StrategyDecision(
        kind="ONE_SIDED_REDUCE",
        reason_codes=("inventory_reduce",),
        intent_plan=(_place_leg(reducing, price),),
    )


def _two_sided_quote(
    observation: StrategyObservation, config: StrategyConfig, anchor: float
) -> StrategyDecision:
    """The E4-T7 QUOTE-math terminal (slot #6) over an admissible in-band anchor (REQ-052/055/056).

    Build BOTH legs (join-or-behind targets + directional tick rounding: BID floors, ASK ceils),
    post-clamp each against the native ``(0, 1)`` interval + boundary zone + tick grid, then resolve
    cardinality:

    * 2 legs survive → ``QUOTE_TWO_SIDED``;
    * exactly 1 → ``QUOTE_ONE_SIDED`` on the surviving side (the other omitted as ``leg_out_of_zone``,
      the recorded reason for the downgrade);
    * 0 → ``NO_QUOTE(leg_out_of_zone)``.

    An admissible anchor guarantees both touches are present (:func:`_venue_anchor`), so
    ``observation.bid`` / ``observation.ask`` are non-``None`` here — the guard narrows them for mypy
    and is unreachable (it re-derives the no-anchor reason rather than fabricating a leg)."""
    best_bid = observation.bid
    best_ask = observation.ask
    if best_bid is None or best_ask is None:  # unreachable: admissible anchor ⇒ both touches present
        return _decide("NO_QUOTE", (_no_anchor_reason(observation, config),))
    tick = observation.tick_size
    bid_price = _bid_leg_price(anchor, best_bid, tick, config)
    ask_price = _ask_leg_price(anchor, best_ask, tick, config)
    legs: list[NeutralIntent] = []
    if _leg_in_zone(bid_price, tick, config):
        legs.append(_place_leg("bid", bid_price))
    if _leg_in_zone(ask_price, tick, config):
        legs.append(_place_leg("ask", ask_price))
    if len(legs) == 2:
        return StrategyDecision(kind="QUOTE_TWO_SIDED", reason_codes=(), intent_plan=tuple(legs))
    if len(legs) == 1:
        return StrategyDecision(
            kind="QUOTE_ONE_SIDED", reason_codes=("leg_out_of_zone",), intent_plan=tuple(legs)
        )
    return _decide("NO_QUOTE", ("leg_out_of_zone",))


def _residual_pull_quote(
    observation: StrategyObservation,
    config: StrategyConfig,
    anchor: float,
    toxic: Literal["ask", "bid"],
) -> StrategyDecision:
    """The E4-T4 directional side-pull as a PRICED quote class (REQ-073 + E4-T7 materialization).

    The guard PULLS the adverse (``toxic``) leg and RESTS the OPPOSITE (surviving) leg; this materialises
    that surviving leg's price through the SAME join-or-behind + directional-round + post-clamp authority
    the two-sided quote and inventory reduce use (:func:`_bid_leg_price` / :func:`_ask_leg_price` /
    :func:`_leg_in_zone`) — never a re-implementation, so the ``residual_band`` middle-band pull emits a
    real ``place_quote`` leg an EMPTY plan could not (the MAJOR-4/A1 defect: E5 cannot recover a price E4
    never materialised). A POSITIVE residual pulls the ASK ⇒ the BID rests (``residual_pull_ask``, floored
    DOWN); a NEGATIVE residual pulls the BID ⇒ the ASK rests (``residual_pull_bid``, ceiled UP) — the
    side is role-driven, never defaulted (REQ-055). If the surviving leg clamps OUT of zone we fail closed
    to ``NO_QUOTE(leg_out_of_zone)`` and NEVER side-flip to the toxic leg to salvage a quote — quoting the
    adverse side is exactly the flow the pull exists to avoid (the same never-flip rule
    :func:`_inventory_reduce` applies to its reducing leg). An admissible anchor guarantees both touches
    are present (:func:`_venue_anchor`), so the ``None`` guard is unreachable and narrows for mypy."""
    best_bid = observation.bid
    best_ask = observation.ask
    if best_bid is None or best_ask is None:  # unreachable: admissible anchor ⇒ both touches present
        return _decide("NO_QUOTE", (_no_anchor_reason(observation, config),))
    tick = observation.tick_size
    if toxic == "ask":
        resting: Literal["bid", "ask"] = "bid"
        reason: ReasonCode = "residual_pull_ask"
        price = _bid_leg_price(anchor, best_bid, tick, config)
    else:
        resting = "ask"
        reason = "residual_pull_bid"
        price = _ask_leg_price(anchor, best_ask, tick, config)
    if not _leg_in_zone(price, tick, config):
        return _decide("NO_QUOTE", ("leg_out_of_zone",))
    return StrategyDecision(
        kind="QUOTE_ONE_SIDED",
        reason_codes=(reason,),
        intent_plan=(_place_leg(resting, price),),
    )


def _quote_disposition(
    observation: StrategyObservation, base: StrategyState, config: StrategyConfig
) -> StrategyDecision:
    """The row-H quote disposition over the venue-mid anchor (REQ-050..054/060/082).

    Precedence spine (later E4 tasks fill the marked slots WITHOUT reshaping this order):

    1. anchor validity — no admissible venue mid → NO_QUOTE class (:func:`_no_anchor_reason`).
    2. boundary zone — an anchor outside ``config.boundary_zone`` → NO_QUOTE ``boundary_zone``.
    3. **INVENTORY slot (E4-T6):** ``|net_position| ≥ inventory_soft_limit`` → ``ONE_SIDED_REDUCE``
       (:func:`_inventory_reduce`, the inventory-reducing leg) — PRE-EMPTS the two-sided band and the
       guard; a reducing leg that is also the guard's toxic leg fails closed (``reduce_conflict``).
    4. two-sided band — an anchor outside ``config.two_sided_band`` (but inside the boundary) is at
       most one-sided (REQ-054): net-flat is the pinned ``two_sided_zone_exit`` abstention; a non-flat
       net quotes the reducing leg (:func:`_inventory_reduce`, same ``reduce_conflict`` fail-close).
    5. **GUARD slot (E4-T3/T4/T5):** the TxLINE side guard → ``QUOTE_ONE_SIDED`` / escalation.
    6. **QUOTE math slot (E4-T7):** ``anchor ± half_spread`` legs, join-or-behind, directional tick
       rounding, post-clamp boundary/cardinality — which can DOWNGRADE the class below. It also fills
       the ``ONE_SIDED_REDUCE`` reducing leg's ``price`` (E4-T6 emits it with ``price=None``).

    Until the guard/quote-math slots are filled a fully-admissible in-band anchor is the eligible
    ``QUOTE_TWO_SIDED`` CLASS (the taxonomy floor; REQ-060).
    """
    anchor = _venue_anchor(observation, config)
    if anchor is None:
        return _decide("NO_QUOTE", (_no_anchor_reason(observation, config),))
    boundary_low, boundary_high = config.boundary_zone
    if not (boundary_low <= anchor <= boundary_high):
        return _decide("NO_QUOTE", ("boundary_zone",))
    # --- INVENTORY slot (E4-T6): |net_position| >= inventory_soft_limit -> ONE_SIDED_REDUCE ---
    # Inventory PRE-EMPTS the two-sided band AND the guard (REQ-083 precedence: data-validity ->
    # NO_QUOTE class -> INVENTORY -> guard -> quote): over the soft limit we quote ONLY the reducing
    # leg regardless of anchor position (even squarely inside the two-sided band). ``_inventory_reduce``
    # reaches DOWN into the guard's toxic side (residual sign) for the ``reduce_conflict`` fail-close.
    net_position = observation.inventory.net_position
    if abs(net_position) >= config.inventory_soft_limit:
        return _inventory_reduce(observation, base, config, anchor, net_position)
    two_sided_low, two_sided_high = config.two_sided_band
    if not (two_sided_low <= anchor <= two_sided_high):
        # Outside the two-sided band, inside the boundary: at most one-sided (REQ-054). This EXTENDS
        # E4-T1's net-flat-only abstention — ``net_position != 0`` quotes the reducing leg (subject to
        # the same ``reduce_conflict`` fail-close), while net-flat keeps the pinned abstention.
        if net_position != 0.0:
            return _inventory_reduce(observation, base, config, anchor, net_position)
        return _decide("NO_QUOTE", ("two_sided_zone_exit",))
    # --- GUARD slot (E4-T3): TxLINE basis/residual guard -> abstain / (E4-T4/T5) escalate ---
    # Guard-scoped (REQ-070 guard block / REQ-074..076): the baseline arm (guard OFF) never enters
    # here, so a stale/missing FV can never make it abstain (Fable F1). The pinned order is FV
    # freshness/suspension/presence (REQ-022/076) -> basis warmup (REQ-032) -> extreme residual
    # (REQ-075). The event gate strictly PRECEDES this whole block: a row-C cooldown (or row-R/E) is
    # resolved upstream in the reducer and never reaches the row-H disposition (REQ-074).
    if config.guard_enabled:
        fv_block = _guard_fv_block_reason(observation, config)
        if fv_block is not None:
            return _decide("NO_QUOTE", (fv_block,))
        if not _basis_is_warm(base, config):
            return _decide("NO_QUOTE", ("basis_warmup",))
        # E4-T5 PRE-MATCH BASIS GATE (REQ-078/AC-054): pre-match ONLY (``phase == 0``), with the
        # basis warm, a persistent basis WIDER than the venue's OWN top-of-book spread
        # (``|basis| >= best_ask − best_bid``) means the pre-match edge estimate is unreliable → fail
        # closed. This comparison is DELIBERATELY spread-relative and is CORRECT/intentional
        # (REQ-078). It is a DISTINCT quantity from the residual band below it: that band is the
        # ABSOLUTE ``residual_band`` config width, NEVER scaled by ``(best_ask − best_bid)`` (REQ-071,
        # the MAJOR-5 bug) — basis-vs-spread HERE, residual-vs-absolute-band THERE. Precedence is
        # load-bearing: this gate PRECEDES the residual wall, so a pre-match block is recorded as
        # ``prematch_basis_exceeds_spread`` and is NEVER mislabeled ``residual_extreme`` (RED-50). The
        # basis reads PRIOR state (compare-then-update). Post-match frames (``phase != 0``) skip this
        # gate entirely. ``_spread`` is non-``None`` here (an admissible anchor implies both touches
        # present); the guard keeps the branch total for mypy.
        prematch_spread = _spread(observation)
        if (
            observation.phase == 0
            and prematch_spread is not None
            and abs(basis_from_state(base, config)) >= prematch_spread
        ):
            return _decide("NO_QUOTE", ("prematch_basis_exceeds_spread",))
        # residual = raw_gap − basis, with raw_gap = fv − anchor (the venue mid; REQ-070), computed by
        # :func:`_guard_residual` — the SAME formula the inventory ``reduce_conflict`` reads, so the
        # load-bearing (MAJOR-5) residual definition lives in ONE place. The gates above (guard on, FV
        # fresh/present, basis warm) already passed — the SAME conditions the helper re-checks — so it
        # returns a value here and never the ``None`` path; ``residual is not None`` narrows for mypy
        # totality (it was ``guard_fv is not None`` before the collapse). Basis reads PRIOR state.
        residual = _guard_residual(observation, base, config, anchor)
        if residual is not None:
            # The extreme band is the ABSOLUTE config value: ``|residual|`` vs ``extreme_multiple ×
            # residual_band``, taken DIRECTLY from config and NEVER scaled by ``(best_ask − best_bid)``
            # (REQ-071 — a spread-relative band is the MAJOR-5 bug).
            if abs(residual) >= config.extreme_multiple * config.residual_band:
                # REQ-075: the sole extreme rule (no separate absolute-cap knob). NO_QUOTE + cancel
                # plan on exposure (the intent-plan wiring is a later E4 task) — never a taker chase.
                return _decide("NO_QUOTE", ("residual_extreme",))
            # E4-T4 DIRECTIONAL SIDE PULL (REQ-073/071/AC-006), INSIDE the admissible band, via the
            # shared :func:`_residual_side` (the SAME sign rule the inventory ``reduce_conflict`` uses).
            # The SIGN of the residual selects the ONE side to pull; the threshold is the ABSOLUTE
            # ``residual_band`` (REQ-071 — the SAME config width the extreme wall scales, NEVER the
            # ``(best_ask − best_bid)`` spread; a spread-relative band is the MAJOR-5 bug). The
            # direction is load-bearing (the worst-bug rule): ``residual = fv − anchor − basis`` with
            # the anchor the venue mid, so a POSITIVE residual means fair value is ABOVE the anchor —
            # the venue's YES ask is too cheap relative to fv, a taker will lift our resting ask
            # adversely, so we PULL THE ASK (the bid may rest → QUOTE_ONE_SIDED). Symmetric: a NEGATIVE
            # residual means fv is BELOW the anchor — our YES bid is too high, so we PULL THE BID. A
            # flipped comparison would quote INTO the adverse flow. No widen path (v0); the pull is on
            # THIS outcome's own book (no naive YES-vs-NO cross-compare). ``residual_band`` (0.02) <
            # ``|residual|`` < extreme (0.06): the middle band; ``|residual| <= residual_band`` is
            # quiescent and falls through to the two-sided taxonomy floor (E4-T5's pre-match basis gate
            # REQ-078 slots between warmup and the extreme wall, WITHOUT reshaping this order).
            side = _residual_side(residual, config)
            if side is not None:
                # E4-T7 materialisation: PULL the toxic ``side`` and price the RESTING (surviving) leg
                # through the shared join-or-behind + directional-round + post-clamp authority. An
                # out-of-zone survivor fails closed to NO_QUOTE(leg_out_of_zone), NEVER a toxic-side flip
                # (MAJOR-4/A1 — an empty ``intent_plan`` cannot place the permitted surviving leg).
                return _residual_pull_quote(observation, config, anchor, side)
    # --- QUOTE math slot (E4-T7): join-or-behind anchor +/- half_spread legs, directional tick
    # rounding, post-clamp + cardinality (REQ-052/055/056). This terminal can DOWNGRADE the eligible
    # two-sided class: both legs clamped out -> NO_QUOTE(leg_out_of_zone), exactly one survives ->
    # QUOTE_ONE_SIDED on that side, both survive -> QUOTE_TWO_SIDED with the priced legs.
    return _two_sided_quote(observation, config, anchor)


def _target_is_unchanged(
    decision: StrategyDecision, observation: StrategyObservation, config: StrategyConfig
) -> bool:
    """Churn suppression (REQ-096 / AC-020 / RED-17): True when the DESIRED priced quote legs already
    match the reconciled resting orders, so re-emitting them would be needless cancel-replace churn.

    The compare is over SIDE + PRICE ONLY — NEVER size (REQ-057/096: a neutral intent carries no
    size and ``resolve_dust_size`` is the sole wire-size authority, so a size drift is NOT a target
    change). A desired leg's side is its physical ``leg_role`` (``bid`` / ``ask`` — authoritative for
    both quoting and reducing legs, :func:`_place_leg`); the reconciled side is the orchestration-layer
    ``RestingOrderView.side`` on the observation's ``InventoryProjection`` (REQ-020(g)), supplied in the
    SAME vocabulary — the reconciled resting state, never a fabricated baseline. Two dispositions are
    "unchanged" IFF there is a one-to-one pairing of desired legs to resting orders on the same side
    whose prices agree within ``config.price_epsilon`` (the ADOPTED Gridora recenter-buffer threshold,
    from the band/anchor edge) AND no leg is added or removed (equal cardinality — the loop consumes one
    resting order per desired leg, so an exhausted pairing leaves none on either side).

    An EMPTY desired plan is never "unchanged" here: a NO_QUOTE that leaves exposure resting is a
    cancel case (E5-T3), not a HOLD, so it returns ``False`` and the disposition stands untouched.
    """
    desired = decision.intent_plan
    if not desired:
        return False
    resting = observation.inventory.resting
    if len(desired) != len(resting):
        return False
    unmatched = list(resting)
    for leg in desired:
        if leg.price is None:  # a placement leg always carries a price; guards for mypy totality
            return False
        for index, order in enumerate(unmatched):
            if (
                order.side == leg.leg_role
                and abs(leg.price - order.price) <= config.price_epsilon
            ):
                unmatched.pop(index)
                break
        else:
            return False
    return True


def _apply_healthy(
    observation: StrategyObservation,
    base: StrategyState,
    config: StrategyConfig,
    *,
    row: Row,
) -> tuple[StrategyDecision, StrategyState]:
    """Rows F / W / H — the ADMITTING tier. Venue accumulators train (both arms, universal gates);
    the guarded basis trains (row F = clear-then-admit via the pre-cleared ``base``); any elapsed
    cooldown clears to ``None`` (WARMUP never anchors one — the liveness E2-T5 proves). Quote:

    * F (fv-epoch): guard inert until the basis re-warms → NO_QUOTE ``basis_warmup``.
    * W (warmup): references below floor → NO_QUOTE ``event_ref_warmup``.
    * H (healthy): a QUOTE-ONLY blocker downgrades to ``NO_QUOTE`` first (admission unaffected) — a
      missing/ambiguous outcome token (:func:`_token_mapping_reason`) PRE-EMPTS the status/stream
      gate (its §4.4 vocabulary precedence: without a resolved venue token no order can be placed at
      all); otherwise the venue-mid anchor + zone spine (:func:`_quote_disposition`) resolves the
      eligible quote class (E4-T1; the guard + quote-math slots refine it in later E4 tasks). A
      resolved disposition whose priced legs already match the reconciled resting orders (side +
      price within ``price_epsilon``) SHORT-CIRCUITS to ``HOLD`` (``hold_unchanged``) with zero
      intents — churn suppression (:func:`_target_is_unchanged`; REQ-096 / AC-020).
    """
    updates = _admit_venue_updates(observation, base, config)
    updates.update(_admit_basis_updates(observation, base, config))
    updates["event_cooldown_until_ts"] = None
    next_state = base.model_copy(update=updates)

    if row == "F":
        return _decide("NO_QUOTE", ("basis_warmup",)), next_state
    if row == "W":
        return _decide("NO_QUOTE", ("event_ref_warmup",)), next_state
    blocker = _token_mapping_reason(observation) or _status_stream_reason(
        observation, base, config
    )
    if blocker is not None:
        return _decide("NO_QUOTE", (blocker,)), next_state
    disposition = _quote_disposition(observation, base, config)
    # Churn suppression (REQ-096 / AC-020): the DESIRED priced legs already resting (side + price
    # within `price_epsilon`) ⇒ re-emitting them is needless cancel-replace churn, so HOLD and emit
    # nothing. State training already folded above (row H admits), so a later genuinely-changed
    # target is not starved of warm references; only the OUTPUT churn is suppressed.
    if _target_is_unchanged(disposition, observation, config):
        return _decide("HOLD", ("hold_unchanged",)), next_state
    return disposition, next_state


# --- The E5-T3 cancel-intent funnel + single-phase invariant (REQ-090/092; AC-016/018/019) -------
# R4-A has NO bare single-order cancel: the honest way to clear stale exposure is a cancel-ALL sweep
# plus a requote on the NEXT decision. So a NO_QUOTE that leaves resting orders on the book is not a
# passive abstention — it must actively withdraw that exposure. This is the ONE place every NO_QUOTE
# cause (HALTED/CLOSED, reset/event trigger, extreme residual, reduce_conflict, boundary/band/stream/
# warmup/token-mapping block) is funneled into the SAME cancel plan, so no cause needs to know about
# cancellation. The single-phase invariant (REQ-092 / global constraint 17) is preserved BY
# CONSTRUCTION: only a NO_QUOTE (whose ``intent_plan`` is always empty) is rewritten, and it is
# rewritten to a cancel-ONLY plan — a placement disposition (QUOTE_*/ONE_SIDED_REDUCE) is never
# touched, so cancel-phase legs and placement-phase legs can never coexist in one plan.


def _cancel_exposure_plan() -> tuple[NeutralIntent, NeutralIntent]:
    """The single-phase cancel plan for a NO_QUOTE that leaves resting exposure (REQ-090/092).

    ``cancel_all_orders`` THEN ``abstain``, in that order — the cancel-ALL sweep withdraws every
    resting order (R4-A exposes no single-order cancel), and the terminal ``abstain`` marks that this
    decision proposes NO new placement (the requote, if any, is the NEXT decision). Both are
    placement-free (``price=None``) and carry NO physical ``leg_role``: a cancel-all names no single
    order, so it takes no per-leg ``client_order_id`` from :func:`_stamp_identity` (which stamps only
    ``leg_role``-bearing legs). This is CANCEL-PHASE ONLY — it holds no ``place_quote`` leg, so a plan
    built from it can never mix the two phases (REQ-092)."""
    return (
        NeutralIntent(kind="cancel_all_orders", leg_role=None),
        NeutralIntent(kind="abstain", leg_role=None),
    )


def _funnel_cancel_on_exposure(
    decision: StrategyDecision, observation: StrategyObservation
) -> StrategyDecision:
    """Compile a raw decision's ``intent_plan`` under the E5-T3 cancel funnel (REQ-090/092).

    A NO_QUOTE that leaves resting exposure (the observation's ``InventoryProjection`` holds any
    ``RestingOrderView``) is rewritten to the single-phase cancel plan (:func:`_cancel_exposure_plan`)
    with ``cancel_exposure_first`` APPENDED to the truthful cause codes — the underlying reason
    (``market_halted`` / ``boundary_zone`` / …) is preserved, never replaced, so provenance still
    names WHY quoting stopped. A NO_QUOTE with NO resting exposure is left untouched (zero intents —
    ``no_quote`` alone is never a cancel), and every placement disposition (QUOTE_*/ONE_SIDED_REDUCE)
    and the churn-suppression HOLD pass through unchanged: only a NO_QUOTE is ever rewritten, and only
    ever to a cancel-ONLY plan, so the single-phase invariant is preserved by construction (REQ-092).
    Pure over ``(decision.kind, decision.reason_codes, observation.inventory.resting)`` — no clock, no
    rng — and it reads no guard/FV field, so the E3-T4 guard-off byte-identity is unaffected."""
    if decision.kind != "NO_QUOTE" or not observation.inventory.resting:
        return decision
    return decision.model_copy(
        update={
            "reason_codes": decision.reason_codes + ("cancel_exposure_first",),
            "intent_plan": _cancel_exposure_plan(),
        }
    )


def _stamp_identity(
    decision: StrategyDecision,
    observation: StrategyObservation,
    prior_state: StrategyState,
    next_state: StrategyState,
    config: StrategyConfig,
    session_id: str,
) -> StrategyDecision:
    """Bind the deterministic decision / client-order ids + the four causal hashes onto a raw
    decision (REQ-025/095/AC-022).

    ``decision_id = H(strategy_id, strategy_revision, config_hash, session_id, observation_hash,
    prior_state_hash)`` via the SOLE canonical serializer — ``strategy_id`` is the runtime-authoritative
    ``config.strategy_id`` (the same literal ``contracts.STRATEGY_ID`` pins), ``strategy_revision`` the
    pinned ``contracts.STRATEGY_REVISION``, and ``session_id`` the per-run seam threaded from
    :func:`decide`. It is a PURE function of the decision's causes — NO wall clock, NO rng, NO call
    counter — so an authorized retry on identical inputs reproduces a byte-identical id while a
    distinct observation (distinct ``observation_hash``) yields a distinct id (REQ-095). Because the id
    is a deterministic function of the already-identical guard-off hashes, the E3-T4 baseline-arm
    byte-identity is unaffected.

    Each intent leg's ``client_order_id = H(decision_id, leg_role)`` is stamped per leg (a leg with no
    physical role keeps its ``None``). Replacement lineage lives in the leg's ``replaces_client_order_id``
    (untouched here): the determinism of ``client_order_id`` is exactly what lets a later replacing leg
    name the EXACT old id — same decision inputs + same role reproduce the same id (REQ-095).
    """
    observation_hash = observation.observation_hash()
    config_hash = config.config_hash()
    prior_state_hash = prior_state.state_hash()
    next_state_hash = next_state.state_hash()
    decision_identity = decision_id(
        config.strategy_id,
        STRATEGY_REVISION,
        config_hash,
        session_id,
        observation_hash,
        prior_state_hash,
    )
    stamped_legs = tuple(
        leg.model_copy(
            update={"client_order_id": client_order_id(decision_identity, leg.leg_role)}
        )
        if leg.leg_role is not None
        else leg
        for leg in decision.intent_plan
    )
    return decision.model_copy(
        update={
            "decision_id": decision_identity,
            "intent_plan": stamped_legs,
            "observation_hash": observation_hash,
            "config_hash": config_hash,
            "prior_state_hash": prior_state_hash,
            "next_state_hash": next_state_hash,
        }
    )


def decide(
    observation: StrategyObservation,
    state: StrategyState,
    config: StrategyConfig,
    *,
    session_id: str = "",
) -> tuple[StrategyDecision, StrategyState]:
    """Evaluate the reducer and return an IDENTITY-STAMPED ``(decision, next_state)`` (pure, total).

    Thin wrapper over :func:`_decide_raw` (the watermark precondition layer + S/R/E/D/C/F/W/H
    reducer): it runs the raw reducer once, then :func:`_funnel_cancel_on_exposure` compiles the final
    ``intent_plan`` (a NO_QUOTE that leaves resting exposure becomes the single-phase cancel plan;
    REQ-090/092), and finally :func:`_stamp_identity` binds the deterministic ``decision_id`` + per-leg
    ``client_order_id``s + the four causal hashes onto the single returned decision — one choke point
    so EVERY branch's decision is compiled and stamped identically (REQ-025/090/092/095/AC-022).

    ``session_id`` is the per-run identity seam bound into ``decision_id``; it is keyword-only with an
    empty default so the pure fixtures / replay callers that pass only ``(observation, state, config)``
    are unaffected, while the orchestration layer supplies the real session when it wires the run loop.
    """
    decision, next_state = _decide_raw(observation, state, config)
    # E5-T3 plan compilation: funnel a NO_QUOTE-with-exposure into the single-phase cancel plan
    # BEFORE stamping, so the returned decision carries the compiled ``intent_plan`` and its identity
    # is bound over the final plan. The funnel never touches ``next_state`` or the decision's causal
    # hashes, so ``decision_id`` (computed from observation/config/prior-state) is unchanged.
    decision = _funnel_cancel_on_exposure(decision, observation)
    stamped = _stamp_identity(
        decision, observation, state, next_state, config, session_id
    )
    return stamped, next_state


def _decide_raw(
    observation: StrategyObservation,
    state: StrategyState,
    config: StrategyConfig,
) -> tuple[StrategyDecision, StrategyState]:
    """Evaluate the watermark precondition layer and return ``(decision, next_state)`` (pure, total).

    Exactly one branch applies, evaluated in this order:

    0. **Cold start / missing snapshot** (no prior book-epoch watermark) — a pure ``decide`` cannot
       tell a genuine first frame from a restart that lost its snapshot from its three inputs, so it
       fails CLOSED-safe: it seeds a fresh post-reset baseline and HOLDs, never optimistically
       quoting. The active ``fail_closed`` / ``fail_open`` SELECTION on a *detected* missing/
       mismatched snapshot is the reconstruction factory's job (REQ-121/035, E5 lane); the pure
       reproduction guarantee (a valid snapshot replays the stream) follows from determinism here.
    1. **Clock regression** (``as_of_ts < last_as_of_ts``) — HOLD ``clock_regression``, state
       unchanged, so half-life / cooldown arithmetic never sees negative elapsed time (REQ-022).
    2. **Epoch regression** (either epoch below its last-seen value) — HOLD ``epoch_regression``,
       state unchanged; NEVER a reset (REQ-033/AC-044).
    3. **book_source_epoch increment** — full REQ-033 reset + sequence RE-BASELINE. Evaluated
       BEFORE sequence-staleness (AC-040/RED-06/37).
    4. **fv_source_epoch increment** (guarded arm, book epoch unchanged) — basis-only reset; venue
       accumulators untouched (Codex-R5 MAJOR-1).
    5. **Sequence staleness** (``observation_sequence ≤`` watermark; a duplicate is stale) — HOLD
       ``stale_observation``, state unchanged, no double-advance (REQ-034).
    6. **Accepted frame → the S/R/E/D/C/F/W/H reducer** — advance the watermark (applying the
       fv-epoch basis-only clear when it fired), classify the frame into EXACTLY ONE row via
       :func:`_classify_row`, and apply that row's ``(smoother, refs, basis, cooldown, quote)``
       transition (REQ-070/081). Rows 1/2/5 above ARE spec row S (STALE → HOLD).
    """
    # (0) Cold start / missing snapshot.
    if state.last_book_source_epoch is None:
        return _hold(), _accept(
            observation, state, config, full_reset=True, basis_reset=False
        )

    # (1) Clock monotonicity vs prior state.
    if state.last_as_of_ts is not None and observation.as_of_ts < state.last_as_of_ts:
        return _hold(("clock_regression",)), state

    # (2) Epoch regression (book generation OR guard FV generation) — never a reset.
    guard_delta = _guard_epoch_delta(observation, state, config)
    if observation.book_source_epoch < state.last_book_source_epoch or guard_delta < 0:
        return _hold(("epoch_regression",)), state

    # (3) book_source_epoch INCREMENT — evaluated BEFORE sequence-staleness (load-bearing). A
    # reconnect is a REQ-033 RESET: first ``_accept(full_reset=True)`` re-baselines the epoch/
    # sequence watermark and clears accumulators, THEN the SAME row-R transition ``_classify_row``→R
    # uses (``_apply_reset``) re-seeds the smoother from this frame's own ok-book mid, anchors the
    # event cooldown at its ``as_of_ts``, and produces NO_QUOTE (Codex Gate#1 MAJOR-2; REQ-070 row R
    # / REQ-081). A bare HOLD here would leave quoting to resume with dwell still owed. The recorded
    # reason is the truthful ``event_ref_warmup`` (REQ-036), NOT ``tick_regime_changed`` — this
    # observation never signalled a tick regime change (Codex Gate#1-R2 MAJOR-2).
    if observation.book_source_epoch > state.last_book_source_epoch:
        base = _accept(observation, state, config, full_reset=True, basis_reset=False)
        return _apply_reset(observation, base, config, reason="event_ref_warmup")

    # (4) fv_source_epoch INCREMENT (guarded arm, book epoch unchanged) → basis-only reset. The
    # sequence is book-epoch-scoped, so its staleness check below still applies to this frame.
    basis_reset = guard_delta > 0

    # (5) Sequence staleness within the unchanged book epoch — HOLD, no double-advance.
    if (
        state.last_observation_sequence is not None
        and observation.observation_sequence <= state.last_observation_sequence
    ):
        return _hold(("stale_observation",)), state

    # (6) Accepted frame — every watermark passed → the S/R/E/D/C/F/W/H reducer. ``base`` advances
    # the watermark (with the fv-epoch basis-only clear) WITHOUT training accumulators; the row apply
    # then folds in that row's transition. Classification reads the PRIOR ``state`` (compare-then-
    # update); exactly one row applies, in spec order R,E,D,C,F,W,H.
    base = _accept(observation, state, config, full_reset=False, basis_reset=basis_reset)
    row = _classify_row(observation, state, config)
    if row == "R":
        return _apply_reset(observation, base, config, reason=_reset_reason(observation, state))
    if row == "E":
        return _apply_event(observation, base, config)
    if row == "D":
        return _apply_data_degraded(observation, base, config)
    if row == "C":
        return _apply_cooldown(base)
    return _apply_healthy(observation, base, config, row=row)
