"""WD-5b — the enriched Proof-Check contract (spec §4.3, SEC-001/002).

DATA + label maps only. This module is on the deterministic trust path
(`veridex.verifier.import_audit` audits `checks/`) — it imports NO LLM SDK.

The 7-member ``CheckId`` enum is **FROZEN** (codex: decided before UI work to avoid
enum churn). CLV is deliberately NOT a CheckId — it is a performance metric (SEC-001);
the proof layer's ``METRICS_RECOMPUTED`` proves the metric table was recomputed faithfully.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

CheckStatus = Literal["pass", "fail", "pending", "not_applicable"]
CheckSeverity = Literal["blocking", "warning", "info"]


class CheckId(str, Enum):
    """The frozen set of 7 top-level Proof Checks (spec §4.3)."""

    EVIDENCE_INTEGRITY = "evidence_integrity"
    LLM_BOUNDARY = "llm_boundary"
    METRICS_RECOMPUTED = "metrics_recomputed"
    MANIFEST_BOUND = "manifest_bound"
    POLICY_OBEYED = "policy_obeyed"
    RECEIPT_SEPARATION = "receipt_separation"
    ANCHOR = "anchor"


#: UI labels (SEC-001: METRICS_RECOMPUTED renders as "Score Recomputed").
CHECK_LABELS: dict[CheckId, str] = {
    CheckId.EVIDENCE_INTEGRITY: "Evidence Integrity",
    CheckId.LLM_BOUNDARY: "LLM Boundary",
    CheckId.METRICS_RECOMPUTED: "Score Recomputed",
    CheckId.MANIFEST_BOUND: "Manifest Bound",
    CheckId.POLICY_OBEYED: "Policy Obeyed",
    CheckId.RECEIPT_SEPARATION: "Receipt Separation",
    CheckId.ANCHOR: "On-Chain Anchor",
}

#: Default severity per check. ANCHOR is informational (a not_applicable/pending anchor in
#: offline replay must never read as a blocking failure — SEC-002/SEC-008).
CHECK_SEVERITY: dict[CheckId, CheckSeverity] = {
    CheckId.EVIDENCE_INTEGRITY: "blocking",
    CheckId.LLM_BOUNDARY: "blocking",
    CheckId.METRICS_RECOMPUTED: "blocking",
    CheckId.MANIFEST_BOUND: "blocking",
    CheckId.POLICY_OBEYED: "blocking",
    CheckId.RECEIPT_SEPARATION: "blocking",
    CheckId.ANCHOR: "info",
}


class CheckResult(BaseModel):
    """A single Proof-Check verdict (PUBLIC name — never a legacy internal name).

    Attributes:
        id: The frozen check identifier.
        label: Human-readable UI label.
        result: ``pass`` / ``fail`` / ``pending`` / ``not_applicable`` — never hardcoded PASS.
        severity: ``blocking`` / ``warning`` / ``info``.
        method: How the verdict was reached (e.g. ``"sha256_evidence_hash"``).
        scope: What was checked (e.g. ``"run_events"`` or ``"law/, scoring.py, ..."``).
        evidence_refs: References into sealed evidence the verdict was derived from.
        rules: Sub-assertions inside the check (Checks Doctrine: facts go in rules, not new checks).
        details: Structured extras (e.g. ``{"recomputed_match": false}``).
        error: Populated only when the check failed-closed on an exception.
    """

    id: CheckId
    label: str
    result: CheckStatus
    severity: CheckSeverity
    method: str
    scope: str
    evidence_refs: list[str] = Field(default_factory=list)
    rules: list[dict[str, Any]] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
