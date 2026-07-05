"""CON-015 goal extraction tests (E1).

Source of truth for a goal is a strictly positive increment in the
carried-forward ``Score["Participant{1,2}"]["Total"]["Goals"]`` value.
``Action`` is advisory only and ``Stats`` is never a trigger.

Fixture shapes here mirror the real recorder output confirmed against
``scripts/txline_live/packs/17588234/scores_17588234.json``:
``Ts`` is 13-digit ms, the ``Score`` block is sparse (present only on some
records), goal actions repeat, and ``Participant1IsHome`` is a top-level bool.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from veridex.backtest.event_probe.extraction import (
    GoalEvent,
    extract_goal_events,
)

_REAL_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "txline_live"
    / "packs"
    / "17588234"
    / "scores_17588234.json"
)


def test_goal_is_score_total_goals_not_stats_not_action():
    """E1-t1: the trigger is Score.Total.Goals, never Stats, never Action."""
    records = [
        {
            "Seq": 1,
            "Ts": 1782500800000,
            "Action": "kickoff",
            "Participant1IsHome": True,
            "Score": {"Participant2": {"Total": {"Goals": 0}}},
        },
        {
            "Seq": 2,
            "Ts": 1782500860000,
            "Action": "goal",
            "Participant1IsHome": True,
            "Score": {"Participant2": {"Total": {"Goals": 1}}},
        },
        # Stats-only movement (corners), no Score at all -> not a goal.
        {
            "Seq": 3,
            "Ts": 1782500900000,
            "Action": "corner",
            "Participant1IsHome": True,
            "Stats": {"8": {"Participant2": 1}},
        },
    ]

    result = extract_goal_events(records)

    assert len(result.events) == 1
    assert isinstance(result.events[0], GoalEvent)
    assert result.events[0].participant == 2


def test_ts_ms_to_seconds():
    """E1-t2: Ts (13-digit ms) is converted to seconds via integer division."""
    records = [
        {"Seq": 1, "Ts": 1782500800000, "Action": "kickoff", "Participant1IsHome": True,
         "Score": {"Participant2": {"Total": {"Goals": 0}}}},
        {"Seq": 2, "Ts": 1782500866926, "Action": "goal", "Participant1IsHome": True,
         "Score": {"Participant2": {"Total": {"Goals": 1}}}},
    ]
    result = extract_goal_events(records)
    assert len(result.events) == 1
    assert result.events[0].t_e == 1782500866       # Ts // 1000, NOT the ms value
    assert result.events[0].scoring_side == "away"  # Participant2, Participant1IsHome=True


def test_sparse_score_carry_forward():
    """E1-t3: records with no Score do not reset the carried count."""
    records = [
        {"Seq": 1, "Ts": 1782500800000, "Action": "goal", "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Goals": 1}}}},
        # A run of records that carry no Score block at all.
        {"Seq": 2, "Ts": 1782500810000, "Action": "possession", "Participant1IsHome": True},
        {"Seq": 3, "Ts": 1782500820000, "Action": "throw_in", "Participant1IsHome": True},
        {"Seq": 4, "Ts": 1782500830000, "Action": "shot", "Participant1IsHome": True},
        {"Seq": 5, "Ts": 1782500840000, "Action": "goal", "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Goals": 2}}}},
    ]
    result = extract_goal_events(records)
    assert len(result.events) == 2
    assert [e.participant for e in result.events] == [1, 1]


def test_repeated_action_goal_dedupes():
    """E1-t4: repeated goal records at the same count collapse to one event."""
    records = [
        {"Seq": 1, "Ts": 1782500860000, "Action": "goal", "Participant1IsHome": True,
         "Score": {"Participant2": {"Total": {"Goals": 1}}}},
        {"Seq": 2, "Ts": 1782500865000, "Action": "goal", "Participant1IsHome": True,
         "Score": {"Participant2": {"Total": {"Goals": 1}}}},
        {"Seq": 3, "Ts": 1782500870000, "Action": "goal", "Participant1IsHome": True,
         "Score": {"Participant2": {"Total": {"Goals": 1}}}},
    ]
    result = extract_goal_events(records)
    assert len(result.events) == 1


def test_goal_kick_is_not_a_goal():
    """E1-t5: Action=='goal_kick' with no Score change is not a goal."""
    records = [
        {"Seq": 1, "Ts": 1782500800000, "Action": "kickoff", "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Goals": 0}}}},
        {"Seq": 2, "Ts": 1782500850000, "Action": "goal_kick", "Participant1IsHome": True},
    ]
    result = extract_goal_events(records)
    assert result.events == []


def test_stats_only_change_is_not_a_goal():
    """E1-t6: incrementing Stats['8'] (corners) with unchanged Goals is not a goal."""
    records = [
        {"Seq": 1, "Ts": 1782500800000, "Action": "corner", "Participant1IsHome": True,
         "Score": {"Participant2": {"Total": {"Corners": 1, "Goals": 0}}},
         "Stats": {"8": {"Participant2": 1}}},
        {"Seq": 2, "Ts": 1782500850000, "Action": "corner", "Participant1IsHome": True,
         "Score": {"Participant2": {"Total": {"Corners": 2, "Goals": 0}}},
         "Stats": {"8": {"Participant2": 2}}},
    ]
    result = extract_goal_events(records)
    assert result.events == []


def test_total_without_goals_key_is_carry_forward_not_unparseable():
    """E1-t6b: a Total with NO Goals key is a carry-forward, not unparseable.

    `Total` is a sparse stat-map; a corners-only update omits `Goals` entirely
    (distinct from t6, where `Goals` is present as 0). Such a record must produce
    no event, must NOT be counted as `unparseable` or `ambiguous_delta`, and must
    not disturb the carried count -- so a later `Goals: 1` still yields one event.
    """
    records = [
        {"Seq": 1, "Ts": 1782500800000, "Action": "kickoff", "Participant1IsHome": True,
         "Score": {"Participant2": {"Total": {"Goals": 0}}}},
        # Corners-only update: Total present, but no `Goals` key at all.
        {"Seq": 2, "Ts": 1782500850000, "Action": "corner", "Participant1IsHome": True,
         "Score": {"Participant2": {"Total": {"Corners": 1}}}},
        {"Seq": 3, "Ts": 1782500900000, "Action": "goal", "Participant1IsHome": True,
         "Score": {"Participant2": {"Total": {"Goals": 1}}}},
    ]
    result = extract_goal_events(records)
    assert len(result.events) == 1
    assert result.events[0].participant == 2
    assert result.events[0].t_e == 1782500900       # the later Goals:1 record
    assert result.excluded["unparseable"] == 0
    assert result.excluded["ambiguous_delta"] == 0


def test_decreasing_total_goals_rejected():
    """E1-t7: a VAR overturn (Goals decreases) is rejected and counted."""
    records = [
        {"Seq": 1, "Ts": 1782500800000, "Action": "goal", "Participant1IsHome": True,
         "Score": {"Participant2": {"Total": {"Goals": 2}}}},
        {"Seq": 2, "Ts": 1782500850000, "Action": "var_end", "Participant1IsHome": True,
         "Score": {"Participant2": {"Total": {"Goals": 1}}}},
    ]
    result = extract_goal_events(records)
    # The 2->? step: Seq1 lifts carried 0->2 (one event); Seq2 is the decrease.
    assert all(e.participant == 2 for e in result.events)
    assert not any(e.t_e == 1782500850 for e in result.events)
    assert result.excluded["decreasing_score"] == 1


def test_both_participants_increment_ambiguous():
    """E1-t8: both participants incrementing in one record is ambiguous."""
    records = [
        {"Seq": 1, "Ts": 1782500800000, "Action": "kickoff", "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Goals": 0}},
                   "Participant2": {"Total": {"Goals": 0}}}},
        {"Seq": 2, "Ts": 1782500850000, "Action": "goal", "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Goals": 1}},
                   "Participant2": {"Total": {"Goals": 1}}}},
    ]
    result = extract_goal_events(records)
    assert result.events == []
    assert result.excluded["ambiguous_delta"] == 1


def test_missing_or_unparseable_score_counted():
    """E1-t9: a malformed Score path is counted as unparseable, never crashes."""
    records = [
        {"Seq": 1, "Ts": 1782500800000, "Action": "goal", "Participant1IsHome": True,
         # Participant2 present but Total is missing entirely on the path we need.
         "Score": {"Participant2": {"H1": {"Goals": 1}}}},
    ]
    result = extract_goal_events(records)
    assert result.excluded["unparseable"] >= 1
    assert result.events == []


def test_scoring_side_maps_via_participant1ishome():
    """E1-t10: home/away label derives from top-level Participant1IsHome."""
    home_records = [
        {"Seq": 1, "Ts": 1782500800000, "Action": "kickoff", "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Goals": 0}},
                   "Participant2": {"Total": {"Goals": 0}}}},
        {"Seq": 2, "Ts": 1782500810000, "Action": "goal", "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Goals": 1}}}},
        {"Seq": 3, "Ts": 1782500820000, "Action": "goal", "Participant1IsHome": True,
         "Score": {"Participant2": {"Total": {"Goals": 1}}}},
    ]
    result = extract_goal_events(home_records)
    by_part = {e.participant: e.scoring_side for e in result.events}
    assert by_part == {1: "home", 2: "away"}

    # Participant1IsHome=False inverts the labels.
    away_records = [
        {"Seq": 1, "Ts": 1782500800000, "Action": "kickoff", "Participant1IsHome": False,
         "Score": {"Participant1": {"Total": {"Goals": 0}},
                   "Participant2": {"Total": {"Goals": 0}}}},
        {"Seq": 2, "Ts": 1782500810000, "Action": "goal", "Participant1IsHome": False,
         "Score": {"Participant1": {"Total": {"Goals": 1}}}},
        {"Seq": 3, "Ts": 1782500820000, "Action": "goal", "Participant1IsHome": False,
         "Score": {"Participant2": {"Total": {"Goals": 1}}}},
    ]
    result = extract_goal_events(away_records)
    by_part = {e.participant: e.scoring_side for e in result.events}
    assert by_part == {1: "away", 2: "home"}


def test_sorted_by_seq_fallback_ts():
    """E1-t11: out-of-order records are processed in Seq order for carry-forward."""
    # Supplied out of Seq order; correct processing yields a clean 0->1->2 climb.
    records = [
        {"Seq": 3, "Ts": 1782500830000, "Action": "goal", "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Goals": 2}}}},
        {"Seq": 1, "Ts": 1782500810000, "Action": "kickoff", "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Goals": 0}}}},
        {"Seq": 2, "Ts": 1782500820000, "Action": "goal", "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Goals": 1}}}},
    ]
    result = extract_goal_events(records)
    assert len(result.events) == 2
    # Events emitted in processed (Seq) order: the 0->1 step, then 1->2.
    assert [e.t_e for e in result.events] == [1782500820, 1782500830]


@pytest.mark.skipif(not _REAL_FIXTURE.exists(), reason="real score fixture not present")
def test_real_fixture_extracts_expected_goals():
    """E1-real: the recorded WC fixture 17588234 finishes 1-4 (5 goals).

    Locks the sparse-`Total` stat-map shape: a participant block whose `Total`
    dict has no `Goals` key (e.g. a corners/cards-only update) is a carry-forward,
    not an `unparseable` reject. No record legitimately increments both
    participants at once, so `ambiguous_delta` must stay zero.
    """
    records = json.loads(_REAL_FIXTURE.read_text())
    result = extract_goal_events(records)

    by_participant = {1: 0, 2: 0}
    for event in result.events:
        by_participant[event.participant] += 1

    assert len(result.events) == 5
    assert by_participant == {1: 1, 2: 4}          # final score P1=1, P2=4
    assert result.excluded["ambiguous_delta"] == 0
    assert result.excluded["decreasing_score"] == 0
    assert result.excluded["unparseable"] == 0
    # Ts is 13-digit ms; t_e must land in the 10-digit seconds range.
    assert all(1_000_000_000 <= e.t_e <= 9_999_999_999 for e in result.events)
