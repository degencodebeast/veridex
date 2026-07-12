"""CON-015 lifecycle-identity retraction + non-circular truth-gate tests.

These cover the phantom-goal fix: the feed retracts a provisional goal by
DELETING the ``Total.Goals`` key in a same-``Id`` ``action_discarded`` full-score
restatement (a key-deletion the old carry-forward reader swallowed). Retraction
is now keyed on stable feed ``Id`` -> standing score-increment event, NOT on
key-absence. Plus the terminal-snapshot / external-manifest acceptance gates that
replace the circular ``confirmed_matches_final_carried`` oracle.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from veridex.backtest.event_probe.extraction import (
    UnsupportedScoreTransition,
    confirmed_matches_terminal_snapshot,
    extract_confirmed_goal_events,
    terminal_snapshot_score,
    validate_against_manifest,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FM_PACK = _REPO_ROOT / "scripts" / "txline_live" / "packs" / "18209181" / "scores_18209181.json"
_FM_FIXTURE_ID = 18209181


def _load_fm_records() -> list[dict]:
    with _FM_PACK.open() as handle:
        return json.load(handle)


def test_fm_id495_key_deletion_discard_yields_2_0() -> None:
    """RED-1: real France-Morocco pack. Seq 534 Id=495 is a provisional Morocco
    goal; Seq 535 (same Id=495, ``action_discarded``) DELETES the Goals key. The
    old reader swallowed it and emitted France 2-1. Fixed: France 2-0, the Morocco
    provisional is in ``reversed`` (provisional_goal_reversed), not ``events``."""
    records = _load_fm_records()
    result = extract_confirmed_goal_events(records)

    by_side = {"home": 0, "away": 0}
    for event in result.events:
        by_side[event.scoring_side] += 1
    # Participant1IsHome=True -> France is home (2), Morocco is away (0).
    assert by_side == {"home": 2, "away": 0}

    # The discarded Morocco provisional (participant 2) is reversed, not confirmed.
    assert any(e.participant == 2 for e in result.reversed)
    assert all(e.participant == 1 for e in result.events)


def test_corners_only_sparse_update_without_discard_carries_goals_forward() -> None:
    """RED-2: an ordinary corners-only sparse update (Total present, no Goals key,
    NO ``action_discarded``, no matching Id) is a pure carry-forward -- it must not
    retract the standing goal."""
    records = [
        {"Seq": 1, "Ts": 1782500800000, "Id": 10, "Action": "goal", "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Goals": 1}}}},
        {"Seq": 2, "Ts": 1782500810000, "Id": 11, "Action": "corner", "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Corners": 3}}}},
    ]
    result = extract_confirmed_goal_events(records)
    assert len(result.events) == 1
    assert result.reversed == []


def test_action_discarded_for_corner_id_does_not_retract_goal() -> None:
    """RED-3: an ``action_discarded`` whose Id belongs to a corner/shot (NOT a
    standing goal) must not touch goal state -- the standing goal stays confirmed."""
    records = [
        {"Seq": 1, "Ts": 1782500800000, "Id": 20, "Action": "goal", "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Goals": 1}}}},
        # A corner (Id 21) is later discarded; goals still carried/present, untouched.
        {"Seq": 2, "Ts": 1782500810000, "Id": 21, "Action": "action_discarded", "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Goals": 1, "Corners": 2}}}},
    ]
    result = extract_confirmed_goal_events(records)
    assert len(result.events) == 1
    assert result.events[0].participant == 1
    assert result.reversed == []


def test_same_id_goal_discard_with_numeric_decrease_retracts_once() -> None:
    """RED-4: a same-Id goal discard that ALSO carries a numeric decrease in the
    same row must retract exactly ONCE (idempotent), never double-pop."""
    records = [
        {"Seq": 1, "Ts": 1782500800000, "Id": 30, "Action": "goal", "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Goals": 1}}}},
        # Same Id 30 discarded AND the score numerically decreases back to 0.
        {"Seq": 2, "Ts": 1782500810000, "Id": 30, "Action": "action_discarded", "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Goals": 0}}}},
    ]
    result = extract_confirmed_goal_events(records)
    assert result.events == []
    assert len(result.reversed) == 1  # retracted once, not twice
    assert result.reversed[0].participant == 1


def test_key_deletion_discard_then_later_real_goal_emits_later_timestamp() -> None:
    """RED-5: a key-deletion discard (Id-matched) followed by a genuine later goal
    emits the LATER timestamp, and the discarded provisional is reversed."""
    records = [
        {"Seq": 1, "Ts": 1782500860000, "Id": 40, "Action": "goal", "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Goals": 1}}}},
        # Key-deletion retraction: Total present, Goals key removed, same Id 40.
        {"Seq": 2, "Ts": 1782500900000, "Id": 40, "Action": "action_discarded", "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Corners": 1}}}},
        # A genuine later goal for the same side.
        {"Seq": 3, "Ts": 1782500990000, "Id": 41, "Action": "goal", "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Goals": 1}}}},
    ]
    result = extract_confirmed_goal_events(records)
    assert len(result.events) == 1
    assert result.events[0].t_e == 1782500990  # the later, real goal
    assert len(result.reversed) == 1
    assert result.reversed[0].t_e == 1782500860  # the discarded provisional


def test_penalty_shaped_increment_then_same_id_deletion_retracts() -> None:
    """RED-6: a penalty/own-goal-shaped increment (Action != 'goal') still enters
    the lifecycle index; a same-Id key-deletion discard retracts it."""
    records = [
        {"Seq": 1, "Ts": 1782500800000, "Id": 50, "Action": "penalty_outcome", "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Goals": 1}}}},
        {"Seq": 2, "Ts": 1782500810000, "Id": 50, "Action": "action_discarded", "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Corners": 1}}}},
    ]
    result = extract_confirmed_goal_events(records)
    assert result.events == []
    assert len(result.reversed) == 1
    assert result.reversed[0].participant == 1


def test_unknown_id_deletion_of_standing_goal_fails_closed() -> None:
    """RED-7: an ``action_discarded`` that DELETES a standing Goals key but whose
    Id matches NO standing score-increment event fails closed with a typed
    exception (the caller converts it to a named coverage exclusion). No Ts-only
    guessing."""
    records = [
        {"Seq": 1, "Ts": 1782500800000, "Id": 60, "Action": "goal", "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Goals": 1}}}},
        # Deletes the standing Goals key, but Id 999 matches no standing goal.
        {"Seq": 2, "Ts": 1782500810000, "Id": 999, "Action": "action_discarded", "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Corners": 1}}}},
    ]
    with pytest.raises(UnsupportedScoreTransition):
        extract_confirmed_goal_events(records)


def test_terminal_snapshot_semantics_and_fm_manifest() -> None:
    """RED-8: ``game_finalised`` is a COMPLETE snapshot -- a present participant
    lacking a Goals key parses as 0 (distinct from ordinary sparse carry-forward).
    confirmed == terminal-snapshot for a canned fixture, and the FM manifest gate
    passes (France 2-0)."""
    # Canned fixture with a terminal game_finalised snapshot: P1=2, P2 present but
    # no Goals key -> terminal snapshot must read P2=0.
    canned = [
        {"Seq": 1, "Ts": 1782500810000, "Id": 1, "Action": "goal", "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Goals": 1}}}},
        {"Seq": 2, "Ts": 1782500820000, "Id": 2, "Action": "goal", "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Goals": 2}}}},
        {"Seq": 3, "Ts": 1782500900000, "Action": "game_finalised", "Participant1IsHome": True,
         "Score": {"Participant1": {"Total": {"Goals": 2, "Corners": 5}},
                   "Participant2": {"Total": {"Corners": 3}}}},
    ]
    assert terminal_snapshot_score(canned) == {1: 2, 2: 0}
    assert confirmed_matches_terminal_snapshot(canned) is True

    fm_records = _load_fm_records()
    assert terminal_snapshot_score(fm_records) == {1: 2, 2: 0}
    assert confirmed_matches_terminal_snapshot(fm_records) is True

    validation = validate_against_manifest(_FM_FIXTURE_ID, fm_records)
    assert validation.status == "match"
    assert validation.confirmed_home == 2
    assert validation.confirmed_away == 0
