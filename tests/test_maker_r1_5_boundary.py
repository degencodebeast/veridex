import pytest
from pydantic import ValidationError

from veridex.maker.diagnostic import AdverseSelectionReport, FORBIDDEN_FILL_FIELDS


def test_report_has_no_fill_or_pnl_or_edge_fields():
    fields = set(AdverseSelectionReport.model_fields)
    assert fields.isdisjoint(FORBIDDEN_FILL_FIELDS)
    assert AdverseSelectionReport().real_executable_edge_bps is None


def test_trade_derived_fields_are_diagnostic_suffixed():
    trade_fields = {"trade_flow_preceding_fv_move_bps_diagnostic", "toxic_vs_benign_flow_ratio_diagnostic"}
    assert trade_fields <= set(AdverseSelectionReport.model_fields)
    for f in trade_fields:
        assert f.endswith("_diagnostic")


def test_report_is_frozen_and_forbids_smuggled_fill_fields():
    r = AdverseSelectionReport()
    with pytest.raises(ValidationError):
        r.real_executable_edge_bps = 7          # frozen → cannot mutate
    with pytest.raises(ValidationError):
        AdverseSelectionReport(pnl=5, fill_price=0.5)   # extra=forbid → smuggled fill field rejected loudly
