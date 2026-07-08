"""E7 — Full R2 report-only sensitivity suite + protection ablation.

R2 is a DECLARED MODEL OVERLAY driven ONLY by the pinned ex-ante fill assumption.
Every bracket/ablation is quadruple-labeled, never ranked, carries no fill /
fill-rate / spread-capture-as-PnL / realized-PnL / executable-edge field, and is
seed-pinned deterministic (a distribution over n_paths, never a cherry-picked
single path).
"""

import pytest

from veridex.maker.r2_bracket import FillAssumptionConfig
from veridex.maker.r2_suite import (
    R2SensitivityScenario,
    render_protection_ablation,
    render_r2_suite,
)

QUAD_LABEL = "REPORT_ONLY / UNCALIBRATED / DECLARED_MODEL_OVERLAY / NOT_A_FILL_PROOF"


def _cfg(**kw):
    base = dict(
        fill_model_id="m1",
        latency_ms=250,
        cross_rule="mid",
        partial_fill_policy="none",
        fill_probability_rule="static_fill_prob",
        rule_params={"p": 0.2},
        draw_mode="DETERMINISTIC_EXPECTED",
        seed=None,
        n_paths=None,
        ex_ante_fields=["quote_price", "quoted_half_spread"],
    )
    base.update(kw)
    return FillAssumptionConfig(**base)


def test_r2_suite_labels_and_no_edge():
    b = render_r2_suite([10, 20, 30], _cfg())
    assert b.ranked is False and b.queue_modeled is False and b.fill_proof is False
    assert b.uses_real_orderbook is False and b.uses_own_fills is False
    assert b.real_executable_edge_bps is None
    assert b.label == QUAD_LABEL
    assert b.fill_rule_source == "pinned_config"
    assert isinstance(b.forbidden_trigger_assertion, str) and b.forbidden_trigger_assertion
    dump = b.model_dump()
    assert "realized_pnl" not in dump or dump["realized_pnl"] is None
    # every data field is simulated_/model_/assumption_-prefixed provenance
    assert b.simulated_expected_inventory_path is not None
    assert b.simulated_spread_capture_range is not None


def test_r2_seeded_is_deterministic_and_distributional():
    cfg = _cfg(draw_mode="SEEDED_STOCHASTIC", seed=1234, n_paths=200, rule_params={"p": 0.5})
    a = render_r2_suite([10, 20, 30, 40], cfg)
    b = render_r2_suite([10, 20, 30, 40], cfg)
    # same seed -> byte-identical output (never a cherry-picked single path)
    assert a == b
    assert a.model_dump() == b.model_dump()
    # the seeded output is a DISTRIBUTION: mean + percentiles, not a lone path
    path = a.simulated_expected_inventory_path
    assert path["draw_mode"] == "SEEDED_STOCHASTIC"
    assert path["seed"] == 1234 and path["n_paths"] == 200
    for key in ("mean_path", "mean_final", "p10_final", "p50_final", "p90_final"):
        assert key in path
    assert path["p10_final"] <= path["p50_final"] <= path["p90_final"]
    assert isinstance(path["mean_path"], list) and len(path["mean_path"]) == 4
    # a different seed changes the drawn distribution
    other = render_r2_suite([10, 20, 30, 40], _cfg(
        draw_mode="SEEDED_STOCHASTIC", seed=9999, n_paths=200, rule_params={"p": 0.5}))
    assert other.simulated_expected_inventory_path["mean_path"] != path["mean_path"]


def test_deterministic_peak_exposure_scans_full_path():
    # M-1: "peak exposure" must scan the WHOLE expected_path, not just the
    # final endpoint. markouts=[200, -100] with p=0.5 -> expected_path =
    # [1.0, 0.5]; the true peak abs-exposure is 1.0 (hit after markout #1),
    # not abs(expected_final) == 0.5.
    cfg = _cfg(rule_params={"p": 0.5})
    b = render_r2_suite([200, -100], cfg)
    path = b.simulated_expected_inventory_path
    assert path["expected_path"] == [1.0, 0.5]
    assert path["expected_final"] == 0.5
    assert b.simulated_expected_exposure["model_peak_exposure"] == 1.0


def test_r2_deterministic_labels_inventory_as_expected_model():
    cfg = _cfg(draw_mode="DETERMINISTIC_EXPECTED")
    b = render_r2_suite([10, 20, 30], cfg)
    path = b.simulated_expected_inventory_path
    assert path["draw_mode"] == "DETERMINISTIC_EXPECTED"
    assert "expected_path" in path and "expected_final" in path
    # expected/model, not shares held
    assert "expected" in path["note"] and "shares held" in path["note"]


def test_protection_ablation_is_declared_overlay():
    cfg_on = _cfg(fill_model_id="on", rule_params={"p": 0.3})
    cfg_off = _cfg(fill_model_id="off", rule_params={"p": 0.6})
    abl = render_protection_ablation([10, 20, 30], cfg_on, cfg_off)
    # both sides carry the four labels + never ranked
    for side in (abl.protection_on, abl.protection_off):
        assert side.label == QUAD_LABEL
        assert side.ranked is False and side.queue_modeled is False
        assert side.fill_proof is False and side.uses_real_orderbook is False
        assert side.uses_own_fills is False and side.real_executable_edge_bps is None
    assert abl.label == QUAD_LABEL
    # event_gate_cost is a model overlay, not a realized number
    assert isinstance(abl.event_gate_cost, dict)
    assert "model_inventory_delta" in abl.event_gate_cost
    dump = abl.model_dump()
    assert "realized_pnl" not in dump
    # the ablation is never a rankable/executable claim
    assert "declared model overlay" in abl.delta_note


def test_r2_sensitivity_scenario_matches_spec_44():
    # M3/§4.4: R2SensitivityScenario is the declared-overlay corner CONTRACT,
    # exactly the six spec fields, guards pinned False, mode enumerated.
    assert set(R2SensitivityScenario.model_fields) == {
        "scenario_id",
        "mode",
        "fill_assumption_hash",
        "label",
        "ranked",
        "queue_modeled",
    }
    cfg = _cfg()
    s = R2SensitivityScenario(
        scenario_id="s1",
        mode="neutral",
        fill_assumption_hash=cfg.config_hash(),
    )
    assert s.mode == "neutral"
    assert s.ranked is False and s.queue_modeled is False
    assert s.label == QUAD_LABEL
    assert s.fill_assumption_hash == cfg.config_hash()
    # all three modes construct
    for mode in ("pessimistic", "neutral", "optimistic"):
        R2SensitivityScenario(scenario_id="s", mode=mode, fill_assumption_hash="h")
    # ranked=True is structurally rejected (never rankable)
    with pytest.raises(Exception):
        R2SensitivityScenario(
            scenario_id="s", mode="neutral", fill_assumption_hash="h", ranked=True
        )
    # queue_modeled=True is structurally rejected (no depth at R2)
    with pytest.raises(Exception):
        R2SensitivityScenario(
            scenario_id="s", mode="neutral", fill_assumption_hash="h", queue_modeled=True
        )
    # an out-of-enum mode is rejected
    with pytest.raises(Exception):
        R2SensitivityScenario(scenario_id="s", mode="bogus", fill_assumption_hash="h")
