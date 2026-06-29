"""API surface contract freeze — fixtures + live responses validate against the pinned models."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from veridex.api.demo_fixtures import build_demo_ticks, contrarian_agent
from veridex.api.router import create_app
from veridex.api.schemas import (
    CompetitionStateResponse,
    FeedHealthResponse,
    InspectorRecord,
    LeaderboardResponse,
    ProofArtifactResponse,
    RuntimeEventsResponse,
    VerifyResponse,
)
from veridex.store import InMemoryStore

_FIXTURES = Path("contracts/fixtures")

# filename -> the response model it must validate against (drift in either side fails the freeze).
_REGISTRY = {
    "leaderboard.json": LeaderboardResponse,
    "competition_state.json": CompetitionStateResponse,
    "proof_artifact.json": ProofArtifactResponse,
    "verify_response.json": VerifyResponse,
    "inspector_record.json": InspectorRecord,
    "feed_health.json": FeedHealthResponse,
    "runtime_events.json": RuntimeEventsResponse,
}


def test_every_committed_fixture_validates_against_its_model() -> None:
    for name, model in _REGISTRY.items():
        data = json.loads((_FIXTURES / name).read_text())
        model.model_validate(data)  # raises on contract drift


def test_verify_endpoint_matches_verify_response_and_carries_proof_artifact() -> None:
    client = TestClient(create_app(store=InMemoryStore()))
    run_id = client.post("/demo/run").json()["run_id"]

    verify = client.post(f"/runs/{run_id}/verify")
    assert verify.status_code == 200
    body = verify.json()
    VerifyResponse.model_validate(body)  # live response conforms to the pinned model

    # WD-1: the recompute confirms the sealed hash, and the embedded ProofArtifact carries the
    # exact fields C1's VerifyResult/ProofArtifact bind to.
    assert body["verified"] is True
    assert body["recomputed_evidence_hash"] == body["evidence_hash"]
    ProofArtifactResponse.model_validate(body["proof_card"])
    assert {"verifier_version", "run", "lineage", "evidence", "checks", "anchor"} <= set(body["proof_card"])


def test_verify_unknown_run_is_404() -> None:
    client = TestClient(create_app(store=InMemoryStore()))
    assert client.post("/runs/nope/verify").status_code == 404


def test_proof_artifact_route_is_get_runs() -> None:
    """Pinned decision: the ProofArtifact source is GET /runs/{id} (no /api/proof route)."""
    client = TestClient(create_app(store=InMemoryStore()))
    run_id = client.post("/demo/run").json()["run_id"]
    pc = client.get(f"/runs/{run_id}")
    assert pc.status_code == 200
    ProofArtifactResponse.model_validate(pc.json())
    assert client.get(f"/api/proof/{run_id}").status_code == 404  # the alternate route is NOT added


# The 7 frozen Proof-Check ids (spec §4.3 / SEC-001). CLV is NOT one of them — it lives in metrics.
_SEVEN_CHECK_IDS = {
    "evidence_integrity",
    "llm_boundary",
    "metrics_recomputed",
    "manifest_bound",
    "policy_obeyed",
    "receipt_separation",
    "anchor",
}


def test_live_checks_block_is_sec001_compliant() -> None:
    """SEC-001 target: the live ``checks`` block holds ONLY the 7 CheckId; CLV lives in ``metrics``.

    This is the FINAL shape the frozen contract (``contracts/veridex_api.contract.ts`` + the
    ``verify_response``/``proof_artifact`` fixtures) pins. Task 5 (WD-5b) migrated the live backend
    so both ``POST /runs/{id}/verify`` and ``GET /runs/{id}`` now emit the 7-CheckId block with CLV
    relocated to ``metrics`` — so this assertion now genuinely passes (the prior strict-xfail guard
    is removed).
    """
    client = TestClient(create_app(store=InMemoryStore()))
    run_id = client.post("/demo/run").json()["run_id"]

    verify_checks = client.post(f"/runs/{run_id}/verify").json()["checks"]
    assert "clv" not in verify_checks  # SEC-001: CLV must never appear in the checks block
    assert set(verify_checks) >= _SEVEN_CHECK_IDS

    proof_checks = client.get(f"/runs/{run_id}").json()["checks"]
    assert "clv" not in proof_checks
    assert set(proof_checks) >= _SEVEN_CHECK_IDS


async def test_verify_route_manifest_hash_matches_seal_time() -> None:
    """Carry #3 (DRY): the verify route rebuilds the manifest with the seal-time helpers.

    The ``POST /runs/{id}/verify`` handler derives ``manifest_hash`` via ``_score_root`` +
    ``_fixture_or_window_id`` (the authoritative ``competition.py`` helpers) over the persisted run,
    so it MUST equal the ``manifest_hash`` computed when the run was originally sealed.
    """
    from veridex.runtime.competition import run_demo_competition
    from veridex.runtime.orchestrator import deterministic_agent

    store = InMemoryStore()
    sealed = await run_demo_competition(
        build_demo_ticks(),
        [deterministic_agent("agent-alpha"), contrarian_agent("agent-beta")],
        source_mode="replay",
        store=store,
        anchor_fn=None,
    )

    client = TestClient(create_app(store=store))
    verify = client.post(f"/runs/{sealed.run.run_id}/verify")
    assert verify.status_code == 200
    assert verify.json()["manifest_hash"] == sealed.manifest_hash
