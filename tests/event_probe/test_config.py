"""E5 sealed-config tests (CON-002..006/009/010/014/016 + PAT-001).

Covers ``config.py``:

* ``ProbeConfig`` -- the single frozen, pinned superset of every predeclared
  threshold; its defaults MUST equal the CON values (silent drift of a v1 default
  is a protocol violation, CON-014).
* ``config_hash()`` -- sha256 over the canonical (sorted-key) JSON dump, mirroring
  ``MarketQualityConfig.filter_config_hash`` / the ``run002_vvv`` seal; stable,
  deterministic, and sensitive to ANY single field change.
* ``to_window_config()`` / ``to_agg_config()`` -- rebuild the E3/E4 pinned configs
  from the sealed fields (single source of truth), so ``ProbeConfig`` drives E3/E4.
* ``verify_pinned`` -- VOID-on-drift (``ProbeVoidError``) BEFORE any I/O (PAT-001).
* ``build_sealed_result`` -- the sealed serializer carrying the full per-event
  ``event_records[]`` audit trail (§4 / GUD-001) + all tallies; writes NO file.
"""

from __future__ import annotations

import builtins
import contextlib
import json

from veridex.backtest.event_probe.aggregate import AggConfig, ProbeResult, SliceVerdict
from veridex.backtest.event_probe.compute import EventRecord, WindowConfig
from veridex.backtest.event_probe.config import (
    ProbeConfig,
    ProbeVoidError,
    build_sealed_result,
    verify_pinned,
)
from veridex.strategies.market_quality import DEFAULT_MARKET_QUALITY_CONFIG


def test_config_defaults_match_spec() -> None:
    # Every pinned default equals its CON value; a drifted default is a CON-014
    # protocol violation, so this guards each one explicitly (incl. the seed).
    cfg = ProbeConfig()
    # window / classifier (CON-002..006)
    assert cfg.pre_window_s == 120
    assert cfg.imm_max_s == 60
    assert cfg.primary_horizon_s == 300
    assert cfg.settle_tol_s == 30
    assert cfg.robustness_horizons_s == (30, 60, 600)
    assert cfg.epsilon == 0.05
    assert cfg.min_odds_states == 3  # CON-008 per-event observability floor
    # aggregation (CON-009/010)
    assert cfg.n_min_global == 30
    assert cfg.n_min_slice == 15
    assert cfg.bootstrap_n == 10000
    assert cfg.ci_level == 0.90
    assert cfg.seed == 20260705
    # near-certain band (CON-016)
    assert cfg.band_lo == 0.05
    assert cfg.band_hi == 0.95


def test_config_hash_stable_and_deterministic() -> None:
    # Same config -> same hash across two calls AND across two equal instances.
    cfg = ProbeConfig()
    assert cfg.config_hash() == cfg.config_hash()
    assert ProbeConfig().config_hash() == ProbeConfig().config_hash()
    # A hex sha256 digest.
    h = cfg.config_hash()
    assert isinstance(h, str) and len(h) == 64
    int(h, 16)  # parses as hex


def test_config_hash_is_threshold_sensitive() -> None:
    # Changing ANY single field yields a DIFFERENT hash (CON-014 anti-drift seal).
    base = ProbeConfig().config_hash()
    assert ProbeConfig(seed=1).config_hash() != base
    assert ProbeConfig(epsilon=0.06).config_hash() != base
    assert ProbeConfig(n_min_global=31).config_hash() != base
    assert ProbeConfig(band_hi=0.94).config_hash() != base
    assert ProbeConfig(robustness_horizons_s=(30, 60, 601)).config_hash() != base
    # CON-008: the min-odds-states floor is SEALED -- a post-hoc 3->2 change VOIDs.
    assert ProbeConfig(min_odds_states=2).config_hash() != base


def test_band_seal_matches_consumed_source() -> None:
    # CON-016: the SEALED band must equal the band E2 (``series.py``) actually
    # consumes (``DEFAULT_MARKET_QUALITY_CONFIG``). Deriving the seal from the same
    # source means a market-quality band change moves ``config_hash()`` -> VOIDs,
    # instead of the sealed band being a decorative literal that never tracks it.
    cfg = ProbeConfig()
    assert cfg.band_lo == DEFAULT_MARKET_QUALITY_CONFIG.band_lo
    assert cfg.band_hi == DEFAULT_MARKET_QUALITY_CONFIG.band_hi
    # The pinned v1 values remain 0.05 / 0.95.
    assert cfg.band_lo == 0.05
    assert cfg.band_hi == 0.95


def test_to_window_and_agg_config_match_sealed() -> None:
    # The sealed config reproduces the E3/E4 pinned configs EXACTLY -- single
    # source of truth, so defaults live in ProbeConfig, not duplicated downstream.
    cfg = ProbeConfig()
    assert cfg.to_window_config() == WindowConfig()
    assert cfg.to_agg_config() == AggConfig()


