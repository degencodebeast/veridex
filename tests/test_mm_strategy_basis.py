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
from pydantic import ValidationError

from veridex.mm_strategy.basis import (
    BasisSample,
    basis,
    event_smoother_update,
    halflife_ewma,
    reference_is_warm,
    residual,
    rolling_depth_reference,
    rolling_median,
    rolling_spread_reference,
)
from veridex.mm_strategy.config import StrategyConfig
from veridex.mm_strategy.contracts import (
    InventoryProjection,
    StrategyObservation,
    StrategyState,
)


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


# --- E2-T2: event smoother (REQ-036 / AC-042 / RED-34) -------------------------------------


def _raw_observation(**overrides: object) -> StrategyObservation:
    """A healthy, guard-off RAW-facts observation; ``overrides`` perturb single fields per test.

    Used to prove the observation carries RAW venue facts ONLY — there is NO producer-supplied
    reference channel to smuggle a precomputed reference through (RED-44 / F2 defect class).
    """
    base: dict[str, object] = {
        "fixture_id": 42,
        "market_ref": "TEAM-A/YES",
        "side": "YES",
        "token_id": "tok-1",
        "venue_market_ref": "0xmarket",
        "tick_size": 0.01,
        "observation_sequence": 10,
        "book_source_epoch": 1,
        "bid": 0.49,
        "ask": 0.51,
        "bid_size": 100.0,
        "ask_size": 120.0,
        "book_status": "ok",
        "status_reason": None,
        "book_recv_ts": 1_000,
        "level_count_in_band": 5,
        "tick_regime_changed": False,
        "phase": 1,
        "suspended": False,
        "match_state_recv_ts": 990,
        "guard_fv": None,
        "market_status": "ACTIVE",
        "market_status_recv_ts": 995,
        "market_status_epoch": 3,
        "order_stream_ok": True,
        "projection_fresh": True,
        "inventory": InventoryProjection(
            net_position=0.0, resting=(), projection_as_of_ts=1_000, fresh=True
        ),
        "as_of_ts": 1_000,
    }
    base.update(overrides)
    return StrategyObservation(**base)


def test_smoother_kind_or_param_change_moves_config_hash() -> None:
    # AC-042 / RED-34: the event smoother kind AND its param are config-hash-bearing — changing
    # EITHER changes config_hash, so two runs with different smoother behavior can never share one
    # identity. A smoother knob that did not enter the hash would let behavior drift silently.
    base = StrategyConfig(guard_enabled=False, event_smoother="ema_alpha", event_smoother_param=0.1)
    kind_changed = StrategyConfig(
        guard_enabled=False, event_smoother="halflife_ewma", event_smoother_param=0.1
    )
    param_changed = StrategyConfig(
        guard_enabled=False, event_smoother="ema_alpha", event_smoother_param=0.2
    )
    assert base.config_hash() != kind_changed.config_hash()
    assert base.config_hash() != param_changed.config_hash()


def test_smoother_ema_alpha_blends_prior_state_toward_value() -> None:
    # ema_alpha is compare-then-update: (1-a)*prev + a*value read from PRIOR state. With a=0.25,
    # prev=0.50, value=0.60 → 0.525 (a quarter of the way), NEVER the raw value or a mean.
    config = StrategyConfig(guard_enabled=False, event_smoother="ema_alpha", event_smoother_param=0.25)
    assert event_smoother_update(0.50, 0.60, config, dt_ms=1000.0) == pytest.approx(0.525)


def test_smoother_halflife_uses_time_decay() -> None:
    # halflife_ewma smoother folds prev toward value on the elapsed dt; one half-life weights them
    # equally (shares the basis halflife authority), so prev=0.0/value=1.0 over one half-life → 0.5.
    config = StrategyConfig(
        guard_enabled=False, event_smoother="halflife_ewma", ewma_halflife_ms=1000
    )
    assert event_smoother_update(0.0, 1.0, config, dt_ms=1000.0) == pytest.approx(0.5)


# --- E2-T2: rolling spread / depth references (REQ-080 / REQ-072 / RED-44 / RED-46) --------


