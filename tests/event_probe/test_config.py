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

import pytest
from pydantic import ValidationError

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
    # slice thresholds (CON-007 v1 predeclared defaults): the favorite/underdog
    # p_pre cutoff and the early/late match-minute boundary -- pinned BEFORE any
    # run (CON-014), so a post-hoc change to either VOIDs via config_hash.
    assert cfg.favorite_prob_cutoff == 0.50
    assert cfg.late_match_minute == 60
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
    # CON-007: the two predeclared slice thresholds are SEALED too -- tuning the
    # favorite cutoff or the late-match boundary after a run diverges the hash.
    assert ProbeConfig(favorite_prob_cutoff=0.55).config_hash() != base
    assert ProbeConfig(late_match_minute=55).config_hash() != base


def test_config_rejects_unknown_field() -> None:
    # A construction-time typo (e.g. ``n_min_globl``) must RAISE, not silently keep
    # the default and produce an identical hash -- that would be exactly the drift
    # the seal exists to prevent (frozen=True stops post-hoc mutation, not typos).
    with pytest.raises(ValidationError):
        ProbeConfig(nonexistent_threshold=1)


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
    assert sealed["verdict"] == "INCONCLUSIVE"
    assert sealed["global"]["verdict"] == "INCONCLUSIVE"
    assert "overall_verdict" not in sealed  # §4 carries no top-level overall_verdict
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


def test_sealed_result_has_section4_toplevel_fields() -> None:
    # §4 conformance: the sealed dict must carry the top-level fields the schema
    # names -- fixtures, total_goal_events, eligible_events, a top-level verdict
    # alias, the CON-014 defaults note, and the raw-delta medians -- AND merge the
    # per-fixture extraction excludes into excluded_by_reason (so decreasing_score
    # / ambiguous_delta / unparseable appear alongside the compute reasons).
    cfg = ProbeConfig()
    records = [
        _event_record(),  # eligible LAG: delta_imm 0.29, delta_settle 0.65, R 0.45
        _event_record(event_class="NO-SIGNAL", exclusion_reason="below_epsilon", R=None),
    ]
    result = ProbeResult(
        overall_verdict="FADE",
        global_n=1,
        global_median_R=0.45,
        global_ci_low=1.1,
        global_ci_high=1.4,
        per_slice=[],
        class_counts={"LAG": 1, "NO-SIGNAL": 1},
        excluded_by_reason={"below_epsilon": 1},
    )

    sealed = build_sealed_result(
        cfg,
        result,
        records,
        fixtures=[111, 222],
        total_goal_events=7,
        extraction_excluded={"decreasing_score": 2, "ambiguous_delta": 0, "unparseable": 1},
    )

    assert sealed["fixtures"] == [111, 222]
    assert sealed["total_goal_events"] == 7
    assert sealed["eligible_events"] == 1  # directional-set size == result.global_n
    # §4: a single top-level verdict; the nested global.verdict mirrors it; NO
    # redundant top-level overall_verdict.
    assert sealed["verdict"] == "FADE"
    assert sealed["global"]["verdict"] == "FADE"
    assert "overall_verdict" not in sealed
    # CON-014 note: values are v1 predeclared defaults, not optimized.
    assert "CON-014" in sealed["predeclared_defaults_note"]
    # §4: raw-delta medians are NESTED in the global block (not top-level), over
    # the eligible (R-not-None) events (GUD-001).
    assert "raw_delta_imm_median" not in sealed
    assert "raw_delta_settle_median" not in sealed
    assert sealed["global"]["raw_delta_imm_median"] == 0.29
    assert sealed["global"]["raw_delta_settle_median"] == 0.65
    # Extraction reasons merged alongside the compute reason.
    assert sealed["excluded_by_reason"]["below_epsilon"] == 1
    assert sealed["excluded_by_reason"]["decreasing_score"] == 2
    assert sealed["excluded_by_reason"]["ambiguous_delta"] == 0
    assert sealed["excluded_by_reason"]["unparseable"] == 1
    json.dumps(sealed)


def test_sealed_result_raw_delta_medians_none_when_no_eligible() -> None:
    # With no eligible (R-not-None) event, the raw-delta medians are None -- an
    # empty directional set never fabricates a zero move.
    cfg = ProbeConfig()
    records = [
        _event_record(event_class="NO-SIGNAL", exclusion_reason="no_pre_tick", R=None),
    ]
    result = ProbeResult(
        overall_verdict="INCONCLUSIVE",
        global_n=0,
        global_median_R=None,
        global_ci_low=None,
        global_ci_high=None,
        per_slice=[],
        class_counts={"NO-SIGNAL": 1},
        excluded_by_reason={"no_pre_tick": 1},
    )
    sealed = build_sealed_result(cfg, result, records, fixtures=[1], total_goal_events=1)
    assert sealed["eligible_events"] == 0
    assert sealed["global"]["raw_delta_imm_median"] is None
    assert sealed["global"]["raw_delta_settle_median"] is None
