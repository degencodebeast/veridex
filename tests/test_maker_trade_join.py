from veridex.maker.trades import TradePrint, AggressorSide, join_trades_to_fixture_with_accounting
from veridex.maker.mapping import ResolvedMarketRecord

def _rec(cid, tok, side):
    return ResolvedMarketRecord(condition_id=cid, fixture_id=1, frame_rows=1,
        market_ref=f"1X2|{side}|full", side=side, source_artifact_content_hash=None,
        source_frames_file="f", token_id=tok, venue="polymarket")

def _tp(cid, tok):
    return TradePrint(ts=1000, price=0.5, size=1.0, aggressor_side=AggressorSide.BUY,
                      condition_id=cid, token_id=tok)

def test_join_groups_trades_by_market_ref_and_counts_unmatched():
    recs = [_rec("0xA", "1", "home")]
    joined, unmatched = join_trades_to_fixture_with_accounting(
        [_tp("0xA", "1"), _tp("0xZ", "9")], recs, fixture_id=1)
    assert joined["1X2|home|full"] and len(joined["1X2|home|full"]) == 1
    assert unmatched == 1
