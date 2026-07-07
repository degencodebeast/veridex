import pytest

from veridex.maker.leaderboard import assert_bracket_not_ranked


def test_rank_input_carrying_bracket_is_rejected():
    with pytest.raises(AssertionError):
        assert_bracket_not_ranked([{"agent_id": "x", "avg_markout_bps": 10, "bracket": {"neutral": 5}}])


def test_clean_rank_input_passes():
    assert_bracket_not_ranked([{"agent_id": "x", "avg_markout_bps": 10, "abstained": 0, "quote_count": 3}])
