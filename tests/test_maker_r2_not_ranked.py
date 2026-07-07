import pytest

from veridex.maker.leaderboard import assert_bracket_not_ranked, rank_makers


def test_rank_input_carrying_bracket_is_rejected():
    with pytest.raises(AssertionError):
        assert_bracket_not_ranked([{"agent_id": "x", "avg_markout_bps": 10, "bracket": {"neutral": 5}}])


def test_clean_rank_input_passes():
    assert_bracket_not_ranked([{"agent_id": "x", "avg_markout_bps": 10, "abstained": 0, "quote_count": 3}])


def test_rank_makers_rejects_bracket_row_via_guard():
    # locks the HB-12 wiring: rank_makers must call the guard, so a bracket row is rejected THROUGH the ranker
    with pytest.raises(AssertionError):
        rank_makers([{"agent_id": "x", "avg_markout_bps": 10, "abstained": 0,
                      "quote_count": 3, "bracket": {"neutral": 5}}])
