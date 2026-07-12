from veridex.maker.config import build_maker_run_config
from veridex.maker.r2_bracket import FillAssumptionConfig

CP1_18 = (17588229,17588234,17588245,17588325,17588391,17588404,17926593,18167317,
          18172280,18172469,18175918,18175981,18175983,18176123,18179550,18179551,18179759,18179763)

def _fa(latency):
    return FillAssumptionConfig(fill_model_id="m1", latency_ms=latency, cross_rule="mid", partial_fill_policy="none")

def test_fill_assumption_change_moves_run_config_hash():
    a = build_maker_run_config(fixture_ids=CP1_18, fill_assumption=_fa(250)).config_hash()
    b = build_maker_run_config(fixture_ids=CP1_18, fill_assumption=_fa(500)).config_hash()
    assert a != b   # CON-003/AC-012: a fill-assumption change moves the run config_hash

def test_no_fill_assumption_leaves_hash_none():
    cfg = build_maker_run_config(fixture_ids=CP1_18)
    assert cfg.fill_assumption_hash is None
