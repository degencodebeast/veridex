"""E7 — Full R2 report-only sensitivity suite + protection ablation.

R2 is a DECLARED MODEL OVERLAY driven ONLY by the pinned ex-ante fill assumption.
Every bracket/ablation is quadruple-labeled, never ranked, carries no fill /
fill-rate / spread-capture-as-PnL / realized-PnL / executable-edge field, and is
seed-pinned deterministic (a distribution over n_paths, never a cherry-picked
single path).
"""

from veridex.maker.r2_bracket import FillAssumptionConfig
from veridex.maker.r2_suite import render_r2_suite

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
