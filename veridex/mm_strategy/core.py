"""Pure-tier strategy core — the watermark precondition layer (MM-R4-B).

``decide()`` is the deterministic, total decision function. This task (E2-T3) implements the
NO-LOOKAHEAD WATERMARK PRECONDITION LAYER that runs BEFORE any quoting logic: clock/epoch
monotonicity vs the carried state, sequence-staleness, the epoch-driven resets, and the
fail-closed restart guarantee. On a frame that passes every watermark it returns ``HOLD`` and
threads the advanced state through; E2-T4 replaces that pass-through with the full
S/R/E/D/C/F/W/H transition reducer.

Load-bearing ordering (REQ-033/034, AC-040, RED-06/37): the ``book_source_epoch`` INCREMENT is
evaluated BEFORE sequence-staleness, so a healthy first frame after a reconnect — whose re-baselined
sequence may sit at or below the old watermark — is accepted as the post-reset baseline instead of
being wrongly rejected as stale.

Import whitelist (load-bearing): stdlib + pydantic + the pure ``mm_strategy`` siblings
(``config`` for the ``StrategyConfig`` type + ``guard_enabled`` / ``restart_policy`` knobs,
``contracts`` for the models) + ``veridex.runtime.evidence`` (transitively) ONLY. No network, no
I/O, no wall clock, no randomness, no module-level mutable state, no process-local cache.
"""

from __future__ import annotations

from typing import Any

from veridex.mm_strategy.config import StrategyConfig
from veridex.mm_strategy.contracts import (
    GuardStateWatermark,
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


def _status_watermark(
    observation: StrategyObservation, state: StrategyState
) -> tuple[int | None, int | None]:
    """The ``(market_status_epoch, market_status_recv_ts)`` to carry forward on an ACCEPTED frame.

    REQ-026/027 (Fable n-m6): the status watermark advances ONLY on an accepted observation whose
    status is not ``UNKNOWN``; an ``UNKNOWN`` status never advances it (the prior watermark stands).
    """
    if observation.market_status == "UNKNOWN":
        return state.last_market_status_epoch, state.last_market_status_recv_ts
    return observation.market_status_epoch, observation.market_status_recv_ts


def _guard_watermark(
    observation: StrategyObservation, config: StrategyConfig
) -> GuardStateWatermark | None:
    """The guard-scoped watermark to seed/carry on an accepted frame — present ONLY when the guard
    is config-enabled AND the observation carries an FV leg (Codex-R5 MAJOR-1: a guard-off or
    FV-absent frame keeps no FV element anywhere in state)."""
    if config.guard_enabled and observation.guard_fv is not None:
        return GuardStateWatermark(fv_source_epoch=observation.guard_fv.fv_source_epoch)
    return None


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
    status_epoch, status_recv_ts = _status_watermark(observation, state)
    update: dict[str, Any] = {
        "last_observation_sequence": observation.observation_sequence,
        "last_book_source_epoch": observation.book_source_epoch,
        "last_as_of_ts": observation.as_of_ts,
        "last_market_status_epoch": status_epoch,
        "last_market_status_recv_ts": status_recv_ts,
        "guard_watermark": _guard_watermark(observation, config),
    }
    if full_reset:
        update.update(
            basis_samples=(),
            smoother_mid=None,
            smoother_mid_ts=None,
            spread_ref_samples=(),
            depth_ref_samples=(),
        )
    elif basis_reset:
        update["basis_samples"] = ()
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
    6. **Clean frame** — advance the watermark (with the fv-epoch basis-only reset when it fired)
       and HOLD; E2-T4 replaces this pass-through with the full reducer.
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

    # (3) book_source_epoch INCREMENT — evaluated BEFORE sequence-staleness (load-bearing).
    if observation.book_source_epoch > state.last_book_source_epoch:
        return _hold(), _accept(
            observation, state, config, full_reset=True, basis_reset=False
        )

    # (4) fv_source_epoch INCREMENT (guarded arm, book epoch unchanged) → basis-only reset. The
    # sequence is book-epoch-scoped, so its staleness check below still applies to this frame.
    basis_reset = guard_delta > 0

    # (5) Sequence staleness within the unchanged book epoch — HOLD, no double-advance.
    if (
        state.last_observation_sequence is not None
        and observation.observation_sequence <= state.last_observation_sequence
    ):
        return _hold(("stale_observation",)), state

    # (6) Clean frame — every watermark passed.
    return _hold(), _accept(
        observation, state, config, full_reset=False, basis_reset=basis_reset
    )
