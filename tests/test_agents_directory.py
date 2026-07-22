"""PUBLIC ``GET /agents/roster`` — the HONEST public agent directory (C1, completion layer).

Pins the fixed contract (rewritten from the old leaky roster):

* The directory is keyed on the immutable ``public_agent_id`` and sources from
  ``store.list_public_agents()`` joined to deployment state. A row is admitted IFF the agent's
  ``visibility == PUBLIC`` **and** it has a linked instance whose deploy status is ``SEALED``.
  Private / pending / failed / running / unlinked agents are EXCLUDED.
* TRUST SURFACE: the response NEVER contains a raw ``operator_id`` / ``owner_ref`` (Privy DID or
  internal id). The only owner rendering is the safe ``owner_public_label``; the legacy ``owner``
  key is GONE.
* Performance columns (``avg_clv_bps`` / ``runs`` / ``valid_pct``) are ``null`` and ``proof_state``
  is ``"unscored"`` until the agent has scored board rows; a scored agent surfaces REAL pooled stats.
* No public+sealed agents -> honest-empty ``{"agents": []}``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi.testclient import TestClient

from veridex.api.router import create_app
from veridex.deploy.instance import AgentInstance, DeployStatus
from veridex.public_agent import OperatorClass, Origin, PublicAgent, Visibility
from veridex.store import InMemoryStore

# Raw owner identifiers that MUST NEVER appear in the public response (trust surface).
_RAW_OWNER_REF_A = "did:privy:secret-owner-a-0x1111222233334444"
_RAW_OWNER_REF_B = "did:privy:secret-owner-b"
_RAW_OPERATOR_ID = "operator-secret-should-never-leak"


def _public_agent(
    public_agent_id: str,
    *,
    visibility: Visibility,
    operator_class: OperatorClass,
    origin: Origin,
    owner_ref: str | None,
    display_name: str,
) -> PublicAgent:
    return PublicAgent(
        public_agent_id=public_agent_id,
        display_name=display_name,
        operator_class=operator_class,
        origin=origin,
        visibility=visibility,
        owner_ref=owner_ref,
        created_at="t1",
        updated_at="t1",
    )


def _instance(instance_id: str, *, status: DeployStatus) -> AgentInstance:
    return AgentInstance(
        instance_id=instance_id,
        template_id="value_clv",
        agent_id="studio-value_clv",
        submitted_config={},
        effective_config={},
        config_hash="c" * 64,
        policy_hash="p" * 64,
        source_mode="replay",
        execution_mode="paper",
        run_id="run-seed",
        status=status,
        operator_id=_RAW_OPERATOR_ID,
        created_at="t1",
        updated_at="t1",
    )


def _projected_row(public_agent_id: str) -> dict[str, Any]:
    """A realistic score_run-shaped PUBLIC row (agent_id == public id, as B1 emits)."""
    return {
        "agent_id": public_agent_id,
        "public_agent_id": public_agent_id,
        "run_id": "r1",
        "source_mode": "replay",
        "total_clv_bps": 120,
        "action_count": 5,
        "avg_clv_bps": 24.0,
        "sim_pnl": 500,
        "brier": 0.21,
        "max_drawdown": -30.0,
        "valid_pct": 90.0,
        "proof_mode": "attested",
    }


def _seed() -> InMemoryStore:
    """Seed six agents of varying visibility/status; (a) is also scored.

    (a) public + SEALED + scored | (b) public + SEALED + unscored | (c) private + SEALED |
    (d) public + PENDING | (e) public + FAILED | (f) public + RUNNING.
    """
    store = InMemoryStore()

    matrix = [
        ("agent_a", Visibility.PUBLIC, DeployStatus.SEALED, OperatorClass.USER, Origin.BYOA,
         _RAW_OWNER_REF_A, "Alpha Bot"),
        ("agent_b", Visibility.PUBLIC, DeployStatus.SEALED, OperatorClass.OFFICIAL, Origin.OFFICIAL,
         None, "Beta Bot"),
        ("agent_c", Visibility.PRIVATE, DeployStatus.SEALED, OperatorClass.USER, Origin.STUDIO,
         _RAW_OWNER_REF_B, "Gamma Bot"),
        ("agent_d", Visibility.PUBLIC, DeployStatus.PENDING, OperatorClass.USER, Origin.STUDIO,
         _RAW_OWNER_REF_B, "Delta Bot"),
        ("agent_e", Visibility.PUBLIC, DeployStatus.FAILED, OperatorClass.USER, Origin.STUDIO,
         _RAW_OWNER_REF_B, "Epsilon Bot"),
        ("agent_f", Visibility.PUBLIC, DeployStatus.RUNNING, OperatorClass.USER, Origin.STUDIO,
         _RAW_OWNER_REF_B, "Zeta Bot"),
    ]

    async def _run() -> None:
        for pid, vis, status, op_class, origin, owner_ref, name in matrix:
            await store.persist_public_agent(
                _public_agent(pid, visibility=vis, operator_class=op_class,
                              origin=origin, owner_ref=owner_ref, display_name=name)
            )
            inst_id = f"inst_{pid}"
            await store.persist_agent_instance(_instance(inst_id, status=status))
            await store.link_instance_public_agent(inst_id, pid)
        # (a) is scored: persist projected board rows keyed on its public id.
        await store.persist_projected_rows([_projected_row("agent_a")])

    asyncio.run(_run())
    return store


# --- admission: ONLY public + SEALED surface -------------------------------------------------


def test_directory_admits_only_public_and_sealed() -> None:
    client = TestClient(create_app(store=_seed()))

    resp = client.get("/agents/roster")

    assert resp.status_code == 200, resp.text
    agents = resp.json()["agents"]
    ids = {a["public_agent_id"] for a in agents}
    # Only (a) public+SEALED and (b) public+SEALED are admitted; private/pending/failed/running out.
    assert ids == {"agent_a", "agent_b"}


# --- trust surface: NO raw owner_ref / operator_id / legacy owner key ------------------------


def test_directory_never_leaks_raw_owner_identity() -> None:
    client = TestClient(create_app(store=_seed()))

    resp = client.get("/agents/roster")

    assert resp.status_code == 200, resp.text
    raw_text = resp.text
    # No raw operator identity anywhere in the serialized response.
    assert "did:privy" not in raw_text
    assert _RAW_OPERATOR_ID not in raw_text
    assert _RAW_OWNER_REF_A not in raw_text
    assert _RAW_OWNER_REF_B not in raw_text
    # The legacy raw-shaped ``owner`` key is GONE — only ``owner_public_label`` remains.
    for row in resp.json()["agents"]:
        assert "owner" not in row
        assert "owner_public_label" in row


# --- honest fields per row: label + real origin + display_name + public_agent_id -------------


def test_directory_rows_carry_honest_public_fields() -> None:
    client = TestClient(create_app(store=_seed()))

    resp = client.get("/agents/roster")

    assert resp.status_code == 200, resp.text
    by_id = {a["public_agent_id"]: a for a in resp.json()["agents"]}

    row_a = by_id["agent_a"]
    assert row_a["display_name"] == "Alpha Bot"
    assert row_a["origin"] == "byoa"  # REAL origin from the public identity, never guessed
    # USER agent with an embeddable wallet -> safe truncated label (never the raw ref).
    assert row_a["owner_public_label"] == "0x1111…34444"

    row_b = by_id["agent_b"]
    assert row_b["display_name"] == "Beta Bot"
    assert row_b["origin"] == "official"
    assert row_b["owner_public_label"] == "Veridex Labs"  # OFFICIAL brand string


# --- scored vs unscored: real pooled stats for (a), honest nulls for (b) ---------------------


def test_directory_scored_and_unscored_perf() -> None:
    client = TestClient(create_app(store=_seed()))

    resp = client.get("/agents/roster")

    assert resp.status_code == 200, resp.text
    by_id = {a["public_agent_id"]: a for a in resp.json()["agents"]}

    # (a) is scored -> REAL pooled stats surface (24.0 == 120 total_clv_bps / 5 actions).
    row_a = by_id["agent_a"]
    assert isinstance(row_a["avg_clv_bps"], (int, float))
    assert row_a["avg_clv_bps"] == 24.0
    assert row_a["runs"] == 1
    assert row_a["proof_state"] == "attested"  # REAL proof state once scored

    # (b) is unscored -> honest nulls + "unscored" proof_state (never fabricated).
    row_b = by_id["agent_b"]
    assert row_b["avg_clv_bps"] is None
    assert row_b["runs"] is None
    assert row_b["valid_pct"] is None
    assert row_b["proof_state"] == "unscored"


# --- honest-empty: no public+sealed agents -> empty directory ---------------------------------


def test_directory_empty_when_no_public_sealed_agents() -> None:
    client = TestClient(create_app(store=InMemoryStore()))

    resp = client.get("/agents/roster")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"agents": []}
