"""Deterministic serialization + evidence hashing + the raw pre-score record (gate 3).

Behavior is test-driven (T6) — stubs raise NotImplementedError so T1 fails meaningfully.
Adapted from `agent-rank/backend/src/services/serialization.py` + `.../integrity/run_auditor.py`.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def serialize_payload(payload: Any) -> str:
    """Canonical JSON: sorted keys, compact separators. (agent-rank parity.)"""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def compute_evidence_hash(events: list[dict[str, Any]]) -> str:
    """SHA-256 over sequence-ordered, canonically-serialized events (cross-process stable)."""
    sorted_events = sorted(events, key=lambda e: e["sequence_no"])
    canonical = "".join(serialize_payload(e) for e in sorted_events)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_raw_prescore_record(
    *,
    evidence_hash: str,
    raw_action: dict[str, Any],
    action_schema_version: str,
    agent_id: str,
    model_prompt_config_hash: str,
    tick_seq: int,
    proof_mode: str,
) -> dict[str, Any]:
    """Bind inputs+action+order+proof-mode BEFORE scoring (gate 3). The verifier scores ONLY from this.

    Must include `record_kind == "raw_prescore"` and a deterministic `raw_prescore_hash`.
    """
    bound = {
        "action_schema_version": action_schema_version,
        "agent_id": agent_id,
        "evidence_hash": evidence_hash,
        "model_prompt_config_hash": model_prompt_config_hash,
        "proof_mode": proof_mode,
        "raw_action": raw_action,
        "tick_seq": tick_seq,
    }
    raw_prescore_hash = hashlib.sha256(
        serialize_payload(bound).encode("utf-8")
    ).hexdigest()
    return {
        "record_kind": "raw_prescore",
        "raw_prescore_hash": raw_prescore_hash,
        **bound,
    }


def score_row_from_prescore(*, raw_prescore_hash: str, recomputed_edge_bps: int) -> dict[str, Any]:
    """Derive the score row ONLY from the bound raw pre-score record hash + recomputed values —
    never from a mutable raw action payload. Proves the 'evidence before scoring' ordering (gate 3).
    """
    return {
        "raw_prescore_hash": raw_prescore_hash,
        "recomputed_edge_bps": recomputed_edge_bps,
    }
