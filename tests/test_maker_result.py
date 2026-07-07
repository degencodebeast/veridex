import veridex.maker.result as result_mod
from veridex.maker.result import MakerArenaResult
from veridex.maker.contracts import MakerRungLabel

def test_result_pins_edge_none_and_n18_and_smalln():
    r = MakerArenaResult(protocol_id="maker-arena-v1", config_hash="abc",
        rung=MakerRungLabel("MM-R1"), fixtures=tuple(range(18)), per_agent=[],
        maker_leaderboard=[], falsification={}, fixture_universe_n=18,
        excluded_by_reason={})
    assert r.real_executable_edge_bps is None and r.fixture_universe_n == 18 and r.small_n_flag is True

def test_result_module_never_imports_score_run():
    import inspect
    src = inspect.getsource(result_mod)
    assert "score_run" not in src and "report.leaderboard" not in src

def test_maker_lane_does_not_mutate_directional_leaderboard():
    from veridex.maker.result import assert_score_run_untouched
    before = [{"agent_id": "drift", "rank": 1, "avg_clv_bps": 12}]
    after = [dict(x) for x in before]
    assert_score_run_untouched(before, after)  # identical → no raise
