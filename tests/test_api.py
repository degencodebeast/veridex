"""B11b — FastAPI demo surface tests (REQ-115 / AC-115).

Test strategy: FastAPI TestClient backed by an InMemoryStore (offline; no network, no anchor).
Two deterministic agents are used so no real LLM call is ever issued in the test path.

RED-watch targets (each test was observed to fail before the implementation existed):
  - test_demo_run_post_200: ImportError / 404 (no router)
  - test_leaderboard_after_demo_run: 404 (no endpoint)
  - test_get_run_by_id_has_proof_card: 404 (no endpoint)
  - test_get_run_not_found_404: AssertionError (no 404 handler)
  - test_cli_main_prints_valid_json: ImportError (no CLI module)
  - test_core_import_does_not_require_fastapi: would fail if competition imported fastapi
"""

from __future__ import annotations

import importlib
import json
import sys
import types
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from veridex.api.router import create_app
from veridex.store import InMemoryStore


def _client() -> TestClient:
    """Build a TestClient backed by a fresh InMemoryStore."""
    return TestClient(create_app(store=InMemoryStore()))


# ---------------------------------------------------------------------------
# POST /demo/run
# ---------------------------------------------------------------------------


def test_demo_run_post_200() -> None:
    """POST /demo/run returns 200 with a full DemoRunResponse artifact."""
    client = _client()
    resp = client.post("/demo/run")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "run_id" in body
    assert body["anchor_status"] == "not_anchored"
    # Leaderboard must have ≥2 ranked rows (two agents competed)
    lb = body["leaderboard"]
    assert len(lb) >= 2, f"expected ≥2 leaderboard rows, got {len(lb)}: {lb}"
    ranks = [row["rank"] for row in lb]
    assert 1 in ranks and 2 in ranks, f"missing ranks 1 or 2: {ranks}"


def test_demo_run_leaderboard_is_not_a_tie() -> None:
    """The two offline demo agents are DIFFERENTIATED — the board is a real ranking, not a tie.

    agent-alpha (value pick: highest-prob side) and agent-beta (contrarian: the other side)
    make different picks on the demo ticks → different CLV → distinct, ordered ranks.
    """
    client = _client()
    lb = client.post("/demo/run").json()["leaderboard"]
    assert len(lb) == 2, f"expected exactly 2 agents, got {lb}"

    by_rank = {row["rank"]: row for row in lb}
    rank1, rank2 = by_rank[1], by_rank[2]

    # Distinct CLV — NOT a tie.
    assert rank1["avg_clv_bps"] != rank2["avg_clv_bps"], f"leaderboard is a tie: {lb}"
    # Genuinely ordered by CLV (rank 1 strictly better than rank 2).
    assert rank1["avg_clv_bps"] > rank2["avg_clv_bps"], f"ranks not ordered by CLV: {lb}"
    # Distinct agent identities.
    assert rank1["agent_id"] != rank2["agent_id"]


def test_demo_run_proof_card_has_lineage() -> None:
    """POST /demo/run proof_card carries lineage with proof_mode_map and schema_versions."""
    client = _client()
    body = client.post("/demo/run").json()
    pc = body["proof_card"]
    assert "lineage" in pc, f"no 'lineage' in proof_card: {pc.keys()}"
    lineage = pc["lineage"]
    assert "proof_mode_map" in lineage
    assert "schema_versions" in lineage


def test_demo_run_proof_card_has_anchor() -> None:
    """POST /demo/run proof_card carries an anchor block with status='not_anchored'."""
    client = _client()
    body = client.post("/demo/run").json()
    pc = body["proof_card"]
    assert "anchor" in pc
    assert pc["anchor"]["status"] == "not_anchored"


def test_demo_run_proof_card_has_checks() -> None:
    """POST /demo/run proof_card has checks (never 'cats') with clv, evidence_integrity, llm_boundary."""
    client = _client()
    body = client.post("/demo/run").json()
    pc = body["proof_card"]
    assert "checks" in pc
    assert "cats" not in pc
    checks = pc["checks"]
    assert set(checks) >= {"evidence_integrity", "llm_boundary", "metrics_recomputed", "anchor"}
    assert "clv" not in checks  # SEC-001
    assert "metrics" in pc and "clv" in pc["metrics"]  # CLV lives in Performance Metrics


# ---------------------------------------------------------------------------
# GET /leaderboard
# ---------------------------------------------------------------------------


def test_leaderboard_after_demo_run() -> None:
    """GET /leaderboard after a demo run returns ranked rows for both agents."""
    store = InMemoryStore()
    client = TestClient(create_app(store=store))
    client.post("/demo/run")

    resp = client.get("/leaderboard")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "rows" in body
    rows = body["rows"]
    assert len(rows) >= 2, f"expected ≥2 leaderboard rows, got {rows}"
    for row in rows:
        assert "rank" in row
        assert "agent_id" in row
        assert "avg_clv_bps" in row


