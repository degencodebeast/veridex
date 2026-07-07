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

def test_maker_rank_key_has_no_clv_and_module_never_imports_directional_scorer():
    # scope the "no CLV" check to the RANK KEY (a later task adds a window_clv_analog labeled aggregate
    # to this same module, which legitimately contains "clv"); the module must never touch the
    # directional scorer.
    assert "clv" not in inspect.getsource(maker_rank_key).lower()     # CLV never in the rank key
    mod_src = inspect.getsource(lb).lower()
    assert "veridex.scoring" not in mod_src      # no import of the directional scorer
    assert "score_run" not in mod_src            # no call to the directional score_run
    assert "avg_clv" not in mod_src              # no directional CLV field
