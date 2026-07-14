"""Pure-tier basis / residual estimators (REQ-072 / AC-059 / RED-02 / RED-55).

``veridex.mm_strategy.basis`` is the deterministic NUMERICAL AUTHORITY the whole strategy reasons
over. These tests pin, against the REAL module (no re-implementation):

- ``test_even_window_median_is_central_pair_mean`` — an even window's ``rolling_median`` is the
  arithmetic MEAN of the two central order statistics (``[.01,.03] → .02``), NOT a lower/upper
  median (``.01`` / ``.03``). This is the load-bearing REQ-072 semantics: a lower median at an
  even ``basis_window`` flips residual pulls at one config hash (Codex R6 MAJOR-3).
- ``test_basis_uses_statistics_median_semantics`` — the ``rolling_median`` estimator inside the
  real ``basis()`` equals ``statistics.median`` byte-for-byte on the same even-window input, so
  ``basis`` and the (E2-T2) REQ-080 venue references share ONE median authority. E2-T2 adds the
  cross-helper parity check; here we assert against ``statistics.median`` directly.
- ``test_persistent_offset_yields_zero_residual`` — a raw gap EQUAL to a fully-persistent offset
  already folded into the basis yields exactly ``0.0`` residual and can never, by itself, become
  tradable edge (RED-02), under BOTH config estimators.

The remaining tests pin the estimator mechanics: odd-window median, the pinned-median authority
across parities, the ``halflife_ewma`` decay step (one half-life halves the prior weight; a
zero-``dt`` resample is a no-op), ``basis_window`` truncation, EWMA persistence, and the signed
residual. Scale/MAD is CUT from v0 (Codex R4 MINOR-1) — there is nothing robust-scale to test.
"""

from __future__ import annotations

import statistics

import pytest

from veridex.mm_strategy.basis import (
    BasisSample,
    basis,
    halflife_ewma,
    residual,
    rolling_median,
)
from veridex.mm_strategy.config import StrategyConfig


def _sample_stream(gaps: tuple[float, ...]) -> tuple[BasisSample, ...]:
    """Accepted-sample stream ``(as_of_ts_ms, gap)`` at a fixed 1000ms cadence (oldest→newest)."""
    return tuple((1000 * i, gap) for i, gap in enumerate(gaps))


# --- rolling_median: the pinned central-pair-mean authority --------------------------------


def test_even_window_median_is_central_pair_mean() -> None:
    # REQ-072 (Codex R6 MAJOR-3): even window → arithmetic mean of the two central order
    # statistics, NEVER a lower/upper median. A lower median (.01) or upper median (.03) here
    # would flip residual pulls at an even basis_window; the shared authority forbids both.
    assert rolling_median((0.01, 0.03)) == 0.02
    assert rolling_median((0.01, 0.03)) != 0.01
    assert rolling_median((0.01, 0.03)) != 0.03


def test_odd_window_median_is_central_order_statistic() -> None:
    # Odd window → the single central order statistic (.01), not the arithmetic mean (~.023).
    assert rolling_median((0.01, 0.01, 0.05)) == 0.01


@pytest.mark.parametrize(
    "samples",
    [
        (0.01, 0.03),  # even
        (0.01, 0.02, 0.03, 0.04),  # even
        (0.01, 0.01, 0.05),  # odd
        (-0.02, 0.00, 0.02, 0.04, 0.10),  # odd, signed gaps
    ],
)
def test_rolling_median_matches_statistics_median_authority(
    samples: tuple[float, ...],
) -> None:
    # ONE median authority: rolling_median IS statistics.median for every parity/sign.
    assert rolling_median(samples) == statistics.median(samples)


# --- halflife_ewma: the deterministic time-decayed update step -----------------------------


def test_halflife_ewma_one_halflife_halves_prior_weight() -> None:
    # After exactly one half-life the prior estimate and the new value are weighted equally.
    assert halflife_ewma(0.0, 1.0, dt_ms=1000.0, halflife_ms=1000.0) == 0.5


def test_halflife_ewma_zero_dt_leaves_estimate_unchanged() -> None:
    # A same-clock resample (dt == 0) contributes decay == 1 → the estimate does not move.
    assert halflife_ewma(0.2, 0.9, dt_ms=0.0, halflife_ms=1000.0) == 0.2


# --- basis: config-selected point estimate over accepted samples ---------------------------


def test_basis_uses_statistics_median_semantics() -> None:
    # The rolling_median estimator inside the REAL basis() shares the statistics.median authority
    # on an even window: basis over (.01,.03) == statistics.median((.01,.03)) == .02 (NOT .01/.03).
    config = StrategyConfig(guard_enabled=False)  # default estimator == "rolling_median"
    even_window = _sample_stream((0.01, 0.03))
    assert config.basis_estimator == "rolling_median"
    assert basis(even_window, config) == statistics.median((0.01, 0.03))
    assert basis(even_window, config) == 0.02


def test_basis_rolling_median_respects_basis_window() -> None:
    # Only the last basis_window samples enter the median: window=3 over
    # gaps [.01,.01,.01,.05,.05] medians the tail [.01,.05,.05] → .05, NOT the full-history .01.
    config = StrategyConfig(guard_enabled=False, basis_estimator="rolling_median", basis_window=3)
    stream = _sample_stream((0.01, 0.01, 0.01, 0.05, 0.05))
    assert basis(stream, config) == 0.05
    assert basis(stream, config) != statistics.median((0.01, 0.01, 0.01, 0.05, 0.05))


def test_basis_halflife_ewma_converges_on_persistent_offset() -> None:
    # The time-decayed estimator over a constant offset returns exactly that offset.
    config = StrategyConfig(
        guard_enabled=False, basis_estimator="halflife_ewma", ewma_halflife_ms=1000
    )
    assert basis(_sample_stream((0.05, 0.05, 0.05, 0.05)), config) == 0.05


def test_basis_requires_at_least_one_sample() -> None:
    # basis is a point estimator over accepted samples; warmup/acceptance is the core's job, so an
    # empty stream is a contract violation, not a silent 0.0.
    config = StrategyConfig(guard_enabled=False)
    with pytest.raises(ValueError, match="at least one"):
        basis((), config)


# --- residual: the signed deviation (RED-02) -----------------------------------------------


@pytest.mark.parametrize(
    "estimator",
    ["rolling_median", "halflife_ewma"],
)
def test_persistent_offset_yields_zero_residual(estimator: str) -> None:
    # RED-02: a raw gap EQUAL to a fully-persistent offset already folded into the basis yields
    # exactly 0.0 residual under BOTH estimators — a persistent offset can never become edge.
    config = StrategyConfig(
        guard_enabled=False, basis_estimator=estimator, ewma_halflife_ms=1000  # type: ignore[arg-type]
    )
    offset = 0.05
    b = basis(_sample_stream((offset, offset, offset, offset)), config)
    assert b == offset
    assert residual(offset, b) == 0.0


def test_residual_is_signed_deviation() -> None:
    # residual is the signed raw_gap − basis: above basis is positive, below is negative.
    assert residual(0.07, 0.05) == pytest.approx(0.02)
    assert residual(0.03, 0.05) == pytest.approx(-0.02)
