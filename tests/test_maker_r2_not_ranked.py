import pytest

from veridex.maker.leaderboard import (
    _R2_BRACKET_KEYS,
    assert_bracket_not_ranked,
    rank_makers,
)
from veridex.maker.r2_suite import R2ProtectionAblation, R2SensitivityBracket

# ``real_executable_edge_bps`` is legitimately present on a maker row
# (``aggregate_agent_metrics`` emits it as ``None``) -> it must NEVER be denied.
LEGIT_MAKER_ROW_FIELDS = {"real_executable_edge_bps"}
_R2_FIELDS = (
    set(R2SensitivityBracket.model_fields) | set(R2ProtectionAblation.model_fields)
) - LEGIT_MAKER_ROW_FIELDS


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


def test_guard_rejects_r2_bracket_field_name():
    with pytest.raises(AssertionError):
        assert_bracket_not_ranked([{"agent_id": "x", "r2_bracket": {"neutral": 5}}])


def test_rank_makers_rejects_r2_bracket_field_row():
    # the canonical MakerArenaResult overlay field is named "r2_bracket" — it must be rejected THROUGH the ranker
    with pytest.raises(AssertionError):
        rank_makers([{"agent_id": "a", "avg_markout_bps": 1, "abstained": 0, "quote_count": 1,
                      "r2_bracket": {"ranked": True, "bracket": {"neutral": 999999}}}])


def test_denylist_exhaustively_covers_r2_fields():
    # the static denylist must cover EVERY R2-specific field (minus the legit-overlap)
    assert _R2_FIELDS <= _R2_BRACKET_KEYS


@pytest.mark.parametrize("field", sorted(_R2_FIELDS))
def test_rank_makers_rejects_every_r2_field(field):
    with pytest.raises(AssertionError):
        rank_makers([{"agent_id": "x", "avg_toxicity_loss_bps": 10, "abstained": 0,
                      "quote_count": 1, field: {"x": 1}}])


def test_normal_maker_row_with_edge_none_still_ranks():
    # the excluded field must NOT bar a legit maker row emitted by the scorer
    out = rank_makers([{"agent_id": "x", "avg_toxicity_loss_bps": 10, "abstained": 0,
                        "quote_count": 1, "real_executable_edge_bps": None}])
    assert out and out[0]["maker_rank"] == 1
