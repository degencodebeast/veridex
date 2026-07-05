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

from dataclasses import dataclass

_PARTICIPANTS: tuple[int, int] = (1, 2)


@dataclass(frozen=True)
class GoalEvent:
    """A single scored goal.

    ``t_e`` is the event time in seconds (13-digit ms ``Ts`` floor-divided by
    1000), ``scoring_side`` is the home/away label, and ``participant`` is the
    1-based participant index that scored.
    """

    t_e: int
    scoring_side: str
    participant: int


@dataclass(frozen=True)
class ExtractionResult:
    events: list[GoalEvent]
    excluded: dict[str, int]  # reason -> count


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
            events.append(
                GoalEvent(
                    t_e=record["Ts"] // 1000,
                    scoring_side=side,
                    participant=participant,
                )
            )
            carried[participant] = new_value
        else:
            # No increment (repeat or unrelated update): sync carried state.
            carried.update(new_goals)

    return ExtractionResult(events=events, excluded=excluded)
