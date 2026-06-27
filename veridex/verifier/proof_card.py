"""Proof card — VerifierRunResponse-shaped JSON (read-only, JSON/static; NO UI). Test-driven (T7).

The judge-visible artifact. `agent-rank`'s `VerifierRunResponse` names its block `cats`; the PUBLIC
proof card must surface it as `checks` / Proof Checks via a thin response adapter (KILL-6 if that
needs broad schema rewrites). Must never expose "cat" in the public JSON.
"""

from __future__ import annotations

from typing import Any


def build_proof_card(
    *,
    run: dict[str, Any],
    evidence: dict[str, Any],
    checks: dict[str, Any],
    proof_mode: str,
) -> dict[str, Any]:
    """Build the public proof-card JSON: {verifier_version, run, lineage{proof_mode}, evidence, checks}."""
    return {
        "run": run,
        "evidence": evidence,
        "checks": checks,
        "proof_mode": proof_mode,
    }
