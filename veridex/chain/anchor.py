"""On-chain anchor — ONE devnet Solana Memo tx per run over a manifest hash. Test-driven (T8).

NOT per-tick anchoring. Payload = SHA-256 of the run manifest.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def run_manifest(
    *,
    run_id: str,
    fixture_or_window_id: str,
    agent_ids: list[str],
    action_evidence_root: str,
    score_root: str,
    proof_mode_map: dict[str, str],
    code_prompt_schema_versions: dict[str, str],
) -> dict[str, Any]:
    """Build the per-run manifest that gets hashed and anchored."""
    return {
        "run_id": run_id,
        "fixture_or_window_id": fixture_or_window_id,
        "agent_ids": agent_ids,
        "action_evidence_root": action_evidence_root,
        "score_root": score_root,
        "proof_mode_map": proof_mode_map,
        "code_prompt_schema_versions": code_prompt_schema_versions,
    }


def run_manifest_hash(manifest: dict[str, Any]) -> str:
    """SHA-256 of the canonically-serialized run manifest.

    Canonical form: json.dumps with sort_keys=True and compact separators — deterministic
    across processes and Python versions.
    """
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def memo_payload_for_manifest(manifest: dict[str, Any]) -> str:
    """The exact payload anchored in the Memo tx == the run-manifest hash (gate 4).

    Pins REQ-005/KILL-4: what we anchor on devnet is the manifest hash, not anything unrelated.
    """
    return run_manifest_hash(manifest)


def anchor_memo(manifest_hash: str) -> str:
    """Send ONE devnet Memo tx whose payload is `manifest_hash`. Returns the tx signature.

    DEFERRED — Phase 0 offline spike only. This function requires:
      - A funded devnet wallet (keypair file or env var).
      - A live Solana devnet RPC endpoint.
      - The `solders` / `solana-py` SDK (intentionally NOT imported in this file,
        which lives on the trust path and must stay stdlib-only).
    Wire this up in the Phase 1 integration step once devnet creds are available.
    A fake/no-op tx would be worse than an explicit deferral — do not stub.
    """
    raise NotImplementedError(
        "anchor_memo requires a live devnet RPC + funded wallet. "
        "Deferred to Phase 1 — see docstring for wiring instructions."
    )
