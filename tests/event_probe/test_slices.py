"""CON-007 slice-tag derivation tests (E6b).

``derive_slice_tags`` maps one goal event + its computed window record to the
FIVE predeclared v1 context dimensions:

* ``scoring_side``    -- home / away (already on the event).
* ``favorite_status`` -- favorite_scorer / underdog_scorer / drawish_or_unknown,
  split on ``cfg.favorite_prob_cutoff`` against ``record.p_pre``.
* ``score_context``   -- equalizer / go_ahead / leader_extends / deficit_reduced /
  other, from the scorer's PRE-goal carried score.
* ``half``            -- first_half / second_half / unknown, from ``match_minute``.
* ``match_timing``    -- early / late / unknown, split on ``cfg.late_match_minute``.

Every bucket is NAMED and returned -- there is no silent drop. Each test forces
one bucket and (where relevant) its adjacent boundary so a wrong branch fails.
"""

from __future__ import annotations

from veridex.backtest.event_probe.compute import EventRecord
from veridex.backtest.event_probe.config import ProbeConfig
from veridex.backtest.event_probe.extraction import GoalEvent
from veridex.backtest.event_probe.slices import derive_slice_tags


def _event(
    *,
    scoring_side: str = "home",
    participant: int = 1,
    scorer_goals_before: int = 0,
    conceded_goals_before: int = 0,
    match_minute: int | None = 20,
) -> GoalEvent:
    return GoalEvent(
        t_e=1000,
        scoring_side=scoring_side,
        participant=participant,
        scorer_goals_before=scorer_goals_before,
        conceded_goals_before=conceded_goals_before,
        match_minute=match_minute,
    )


def _record(*, p_pre: float | None = 0.55) -> EventRecord:
    return EventRecord(
        t_e=1000,
        scoring_side="home",
        participant=1,
        p_pre=p_pre,
        p_imm=0.62,
        p_settle=0.70,
        delta_imm=0.29,
        delta_settle=0.65,
        R=0.45,
        event_class="LAG",
        exclusion_reason=None,
        grid={},
        slice_tags={},
    )


def test_all_five_dimensions_present_and_named() -> None:
    tags = derive_slice_tags(_event(), _record(), ProbeConfig())
    assert set(tags) == {
        "scoring_side", "favorite_status", "score_context", "half", "match_timing",
    }
    # No silent drop: every dimension resolves to a non-empty named bucket.
    assert all(isinstance(v, str) and v for v in tags.values())


def test_scoring_side_passthrough() -> None:
    assert derive_slice_tags(
        _event(scoring_side="away"), _record(), ProbeConfig()
    )["scoring_side"] == "away"


def test_favorite_vs_underdog_vs_drawish() -> None:
    cfg = ProbeConfig()  # favorite_prob_cutoff = 0.50
    assert derive_slice_tags(
        _event(), _record(p_pre=0.60), cfg
    )["favorite_status"] == "favorite_scorer"
    assert derive_slice_tags(
        _event(), _record(p_pre=0.40), cfg
    )["favorite_status"] == "underdog_scorer"
    # p_pre is None (no pre-tick / excluded) -> drawish_or_unknown, never invented.
    assert derive_slice_tags(
        _event(), _record(p_pre=None), cfg
    )["favorite_status"] == "drawish_or_unknown"


def test_favorite_cutoff_is_inclusive_at_boundary() -> None:
    cfg = ProbeConfig()  # cutoff 0.50: p_pre >= 0.50 is favorite
    assert derive_slice_tags(
        _event(), _record(p_pre=0.50), cfg
    )["favorite_status"] == "favorite_scorer"
    assert derive_slice_tags(
        _event(), _record(p_pre=0.4999), cfg
    )["favorite_status"] == "underdog_scorer"


def test_score_context_equalizer() -> None:
    # Scorer was down exactly one (0 vs 1) -> equalizer.
    tags = derive_slice_tags(
        _event(scorer_goals_before=0, conceded_goals_before=1), _record(), ProbeConfig()
    )
    assert tags["score_context"] == "equalizer"


def test_score_context_go_ahead() -> None:
    # Scorer was level (1 vs 1) -> goes ahead.
    tags = derive_slice_tags(
        _event(scorer_goals_before=1, conceded_goals_before=1), _record(), ProbeConfig()
    )
    assert tags["score_context"] == "go_ahead"


def test_score_context_leader_extends() -> None:
    # Scorer was already ahead (2 vs 1) -> extends the lead.
    tags = derive_slice_tags(
        _event(scorer_goals_before=2, conceded_goals_before=1), _record(), ProbeConfig()
    )
    assert tags["score_context"] == "leader_extends"


def test_score_context_deficit_reduced() -> None:
    # Scorer was down two or more (0 vs 2) -> still behind, one closer.
    tags = derive_slice_tags(
        _event(scorer_goals_before=0, conceded_goals_before=2), _record(), ProbeConfig()
    )
    assert tags["score_context"] == "deficit_reduced"


def test_half_first_second_and_unknown() -> None:
    cfg = ProbeConfig()
    assert derive_slice_tags(
        _event(match_minute=20), _record(), cfg
    )["half"] == "first_half"
    assert derive_slice_tags(
        _event(match_minute=50), _record(), cfg
    )["half"] == "second_half"
    # No clock source -> unknown (named + counted, never fabricated).
    assert derive_slice_tags(
        _event(match_minute=None), _record(), cfg
    )["half"] == "unknown"


def test_half_boundary_at_45() -> None:
    cfg = ProbeConfig()
    assert derive_slice_tags(
        _event(match_minute=44), _record(), cfg
    )["half"] == "first_half"
    assert derive_slice_tags(
        _event(match_minute=45), _record(), cfg
    )["half"] == "second_half"


def test_match_timing_early_late_unknown() -> None:
    cfg = ProbeConfig()  # late_match_minute = 60
    assert derive_slice_tags(
        _event(match_minute=30), _record(), cfg
    )["match_timing"] == "early"
    assert derive_slice_tags(
        _event(match_minute=70), _record(), cfg
    )["match_timing"] == "late"
    assert derive_slice_tags(
        _event(match_minute=None), _record(), cfg
    )["match_timing"] == "unknown"


def test_match_timing_boundary_at_late_cutoff() -> None:
    cfg = ProbeConfig()  # late_match_minute = 60: minute >= 60 is late
    assert derive_slice_tags(
        _event(match_minute=59), _record(), cfg
    )["match_timing"] == "early"
    assert derive_slice_tags(
        _event(match_minute=60), _record(), cfg
    )["match_timing"] == "late"


def test_thresholds_are_read_from_cfg_not_hardcoded() -> None:
    # A non-default (but hash-diverging) cfg must move the buckets -- proving the
    # tagger reads cfg, not inlined literals. p_pre 0.55 is a favorite at cutoff
    # 0.50 but an underdog at cutoff 0.60; minute 65 is late at 60 but early at 70.
    strict = ProbeConfig(favorite_prob_cutoff=0.60, late_match_minute=70)
    tags = derive_slice_tags(
        _event(match_minute=65), _record(p_pre=0.55), strict
    )
    assert tags["favorite_status"] == "underdog_scorer"
    assert tags["match_timing"] == "early"
