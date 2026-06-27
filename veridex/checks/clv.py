"""CLV Check — a deterministic Proof Check (sibling of agent-rank's "Cat" modules). Test-driven (T5).

TRUST PATH: this module MUST NOT import agno/openai/anthropic/litellm (gate 2). It recomputes
edge/CLV from evidence; the LLM-claimed edge is IGNORED (gate 1).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CheckResult(BaseModel):
    """A single Proof Check verdict (surfaced publicly as a `checks` entry, never "cat")."""

    check: str
    result: Literal["pass", "fail"]
    rules: list[dict] = Field(default_factory=list)
    reason: str | None = None


def compute_clv_check(
    *,
    recomputed_edge_bps: int,
    claimed_edge_bps: int | None = None,
    proof_mode: str = "LLM/evidence-verified",
) -> CheckResult:
    """Deterministic CLV Check. Scores on `recomputed_edge_bps` only; `claimed_edge_bps` is ignored."""
    passed = recomputed_edge_bps > 0
    return CheckResult(
        check="clv",
        result="pass" if passed else "fail",
        reason=f"recomputed_edge_bps={recomputed_edge_bps}bps; claimed_edge_bps recorded as untrusted metadata only",
        rules=[
            {
                "rule": "gate1_ignore_claimed_edge",
                "recomputed_edge_bps": recomputed_edge_bps,
                "claimed_edge_bps_untrusted": claimed_edge_bps,
                "proof_mode": proof_mode,
            }
        ],
    )
