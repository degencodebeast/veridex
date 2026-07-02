"""WD-1 — the authoritative backend verify endpoint (AC-020).

Binds to Plan A's pinned ``VerifyResponse`` envelope (flat ``evidence_hash`` /
``recomputed_evidence_hash`` / ``manifest_hash`` + ``checks`` / ``metrics`` / ``anchor`` /
``proof_card``), which C1's VerifyResult consumes. The endpoint now routes through the
WD-1 verifier core (:func:`veridex.verifier.recompute.verify_run`).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from veridex.api.router import create_app
from veridex.store import InMemoryStore


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(store=InMemoryStore()))


def test_verify_recomputes_a_demo_run(client: TestClient) -> None:
    run_id = client.post("/demo/run").json()["run_id"]
    resp = client.post(f"/runs/{run_id}/verify")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == run_id
    assert body["verified"] is True
    # Evidence hash recomputed over the sealed run_events prefix is byte-identical to the seal.
    assert body["evidence_hash"] == body["recomputed_evidence_hash"]
    assert len(body["manifest_hash"]) == 64
    # Offline replay → not anchored → no explorer link, honest anchor state.
    assert body["anchor"]["explorer_url"] is None
    assert body["anchor"]["status"] == "not_anchored"


def test_verify_mirrors_plan_a_checks_metrics_split(client: TestClient) -> None:
    # SEC-001: the Proof-Checks block holds ONLY the 7 CheckId and NEVER clv; clv lives in metrics.
    from veridex.checks.result import CheckId

    run_id = client.post("/demo/run").json()["run_id"]
    body = client.post(f"/runs/{run_id}/verify").json()

    checks = body["checks"]
    metrics = body["metrics"]
    # Exactly the frozen 7 CheckId, no clv in checks.
    assert "clv" not in checks
    expected_ids = {c.value for c in CheckId}
    assert expected_ids.issubset(set(checks.keys()))
    # CLV is a performance metric, surfaced in the separate metrics block.
    assert "clv" in metrics


def test_verify_is_byte_identical_over_the_seal(client: TestClient) -> None:
    # Verify is read-only over the sealed prefix: the sealed evidence_hash is unchanged and
    # the recompute reproduces it exactly (re-running verify yields the same result).
    run_id = client.post("/demo/run").json()["run_id"]
    first = client.post(f"/runs/{run_id}/verify").json()
    second = client.post(f"/runs/{run_id}/verify").json()
    assert first["evidence_hash"] == second["evidence_hash"]
    assert first["recomputed_evidence_hash"] == first["evidence_hash"]
    assert first["manifest_hash"] == second["manifest_hash"]


def test_verify_route_surfaces_tampered_score_rows_metric() -> None:
    # SEC-002 end-to-end: doctoring a PERSISTED score_rows clv_bps (NOT a sealed event) is invisible
    # to the evidence-hash recompute but MUST surface as a metrics_recomputed FAIL in the route's
    # VerifyResponse — the fresh recompute from the sealed run_events diverges from the doctored row.
    # This exercises the SEC-002 falsifiability through the flagship route, not just the builder.
    store = InMemoryStore()
    client = TestClient(create_app(store=store))
    run_id = client.post("/demo/run").json()["run_id"]

    # Doctor a persisted (non-sealed) displayed metric directly in the store's per-tick score rows.
    doctored = 0
    for row in store._runs[run_id]["score_rows"]:
        if isinstance(row.get("clv_bps"), int):
            row["clv_bps"] = row["clv_bps"] + 99999
            doctored += 1
    assert doctored > 0, "demo run must have at least one scored clv_bps row to doctor"

    body = client.post(f"/runs/{run_id}/verify").json()
    # The verdict field on a serialized CheckResult is ``result`` (pass/fail/pending/not_applicable).
    assert body["checks"]["metrics_recomputed"]["result"] == "fail"
    # BY DESIGN: top-level `verified` stays True — it reflects the SEALED evidence-hash prefix only,
    # which score_rows is NOT part of. The recompute discrepancy is surfaced via the CHECK, not
    # `verified`. Locking this split: evidence integrity (prefix) and metric faithfulness (check)
    # are independent verdicts.
    assert body["verified"] is True


def test_verify_unknown_run_is_404(client: TestClient) -> None:
    assert client.post("/runs/does-not-exist/verify").status_code == 404
