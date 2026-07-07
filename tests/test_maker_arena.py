from veridex.maker.falsification import run_falsification_arena
from veridex.maker.agents import NaiveMarketMakerAgent, TxLineFairMarketMakerAgent

# Fair value RISES each horizon; naive is anchored to a stale-LOW venue mid (0.50) so its ask (0.52)
# sits far below rising fair and is systematically picked off (high toxicity); the TxLINE-FV candidate
# (quotes ~0.60) tracks fair (much lower toxicity) → candidate separates.
_FAIR = {t: 0.60 + (t // 60) * 0.006 for t in range(0, 660, 60)}

def _ref_at(mk, side, ts):
    return _FAIR.get(ts)

def _adverse_tape():
    return [{"ts": t, "fv": 0.60, "mid": 0.50} for t in range(0, 600, 60)]

def test_scorer_has_teeth_candidate_is_less_toxic_and_separates():
    res = run_falsification_arena(
        tape=_adverse_tape(), naive=NaiveMarketMakerAgent(), candidate=TxLineFairMarketMakerAgent(),
        ref_at=_ref_at, horizons_s=(60,), has_trade_reference=True)
    assert res["falsification"].verdict == "SEPARATED"
    assert res["headline"] == "SEPARATED_QUOTE_QUALITY"

def test_no_separation_without_trade_reference_is_inconclusive_not_edge():
    # flat fair + venue mid == fv → naive and candidate quote identically → equal toxicity → CI straddles 0
    flat_fair = {t: 0.60 for t in range(0, 660, 60)}
    res = run_falsification_arena(
        tape=[{"ts": t, "fv": 0.60, "mid": 0.60} for t in range(0, 600, 60)],
        naive=NaiveMarketMakerAgent(), candidate=TxLineFairMarketMakerAgent(),
        ref_at=lambda mk, side, ts: flat_fair.get(ts), horizons_s=(60,), has_trade_reference=False)
    assert res["headline"] == "INCONCLUSIVE"   # AC-017: TxLINE-predicts-itself is not an edge
