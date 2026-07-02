"""CLV Check — a deterministic Proof Check module. Test-driven (T5).

TRUST PATH: this module MUST NOT import agno/openai/anthropic/litellm (gate 2). It recomputes
edge/CLV from evidence; the LLM-claimed edge is IGNORED (gate 1).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ClvCheckResult(BaseModel):
    """A single legacy CLV-Check verdict (renamed from ``CheckResult`` to avoid colliding with the
    canonical 7-member :class:`veridex.checks.result.CheckResult` taxonomy, WD-5b Task 5).

    Surfaced publicly as a ``checks`` entry, never a legacy internal name. Still consumed only by the sealed CLV law
    (:func:`veridex.law.recompute.recompute`) via :func:`compute_clv_check` — its ``reason`` string
    rides along on every recompute verdict as untrusted-edge metadata.
    """

    check: str
    result: Literal["pass", "fail"]
    rules: list[dict] = Field(default_factory=list)
    reason: str | None = None


def compute_clv_check(
    *,
    recomputed_edge_bps: int,
    claimed_edge_bps: int | None = None,
    proof_mode: str = "LLM/evidence-verified",
) -> ClvCheckResult:
    """Deterministic CLV Check. Scores on `recomputed_edge_bps` only; `claimed_edge_bps` is ignored."""
    passed = recomputed_edge_bps > 0
    return ClvCheckResult(
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
