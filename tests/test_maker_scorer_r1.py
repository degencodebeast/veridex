from veridex.maker.contracts import Side, TargetQuote, TargetQuoteSet
from veridex.maker.scorer import score_r1_markout, QuoteMarkout

def _ref_source(series):  # {(market_key, ts): fair}
    return lambda mk, side, ts: series.get((mk, ts))

def test_scores_quote_when_future_ref_present():
    series = {("1X2|home|full", 1000): 0.60, ("1X2|home|full", 1060): 0.62}
    qs = [TargetQuoteSet(fixture_id=1, tick_seq=0, ts=1000,
                         quotes=[TargetQuote(side=Side.BID, market_key="1X2|home|full", price=0.58, size=1.0)])]
    marks, acc = score_r1_markout(qs, _ref_source(series), horizons_s=(60,))
    assert acc.scored == 1 and acc.abstained == 0 and marks[0].markout_bps > 0

def test_abstains_when_future_ref_missing_never_imputes():
    series = {("1X2|home|full", 1000): 0.60}   # no t+60 point
    qs = [TargetQuoteSet(fixture_id=1, tick_seq=0, ts=1000,
                         quotes=[TargetQuote(side=Side.BID, market_key="1X2|home|full", price=0.58, size=1.0)])]
    marks, acc = score_r1_markout(qs, _ref_source(series), horizons_s=(60,))
    assert acc.scored == 0 and acc.abstained == 1 and marks == []

def test_no_executable_edge_or_fill_field_on_quote_markout():
    assert "real_executable_edge_bps" not in QuoteMarkout.model_fields
    assert "pnl" not in QuoteMarkout.model_fields
    assert "fill_price" not in QuoteMarkout.model_fields
