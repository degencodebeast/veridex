import pytest
from veridex.maker.r2_bracket import FillAssumptionConfig, render_sensitivity_bracket

def _cfg():
    return FillAssumptionConfig(fill_model_id="m1", latency_ms=250, cross_rule="mid", partial_fill_policy="none")

def test_bracket_is_labeled_uncalibrated_and_never_ranked():
    b = render_sensitivity_bracket([10, 20, 30], _cfg())
    assert b["label"] == "UNCALIBRATED / declared model overlay"
    assert b["ranked"] is False and b["queue_modeled"] is False
    assert set(b["bracket"]) == {"pessimistic", "neutral", "optimistic"}

def test_queue_modeled_true_is_rejected():
    with pytest.raises(Exception):
        FillAssumptionConfig(fill_model_id="m1", latency_ms=1, cross_rule="mid",
                             partial_fill_policy="none", queue_modeled=True)

def test_fill_assumption_change_moves_config_hash():
    a = _cfg().config_hash()
    b = FillAssumptionConfig(fill_model_id="m2", latency_ms=250, cross_rule="mid",
                             partial_fill_policy="none").config_hash()
    assert a != b
