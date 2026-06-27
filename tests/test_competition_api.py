"""Phase-2A Task 6 — Competition REST endpoint tests (TDD).

Tests for all 6 competition endpoints (Part B) plus Phase-1 regression tests
(REQ-222 / AC-212). FastAPI TestClient backed by InMemoryStore — fully offline,
no network, no LLM, no DB.

TDD sequence: each test was written BEFORE the corresponding endpoint existed and
should initially return 404 / 422 / AttributeError before implementation.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from veridex.api.router import create_app
from veridex.store import InMemoryStore

# ---------------------------------------------------------------------------
# Shared fixtures and helpers
# ---------------------------------------------------------------------------

_COMPETITION_CONFIG = {
    "competition_type": "replay_arena",
    "source_mode": "replay",
    "execution_mode": "paper",
    "market_scope": "WC:TEST",
    "roster_size": 2,
}

_AGENT_ENTRY_A = {
    "agent_id": "agent-alpha",
    "owner": "team-a",
    "strategy": "value_clv",
    "model": None,
    "proof_mode": "reproducible",
}

_AGENT_ENTRY_B = {
    "agent_id": "agent-beta",
    "owner": "team-b",
    "strategy": "contrarian_clv",
    "model": None,
    "proof_mode": "reproducible",
}


def _client() -> TestClient:
    """Build a TestClient backed by a fresh InMemoryStore."""
    return TestClient(create_app(store=InMemoryStore()))


def _fully_run_client() -> tuple[TestClient, str]:
    """Create a client, run a full competition, and return (client, competition_id)."""
    store = InMemoryStore()
    client = TestClient(create_app(store=store))
    comp_id = client.post("/competitions", json=_COMPETITION_CONFIG).json()["competition_id"]
    client.post(f"/competitions/{comp_id}/agents", json=_AGENT_ENTRY_A)
    client.post(f"/competitions/{comp_id}/agents", json=_AGENT_ENTRY_B)
    resp = client.post(f"/competitions/{comp_id}/start")
    assert resp.status_code == 200, resp.text
    return client, comp_id


# ---------------------------------------------------------------------------
# POST /competitions
# ---------------------------------------------------------------------------


def test_create_competition_returns_competition_id_and_draft_status() -> None:
    """POST /competitions returns {competition_id, status='draft'}."""
    client = _client()
    resp = client.post("/competitions", json=_COMPETITION_CONFIG)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "competition_id" in body
    assert body["competition_id"].startswith("c_")
    assert body["status"] == "draft"


def test_create_competition_invalid_body_422() -> None:
    """POST /competitions with missing required fields returns 422."""
    client = _client()
    resp = client.post("/competitions", json={"competition_type": "replay_arena"})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /competitions/{id}/agents
# ---------------------------------------------------------------------------


def test_register_agent_returns_agent_id_config_hash_and_proof_mode() -> None:
    """POST /competitions/{id}/agents returns {agent_id, config_hash, proof_mode}."""
    client = _client()
    comp_id = client.post("/competitions", json=_COMPETITION_CONFIG).json()["competition_id"]
    resp = client.post(f"/competitions/{comp_id}/agents", json=_AGENT_ENTRY_A)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["agent_id"] == "agent-alpha"
    assert body["proof_mode"] == "reproducible"
    assert body["config_hash"] is not None
    assert len(body["config_hash"]) == 64  # SHA-256 hex digest


def test_register_agent_llm_proof_mode_normalized() -> None:
    """POST /competitions/{id}/agents normalizes 'LLM/evidence-verified' → 'verified'."""
    client = _client()
    comp_id = client.post("/competitions", json=_COMPETITION_CONFIG).json()["competition_id"]
    entry = {**_AGENT_ENTRY_A, "proof_mode": "LLM/evidence-verified"}
    resp = client.post(f"/competitions/{comp_id}/agents", json=entry)
    assert resp.status_code == 200
    assert resp.json()["proof_mode"] == "verified"


def test_register_agent_unknown_competition_404() -> None:
    """POST /competitions/unknown/agents returns 404."""
    client = _client()
    resp = client.post("/competitions/does-not-exist/agents", json=_AGENT_ENTRY_A)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /competitions/{id}/start
# ---------------------------------------------------------------------------


def test_start_non_paper_execution_mode_returns_400() -> None:
    """AC-217: POST /start with execution_mode='dry_run' returns 400; no events created."""
    store = InMemoryStore()
    client = TestClient(create_app(store=store))
    dry_run_config = {**_COMPETITION_CONFIG, "execution_mode": "dry_run"}
    comp_id = client.post("/competitions", json=dry_run_config).json()["competition_id"]
    client.post(f"/competitions/{comp_id}/agents", json=_AGENT_ENTRY_A)
    client.post(f"/competitions/{comp_id}/agents", json=_AGENT_ENTRY_B)

    resp = client.post(f"/competitions/{comp_id}/start")

    assert resp.status_code == 400, resp.text
    assert "execution_mode_not_available_in_phase_2a" in resp.json()["detail"]
    # Gate must fire before any mutation — no events should be present.
    events = client.get(f"/competitions/{comp_id}/events?since_seq=-1").json()
    assert events == []


def test_start_competition_returns_finalized_status_and_run_id() -> None:
    """POST /competitions/{id}/start returns status='finalized' and a set run_id."""
    client = _client()
    comp_id = client.post("/competitions", json=_COMPETITION_CONFIG).json()["competition_id"]
    client.post(f"/competitions/{comp_id}/agents", json=_AGENT_ENTRY_A)
    client.post(f"/competitions/{comp_id}/agents", json=_AGENT_ENTRY_B)

    resp = client.post(f"/competitions/{comp_id}/start")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["competition_id"] == comp_id
    assert body["status"] == "finalized"
    assert isinstance(body["run_id"], str) and len(body["run_id"]) > 0


def test_start_competition_unknown_competition_404() -> None:
    """POST /competitions/unknown/start returns 404."""
    client = _client()
    resp = client.post("/competitions/does-not-exist/start")
    assert resp.status_code == 404


def test_start_already_finalized_returns_409() -> None:
    """POST /competitions/{id}/start on a finalized competition returns 409 Conflict."""
    store = InMemoryStore()
    client = TestClient(create_app(store=store))
    comp_id = client.post("/competitions", json=_COMPETITION_CONFIG).json()["competition_id"]
    client.post(f"/competitions/{comp_id}/agents", json=_AGENT_ENTRY_A)
    client.post(f"/competitions/{comp_id}/agents", json=_AGENT_ENTRY_B)
    client.post(f"/competitions/{comp_id}/start")
    # second start on a finalized competition → 409
    resp = client.post(f"/competitions/{comp_id}/start")
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# GET /competitions/{id}  — state
# ---------------------------------------------------------------------------


def test_get_competition_state_200_after_start() -> None:
    """GET /competitions/{id} returns 200 with status='finalized', 2 roster entries, leaderboard ≥2, run_id."""
    client, comp_id = _fully_run_client()

    resp = client.get(f"/competitions/{comp_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["competition_id"] == comp_id
    assert body["status"] == "finalized"
    assert len(body["roster"]) == 2
    assert len(body["leaderboard"]) >= 2
    assert body["latest_seq"] > 0
    assert body["run_id"] is not None and len(body["run_id"]) > 0


def test_get_competition_state_leaderboard_ranked() -> None:
    """GET /competitions/{id} leaderboard has ranks 1 & 2; rank-1 CLV >= rank-2 CLV."""
    client, comp_id = _fully_run_client()
    body = client.get(f"/competitions/{comp_id}").json()
    lb = body["leaderboard"]
    ranks = [row["rank"] for row in lb]
    assert 1 in ranks and 2 in ranks
    by_rank = {row["rank"]: row for row in lb}
    r1_clv = by_rank[1]["mean_clv_bps"] or 0.0
    r2_clv = by_rank[2]["mean_clv_bps"] or 0.0
    assert r1_clv >= r2_clv, f"rank-1 CLV {r1_clv} < rank-2 CLV {r2_clv}"


def test_get_competition_state_draft_has_empty_leaderboard_and_zero_seq() -> None:
    """GET /competitions/{id} in DRAFT returns empty leaderboard, latest_seq=0, run_id=None."""
    client = _client()
    comp_id = client.post("/competitions", json=_COMPETITION_CONFIG).json()["competition_id"]
    body = client.get(f"/competitions/{comp_id}").json()
    assert body["status"] == "draft"
    assert body["leaderboard"] == []
    assert body["latest_seq"] == 0
    assert body["run_id"] is None


def test_get_competition_state_unknown_404() -> None:
    """GET /competitions/unknown returns 404."""
    client = _client()
    resp = client.get("/competitions/does-not-exist")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /competitions/{id}/events
# ---------------------------------------------------------------------------


def test_get_events_returns_ordered_log_after_start() -> None:
    """GET /competitions/{id}/events?since_seq=0 returns seq≥1 events, ascending order."""
    client, comp_id = _fully_run_client()

    resp = client.get(f"/competitions/{comp_id}/events?since_seq=0")
    assert resp.status_code == 200, resp.text
    events = resp.json()
    assert len(events) > 0
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs)
    assert all(s > 0 for s in seqs)  # since_seq=0 excludes seq=0


def test_get_events_since_seq_filters_correctly() -> None:
    """Two consecutive since_seq calls return strictly monotone tails."""
    client, comp_id = _fully_run_client()

    first_call = client.get(f"/competitions/{comp_id}/events?since_seq=0").json()
    assert len(first_call) > 1
    midpoint = first_call[len(first_call) // 2]["seq"]
    second_call = client.get(f"/competitions/{comp_id}/events?since_seq={midpoint}").json()
    assert all(e["seq"] > midpoint for e in second_call)


def test_get_events_beyond_max_seq_returns_empty() -> None:
    """GET /competitions/{id}/events?since_seq=9999 returns []."""
    client, comp_id = _fully_run_client()
    resp = client.get(f"/competitions/{comp_id}/events?since_seq=9999")
    assert resp.status_code == 200
    assert resp.json() == []


def test_get_events_unknown_competition_404() -> None:
    """GET /competitions/unknown/events returns 404."""
    client = _client()
    resp = client.get("/competitions/does-not-exist/events")
    assert resp.status_code == 404


def test_get_events_default_since_seq_excludes_seq0() -> None:
    """GET /competitions/{id}/events (no since_seq) defaults to since_seq=0, excluding seq=0."""
    client, comp_id = _fully_run_client()
    resp = client.get(f"/competitions/{comp_id}/events")
    assert resp.status_code == 200
    events = resp.json()
    assert all(e["seq"] > 0 for e in events)


# ---------------------------------------------------------------------------
# GET /competitions
# ---------------------------------------------------------------------------


def test_list_competitions_no_filter_returns_all() -> None:
    """GET /competitions (no filter) returns all competitions."""
    store = InMemoryStore()
    client = TestClient(create_app(store=store))
    client.post("/competitions", json=_COMPETITION_CONFIG)
    client.post("/competitions", json=_COMPETITION_CONFIG)

    resp = client.get("/competitions")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 2
    assert all("competition_id" in item and "status" in item for item in items)


def test_list_competitions_status_filter_returns_only_matching() -> None:
    """GET /competitions?status=finalized returns only finalized competitions."""
    store = InMemoryStore()
    client = TestClient(create_app(store=store))
    # Run one to finalized
    comp_id = client.post("/competitions", json=_COMPETITION_CONFIG).json()["competition_id"]
    client.post(f"/competitions/{comp_id}/agents", json=_AGENT_ENTRY_A)
    client.post(f"/competitions/{comp_id}/agents", json=_AGENT_ENTRY_B)
    client.post(f"/competitions/{comp_id}/start")
    # A second one stays in draft
    client.post("/competitions", json=_COMPETITION_CONFIG)

    resp = client.get("/competitions?status=finalized")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["competition_id"] == comp_id
    assert items[0]["status"] == "finalized"


def test_list_competitions_empty_returns_empty_list() -> None:
    """GET /competitions on a fresh store returns []."""
    client = _client()
    resp = client.get("/competitions")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_competitions_invalid_status_422() -> None:
    """GET /competitions?status=bad_value returns 422."""
    client = _client()
    resp = client.get("/competitions?status=not_a_real_status")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Phase-1 regression (REQ-222 / AC-212)
# ---------------------------------------------------------------------------


def test_phase1_demo_run_still_200() -> None:
    """POST /demo/run → 200 (Phase-1 regression)."""
    client = _client()
    resp = client.post("/demo/run")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "run_id" in body
    assert len(body.get("leaderboard", [])) >= 2


def test_phase1_leaderboard_still_200() -> None:
    """GET /leaderboard → 200 (Phase-1 regression)."""
    client = _client()
    resp = client.get("/leaderboard")
    assert resp.status_code == 200
    assert "rows" in resp.json()


def test_phase1_run_not_found_still_404() -> None:
    """GET /runs/unknown → 404 (Phase-1 regression)."""
    client = _client()
    resp = client.get("/runs/not-a-run")
    assert resp.status_code == 404
