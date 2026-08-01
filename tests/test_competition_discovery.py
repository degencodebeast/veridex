"""Real create+start → discovery acceptance (spec §8 cross-page create/start flow).

Proves the exact backend lifecycle the Competitions/Arena pages depend on: a real competition,
created then started through the production routes, finalizes SYNCHRONOUSLY (status='finalized' +
run_id) and is then returned by the UNFILTERED GET /competitions list reader that the frontend
getCompetitions() consumes. This is the real backend half of the discovery acceptance — it can fail
even when a mocked-frontend record test passes, which is why it exists.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from veridex.api.router import create_app
from veridex.store import InMemoryStore

_CONFIG = {
    "competition_type": "replay_arena",
    "source_mode": "replay",
    "execution_mode": "paper",  # paper start needs no operator token
    "market_scope": "WC:NLD-MAR",
    "roster_size": 2,
}
_AGENT_A = {"agent_id": "agent-alpha", "owner": "team-a", "strategy": "baseline", "model": None, "proof_mode": "reproducible"}
_AGENT_B = {"agent_id": "agent-beta", "owner": "team-b", "strategy": "contrarian", "model": None, "proof_mode": "reproducible"}


def test_create_start_finalizes_and_appears_in_unfiltered_list() -> None:
    client = TestClient(create_app(store=InMemoryStore()))

    # CREATE
    created = client.post("/competitions", json=_CONFIG)
    assert created.status_code == 200, created.text
    comp_id = created.json()["competition_id"]
    assert created.json()["status"] == "draft"

    # ROSTER (roster_size=2 → 2 agents required before start)
    client.post(f"/competitions/{comp_id}/agents", json=_AGENT_A)
    client.post(f"/competitions/{comp_id}/agents", json=_AGENT_B)

    # START → synchronous finalize (the real enum value the frontend must surface)
    started = client.post(f"/competitions/{comp_id}/start")
    assert started.status_code == 200, started.text
    body = started.json()
    assert body["competition_id"] == comp_id
    assert body["status"] == "finalized"
    assert isinstance(body["run_id"], str) and body["run_id"]

    # The UNFILTERED list reader the Competitions page consumes returns the finalized record,
    # carrying config.market_scope so getCompetitions() derives a real title.
    listed = client.get("/competitions")
    assert listed.status_code == 200, listed.text
    match = [r for r in listed.json() if r["competition_id"] == comp_id]
    assert len(match) == 1, listed.json()
    assert match[0]["status"] == "finalized"
    assert match[0]["config"]["market_scope"] == "WC:NLD-MAR"
    assert match[0]["run_id"] == body["run_id"]
