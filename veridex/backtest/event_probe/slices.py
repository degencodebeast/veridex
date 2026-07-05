"""CON-007 context-slice tagging for the event-window fork probe (E6b).

Maps one goal event and its computed window record to the FIVE predeclared v1
context dimensions the aggregator (E4) partitions on. Every value is a NAMED
bucket -- ``unknown`` / ``drawish_or_unknown`` / ``other`` are explicit buckets
that are still counted, never a silent drop (CON-007 / GUD-002).

The two numeric knobs (``favorite_prob_cutoff``, ``late_match_minute``) are read
from the sealed :class:`ProbeConfig` -- no threshold is inlined here -- so a
post-hoc change to either moves ``config_hash()`` and VOIDs the run (CON-014).
The half boundary (45') is not a tunable but the structural halftime split, so it
is a module constant rather than a config knob.

Trust boundary (CON-012, rung-1): this module imports ONLY sibling ``event_probe``
units (extraction / compute / config); it touches nothing in the trust core.
"""

from __future__ import annotations

from veridex.backtest.event_probe.compute import EventRecord
from veridex.backtest.event_probe.config import ProbeConfig
from veridex.backtest.event_probe.extraction import GoalEvent

#: The structural halftime boundary in match minutes: minute < 45 is the first
#: half, >= 45 the second. Not a tunable threshold (unlike the CON-007 knobs) --
#: it is the definition of "half" itself, so it is a constant, not a config field.
#: Only used as the FALLBACK when no StatusId period marker is present.
_HALFTIME_MINUTE: int = 45

#: The authoritative ``StatusId`` period codes (verified across the 18-fixture
#: universe): 2=first half, 4=second half, 7/9=extra time (first/second ET period).
#: All other codes are non-play phases (pre-match, halftime, full time, penalties)
#: that never legitimately host a goal.
_STATUS_FIRST_HALF: int = 2
_STATUS_SECOND_HALF: int = 4
_STATUS_EXTRA_TIME: frozenset[int] = frozenset({7, 9})


def _favorite_status(p_pre: float | None, cutoff: float) -> str:
    """favorite_scorer / underdog_scorer / drawish_or_unknown from ``p_pre``.

    ``p_pre >= cutoff`` -> the scorer was the market favorite; below -> underdog;
    ``None`` (no valid pre-tick / excluded event) -> drawish_or_unknown, never a
    fabricated side.
    """
    if p_pre is None:
        return "drawish_or_unknown"
    return "favorite_scorer" if p_pre >= cutoff else "underdog_scorer"


def _score_context(scorer_before: int, conceded_before: int) -> str:
    """Scorer-team score_context from the PRE-goal carried score.

    Measured from the scorer's perspective just BEFORE the goal:

    * scorer was down exactly one   -> ``equalizer``      (levels the game)
    * scorer was level              -> ``go_ahead``       (takes the lead)
    * scorer was already ahead      -> ``leader_extends`` (extends the lead)
    * scorer was down two or more   -> ``deficit_reduced``(still behind, one closer)

    The four buckets tile every integer relation; ``other`` is the named
    safety bucket for any state that somehow escapes them (kept, never dropped).
    """
    diff = scorer_before - conceded_before
    if diff == -1:
        return "equalizer"
    if diff == 0:
        return "go_ahead"
    if diff >= 1:
        return "leader_extends"
    if diff <= -2:
        return "deficit_reduced"
    return "other"  # pragma: no cover - unreachable for integer scores


def _half(status_id: int | None, match_minute: int | None) -> str:
    """first_half / second_half / extra_time / unknown for the goal.

    The pinned rule is "half from the period marker IF PRESENT, else match-minute":

    * ``StatusId`` 2 / 4 -> first_half / second_half (authoritative; correctly
      keeps first-half stoppage goals in the first half, which a clock-only split
      cannot).
    * ``StatusId`` 7 / 9 -> ``extra_time`` -- its own named bucket, never folded
      into second_half.
    * ``StatusId`` absent -> fall back to the match clock at the 45' split.
    * anything left -- a present but non-play StatusId, or an absent marker with no
      clock either -> ``unknown`` (named + counted, never guessed).
    """
    if status_id == _STATUS_FIRST_HALF:
        return "first_half"
    if status_id == _STATUS_SECOND_HALF:
        return "second_half"
    if status_id in _STATUS_EXTRA_TIME:
        return "extra_time"
    if status_id is None and match_minute is not None:
        return "first_half" if match_minute < _HALFTIME_MINUTE else "second_half"
    return "unknown"


def _match_timing(match_minute: int | None, late_minute: int) -> str:
    """early / late / unknown, split on ``cfg.late_match_minute``."""
    if match_minute is None:
        return "unknown"
    return "late" if match_minute >= late_minute else "early"


def derive_slice_tags(
    event: GoalEvent, record: EventRecord, cfg: ProbeConfig
) -> dict[str, str]:
    """Return the five CON-007 context-slice tags for one goal event.

    Reads ``record.p_pre`` (favorite split), the event's pre-goal carried score
    (score_context) and ``match_minute`` (half / timing), and the two sealed
    thresholds on ``cfg``. Every dimension resolves to a named bucket.
    """
    return {
        "scoring_side": event.scoring_side,
        "favorite_status": _favorite_status(record.p_pre, cfg.favorite_prob_cutoff),
        "score_context": _score_context(
            event.scorer_goals_before, event.conceded_goals_before
        ),
        "half": _half(event.status_id, event.match_minute),
        "match_timing": _match_timing(event.match_minute, cfg.late_match_minute),
    }
