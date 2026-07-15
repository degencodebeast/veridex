"""Pure-tier basis / residual estimators (REQ-072 / AC-059 / RED-02 / RED-55).

The deterministic NUMERICAL AUTHORITY the whole strategy reasons over. The ``basis`` is the
config-selected point estimate of the persistent venue↔TxLINE gap; the ``residual`` is a raw gap's
signed deviation from that basis.

**EVERY rolling median here uses the SAME pinned semantics — Python ``statistics.median`` (an even
window takes the arithmetic MEAN of the two central order statistics), REQ-072.** This is
load-bearing: at an even ``basis_window`` a lower/upper median would flip residual pulls and
REQ-078 at one config hash (Codex R6 MAJOR-3), so the basis and the REQ-080 venue references MUST
share exactly this one authority — never a custom or lower/upper median. Robust-scale (MAD) is CUT
from v0 (Codex R4 MINOR-1); there is deliberately no scale estimator here.

Import whitelist (load-bearing, enforced by ``tests/test_mm_strategy_purity.py``): stdlib +
pydantic + the pure ``mm_strategy`` siblings + ``veridex.runtime.evidence`` ONLY. This module
imports the ``config`` sibling for the estimator selection — REQ-002 EXPLICITLY permits pure-tier
modules to import each other. No I/O, no wall clock, no randomness: every function is a pure,
deterministic function of its arguments.
"""

from __future__ import annotations

import math
import statistics

from veridex.mm_strategy.config import StrategyConfig
from veridex.mm_strategy.contracts import StrategyState

# One accepted basis sample: ``(as_of_ts_ms, raw_gap)``. The gap is a native-probability
# difference (venue mid − TxLINE fair value); the timestamp is the observation clock the
# time-decayed EWMA estimator decays on (REQ-072 "time-decayed on ``as_of_ts``").
BasisSample = tuple[int, float]


def rolling_median(samples: tuple[float, ...]) -> float:
    """The pinned rolling median of ``samples`` via ``statistics.median`` (REQ-072).

    An even-length window returns the arithmetic MEAN of the two central order statistics (never
    a lower/upper median) — the SAME semantics every rolling median in the spec uses, so the
    basis and the REQ-080 venue references can never diverge at a config hash.
    """
    return float(statistics.median(samples))


def halflife_ewma(prev: float, value: float, dt_ms: float, halflife_ms: float) -> float:
    """One time-decayed EWMA update: blend ``prev`` toward ``value`` over ``dt_ms`` (REQ-072).

    The weight retained on the prior estimate over the elapsed interval is
    ``0.5 ** (dt_ms / halflife_ms)`` — exactly one half-life halves it — so the update is
    deterministic in the observation clock and independent of sampling cadence. ``dt_ms == 0`` (a
    same-clock resample) retains full weight on ``prev`` and leaves the estimate unchanged.
    """
    decay = 0.5 ** (dt_ms / halflife_ms)
    return float(decay * prev + (1.0 - decay) * value)


def basis(raw_gaps: tuple[BasisSample, ...], config: StrategyConfig) -> float:
    """Config-selected point estimate of the persistent gap over accepted ``raw_gaps`` (REQ-072).

    ``raw_gaps`` is the ordered (oldest→newest) tuple of accepted ``(as_of_ts_ms, gap)`` samples;
    warmup / sample acceptance is the core's responsibility, so this pure reducer assumes at least
    one sample. ``config.basis_estimator`` selects:

    * ``rolling_median`` (default) — :func:`rolling_median` over the gaps of the last
      ``basis_window`` samples (the pinned central-pair-mean median).
    * ``halflife_ewma`` — the time-decayed EWMA folded over the samples on their real ``as_of_ts``
      spacing, seeded from the oldest sample's gap.
    """
    if not raw_gaps:
        raise ValueError("basis requires at least one accepted sample")
    if config.basis_estimator == "rolling_median":
        window = raw_gaps[-config.basis_window :]
        return rolling_median(tuple(gap for _, gap in window))
    # halflife_ewma: fold each accepted sample in on its real inter-sample interval.
    prev_ts, acc = raw_gaps[0]
    for ts, gap in raw_gaps[1:]:
        acc = halflife_ewma(acc, gap, float(ts - prev_ts), float(config.ewma_halflife_ms))
        prev_ts = ts
    return acc