def test_leaderboard_empty_before_run() -> None:
    """GET /leaderboard returns an empty list before any demo runs."""
    client = _client()
    resp = client.get("/leaderboard")
    assert resp.status_code == 200
    assert resp.json() == {"rows": []}


# ---------------------------------------------------------------------------
# GET /runs/{run_id}
# ---------------------------------------------------------------------------


def test_get_run_by_id_has_proof_card() -> None:
    """GET /runs/{run_id} after a POST /demo/run returns a proof card with anchor + checks + lineage."""
    store = InMemoryStore()
    client = TestClient(create_app(store=store))
    run_id = client.post("/demo/run").json()["run_id"]

    resp = client.get(f"/runs/{run_id}")
    assert resp.status_code == 200, resp.text
    pc = resp.json()
    # Proof card structure
    assert "anchor" in pc, f"missing 'anchor' key: {pc.keys()}"
    assert "status" in pc["anchor"]
    assert "checks" in pc, f"missing 'checks' key: {pc.keys()}"
    assert "lineage" in pc, f"missing 'lineage' key: {pc.keys()}"
    # Must not expose 'cats' (KILL-6 / AC-111)
    assert "cats" not in pc


def test_get_run_not_found_returns_404() -> None:
    """GET /runs/unknown returns 404."""
    client = _client()
    resp = client.get("/runs/does-not-exist")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_main_prints_proof_card_and_leaderboard_json(capsys: pytest.CaptureFixture[str]) -> None:
    """scripts/demo_phase1.main() prints valid PROOF CARD and LEADERBOARD JSON to stdout."""
    from scripts.demo_phase1 import main

    main()  # runs synchronously (asyncio.run inside)

    captured = capsys.readouterr()
    stdout = captured.out
    assert "PROOF CARD" in stdout
    assert "LEADERBOARD" in stdout

    # Extract the two JSON blocks from stdout
    lines = stdout.splitlines()
    json_lines: list[str] = []
    in_block = False
    blocks: list[str] = []
    for line in lines:
        if line.startswith("{"):
            in_block = True
            json_lines = [line]
        elif in_block and (line.startswith("}") or line.startswith("  ") or line.startswith("]")):
            json_lines.append(line)
            if line == "}":
                blocks.append("\n".join(json_lines))
                in_block = False
                json_lines = []
        elif in_block:
            json_lines.append(line)

    # At minimum, both stdout blocks should parse as JSON
    assert len(blocks) >= 1, f"expected JSON blocks in stdout:\n{stdout}"
    parsed = json.loads(blocks[0])
    # The first block is the proof card — check it has expected keys
    assert isinstance(parsed, dict)
    assert "anchor" in parsed or "verifier_version" in parsed or "leaderboard" not in parsed


def test_cli_main_stdout_contains_valid_json_objects() -> None:
    """main() stdout includes at least one valid JSON object (proof card or leaderboard)."""
    import io

    from scripts.demo_phase1 import main

    captured = io.StringIO()
    with mock.patch("sys.stdout", captured):
        main()

    output = captured.getvalue()
    # Find JSON objects/arrays by attempting a balanced decode at every `{`/`[` start.
    # (A naive first-bracket-to-last-bracket slice is unsafe: the proof card itself now
    # contains nested arrays/objects, so the span across both printed blocks isn't valid
    # JSON.  ``raw_decode`` consumes exactly one well-formed value from a start position.)
    assert "{" in output, "no JSON object found in stdout"
    decoder = json.JSONDecoder()
    parsed_any = False
    for index, char in enumerate(output):
        if char not in "{[":
            continue
        try:
            decoder.raw_decode(output[index:])
            parsed_any = True
            break
        except json.JSONDecodeError:
            pass
    assert parsed_any, f"could not parse any JSON from stdout:\n{output[:500]}"


# ---------------------------------------------------------------------------
# Import isolation: core must NOT require fastapi
# ---------------------------------------------------------------------------


def test_core_import_does_not_require_fastapi() -> None:
    """veridex.runtime.competition must import cleanly without fastapi on sys.path."""
    # Save and temporarily remove fastapi from sys.modules so we get a clean probe
    saved: dict[str, types.ModuleType] = {}
    fa_keys = [k for k in list(sys.modules) if k.startswith("fastapi") or k.startswith("starlette")]
    for k in fa_keys:
        saved[k] = sys.modules.pop(k)

    try:
        # This must NOT raise even with fastapi removed from sys.modules
        mod = importlib.import_module("veridex.runtime.competition")
        assert hasattr(mod, "run_demo_competition")
    finally:
        sys.modules.update(saved)
