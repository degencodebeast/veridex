"""CON-015 CONFIRMED-goal extraction tests (event-study leg).

`extract_confirmed_goal_events` has the same contract as `extract_goal_events`
(a goal is a strictly-positive increment in the sparse-carried
`Score["Participant{1,2}"]["Total"]["Goals"]`; `Action` is advisory, `Stats`
is never a trigger) PLUS a reversal lifecycle: a later score DECREASE that
undoes a prior increment RETRACTS the earlier provisional goal -- it must not
stand as a confirmed goal, and is recorded separately in ``reversed`` instead.

The invariant these finalize semantics buy us: for every fixture the count of
CONFIRMED events per side equals the FINAL carried
``Score.Participant{side}.Total.Goals`` (internal consistency on one feed).
"""

from __future__ import annotations

import pytest

from veridex.backtest.event_probe.extraction import (
    ConfirmedExtractionResult,
    UnsupportedScoreTransition,
    confirmed_matches_final_carried,
    extract_confirmed_goal_events,
    extract_goal_events,
)


def test_unconfirmed_then_confirmed_repeat_is_one_goal() -> None:
    """C-t1: an unconfirmed goal row then a confirmed repeat at the same score
    line collapses to exactly ONE confirmed goal (dedup by carry-forward)."""
    records = [
        {"Seq": 1, "Ts": 1782500860000, "Action": "goal", "Participant1IsHome": True,
         "Score": {"Participant2": {"Total": {"Goals": 1}}}},
        # Same score line repeated (confirmation), not a second goal.
        {"Seq": 2, "Ts": 1782500865000, "Action": "goal", "Participant1IsHome": True,
         "Score": {"Participant2": {"Total": {"Goals": 1}}}},
    ]
    result = extract_confirmed_goal_events(records)
    assert isinstance(result, ConfirmedExtractionResult)
    assert len(result.events) == 1
    assert result.events[0].participant == 2
    assert result.reversed == []


def test_penalty_outcome_increment_is_confirmed_goal() -> None:
    """C-t2: a `penalty_outcome` record that increments the score is ONE
    confirmed goal -- proves Action is advisory, not a required trigger."""
    records = [
        {"Seq": 1, "Ts": 1782500800000, "Action": "kickoff", "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Goals": 0}}}},
        {"Seq": 2, "Ts": 1782500900000, "Action": "penalty_outcome", "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Goals": 1}}}},
    ]
    result = extract_confirmed_goal_events(records)
    assert len(result.events) == 1
    assert result.events[0].participant == 1
    assert result.events[0].scoring_side == "home"
    assert result.reversed == []


def test_own_goal_shaped_attributes_to_score_line_side() -> None:
    """C-t3: an own-goal-shaped record (the OTHER participant's Total
    increments) is a confirmed goal attributed to the score-line side; the
    scorer's identity is irrelevant here."""
    records = [
        {"Seq": 1, "Ts": 1782500800000, "Action": "kickoff", "Participant1IsHome": True,
         "Score": {"Participant2": {"Total": {"Goals": 0}}}},
        # Team 1 attacks but it is booked as Participant2's goal (own goal).
        {"Seq": 2, "Ts": 1782500900000, "Action": "own_goal", "Participant1IsHome": True,
         "Score": {"Participant2": {"Total": {"Goals": 1}}}},
    ]
    result = extract_confirmed_goal_events(records)
    assert len(result.events) == 1
    assert result.events[0].participant == 2
    assert result.events[0].scoring_side == "away"  # Participant2, Participant1IsHome=True
    assert result.reversed == []


def test_confirmed_goal_then_reversal_moves_to_reversed() -> None:
    """C-t4 (key new behavior): a confirmed goal followed by an
    `action_discarded`/score-decrease that reverses it is NOT in ``events`` and
    IS in ``reversed``. The trigger is the score decrease, not the Action."""
    records = [
        {"Seq": 1, "Ts": 1782500860000, "Action": "goal", "Participant1IsHome": True,
         "Score": {"Participant2": {"Total": {"Goals": 1}}}},
        # VAR overturn: score decreases back to 0 -> the provisional goal is void.
        {"Seq": 2, "Ts": 1782500920000, "Action": "action_discarded", "Participant1IsHome": True,
         "Score": {"Participant2": {"Total": {"Goals": 0}}}},
    ]
    result = extract_confirmed_goal_events(records)
    assert result.events == []
    assert len(result.reversed) == 1
    assert result.reversed[0].participant == 2
    assert result.reversed[0].t_e == 1782500860  # the original provisional goal


def test_increment_reverse_then_rescore_final_count() -> None:
    """C-t5: increment -> reverse -> re-score (goal disallowed, then a real
    later goal) yields the correct final confirmed count of one."""
    records = [
        {"Seq": 1, "Ts": 1782500860000, "Action": "goal", "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Goals": 1}}}},
        {"Seq": 2, "Ts": 1782500900000, "Action": "action_discarded", "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Goals": 0}}}},
        # A genuine later goal for the same side.
        {"Seq": 3, "Ts": 1782500990000, "Action": "goal", "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Goals": 1}}}},
    ]
    result = extract_confirmed_goal_events(records)
    assert len(result.events) == 1
    assert result.events[0].t_e == 1782500990  # the real, later goal
    assert len(result.reversed) == 1
    assert result.reversed[0].t_e == 1782500860  # the disallowed one