def basis_from_state(state: StrategyState, config: StrategyConfig) -> float:
    """Config-selected basis read from carry-forward :class:`StrategyState` (REQ-070/072).

    The single READ authority for the persistent gap, so a caller never re-folds raw history and
    reintroduces the Codex Gate#1-R2 MAJOR-1 truncation defect:

    * ``rolling_median`` — :func:`basis` over ``state.basis_samples`` (the bounded raw window whose
      ``[-basis_window:]`` truncation is EXACT for a median).
    * ``halflife_ewma`` — the bounded sufficient accumulator ``state.basis_ewma_value``, folded one
      admitted sample at a time by the core, so the online result is independent of how much raw
      history is retained.

    Warmup / sample acceptance is the core's job, so — like :func:`basis` — an unseeded EWMA
    accumulator is a contract violation, not a silent ``0.0``.
    """
    if config.basis_estimator == "halflife_ewma":
        if state.basis_ewma_value is None:
            raise ValueError("basis requires at least one accepted sample")
        return state.basis_ewma_value
    return basis(state.basis_samples, config)


def residual(raw_gap: float, basis: float) -> float:
    """The raw gap's signed deviation from the basis (REQ-072 / RED-02).

    ``raw_gap - basis``: a raw gap EQUAL to the basis — a fully persistent offset already folded
    into the estimate — yields exactly ``0.0`` and can never, by itself, become tradable edge.
    """
    return raw_gap - basis


# --- Directional tick rounding (maker-safety invariant; REQ-055) ---------------------------
# The BID / ASK sides round in OPPOSITE directions so a rounded quote never IMPROVES past its
# join-or-behind target (crossing up on a bid, down on an ask). A single side-agnostic
# round-to-nearest is the rust_mm_bot / smm REJECT anti-pattern: nearest can round a bid UP through
# the best bid and an ask DOWN through the best ask, turning a resting maker quote into an improving
# (potentially crossing) one. Both helpers snap the price/tick RATIO to 9 dp before the floor/ceil so
# a value that is already on-tick (whose ratio is ``39.9999…`` in binary) never drops a whole tick on
# representation dust, and re-snap the product to 12 dp so the returned price carries no FP tail. The
# R4-A wire layer remains the AUTHORITATIVE tick / non-crossing check — this is the proposal courtesy.


def floor_to_tick(price: float, tick: float) -> float:
    """Round ``price`` DOWN to the nearest ``tick`` multiple — the maker-safe BID rounding (REQ-055).

    A bid rests AT or BELOW its target, so rounding DOWN can never improve it past (cross up through)
    the target. Mirror of :func:`ceil_to_tick`; the ratio is snapped to 9 dp before the floor to
    absorb binary-representation dust and the product is re-snapped so no FP tail leaks into the price.

    Precondition: ``tick`` is finite and strictly positive (defense-in-depth — the AUTHORITATIVE
    guard is the ``StrategyObservation.tick_size`` construction validator, so an observation carrying
    ``tick_size <= 0`` never exists to reach here; Gate #2 MAJOR-5).
    """
    if not math.isfinite(tick) or tick <= 0.0:
        raise ValueError(f"tick must be a finite, strictly positive grid size, got {tick!r}")
    ticks = math.floor(round(price / tick, 9))
    return round(ticks * tick, 12)


def ceil_to_tick(price: float, tick: float) -> float:
    """Round ``price`` UP to the nearest ``tick`` multiple — the maker-safe ASK rounding (REQ-055).

    An ask rests AT or ABOVE its target, so rounding UP can never improve it past (cross down through)
    the target. Mirror of :func:`floor_to_tick` (same FP-tolerance treatment, opposite direction).

    Precondition: ``tick`` is finite and strictly positive (defense-in-depth — the AUTHORITATIVE
    guard is the ``StrategyObservation.tick_size`` construction validator; Gate #2 MAJOR-5).
    """
    if not math.isfinite(tick) or tick <= 0.0:
        raise ValueError(f"tick must be a finite, strictly positive grid size, got {tick!r}")
    ticks = math.ceil(round(price / tick, 9))
    return round(ticks * tick, 12)


