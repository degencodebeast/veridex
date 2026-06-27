"""TxLINE-native on-chain authenticity labeling (gate 4). Test-driven (T8).

Honest split (Codex R1/R2): `validateStat` authenticates SCORE/STAT inputs only (claim-strengthening,
IF the proof path is confirmed). ODDS/StablePrice inputs are `recorded_evidence` unless an
odds-specific validation path is confirmed — we do NOT claim TxLINE-native authentication for odds.
"""
from __future__ import annotations


def txline_native_authenticity(evidence_kind: str, *, odds_proof_confirmed: bool = False) -> str:
    """Return the on-chain authenticity LABEL for an evidence kind:
    "scores" -> "validateStat"; "odds" -> "recorded_evidence" (unless odds_proof_confirmed).
    """
    if evidence_kind == "scores":
        return "validateStat"
    if evidence_kind == "odds":
        # A confirmed odds-proof is a DISTINCT path — never relabel it `validateStat`
        # (that is TxLINE's scores-only on-chain mechanism; reusing it for odds overclaims).
        if odds_proof_confirmed:
            return "odds_proof_confirmed"
        return "recorded_evidence"
    raise ValueError(f"Unknown evidence_kind: {evidence_kind!r}")
