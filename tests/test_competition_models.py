"""Tests for veridex.competition.models — Phase 2A Task 1.

TDD red-set: written before the implementation exists; all tests should fail
with ModuleNotFoundError until the package is scaffolded.
"""

import pytest

from veridex.competition.models import (
    AgentEntry,
    Competition,
    CompetitionConfig,
    CompetitionStatus,
    CompetitionType,
    ExecutionMode,
)


def test_valid_competition_config() -> None:
    """CompetitionConfig accepts valid input and applies defaults."""
    c = CompetitionConfig(
        competition_type=CompetitionType.REPLAY_ARENA,
        source_mode="replay",
        market_scope="WC:FRA-BRA",
        roster_size=2,
    )
    assert c.execution_mode is ExecutionMode.PAPER  # default


def test_roster_size_floor_rejected() -> None:
    """roster_size < 2 must raise a Pydantic ValidationError (ge=2 constraint)."""
    with pytest.raises(ValueError):
        CompetitionConfig(
            competition_type=CompetitionType.LIVE_ARENA,
            source_mode="live",
            market_scope="x",
            roster_size=1,
        )


def test_status_transitions_monotonic() -> None:
    """Forward transitions are accepted; backward jumps raise ValueError."""
    comp = Competition(
        competition_id="c1",
        config=CompetitionConfig(
            competition_type=CompetitionType.REPLAY_ARENA,
            source_mode="replay",
            market_scope="x",
            roster_size=2,
        ),
        status=CompetitionStatus.DRAFT,
        entries=[],
        run_id=None,
    )
    comp.advance_status(CompetitionStatus.OPEN)
    comp.advance_status(CompetitionStatus.RUNNING)
    with pytest.raises(ValueError):
        comp.advance_status(CompetitionStatus.DRAFT)  # illegal backward jump


def test_advance_status_skip_rejected() -> None:
    """Skipping a step (draft→running) must raise ValueError."""
    comp = Competition(
        competition_id="c2",
        config=CompetitionConfig(
            competition_type=CompetitionType.REPLAY_ARENA,
            source_mode="replay",
            market_scope="x",
            roster_size=2,
        ),
        status=CompetitionStatus.DRAFT,
        entries=[],
        run_id=None,
    )
    with pytest.raises(ValueError):
        comp.advance_status(CompetitionStatus.RUNNING)  # skip: draft→running


def test_advance_status_same_rejected() -> None:
    """Transitioning to the current status must raise ValueError."""
    comp = Competition(
        competition_id="c3",
        config=CompetitionConfig(
            competition_type=CompetitionType.REPLAY_ARENA,
            source_mode="replay",
            market_scope="x",
            roster_size=2,
        ),
        status=CompetitionStatus.OPEN,
        entries=[],
        run_id=None,
    )
    with pytest.raises(ValueError):
        comp.advance_status(CompetitionStatus.OPEN)  # same status


def test_agent_entry_defaults() -> None:
    """AgentEntry defaults: execution_eligibility=False, config_hash=None."""
    entry = AgentEntry(
        agent_id="agent-42",
        owner="team-x",
        strategy="kelly_clv_v2",
        model=None,
        proof_mode="zk",
    )
    assert entry.execution_eligibility is False
    assert entry.config_hash is None


def test_models_no_llm_imports() -> None:
    """models.py must not import any LLM SDK (static AST audit)."""
    from pathlib import Path

    from veridex.verifier.import_audit import assert_no_llm_imports

    assert_no_llm_imports(Path("veridex/competition/models.py"))