# --- Event smoother + rolling venue references (REQ-036 / REQ-080 / AC-042 / RED-34/44/46) --
# STATE-carried accumulators the pure core recomputes from RAW venue facts (bid/ask/sizes) held on
# ``StrategyState`` between frames — a producer-supplied smoother/reference is FORBIDDEN (the Fable
# F2 defect class, RED-44). Every rolling reference reuses the SAME :func:`rolling_median`
# (``statistics.median``) authority the basis uses, so the basis and the REQ-080 references can
# never diverge at one config hash (REQ-072). All pure functions of ``(prior state, raw facts,
# config)`` — no I/O, no clock, no randomness.


def event_smoother_update(
    prev: float, value: float, config: StrategyConfig, dt_ms: float
) -> float:
    """One config-selected event-smoother update: blend ``prev`` toward ``value`` (REQ-036).

    COMPARE-then-UPDATE: the caller reads ``prev`` off the PRIOR state, then folds the new raw
    ``ok``-book ``value`` (a mid) in. The smoother is re-seeded from a row-R reset frame's own mid
    by the reducer; this is the between-seed step. ``config.event_smoother`` selects:

    * ``ema_alpha`` (default) — constant-weight blend ``(1 - a) * prev + a * value`` with
      ``a = config.event_smoother_param`` (independent of ``dt_ms``).
    * ``halflife_ewma`` — the time-decayed :func:`halflife_ewma` step over the elapsed ``dt_ms`` on
      ``config.ewma_halflife_ms`` (the SAME halflife authority the basis EWMA uses; §9.1 carries no
      separate smoother-halflife knob, and adding one would be a config revision).

    Both the smoother KIND and its param are config-hash-bearing (AC-042/RED-34): they enter
    ``config_hash`` through ``model_dump``, so no smoother behavior change is ever hash-silent.
    """
    if config.event_smoother == "ema_alpha":
        alpha = config.event_smoother_param
        return (1.0 - alpha) * prev + alpha * value
    return halflife_ewma(prev, value, dt_ms, float(config.ewma_halflife_ms))


def rolling_spread_reference(
    spread_samples: tuple[float, ...], config: StrategyConfig
) -> float:
    """Rolling spread reference: :func:`rolling_median` over the last ``rolling_spread_window``
    RAW spreads (REQ-080 / REQ-072).

    ``spread_samples`` is the ordered (oldest→newest) tuple of accepted RAW ``ask - bid`` spreads
    the core has trained since the last reset; only the last ``config.rolling_spread_window`` enter
    the median (the window is config-hash-bound). Warmup / acceptance is the core's job, so — like
    :func:`basis` — this reducer assumes at least one sample. Uses the pinned ``statistics.median``
    authority (never the mean; RED-46).
    """
    if not spread_samples:
        raise ValueError("rolling_spread_reference requires at least one accepted sample")
    window = spread_samples[-config.rolling_spread_window :]
    return rolling_median(window)


def rolling_depth_reference(
    depth_samples: tuple[float, ...], config: StrategyConfig
) -> float:
    """Rolling depth reference: :func:`rolling_median` over the last ``rolling_depth_window`` RAW
    top-of-book depths (REQ-080 / REQ-072).

    ``depth_samples`` is the ordered (oldest→newest) tuple of accepted RAW top-depth samples since
    the last reset; only the last ``config.rolling_depth_window`` enter the median. Shares the
    ``statistics.median`` authority with the spread reference and the basis (never the mean;
    RED-46), and — like :func:`basis` — assumes at least one accepted sample.
    """
    if not depth_samples:
        raise ValueError("rolling_depth_reference requires at least one accepted sample")
    window = depth_samples[-config.rolling_depth_window :]
    return rolling_median(window)


def reference_is_warm(sample_count: int, config: StrategyConfig) -> bool:
    """True once at least ``config.ref_min_samples`` RAW samples have accumulated post-reset (REQ-080).

    The rolling references are live only after this warmup floor is reached; below it the core
    withholds quoting (``event_ref_warmup``) so a thin post-reset window is never trusted as a
    reference. A pure predicate over the state-carried sample count — no clock, no randomness.
    """
    return sample_count >= config.ref_min_samples
