"""Competitor-strategy replication benchmark — Phase 2D Post-2D Tasks 7-8b (SX / M2).

Answers "how would sharpline/sports-workbench have fared on TxLINE data" WITHOUT importing a
single line of competitor code (CON-007: no competitor code on a trust-path module) — only
their published detection *algorithm*, re-expressed as a Veridex config over the existing
`sharp_stats` primitives (`veridex/strategies/sharp_stats.py`). Every scored number in a
`StrategyBenchmarkResult` comes from the injected Veridex `score_fn` seam; this module computes
no CLV/scoring itself, it only decides WHEN a translated detector would have fired.

Competitors are evaluated on rung-1 evidence only — `evidence_rung` is pinned to
`"txline-only"` and rejects anything stronger (REQ-SX-004): a competitor replication is never
allowed to borrow a venue-fill-grade evidence rung it never earned.
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, field_validator

from veridex.provenance import EvidenceRung
from veridex.runtime.evidence import serialize_payload


class CompetitorReplicationConfig(BaseModel):
    """A competitor detector translated into Veridex terms — never the competitor's own code."""

    source_repo: str
    source_strategy: str
    strategy: str
    translated_params: dict[str, float]
    notes: str

    def veridex_config_hash(self) -> str:
        """SHA-256 over the canonically-serialized translated config (stable, order-independent)."""
        payload = {"strategy": self.strategy, "translated_params": self.translated_params}
        return hashlib.sha256(serialize_payload(payload).encode()).hexdigest()


class StrategyBenchmarkResult(BaseModel):
    """Outcome of replaying a translated competitor detector on Veridex-scored TxLINE data.

    Competitors are rung-1 ONLY (REQ-SX-004, CON-007): a `StrategyBenchmarkResult` never
    carries venue-fill-grade evidence — it can only ever claim what a pure TxLINE replay proves.
    """

    benchmark_id: str
    source_strategy: str
    veridex_config_hash: str
    pack_content_hash: str
    evidence_rung: str
    fire_count: int
    scored_count: int
    avg_clv_bps: float | None
    abstain_count: int
    provenance: str

    @field_validator("evidence_rung")
    @classmethod
    def _rung_must_be_txline_only(cls, value: str) -> str:
        if value != EvidenceRung.TXLINE_ONLY.value:
            raise ValueError(
                f"competitor benchmark evidence_rung must be {EvidenceRung.TXLINE_ONLY.value!r} "
                f"(competitors are rung-1 only, REQ-SX-004), got {value!r}"
            )
        return value
