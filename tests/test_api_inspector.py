"""C1 Inspector — GET /runs/{run_id}/actions/{seq} → InspectorRecord (per-action forensic view).

The route serves the FROZEN InspectorRecord shape (schemas.py) from the SEALED run: the AgentAction
+ entry market_state from run_events, the law-recomputed clv_bps from score_rows, and the action's
reason/confidence/claimed_edge_bps as the UNTRUSTED-never-scored block (SEC-003/007).
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from veridex.api.router import create_app
from veridex.ingest.marketstate import replay_marketstates
from veridex.runtime.orchestrator import run_competition
from veridex.store import InMemoryStore
from veridex.strategies.momentum import momentum_agent

WD2_FIXTURE = str(Path(__file__).parent / "fixtures" / "wd2_momentum_replay.json")


async def _stored_momentum_run() -> tuple[TestClient, str, int]:
    """Persist a single-agent momentum run and return (client, run_id, follow_momentum_seq).

    The FOLLOW_MOMENTUM decision carries claimed_edge_bps (untrusted), so its sealed sequence_no
    exercises the untrusted-metadata + scored-clv split end-to-end.
    """
    store = InMemoryStore()
    ticks = replay_marketstates(WD2_FIXTURE)
    run = await run_competition(ticks, [momentum_agent("mom", min_momentum_bps=50)], source_mode="replay", store=store)
    client = TestClient(create_app(store=store))
    follow_seq = next(
        e["sequence_no"]
        for e in run.run_events
        if e.get("event_type") == "decision"
        and e.get("action_payload_json")
        and "claimed_edge_bps" in e["action_payload_json"]
    )
    return client, run.run_id, follow_seq


async def test_inspector_record_for_a_stored_action() -> None:
    client, run_id, seq = await _stored_momentum_run()
    resp = client.get(f"/runs/{run_id}/actions/{seq}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == run_id
    assert body["agent_id"] == "mom"
    # AgentAction from the sealed decision event.
    assert body["agent_action"]["type"] == "FOLLOW_MOMENTUM"
    # Entry market_state from the sealed tick snapshot.
    assert body["market_state"]
    # SCORED metric: law-recomputed clv (an int here), echoed in the recompute block.
    assert isinstance(body["clv_bps"], int)
    assert body["recompute"]["clv_bps"] == body["clv_bps"]
    # UNTRUSTED-never-scored block (SEC-003/007): the claimed edge is surfaced as untrusted, NOT clv.
    assert "claimed_edge_bps" in body["untrusted_llm_metadata"]
    assert body["untrusted_llm_metadata"]["claimed_edge_bps"] != body["clv_bps"]


async def test_inspector_record_conforms_to_frozen_model() -> None:
    client, run_id, seq = await _stored_momentum_run()
    body = client.get(f"/runs/{run_id}/actions/{seq}").json()
    expected_keys = {
        "run_id",
        "agent_id",
        "tick_seq",
        "market_state",
        "agent_action",
        "recompute",
        "clv_bps",
        "untrusted_llm_metadata",
    }
    assert set(body.keys()) == expected_keys


def test_inspector_unknown_run_is_404() -> None:
    client = TestClient(create_app(store=InMemoryStore()))
    assert client.get("/runs/does-not-exist/actions/1").status_code == 404


async def test_inspector_unknown_or_non_action_seq_is_404() -> None:
    client, run_id, _ = await _stored_momentum_run()
    # Out-of-range seq → honest 404.
    assert client.get(f"/runs/{run_id}/actions/99999").status_code == 404
    # seq 0 is the first tick event, not an action → honest 404.
    assert client.get(f"/runs/{run_id}/actions/0").status_code == 404
