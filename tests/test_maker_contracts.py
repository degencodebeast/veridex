import pytest
from pydantic import ValidationError
from veridex.maker.contracts import Side, TargetQuote, TargetQuoteSet, MakerRungLabel

def test_target_quote_is_frozen_and_forbids_extra():
    q = TargetQuote(side=Side.BID, market_key="1X2|home|full", price=0.58, size=100.0)
    assert q.post_only is True and q.reason == "quote"
    with pytest.raises(ValidationError):
        TargetQuote(side=Side.BID, market_key="k", price=0.5, size=1.0, bogus=1)  # extra=forbid
    with pytest.raises(ValidationError):
        q.price = 0.6  # frozen

def test_empty_quote_set_is_valid_abstention():
    s = TargetQuoteSet(fixture_id=17588229, tick_seq=0, ts=1000, quotes=[])
    assert s.quotes == [] and s.regime == "QUIET"

def test_rung_label_enum_values():
    assert MakerRungLabel("MM-R1.5").value == "MM-R1.5"
    assert {r.value for r in MakerRungLabel} == {"MM-R1","MM-R1.5","MM-R2","MM-R3","MM-R4"}
