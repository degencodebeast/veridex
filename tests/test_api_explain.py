"""Proof Explainer Phase A — endpoint teeth (TDD, RED-proofed).

``POST /runs/{id}/explain`` is the Proof Explainer surface. It is READ-ONLY, NON-scoring, and
does NO writes/mutation/control. These tests pin the two trust-critical endpoint teeth:

* TEETH 2 — "CHANGES NOTHING" (headline): the served ProofArtifact + VerifyResponse (checks,
  metrics, evidence_hash, manifest_hash, proof_card) are BYTE-IDENTICAL whether or not the
  explainer has been invoked. RED-proof: if the endpoint wrote back / mutated the run, the
  before/after verify + proof-card bytes would diverge.
* TEETH 3 — SANITIZED READ-MODEL: the endpoint hands the explainer ONLY whitelisted served fields
  (ProofArtifactResponse + VerifyResponse + glossary) — never a raw RunResult / unsealed state /
  store handle. RED-proof: a leaked raw field breaks the exact-whitelist key assertion.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from veridex.api.router import create_app
from veridex.config import Settings
from veridex.store import InMemoryStore

_PROOF_ARTIFACT_FIELDS = {"verifier_version", "run", "lineage", "evidence", "checks", "anchor", "metrics"}
_VERIFY_FIELDS = {
    "run_id",
    "verified",
    "evidence_hash",
    "recomputed_evidence_hash",
    "manifest_hash",
    "checks",
    "metrics",
    "anchor",
}


@pytest.fixture
def client() -> TestClient:
    # Inject keyless settings so the explain endpoint stays OFFLINE (graceful degrade) — the test
    # suite must never make a real OpenRouter call nor import httpx (the venue offline-safety guard).
    return TestClient(create_app(store=InMemoryStore(), settings=Settings(_env_file=None)))


# ---------------------------------------------------------------------------
# TEETH 2 — "CHANGES NOTHING" (headline)
# ---------------------------------------------------------------------------


def test_explain_changes_nothing_byte_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate the explainer being AVAILABLE and producing a full narration — proving that even a
    # produced explanation changes NOTHING on the proof path (offline: no real LLM call).
    async def _available(read_model, *, question=None, target_field=None, settings=None, client=None):
        return {"explanation": "Here is a long plain-language narration of the proof.", "disclaimer": "d", "footer": "f"}

    monkeypatch.setattr("veridex.api.router.explain_proof", _available)
    client = TestClient(create_app(store=InMemoryStore(), settings=Settings(_env_file=None)))
    run_id = client.post("/demo/run").json()["run_id"]

    # Snapshot the served proof artifact + verify BEFORE the explainer is ever invoked.
    proof_before = client.get(f"/runs/{run_id}").json()
    verify_before = client.post(f"/runs/{run_id}/verify").json()

    # Invoke the explainer (available, producing narration).
    explain = client.post(f"/runs/{run_id}/explain")
    assert explain.status_code == 200
    assert explain.json()["explanation"]  # narration WAS produced

    # Snapshot AFTER: every proof/verify byte must be identical — the explainer changed nothing.
    proof_after = client.get(f"/runs/{run_id}").json()
    verify_after = client.post(f"/runs/{run_id}/verify").json()

    assert json.dumps(proof_after, sort_keys=True) == json.dumps(proof_before, sort_keys=True)
    assert json.dumps(verify_after, sort_keys=True) == json.dumps(verify_before, sort_keys=True)
    # Spell out the trust-critical fields explicitly (headline invariants).
    assert verify_after["evidence_hash"] == verify_before["evidence_hash"]
    assert verify_after["recomputed_evidence_hash"] == verify_before["recomputed_evidence_hash"]
    assert verify_after["manifest_hash"] == verify_before["manifest_hash"]
    assert verify_after["checks"] == verify_before["checks"]
    assert verify_after["metrics"] == verify_before["metrics"]
    assert verify_after["proof_card"] == verify_before["proof_card"]


def test_explain_is_read_only_and_returns_the_disclaimer_envelope(client: TestClient) -> None:
    run_id = client.post("/demo/run").json()["run_id"]
    body = client.post(f"/runs/{run_id}/explain").json()
    assert set(body.keys()) == {"explanation", "disclaimer", "footer"}
    # The response labels itself educational-only and points at the deterministic verifier.
    assert "does not verify" in body["disclaimer"]
    assert "source of truth" in body["footer"].lower()


def test_explain_unknown_run_is_404(client: TestClient) -> None:
    assert client.post("/runs/does-not-exist/explain").status_code == 404


# ---------------------------------------------------------------------------
# TEETH 3 — SANITIZED READ-MODEL
# ---------------------------------------------------------------------------


def test_explain_passes_only_the_sanitized_whitelisted_read_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spy on the explainer to capture EXACTLY what the endpoint hands it.

    RED-proof: leaking a raw field (e.g. the RunResult, run_events, unsealed/live state, or the
    store handle) would either add a top-level key (breaking the exact-whitelist assertion) or make
    ``json.dumps`` raise (a raw ``RunResult`` / store handle is not plain-JSON) — either way, FAIL.
    """
    captured: dict = {}

    async def _spy(read_model, *, question=None, target_field=None, settings=None, client=None):
        captured["read_model"] = read_model
        return {"explanation": "spy", "disclaimer": "d", "footer": "f"}

    # The endpoint looks up ``explain_proof`` as a router module global at call time.
    monkeypatch.setattr("veridex.api.router.explain_proof", _spy)

    client = TestClient(create_app(store=InMemoryStore()))
    run_id = client.post("/demo/run").json()["run_id"]
    resp = client.post(f"/runs/{run_id}/explain")
    assert resp.status_code == 200

    read_model = captured["read_model"]
    # Exactly the three whitelisted top-level buckets — no raw run / unsealed / DB-handle leakage.
    assert set(read_model.keys()) == {"proof_artifact", "verify", "glossary"}
    # Fully plain-JSON serializable: a raw RunResult / store handle would raise here.
    json.dumps(read_model)
    # The served view-models carry ONLY their frozen served fields.
    assert set(read_model["proof_artifact"].keys()) == _PROOF_ARTIFACT_FIELDS
    assert set(read_model["verify"].keys()) == _VERIFY_FIELDS
    # No raw sealed internals leak through the served shape.
    assert "run_events" not in read_model["proof_artifact"]
    assert "score_rows" not in read_model["verify"]
    # The pinned 13-term glossary rides along, grounding the narration.
    assert "clv" in read_model["glossary"]
    assert read_model["glossary"]["clv"]["label"] == "CLV"


def test_explain_forwards_question_and_target_field(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    async def _spy(read_model, *, question=None, target_field=None, settings=None, client=None):
        captured["question"] = question
        captured["target_field"] = target_field
        return {"explanation": "spy", "disclaimer": "d", "footer": "f"}

    monkeypatch.setattr("veridex.api.router.explain_proof", _spy)
    client = TestClient(create_app(store=InMemoryStore()))
    run_id = client.post("/demo/run").json()["run_id"]
    client.post(f"/runs/{run_id}/explain", json={"question": "what is CLV?", "target_field": "checks"})
    assert captured["question"] == "what is CLV?"
    assert captured["target_field"] == "checks"
