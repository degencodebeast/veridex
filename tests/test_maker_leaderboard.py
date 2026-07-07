import inspect
from veridex.maker.leaderboard import maker_rank_key, rank_makers
import veridex.maker.leaderboard as lb

def test_ranks_by_markout_not_clv():
    ranked = rank_makers([
        {"agent_id": "naive-mm", "avg_markout_bps": -20, "abstained": 0, "quote_count": 10},
        {"agent_id": "txline-fair-mm", "avg_markout_bps": 60, "abstained": 0, "quote_count": 10}])
    assert ranked[0]["agent_id"] == "txline-fair-mm" and ranked[0]["maker_rank"] == 1

def test_none_markout_sorts_last():
    ranked = rank_makers([
        {"agent_id": "a", "avg_markout_bps": None, "abstained": 5, "quote_count": 5},
        {"agent_id": "b", "avg_markout_bps": 10, "abstained": 0, "quote_count": 5}])
    assert ranked[0]["agent_id"] == "b"

def test_maker_rank_key_has_no_clv_and_module_never_imports_score_run():
    # scope the "no CLV" check to the RANK KEY (a later task adds a window_clv_analog labeled aggregate
    # to this same module, which legitimately contains "clv"); the module must never touch score_run.
    assert "clv" not in inspect.getsource(maker_rank_key).lower()
    mod_src = inspect.getsource(lb).lower()
    assert "score_run" not in mod_src and "_rank_key" not in mod_src and "avg_clv" not in mod_src
