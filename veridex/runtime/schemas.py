"""Thin structured data shapes for the run/evidence model.

DATA ONLY — no behavior here (behavior lives in test-driven modules). These are the
constrained `AgentAction` the decision layer emits and the `RunEvent` evidence record.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SportsActionType(str, Enum):
    """Constrained action set the agent may emit (sports adaptation of the run-model action enum)."""

    WAIT = "WAIT"
    FLAG_VALUE = "FLAG_VALUE"
    FOLLOW_MOMENTUM = "FOLLOW_MOMENTUM"
    FADE = "FADE"
    WIDEN_OR_SUSPEND = "WIDEN_OR_SUSPEND"


class AgentAction(BaseModel):
    """Structured, constrained decision. `frozen` enforces an immutable decision contract.

    `params` may carry {market, side, reason, confidence}. NOTE: `reason`/`confidence` are
    UX/rationale only and are NEVER scored or trusted (gate 1).
    """

    model_config = ConfigDict(frozen=True)
    type: SportsActionType
    params: dict[str, Any] = Field(default_factory=dict)


class RunEvent(BaseModel):
    """Evidence record per tick (the ``RunEvent`` shape of the run/evidence model)."""

    sequence_no: int
    event_type: str
    state_snapshot_json: str | None = None
    action_payload_json: str | None = None
    validation_payload_json: str | None = None
    result_payload_json: str | None = None
