import inspect
from veridex.maker.leaderboard import maker_rank_key, rank_makers
import veridex.maker.leaderboard as lb

def test_ranks_by_toxicity_loss_not_mean_markout():
    # The real-seal scenario: the NAIVE control has a HIGHER mean markout (1060 vs 1050)
    # -- because two-sided mean markout is dominated by half_spread/ref_now geometry, not
    # quote quality -- but a WORSE (higher) adverse-selection toxicity loss (80 vs 40).
    # Ranking must follow toxicity (the falsification axis): lower loss wins rank 1.
    ranked = rank_makers([
        {"agent_id": "naive", "avg_markout_bps": 1060, "avg_toxicity_loss_bps": 80, "abstained": 0, "quote_count": 100},
        {"agent_id": "txline-fair", "avg_markout_bps": 1050, "avg_toxicity_loss_bps": 40, "abstained": 0, "quote_count": 100}])
    assert ranked[0]["agent_id"] == "txline-fair" and ranked[0]["maker_rank"] == 1
    assert ranked[1]["agent_id"] == "naive" and ranked[1]["maker_rank"] == 2

def test_none_toxicity_sorts_last():
    ranked = rank_makers([
        {"agent_id": "a", "avg_toxicity_loss_bps": None, "abstained": 5, "quote_count": 5},
        {"agent_id": "b", "avg_toxicity_loss_bps": 10, "abstained": 0, "quote_count": 5}])
    assert ranked[0]["agent_id"] == "b"

def test_rank_makers_empty_list_returns_empty():
    assert rank_makers([]) == []

def test_maker_rank_key_has_no_clv_and_module_never_imports_directional_scorer():
    # scope the "no CLV" check to the RANK KEY (a later task adds a window_clv_analog labeled aggregate
    # to this same module, which legitimately contains "clv"); the module must never touch the
    # directional scorer.
    assert "clv" not in inspect.getsource(maker_rank_key).lower()     # CLV never in the rank key
    mod_src = inspect.getsource(lb).lower()
    assert "veridex.scoring" not in mod_src      # no import of the directional scorer
    assert "score_run" not in mod_src            # no call to the directional score_run
    assert "avg_clv" not in mod_src              # no directional CLV field
