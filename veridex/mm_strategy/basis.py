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

import statistics

from veridex.mm_strategy.config import StrategyConfig

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
    return statistics.median(samples)


def halflife_ewma(prev: float, value: float, dt_ms: float, halflife_ms: float) -> float:
    """One time-decayed EWMA update: blend ``prev`` toward ``value`` over ``dt_ms`` (REQ-072).

    The weight retained on the prior estimate over the elapsed interval is
    ``0.5 ** (dt_ms / halflife_ms)`` — exactly one half-life halves it — so the update is
    deterministic in the observation clock and independent of sampling cadence. ``dt_ms == 0`` (a
    same-clock resample) retains full weight on ``prev`` and leaves the estimate unchanged.
    """
    decay = 0.5 ** (dt_ms / halflife_ms)
    return decay * prev + (1.0 - decay) * value


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


def residual(raw_gap: float, basis: float) -> float:
    """The raw gap's signed deviation from the basis (REQ-072 / RED-02).

    ``raw_gap - basis``: a raw gap EQUAL to the basis — a fully persistent offset already folded
    into the estimate — yields exactly ``0.0`` and can never, by itself, become tradable edge.
    """
    return raw_gap - basis