def test_reference_uses_median_not_mean() -> None:
    # RED-46: the rolling reference is statistics.median over the raw window, NOT the mean. Window
    # (.01,.01,.01,.05,.05) → median .01 (central order statistic), NEVER the mean .026 — a mean
    # reference would be dragged up by the two wide samples and mis-scale every downstream gate.
    window = (0.01, 0.01, 0.01, 0.05, 0.05)
    config = StrategyConfig(guard_enabled=False)  # default windows (120) ≥ len(window)
    assert rolling_spread_reference(window, config) == 0.01
    assert rolling_depth_reference(window, config) == 0.01
    assert rolling_spread_reference(window, config) != pytest.approx(0.026)
    assert rolling_depth_reference(window, config) != pytest.approx(0.026)


def test_references_are_state_carried_not_observation() -> None:
    # RED-44 (Fable F2 defect class): the references are STATE accumulators the pure core recomputes
    # from RAW facts — they are NEVER read from a producer-supplied field on the observation.
    config = StrategyConfig(guard_enabled=False)
    raw_spreads = (0.02, 0.02, 0.02, 0.04, 0.04)
    raw_depths = (100.0, 100.0, 100.0, 60.0, 60.0)
    # (a) the reference is a pure statistics.median over the state-carried raw window + config only.
    assert rolling_spread_reference(raw_spreads, config) == statistics.median(raw_spreads)
    assert rolling_depth_reference(raw_depths, config) == statistics.median(raw_depths)
    # (b) the observation carries RAW venue facts ONLY — no reference-VALUE field exists to read
    #     (the ``*_ref`` identity fields like ``market_ref`` are venue identifiers, not references).
    obs_fields = set(StrategyObservation.model_fields)
    assert not any("reference" in name for name in obs_fields)
    # (c) the frozen extra="forbid" contract REJECTS a precomputed reference smuggled onto the
    #     observation, so a producer reference can never override the state recomputation.
    with pytest.raises(ValidationError):
        _raw_observation(spread_reference=0.99)


def test_references_share_the_rolling_median_authority() -> None:
    # Cross-helper parity (the check E2-T1 deferred here): the references and basis() share ONE
    # median authority — rolling_median IS statistics.median — so basis and the REQ-080 references
    # can never diverge at a config hash on an even window.
    even = (0.01, 0.03)
    config = StrategyConfig(guard_enabled=False)
    assert rolling_spread_reference(even, config) == rolling_median(even)
    assert rolling_depth_reference(even, config) == statistics.median(even)


def test_references_respect_hash_bound_windows() -> None:
    # Only the last rolling_*_window raw samples enter the median (the window is config-hash-bound).
    # window=3 over (.01,.01,.01,.05,.05) medians the tail (.01,.05,.05) → .05, NOT the history .01.
    config = StrategyConfig(guard_enabled=False, rolling_spread_window=3, rolling_depth_window=3)
    samples = (0.01, 0.01, 0.01, 0.05, 0.05)
    assert rolling_spread_reference(samples, config) == 0.05
    assert rolling_depth_reference(samples, config) == 0.05


def test_reference_warmup_is_bound_by_ref_min_samples() -> None:
    # A reference is live only once ref_min_samples raw samples have accumulated post-reset; below
    # that the core withholds quoting (event_ref_warmup), so a thin post-reset window is never trusted.
    config = StrategyConfig(guard_enabled=False, ref_min_samples=20)
    assert not reference_is_warm(19, config)
    assert reference_is_warm(20, config)
    assert reference_is_warm(50, config)


def test_empty_reference_window_is_a_contract_violation() -> None:
    # Like basis(), the references are reducers over accepted samples; warmup/acceptance is the
    # core's job, so an empty window is a contract violation, not a silent 0.0.
    config = StrategyConfig(guard_enabled=False)
    with pytest.raises(ValueError, match="at least one"):
        rolling_spread_reference((), config)
    with pytest.raises(ValueError, match="at least one"):
        rolling_depth_reference((), config)


# --- E2-T2: StrategyState accumulator fields (REQ-031 append-only extension) ---------------


def test_strategy_state_defaults_leave_accumulators_empty() -> None:
    # The E2-T2 accumulator fields extend the E1-T2 shell append-only with SAFE defaults, so a
    # fresh StrategyState() (the purity fixture) still constructs and carries no smoother/reference.
    state = StrategyState()
    assert state.smoother_mid is None
    assert state.smoother_mid_ts is None
    assert state.spread_ref_samples == ()
    assert state.depth_ref_samples == ()
