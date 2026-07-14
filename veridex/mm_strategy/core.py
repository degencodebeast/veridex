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
    event_smoother_update,
    halflife_ewma,
    reference_is_warm,
)
from veridex.mm_strategy.config import StrategyConfig
from veridex.mm_strategy.contracts import (
    DecisionKind,
    GuardStateWatermark,
    MarketStatus,
    ReasonCode,
    StrategyDecision,
    StrategyObservation,
    StrategyState,
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
    }
    if full_reset:
        update.update(
            basis_samples=(),
            basis_ewma_value=None,
            basis_ewma_ts=None,
            smoother_mid=None,
            smoother_mid_ts=None,
            spread_ref_samples=(),
            depth_ref_samples=(),
        )
    elif basis_reset:
        update.update(basis_samples=(), basis_ewma_value=None, basis_ewma_ts=None)
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
# SCOPE (E2-T4): the reset/event triggers detectable from a SINGLE frame + state are wired here —
# ``tick_regime_changed`` (row R) and ``book_status ∈ {gap, excluded}`` (row E, the RED-53 canonical
# newly-entered gap book), plus data-degraded book-stale / leg-skew (row D). The REMAINING REQ-080
# triggers that require prior-frame comparison (phase transition, gap-episode END, suspension→reopen,
# depth-vanish, spread-blowout, mid-jump, level-count floor) are completed in E4 alongside the
# cancel-plan / intent-plan wiring; they slot into the SAME row-E / row-R branches without reshaping
# this reducer. The row-H quote here is the eligible DecisionKind CLASS only — E4 fills anchor/zones/
# prices and the intent plan.

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


def _classify_row(
    observation: StrategyObservation, state: StrategyState, config: StrategyConfig
) -> Row:
    """Classify an ACCEPTED observation into EXACTLY ONE of the reducer rows R/E/D/C/F/W/H.

    Rows are evaluated in spec REQ-070 order, so an earlier row PRE-EMPTS a later one — most notably
    a ``tick_regime_changed`` reset (row R) pre-empts the REQ-080 book triggers (row E) for the same
    frame (Fable-plan-review Minor-1). Purely a function of ``(observation, prior state, config)`` —
    no clock, no randomness — so decision identity reproduces. (Row S is handled upstream by the
    watermark layer and never reaches here.)
    """
    if observation.tick_regime_changed:
        return "R"
    if observation.book_status in ("gap", "excluded"):
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


def _admit_basis_updates(
    observation: StrategyObservation, base: StrategyState, config: StrategyConfig
) -> dict[str, Any]:
    """The basis training delta for an ADMITTING row — GUARDED arm ONLY (baseline never trains a
    basis). Folds THIS frame's ``raw_gap = fv − mid`` into the config-selected basis state. For a
    row-F fv-epoch frame both bases were already cleared by ``_accept(basis_reset=True)``, so the
    fold yields exactly the current sample — the REQ-070 clear-then-admit (Codex R6 MAJOR-1.2).

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
    mid = _mid(observation)
    if mid is None:
        return {}
    raw_gap = observation.guard_fv.fv - mid
    if config.basis_estimator == "halflife_ewma":
        prev = base.basis_ewma_value
        prev_ts = base.basis_ewma_ts
        if prev is None or prev_ts is None:
            return {"basis_ewma_value": raw_gap, "basis_ewma_ts": observation.as_of_ts}
        dt_ms = float(observation.as_of_ts - prev_ts)
        value = halflife_ewma(prev, raw_gap, dt_ms, float(config.ewma_halflife_ms))
        return {"basis_ewma_value": value, "basis_ewma_ts": observation.as_of_ts}
    samples = base.basis_samples + ((observation.as_of_ts, raw_gap),)
    return {"basis_samples": samples[-config.basis_window :]}


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
            "event_cooldown_until_ts": _cooldown_deadline(observation, config),
        }
    )
    return _decide("NO_QUOTE", (reason,)), next_state


def _apply_event(
    observation: StrategyObservation, base: StrategyState, config: StrategyConfig
) -> tuple[StrategyDecision, StrategyState]:
    """Row E (EVENT-TRIGGER): NO admission (a trigger frame never trains accumulators); NO_QUOTE +
    an event cooldown anchored at this frame. The E2-T4 self-contained trigger is entry into a
    ``gap`` / ``excluded`` book (RED-53); the ratio/jump triggers slot in here in E4."""
    reason: ReasonCode = "book_gap" if observation.book_status == "gap" else "book_excluded"
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
    * H (healthy): the eligible quote CLASS (``QUOTE_TWO_SIDED``; E4 refines zones/prices), unless a
      QUOTE-ONLY status/stream blocker downgrades it to ``NO_QUOTE`` — admission is unaffected.
    """
    updates = _admit_venue_updates(observation, base, config)
    updates.update(_admit_basis_updates(observation, base, config))
    updates["event_cooldown_until_ts"] = None
    next_state = base.model_copy(update=updates)

    if row == "F":
        return _decide("NO_QUOTE", ("basis_warmup",)), next_state
    if row == "W":
        return _decide("NO_QUOTE", ("event_ref_warmup",)), next_state
    blocker = _status_stream_reason(observation, base, config)
    if blocker is not None:
        return _decide("NO_QUOTE", (blocker,)), next_state
    return _decide("QUOTE_TWO_SIDED", ()), next_state


def decide(
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
        return _apply_reset(observation, base, config, reason="tick_regime_changed")
    if row == "E":
        return _apply_event(observation, base, config)
    if row == "D":
        return _apply_data_degraded(observation, base, config)
    if row == "C":
        return _apply_cooldown(base)
    return _apply_healthy(observation, base, config, row=row)
