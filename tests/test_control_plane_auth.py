"""Phase-2B Task 7 — control-plane auth tests (the fail-closed KEYSTONE, TDD).

The control plane FAILS CLOSED: every WRITE that can touch capital or policy state requires a
valid operator bearer token (REQ-2B-18/19, AC-2B-15). Reads and the spectator WS stay public.

* Missing / malformed / wrong token → 401.
* Authenticated-but-wrong-owner (``competition.config.operator_id`` ≠ principal) → 403, no mutation.
* A ``paper`` start stays open/public (no auth).
* Reads (``GET``) never require auth.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from veridex.api.router import create_app
from veridex.config import Settings
from veridex.store import InMemoryStore

_OP_TOKEN = "secret-op-token"
_OP_ID = "op-main"


def _settings() -> Settings:
    """Build offline settings with an operator token + id configured (env-independent)."""
    return Settings(_env_file=None, operator_token=_OP_TOKEN, operator_id=_OP_ID)  # type: ignore[call-arg]


def _permissive_envelope() -> dict[str, object]:
    """A permissive policy envelope (allows the demo venue/market, auto-approves)."""
    return {
        "max_stake": 100.0,
        "max_orders_per_run": 100,
        "max_orders_per_session": 100,
        "max_orders_per_day": 100,
        "venue_allowlist": ["fake"],
        "market_allowlist": [
            "OVERUNDER_PARTICIPANT_GOALS|half=1|line=1",
            "1X2_PARTICIPANT_RESULT||",
        ],
        "min_edge_bps": -100000,
        "max_slippage_bps": 100000,
        "max_price": 1.0e9,
        "max_quote_age_s": 10**9,
        "cooldown_s": 0,
        "human_approval_threshold": 1.0e12,
        "kill_switch": False,
    }


def _dry_run_config(*, operator_id: str | None, envelope: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "competition_type": "replay_arena",
        "source_mode": "replay",
        "execution_mode": "dry_run",
        "market_scope": "WC:TEST",
        "roster_size": 2,
        "operator_id": operator_id,
        "policy_envelope": envelope if envelope is not None else _permissive_envelope(),
    }


_AGENT_A = {
    "agent_id": "agent-alpha",
    "owner": "team-a",
    "strategy": "value_clv",
    "model": None,
    "proof_mode": "reproducible",
    "execution_eligibility": True,
}
_AGENT_B = {
    "agent_id": "agent-beta",
    "owner": "team-b",
    "strategy": "contrarian_clv",
    "model": None,
    "proof_mode": "reproducible",
    "execution_eligibility": True,
}


@pytest.fixture
def client() -> TestClient:
    """A TestClient backed by an InMemoryStore with operator auth configured."""
    return TestClient(create_app(store=InMemoryStore(), settings=_settings()))


@pytest.fixture
def op_headers() -> dict[str, str]:
    """Valid operator bearer header (authenticates as ``op-main``)."""
    return {"Authorization": f"Bearer {_OP_TOKEN}"}


def _make_dry_run(client: TestClient, *, operator_id: str | None) -> str:
    """Create a dry_run competition with 2 eligible agents; return its id."""
    comp_id = client.post("/competitions", json=_dry_run_config(operator_id=operator_id)).json()["competition_id"]
    client.post(f"/competitions/{comp_id}/agents", json=_AGENT_A)
    client.post(f"/competitions/{comp_id}/agents", json=_AGENT_B)
    return comp_id


# ---------------------------------------------------------------------------
# Missing / invalid auth → 401 (fail closed)
# ---------------------------------------------------------------------------


def test_killswitch_requires_auth(client: TestClient) -> None:  # AC-2B-15
    assert client.post("/competitions/c/kill-switch").status_code in (401, 403)


def test_approve_unauth_no_submit(client: TestClient) -> None:  # AC-2B-15
    assert client.post("/executions/e/approve").status_code in (401, 403)


def test_nonpaper_start_requires_auth(client: TestClient) -> None:  # AC-2B-15
    comp_id = _make_dry_run(client, operator_id=_OP_ID)
    # No Authorization header on a dry_run start → fail closed.
    resp = client.post(f"/competitions/{comp_id}/start")
    assert resp.status_code in (401, 403)
    # No execution events should have been produced.
    events = client.get(f"/competitions/{comp_id}/events?since_seq=-1").json()
    assert events == []


def test_invalid_token_is_401(client: TestClient) -> None:  # AC-2B-15
    comp_id = _make_dry_run(client, operator_id=_OP_ID)
    resp = client.post(f"/competitions/{comp_id}/start", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


def test_malformed_header_is_401(client: TestClient) -> None:  # AC-2B-15
    assert client.post("/competitions/c/kill-switch", headers={"Authorization": _OP_TOKEN}).status_code == 401


# ---------------------------------------------------------------------------
# Wrong owner → 403, no mutation
# ---------------------------------------------------------------------------


def test_wrong_owner_start_403(client: TestClient, op_headers: dict[str, str]) -> None:  # AC-2B-15
    comp_id = _make_dry_run(client, operator_id="someone-else")
    resp = client.post(f"/competitions/{comp_id}/start", headers=op_headers)
    assert resp.status_code == 403
    assert client.get(f"/competitions/{comp_id}/events?since_seq=-1").json() == []


def test_wrong_owner_killswitch_403_no_mutation(client: TestClient, op_headers: dict[str, str]) -> None:  # AC-2B-15
    comp_id = _make_dry_run(client, operator_id="someone-else")
    resp = client.post(f"/competitions/{comp_id}/kill-switch", headers=op_headers)
    assert resp.status_code == 403
    # Envelope unchanged (kill_switch stays False).
    state = client.get(f"/competitions/{comp_id}").json()
    assert state["config"]["policy_envelope"]["kill_switch"] is False


# ---------------------------------------------------------------------------
# Paper start + reads stay public
# ---------------------------------------------------------------------------


def test_paper_start_is_public(client: TestClient) -> None:  # AC-2B-15
    paper_config = {
        "competition_type": "replay_arena",
        "source_mode": "replay",
        "execution_mode": "paper",
        "market_scope": "WC:TEST",
        "roster_size": 2,
    }
    comp_id = client.post("/competitions", json=paper_config).json()["competition_id"]
    client.post(f"/competitions/{comp_id}/agents", json=_AGENT_A)
    client.post(f"/competitions/{comp_id}/agents", json=_AGENT_B)
    # No auth header → still 200 for a paper competition.
    resp = client.post(f"/competitions/{comp_id}/start")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "finalized"


def test_reads_are_public(client: TestClient) -> None:  # AC-2B-15
    comp_id = _make_dry_run(client, operator_id=_OP_ID)
    assert client.get(f"/competitions/{comp_id}").status_code == 200
    assert client.get(f"/competitions/{comp_id}/events").status_code == 200
    assert client.get(f"/competitions/{comp_id}/executions").status_code == 200
