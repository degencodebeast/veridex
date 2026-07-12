"""CON-015 goal-event extraction from live score records.

A goal is defined *only* as a strictly positive increment in the
carried-forward ``Score["Participant{1,2}"]["Total"]["Goals"]`` value. The
``Stats`` block (stat-type indexed, e.g. ``Stats["8"]`` = corners) is never a
trigger, and ``Action`` is advisory only: goal records repeat the same count
(deduped here by carry-forward), ``goal_kick`` is unrelated, and a stats-only
change is not a goal.

The ``Score`` block is sparse -- present on only a fraction of records and often
carrying just the participant that changed -- so each participant's last-known
``Total.Goals`` is carried forward; a record with no ``Score`` never resets it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

_PARTICIPANTS: tuple[int, int] = (1, 2)

#: External final-score manifest (fixture_id -> {home_team, away_team, home_score,
#: away_score, source, retrieved_ts}). Fixtures ABSENT from it are UNVALIDATED —
#: labeled, never silently confirmed. See :func:`validate_against_manifest`.
_MANIFEST_PATH = Path(__file__).with_name("final_score_manifest.json")


class UnsupportedScoreTransition(Exception):
    """A score transition that cannot be represented as one observed goal per record.

    Raised (fixture-level) by :func:`extract_confirmed_goal_events` on a
    corrupt/incomplete tape — a negative score, or a jump of more than one goal in a
    single record (a lost goal record, whose time cannot be synthesized). The caller
    MUST convert this into an explicit, named coverage exclusion for that fixture —
    it never aborts a batch opaquely and never fabricates a goal time.
    """


class AmbiguousGoalReversal(UnsupportedScoreTransition):
    """An ``action_discarded`` that DELETES a standing ``Total.Goals`` key while its
    stable feed ``Id`` matches NO currently-standing score-increment event.

    Fail closed rather than guess by timestamp proximity (nearby unrelated actions
    or repeated provisional rows could retract the wrong goal). Subclasses
    :class:`UnsupportedScoreTransition` so callers that already convert that into a
    named coverage exclusion catch this too, while remaining distinguishable.
    """


@dataclass(frozen=True)
class GoalEvent:
    """A single scored goal.

    ``t_e`` is the event time in seconds (13-digit ms ``Ts`` floor-divided by
    1000), ``scoring_side`` is the home/away label, and ``participant`` is the
    1-based participant index that scored.

    The remaining fields are the raw material the CON-007 slice tagger consumes:

    * ``scorer_goals_before`` / ``conceded_goals_before`` -- the carried
      ``Total.Goals`` of the scoring side and its opponent *just before* this goal
      (the pre-goal score), from which score_context (equalizer / go_ahead /
      leader_extends / deficit_reduced) is derived downstream.
    * ``match_minute`` -- the goal record's ``Clock.Seconds // 60`` (the continuous
      match-elapsed clock verified in the real fixtures), or ``None`` when the
      record carries no readable clock -- never fabricated, tagged `unknown`
      downstream. Defaults keep the dataclass additive for direct constructions.
    * ``status_id`` -- the goal record's ``StatusId`` period marker (2=first half,
      4=second half, 7/9=extra time -- verified across the 18-fixture universe), or
      ``None`` when absent. It is the AUTHORITATIVE half source; the slice tagger
      prefers it over ``match_minute`` (which mislabels first-half stoppage goals
      and cannot see extra time).
    """

    t_e: int
    scoring_side: str
    participant: int
    scorer_goals_before: int = 0
    conceded_goals_before: int = 0
    match_minute: int | None = None
    status_id: int | None = None


@dataclass(frozen=True)
class ExtractionResult:
    events: list[GoalEvent]
    excluded: dict[str, int]  # reason -> count


@dataclass(frozen=True)
class ConfirmedExtractionResult:
    """The finalized ("confirmed") view of a score feed.

    Same contract as ``ExtractionResult`` plus the reversal lifecycle: a later
    score DECREASE that undoes a prior increment retracts the earlier
    provisional goal, so ``events`` holds only goals that *stand* at the end of
    the feed and ``reversed`` holds the provisional-then-reversed ones. The two
    lists are disjoint -- a reversed goal is never counted as confirmed.
    """

    events: list[GoalEvent]  # final CONFIRMED goals only
    reversed: list[GoalEvent]  # provisional goals later undone by a score decrease
    excluded: dict[str, int]  # reason -> count (carried over from extract_goal_events)


@dataclass(frozen=True)
class ManifestValidation:
    """Outcome of validating a fixture against the EXTERNAL final-score manifest.

    ``status`` is ``"match"`` / ``"mismatch"`` when the fixture is in the manifest,
    or ``"unvalidated"`` when it is absent (labeled, never silently confirmed). The
    score fields are ``None`` for the unvalidated case.
    """

    fixture_id: int
    status: str
    confirmed_home: int | None = None
    confirmed_away: int | None = None
    manifest_home: int | None = None
    manifest_away: int | None = None


def _sort_key(record: dict) -> tuple[int, int]:
    """Order by ``Seq``; fall back to ``Ts`` when a record has no ``Seq``.

    Records that carry ``Seq`` sort ahead of (and independently from) records
    that only carry ``Ts``, so the presence of a stray un-sequenced record never
    interleaves into the sequenced stream.
    """
    seq = record.get("Seq")
    if seq is not None:
        return (0, seq)
    return (1, record.get("Ts", 0))


def _match_minute(record: dict) -> int | None:
    """Return the record's match minute from ``Clock.Seconds``, else ``None``.

    ``Clock`` is ``{"Running": bool, "Seconds": int}`` -- a continuous
    match-elapsed clock (first half 0..~2989s incl. stoppage, then 2700..~5700s in
    the second half; it never resets), so ``Seconds // 60`` is the match minute.
    Absent / malformed clock -> ``None`` (the goal's timing is `unknown`, never
    invented).
    """
    clock = record.get("Clock")
    if not isinstance(clock, dict):
        return None
    seconds = clock.get("Seconds")
    if not isinstance(seconds, int) or isinstance(seconds, bool):
        return None
    return seconds // 60


def _status_id(record: dict) -> int | None:
    """Return the record's ``StatusId`` period marker as an int, else ``None``.

    ``StatusId`` is the feed's authoritative match-phase code (2=first half,
    4=second half, 7/9=extra time -- verified across the fixture universe). A
    missing or non-integer value yields ``None`` (no trusted period marker).
    """
    status = record.get("StatusId")
    if not isinstance(status, int) or isinstance(status, bool):
        return None
    return status


def _scoring_side(participant: int, participant1_is_home: bool) -> str:
    """Map a participant index to its home/away label via ``Participant1IsHome``."""
    p1_side = "home" if participant1_is_home else "away"
    p2_side = "away" if participant1_is_home else "home"
    return p1_side if participant == 1 else p2_side


def _read_new_goals(score: dict) -> dict[int, int] | None:
    """Read each present participant's ``Total.Goals`` from a score block.

    ``Total`` is a *sparse stat-map*: it holds only the stats recorded so far
    (``Corners``, ``YellowCards``, ``Goals`` ...), so a present participant may
    legitimately carry a ``Total`` with no ``Goals`` key at all (e.g. a
    corners-only update). That is a carry-forward, not a reject -- such a
    participant is simply omitted from the returned map.

    Returns a ``{participant: goals}`` map for participants that carry a
    readable ``Total.Goals`` this record, or ``None`` if a present participant's
    path is genuinely malformed (``Total`` missing/not-a-dict, or a non-integer
    ``Goals`` where an increment would otherwise be read).
    """
    new_goals: dict[int, int] = {}
    for participant in _PARTICIPANTS:
        block = score.get(f"Participant{participant}")
        if block is None:
            continue  # absent -> carry the last-known count forward
        if not isinstance(block, dict):
            return None
        total = block.get("Total")
        if not isinstance(total, dict):
            return None  # participant present but Total missing -> malformed
        if "Goals" not in total:
            continue  # no goal stat recorded this record -> carry forward
        goals = total["Goals"]
        if not isinstance(goals, int) or isinstance(goals, bool):
            return None
        new_goals[participant] = goals
    return new_goals


def extract_goal_events(records: list[dict]) -> ExtractionResult:
    carried: dict[int, int] = {1: 0, 2: 0}
    events: list[GoalEvent] = []
    excluded: dict[str, int] = {
        "decreasing_score": 0,
        "ambiguous_delta": 0,
        "unparseable": 0,
    }

    for record in sorted(records, key=_sort_key):
        score = record.get("Score")
        if not isinstance(score, dict):
            continue  # no score block -> carry forward, nothing changes

        new_goals = _read_new_goals(score)
        if new_goals is None:
            excluded["unparseable"] += 1
            continue

        increments = {p: v for p, v in new_goals.items() if v > carried[p]}
        decreased = any(v < carried[p] for p, v in new_goals.items())

        if decreased:
            excluded["decreasing_score"] += 1
            continue
        if len(increments) >= 2:
            excluded["ambiguous_delta"] += 1
            continue

        if increments:
            (participant, new_value), = increments.items()
            side = _scoring_side(participant, bool(record.get("Participant1IsHome")))
            opponent = 2 if participant == 1 else 1
            events.append(
                GoalEvent(
                    t_e=record["Ts"] // 1000,
                    scoring_side=side,
                    participant=participant,
                    # Carried counts are still the PRE-goal state here (this
                    # participant's carry is updated only after appending).
                    scorer_goals_before=carried[participant],
                    conceded_goals_before=carried[opponent],
                    match_minute=_match_minute(record),
                    status_id=_status_id(record),
                )
            )
            carried[participant] = new_value
        else:
            # No increment (repeat or unrelated update): sync carried state.
            carried.update(new_goals)

    return ExtractionResult(events=events, excluded=excluded)


def extract_confirmed_goal_events(records: list[dict]) -> ConfirmedExtractionResult:
    """Finalized ("confirmed") goal extraction with lifecycle-identity retraction.

    Creation is unchanged from :func:`extract_goal_events` -- a goal is a strictly
    positive increment in the sparse-carried ``Total.Goals``; ``Action`` is advisory
    (so penalty_outcome/own-goals that move the score ARE captured, and a
    penalty/own-goal-shaped increment enters the lifecycle index regardless of its
    label); ``ambiguous_delta``/``unparseable`` exclusions and ``GoalEvent`` field
    semantics carry over.

    Reversal lifecycle (two independent, idempotent paths):

    * **Lifecycle identity** -- each score-increment event is indexed by its stable
      feed ``Id`` -> its standing ``GoalEvent``. On ``Action:"action_discarded"``,
      if that same ``Id`` maps to a currently-standing event, that exact event is
      RETRACTED (moved to ``reversed``, the participant's score restored). This is
      how the feed retracts a provisional goal by DELETING the ``Total.Goals`` key
      in a same-``Id`` full-score restatement -- a key-deletion the sparse
      carry-forward reader alone cannot see. A missing ``Goals`` key is NOT treated
      as zero globally: a corners-only/unrelated sparse update legitimately omits it
      and must carry the score forward.
    * **Numeric decrease** -- a later score DECREASE retracts standing provisional
      goal(s) down to the new value (works without an ``Id``). When a discard row
      carries BOTH a matching ``Id`` and a numeric decrease, the ``Id`` path runs
      first and the numeric path then sees no remaining decrease -> retracts once.

    Fail closed (typed :class:`UnsupportedScoreTransition`) on: a negative score, a
    multi-goal jump in one record, or an ``action_discarded`` that DELETES a standing
    ``Total.Goals`` key with NO matching active ``Id`` (:class:`AmbiguousGoalReversal`
    -- no Ts-only guessing). The caller converts these into named coverage exclusions.

    Acceptance is validated OUT of band -- see :func:`terminal_snapshot_score` /
    :func:`confirmed_matches_terminal_snapshot` and :func:`validate_against_manifest`.
    :func:`confirmed_matches_final_carried` is only a narrow structural check.
    """
    carried: dict[int, int] = {1: 0, 2: 0}
    events: list[GoalEvent] = []
    reversed_goals: list[GoalEvent] = []
    # Per-participant stack of goals that currently STAND (mirror of ``events``,
    # partitioned by side) so a numeric decrease can retract the right goals.
    standing: dict[int, list[GoalEvent]] = {1: [], 2: []}
    # Lifecycle index: stable feed ``Id`` -> the standing score-increment event it
    # created. Cleared whenever that event is retracted, so an ``action_discarded``
    # only reverses a goal whose Id is CURRENTLY standing.
    active_by_id: dict[object, GoalEvent] = {}
    excluded: dict[str, int] = {
        "decreasing_score": 0,
        "ambiguous_delta": 0,
        "unparseable": 0,
    }

    def _retract(event: GoalEvent) -> None:
        """Move a currently-standing event to ``reversed`` and drop its indexes."""
        events.remove(event)
        standing[event.participant].remove(event)
        reversed_goals.append(event)
        for key in [k for k, v in active_by_id.items() if v is event]:
            del active_by_id[key]

    for record in sorted(records, key=_sort_key):
        score = record.get("Score")
        if not isinstance(score, dict):
            continue  # no score block -> carry forward, nothing changes

        new_goals = _read_new_goals(score)
        if new_goals is None:
            excluded["unparseable"] += 1
            continue

        action = record.get("Action")
        record_id = record.get("Id")

        # Lifecycle-identity retraction: an ``action_discarded`` whose stable Id maps
        # to a currently-standing goal retracts THAT exact event and restores its
        # participant's score by one. Runs BEFORE the numeric-decrease path so a row
        # carrying both cannot double-pop.
        handled_by_id = False
        if action == "action_discarded" and record_id is not None and record_id in active_by_id:
            event = active_by_id[record_id]
            _retract(event)
            carried[event.participant] -= 1
            handled_by_id = True

        # Fail closed: an ``action_discarded`` that DELETES a standing Goals key but
        # matched no active Id is an ambiguous lifecycle we refuse to guess.
        if action == "action_discarded" and not handled_by_id:
            for participant in _PARTICIPANTS:
                block = score.get(f"Participant{participant}")
                if not isinstance(block, dict):
                    continue
                total = block.get("Total")
                if isinstance(total, dict) and "Goals" not in total and standing[participant]:
                    raise AmbiguousGoalReversal(
                        f"participant {participant} action_discarded deletes standing "
                        f"Goals key (Id {record_id!r}) with no matching active event"
                    )

        # Fail closed on transitions that cannot be one observed goal per record:
        # a negative score, or a jump of more than one goal in a single record (a
        # lost goal record whose time cannot be synthesized).
        for participant, value in new_goals.items():
            if value < 0:
                raise UnsupportedScoreTransition(
                    f"participant {participant} negative score {value} "
                    f"(carried {carried[participant]})"
                )
            if value > carried[participant] + 1:
                raise UnsupportedScoreTransition(
                    f"participant {participant} multi-goal jump "
                    f"{carried[participant]}->{value} (>1 goal in one record)"
                )

        increments = {p: v for p, v in new_goals.items() if v > carried[p]}
        decrements = {p: v for p, v in new_goals.items() if v < carried[p]}

        # Numeric-decrease reversal: retract standing goal(s) down to the new value.
        for participant, new_value in decrements.items():
            while len(standing[participant]) > new_value:
                _retract(standing[participant][-1])
            carried[participant] = new_value

        if len(increments) >= 2:
            excluded["ambiguous_delta"] += 1
            continue

        if increments:
            ((participant, new_value),) = increments.items()
            side = _scoring_side(participant, bool(record.get("Participant1IsHome")))
            opponent = 2 if participant == 1 else 1
            event = GoalEvent(
                t_e=record["Ts"] // 1000,
                scoring_side=side,
                participant=participant,
                # Carried counts are still the PRE-goal state here (this
                # participant's carry is updated only after appending).
                scorer_goals_before=carried[participant],
                conceded_goals_before=carried[opponent],
                match_minute=_match_minute(record),
                status_id=_status_id(record),
            )
            events.append(event)
            standing[participant].append(event)
            if record_id is not None:
                active_by_id[record_id] = event
            carried[participant] = new_value
        elif not decrements and not handled_by_id:
            # No increment and no reversal (repeat or unrelated update): sync
            # carried state. (After a reversal, carried is already correct.)
            carried.update(new_goals)

    # Internal invariant: the goals that stand per side ARE the confirmed events
    # per side, and equal the final carried score (goals increment by one in the
    # feed, so a standing goal maps one-to-one to a carried unit).
    for participant in _PARTICIPANTS:
        assert len(standing[participant]) == carried[participant]

    return ConfirmedExtractionResult(events=events, reversed=reversed_goals, excluded=excluded)


def _final_carried_goals(records: list[dict]) -> dict[int, int]:
    """Carry each participant's last-known ``Total.Goals`` forward to the end.

    This is the raw final score the feed itself reports -- the reversal record
    lowers the value, so the final carried figure already reflects overturns.
    Malformed / score-less / goal-less records are skipped (carry-forward), the
    same shape :func:`_read_new_goals` recognises.
    """
    carried: dict[int, int] = {1: 0, 2: 0}
    for record in sorted(records, key=_sort_key):
        score = record.get("Score")
        if not isinstance(score, dict):
            continue
        new_goals = _read_new_goals(score)
        if new_goals is None:
            continue
        carried.update(new_goals)
    return carried


def confirmed_matches_final_carried(records: list[dict]) -> bool:
    """NARROW structural-consistency check -- NOT a truth or acceptance oracle.

    ``True`` iff the number of CONFIRMED goals per participant equals that
    participant's FINAL carried ``Total.Goals``. Both sides derive from the SAME
    ``_read_new_goals`` carry-forward, so this is CIRCULAR: it cannot catch a
    key-deletion retraction that both derivations swallow identically. Kept only as
    a cheap internal structural smoke-check. Use :func:`confirmed_matches_terminal_snapshot`
    (same-feed, terminal-snapshot semantics) and :func:`validate_against_manifest`
    (external truth) for acceptance.
    """
    result = extract_confirmed_goal_events(records)
    confirmed_by_participant: dict[int, int] = {1: 0, 2: 0}
    for event in result.events:
        confirmed_by_participant[event.participant] += 1
    return confirmed_by_participant == _final_carried_goals(records)


def terminal_snapshot_score(records: list[dict]) -> dict[int, int] | None:
    """Return per-participant goals from the ``game_finalised`` COMPLETE snapshot.

    ``game_finalised`` restates the full final score, so its semantics DIFFER from
    an ordinary sparse update: a PRESENT participant whose ``Total`` carries no
    ``Goals`` key scored ZERO (not carry-forward). Returns ``{participant: goals}``,
    or ``None`` when the feed has no readable ``game_finalised`` terminal snapshot
    (the fixture then cannot be terminal-validated).
    """
    for record in sorted(records, key=_sort_key):
        if record.get("Action") != "game_finalised":
            continue
        score = record.get("Score")
        if not isinstance(score, dict):
            continue
        snapshot: dict[int, int] = {}
        for participant in _PARTICIPANTS:
            block = score.get(f"Participant{participant}")
            total = block.get("Total") if isinstance(block, dict) else None
            goals = total.get("Goals") if isinstance(total, dict) else None
            snapshot[participant] = goals if isinstance(goals, int) and not isinstance(goals, bool) else 0
        return snapshot
    return None


def confirmed_matches_terminal_snapshot(records: list[dict]) -> bool:
    """Acceptance check: confirmed goals per side == the ``game_finalised`` snapshot.

    A same-feed cross-check that is NON-circular w.r.t. the extractor's
    carry-forward blind spot (it reads the terminal snapshot under complete-snapshot
    semantics). ``False`` when there is no terminal snapshot to check against.
    """
    snapshot = terminal_snapshot_score(records)
    if snapshot is None:
        return False
    result = extract_confirmed_goal_events(records)
    confirmed_by_participant: dict[int, int] = {1: 0, 2: 0}
    for event in result.events:
        confirmed_by_participant[event.participant] += 1
    return confirmed_by_participant == snapshot


def _participant1_is_home(records: list[dict]) -> bool | None:
    """The fixture's ``Participant1IsHome`` flag (constant across the feed), or None."""
    for record in records:
        flag = record.get("Participant1IsHome")
        if isinstance(flag, bool):
            return flag
    return None


def _load_manifest() -> dict[str, dict]:
    """Load the external final-score manifest, or ``{}`` when absent/unreadable."""
    if not _MANIFEST_PATH.exists():
        return {}
    with _MANIFEST_PATH.open() as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {}


def validate_against_manifest(fixture_id: int, records: list[dict]) -> ManifestValidation:
    """Validate a fixture's confirmed goals against the EXTERNAL final-score manifest.

    Fixtures ABSENT from the manifest are labeled ``"unvalidated"`` (excluded from
    headline results), never silently confirmed. Present fixtures compare
    confirmed-per-side (mapped home/away via ``Participant1IsHome``) to the manifest
    score, yielding ``"match"`` or ``"mismatch"``.
    """
    entry = _load_manifest().get(str(fixture_id))
    if entry is None:
        return ManifestValidation(fixture_id=fixture_id, status="unvalidated")

    result = extract_confirmed_goal_events(records)
    by_side = {"home": 0, "away": 0}
    for event in result.events:
        by_side[event.scoring_side] += 1

    manifest_home = int(entry["home_score"])
    manifest_away = int(entry["away_score"])
    matches = by_side["home"] == manifest_home and by_side["away"] == manifest_away
    return ManifestValidation(
        fixture_id=fixture_id,
        status="match" if matches else "mismatch",
        confirmed_home=by_side["home"],
        confirmed_away=by_side["away"],
        manifest_home=manifest_home,
        manifest_away=manifest_away,
    )
