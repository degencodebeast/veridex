"""API surface contract freeze — fixtures + live responses validate against the pinned models."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

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