def test_void_on_drift_raises_before_io() -> None:
    # A live hash != the pinned hash VOIDs (ProbeVoidError); a matching hash does
    # not raise. This path performs no file I/O (verified by patching open below).
    cfg = ProbeConfig()
    try:
        verify_pinned(cfg, "deadbeef")
    except ProbeVoidError:
        pass
    else:  # pragma: no cover - the assert path is the failure signal
        raise AssertionError("verify_pinned must raise ProbeVoidError on hash drift")

    # The pinned (matching) hash must NOT raise.
    verify_pinned(cfg, cfg.config_hash())


def test_verify_pinned_performs_no_file_io(monkeypatch) -> None:
    # PAT-001: the VOID check runs BEFORE any I/O -- prove it opens no file for
    # writing regardless of outcome.
    real_open = builtins.open

    def _no_write_open(file, mode="r", *args, **kwargs):
        if any(flag in mode for flag in ("w", "a", "x", "+")):
            raise AssertionError(f"verify_pinned wrote a file: open({file!r}, {mode!r})")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _no_write_open)

    cfg = ProbeConfig()
    verify_pinned(cfg, cfg.config_hash())  # matching -> no raise, no write
    with contextlib.suppress(ProbeVoidError):
        verify_pinned(cfg, "deadbeef")  # drift -> raise, still no write


def _event_record(**overrides) -> EventRecord:
    """An eligible LAG record carrying the full §4 audit surface."""
    base = {
        "t_e": 1782500866,
        "scoring_side": "home",
        "participant": 1,
        "p_pre": 0.55,
        "p_imm": 0.62,
        "p_settle": 0.70,
        "delta_imm": 0.29,
        "delta_settle": 0.65,
        "R": 0.45,
        "event_class": "LAG",
        "exclusion_reason": None,
        "grid": {30: 0.5, 60: 0.48, 300: 0.45, 600: 0.4},
        "slice_tags": {"venue_side": "home", "scorer_rank": "favorite"},
    }
    base.update(overrides)
    return EventRecord(**base)


def test_sealed_result_schema_has_event_records(monkeypatch) -> None:
    # The sealed serializer carries config + config_hash, the overall verdict and
    # global stats, per_slice, the full per-event event_records[] audit trail (§4 /
    # GUD-001), and both tally maps -- and writes NO file.
    real_open = builtins.open

    def _no_write_open(file, mode="r", *args, **kwargs):
        if any(flag in mode for flag in ("w", "a", "x", "+")):
            raise AssertionError(f"build_sealed_result wrote a file: open({file!r}, {mode!r})")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _no_write_open)

    cfg = ProbeConfig()
    records = [
        _event_record(),
        _event_record(
            event_class="NO-SIGNAL",
            exclusion_reason="below_epsilon",
            R=None,
        ),
    ]
    result = ProbeResult(
        overall_verdict="INCONCLUSIVE",
        global_n=1,
        global_median_R=0.45,
        global_ci_low=0.2,
        global_ci_high=0.9,
        per_slice=[
            SliceVerdict(
                slice="favorite",
                n=1,
                median_R=0.45,
                ci_low=None,
                ci_high=None,
                verdict="DESCRIPTIVE_ONLY",
            )
        ],
        class_counts={"LAG": 1, "NO-SIGNAL": 1},
        excluded_by_reason={"below_epsilon": 1, "ambiguous_delta": 0},
    )

    sealed = build_sealed_result(cfg, result, records)

    # Top-level seal fields.
    assert sealed["config_hash"] == cfg.config_hash()
    assert sealed["config"] == cfg.model_dump()
    assert sealed["overall_verdict"] == "INCONCLUSIVE"
    assert sealed["class_counts"] == {"LAG": 1, "NO-SIGNAL": 1}
    assert sealed["excluded_by_reason"]["below_epsilon"] == 1
    assert sealed["global"]["median_R"] == 0.45
    assert sealed["per_slice"][0]["verdict"] == "DESCRIPTIVE_ONLY"

    # event_records[] carries every §4 audit field for every record.
    assert len(sealed["event_records"]) == 2
    required = {
        "t_e", "scoring_side", "participant", "p_pre", "p_imm", "p_settle",
        "delta_imm", "delta_settle", "R", "event_class", "exclusion_reason",
        "slice_tags", "grid",
    }
    for row in sealed["event_records"]:
        assert required <= set(row)
    first = sealed["event_records"][0]
    assert first["t_e"] == 1782500866
    assert first["scoring_side"] == "home"
    assert first["participant"] == 1
    assert first["event_class"] == "LAG"
    assert first["slice_tags"] == {"venue_side": "home", "scorer_rank": "favorite"}
    # grid keys are JSON-safe (stringified) and preserve every horizon.
    assert set(first["grid"]) == {"30", "60", "300", "600"}

    # The whole sealed artifact is JSON-serializable (no tuples/ints-as-keys leak).
    json.dumps(sealed)
