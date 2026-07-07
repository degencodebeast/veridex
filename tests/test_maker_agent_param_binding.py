from veridex.maker.config import build_maker_run_config

CP1_18 = (17588229,17588234,17588245,17588325,17588391,17588404,17926593,18167317,
          18172280,18172469,18175918,18175981,18175983,18176123,18179550,18179551,18179759,18179763)

class _FakeAgent:
    def __init__(self, h): self._h = h
    def params_hash_inputs(self): return self._h

def test_agent_param_change_moves_config_hash():
    a = build_maker_run_config(fixture_ids=CP1_18, agents=(_FakeAgent("half_spread=0.02"),)).config_hash()
    b = build_maker_run_config(fixture_ids=CP1_18, agents=(_FakeAgent("half_spread=0.03"),)).config_hash()
    assert a != b   # an agent behavior-param change moves config_hash (SEC-006)

def test_no_agents_leaves_empty_agent_hashes():
    cfg = build_maker_run_config(fixture_ids=CP1_18)
    assert cfg.agent_config_hashes == ()
