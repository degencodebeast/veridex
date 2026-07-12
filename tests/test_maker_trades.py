import json, pytest
from veridex.maker.trades import TradePrint, AggressorSide, load_trade_prints
from veridex.maker.markout import MarkoutError

def _write(tmp_path, rows):
    p = tmp_path / "trades.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows)); return p

def test_loads_valid_trade_prints(tmp_path):
    p = _write(tmp_path, [{"ts": 1000, "price": 0.59, "size": 120.0,
                           "aggressor_side": "buy", "condition_id": "0xabc", "token_id": "111"}])
    trades = load_trade_prints(p)
    assert len(trades) == 1 and trades[0].aggressor_side == AggressorSide.BUY and trades[0].price == 0.59

def test_rejects_decimal_priced_trade_file(tmp_path):
    p = _write(tmp_path, [{"ts": 1000, "price": 1.69, "size": 1.0,
                           "aggressor_side": "buy", "condition_id": "0xabc", "token_id": "111"}])
    with pytest.raises(MarkoutError):
        load_trade_prints(p)

def test_trade_print_has_no_fill_or_edge_field():
    assert "fill_price" not in TradePrint.model_fields
    assert "real_executable_edge_bps" not in TradePrint.model_fields
    assert "pnl" not in TradePrint.model_fields