def test_sparse_carry_forward_never_resets_or_reverses() -> None:
    """C-t6: records with no `Score`, or a `Total` with no `Goals` key, are
    pure carry-forwards -- they never reset the count and never look like a
    reversal."""
    records = [
        {"Seq": 1, "Ts": 1782500800000, "Action": "goal", "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Goals": 1}}}},
        {"Seq": 2, "Ts": 1782500810000, "Action": "possession", "Participant1IsHome": True},
        # Corners-only update: Total present, no Goals key.
        {"Seq": 3, "Ts": 1782500820000, "Action": "corner", "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Corners": 1}}}},
        {"Seq": 4, "Ts": 1782500840000, "Action": "goal", "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Goals": 2}}}},
    ]
    result = extract_confirmed_goal_events(records)
    assert len(result.events) == 2
    assert [e.participant for e in result.events] == [1, 1]
    assert result.reversed == []


def test_empty_pack_yields_no_events_no_crash() -> None:
    """C-t7: an empty score pack (`[]` / a 2-byte file) yields zero events and
    zero reversed goals without crashing."""
    result = extract_confirmed_goal_events([])
    assert result.events == []
    assert result.reversed == []
    assert isinstance(result.excluded, dict)


def test_fixture_invariant_confirmed_equals_final_carried_with_reversal() -> None:
    """C-t8: on a canned multi-goal fixture that INCLUDES a reversal, the
    confirmed-events-per-side count equals the final carried score. The old
    extractor (which never retracts) over-counts on this same fixture."""
    records = [
        # P1 climbs 0 -> 1 -> 2 -> 3.
        {"Seq": 1, "Ts": 1782500810000, "Action": "goal", "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Goals": 1}}}},
        {"Seq": 2, "Ts": 1782500820000, "Action": "goal", "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Goals": 2}}}},
        {"Seq": 3, "Ts": 1782500830000, "Action": "goal", "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Goals": 3}}}},
        # P2 scores once.
        {"Seq": 4, "Ts": 1782500840000, "Action": "goal", "Participant1IsHome": True,
         "Score": {"Participant2": {"Total": {"Goals": 1}}}},
        # VAR overturns P1's third goal: 3 -> 2 (final score P1=2, P2=1).
        {"Seq": 5, "Ts": 1782500850000, "Action": "action_discarded", "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Goals": 2}}}},
    ]

    result = extract_confirmed_goal_events(records)
    by_participant = {1: 0, 2: 0}
    for event in result.events:
        by_participant[event.participant] += 1

    assert by_participant == {1: 2, 2: 1}  # confirmed == final carried
    assert len(result.reversed) == 1
    assert confirmed_matches_final_carried(records) is True

    # The old extractor never retracts, so it over-counts on this same feed.
    old = extract_goal_events(records)
    old_by_participant = {1: 0, 2: 0}
    for event in old.events:
        old_by_participant[event.participant] += 1
    assert old_by_participant == {1: 3, 2: 1}  # 3 P1 goals stand -> mismatch vs final 2


def test_multi_goal_jump_is_unsupported_not_crash() -> None:
    """C-t9: a single record jumping P1 0->2 cannot map to one goal-per-record
    (a goal record was lost) -> fail closed with a typed, auditable exception the
    caller converts to a coverage exclusion, NOT a bare AssertionError, and NEVER
    a synthesized goal time."""
    records = [
        {"Seq": 1, "Ts": 1782500860000, "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Goals": 2}}}},
    ]
    with pytest.raises(UnsupportedScoreTransition):
        extract_confirmed_goal_events(records)


def test_negative_score_is_unsupported_not_crash() -> None:
    """C-t10: an invalid negative score (0->1->-1) must fail closed with the typed
    exception, not an IndexError from popping an empty standing stack."""
    records = [
        {"Seq": 1, "Ts": 1782500860000, "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Goals": 1}}}},
        {"Seq": 2, "Ts": 1782500870000, "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Goals": -1}}}},
    ]
    with pytest.raises(UnsupportedScoreTransition):
        extract_confirmed_goal_events(records)


def test_decrease_beyond_standing_is_unsupported_not_crash() -> None:
    """C-t11: a decrease that would pop below the standing stack (0->2 built as
    two goals, then a jump-decrease straight to -1) fails closed with the typed
    exception rather than crashing mid-batch."""
    records = [
        {"Seq": 1, "Ts": 1782500860000, "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Goals": 1}}}},
        {"Seq": 2, "Ts": 1782500870000, "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Goals": 2}}}},
        {"Seq": 3, "Ts": 1782500880000, "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Goals": -1}}}},
    ]
    with pytest.raises(UnsupportedScoreTransition):
        extract_confirmed_goal_events(records)
