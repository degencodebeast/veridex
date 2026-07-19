"""Phase-2B Task 7 — service integration + live producer tests (the keystone, TDD).

Execution runs STRICTLY DOWNSTREAM of the seal as a SECOND derived block (seq AFTER the 2A
derived tail) and is EXCLUDED from the AC-213 sealed-prefix parity check. Running it leaves the
skill/scoring artifacts (evidence_hash, score_rows, leaderboard, proof-card skill block)
byte-identical — receipt ≠ skill (AC-2B-05/16). The proof-card execution receipts are a separate,
non-scoring, off-chain venue artifact distinct from the Phase-1 Memo anchor (AC-2B-12).

The authorized human-approval flow re-checks law + policy + eligibility before submitting, and
fails closed when the kill-switch has been flipped.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from veridex.api.router import create_app
from veridex.api.ws import ArenaConnectionManager
from veridex.competition.events import CompetitionEvent, build_evidence_event
from veridex.config import Settings
from veridex.store import InMemoryStore

_OP_TOKEN = "secret-op-token"
_OP_ID = "op-main"
_OVERUNDER = "OVERUNDER_PARTICIPANT_GOALS|half=1|line=1"


def _settings() -> Settings:
    return Settings(_env_file=None, operator_token=_OP_TOKEN, operator_id=_OP_ID)  # type: ignore[call-arg]


def _envelope(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "max_stake": 100.0,
        "max_orders_per_run": 100,
        "max_orders_per_session": 100,
        "max_orders_per_day": 100,
        "venue_allowlist": ["fake"],
        "market_allowlist": [_OVERUNDER, "1X2_PARTICIPANT_RESULT||"],
        "min_edge_bps": -100000,
        "max_slippage_bps": 100000,
        "max_price": 1.0e9,
        "max_quote_age_s": 10**9,
        "cooldown_s": 0,
        "human_approval_threshold": 1.0e12,
        "kill_switch": False,
    }
    base.update(overrides)
    return base


def _config(
    *, execution_mode: str, operator_id: str | None = _OP_ID, envelope: dict[str, object] | None = None
) -> dict[str, object]:
    cfg: dict[str, object] = {
        "competition_type": "replay_arena",
        "source_mode": "replay",
        "execution_mode": execution_mode,
        "market_scope": "WC:TEST",
        "roster_size": 2,
        "operator_id": operator_id,
    }
    if execution_mode != "paper":
        cfg["policy_envelope"] = envelope if envelope is not None else _envelope()
    return cfg


_AGENT_A = {
    "agent_id": "agent-alpha",
    "owner": "team-a",
    "strategy": "baseline",
    "model": None,
    "proof_mode": "reproducible",
    "execution_eligibility": True,
}
_AGENT_B = {
    "agent_id": "agent-beta",
    "owner": "team-b",
    "strategy": "contrarian",
    "model": None,
    "proof_mode": "reproducible",
    "execution_eligibility": True,
}


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(store=InMemoryStore(), settings=_settings()))


@pytest.fixture
def op_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_OP_TOKEN}"}


def _run(client: TestClient, cfg: dict[str, object], headers: dict[str, str] | None = None) -> str:
    comp_id = client.post("/competitions", json=cfg).json()["competition_id"]
    client.post(f"/competitions/{comp_id}/agents", json=_AGENT_A)
    client.post(f"/competitions/{comp_id}/agents", json=_AGENT_B)
    resp = client.post(f"/competitions/{comp_id}/start", headers=headers or {})
    assert resp.status_code == 200, resp.text
    return comp_id


# ---------------------------------------------------------------------------
# Dry-run produces execution events; payout_status never present
# ---------------------------------------------------------------------------


def test_dry_run_competition_emits_execution_events(
    client: TestClient, op_headers: dict[str, str]
) -> None:  # AC-2B-08/09
    comp_id = _run(client, _config(execution_mode="dry_run"), headers=op_headers)
    events = client.get(f"/competitions/{comp_id}/events?since_seq=-1").json()
    types = {e["event_type"] for e in events}
    assert "policy_result" in types
    assert "execution_receipt" in types
    assert "execution_submitted" in types
    # payout_status (Phase-2D) is NEVER emitted by any current lane.
    assert "payout_status" not in types


def test_execution_block_is_after_2a_tail_and_excluded_from_prefix(
    client: TestClient, op_headers: dict[str, str]
) -> None:
    """Execution events form a SECOND derived block: seq strictly after the 2A finalized tail."""
    comp_id = _run(client, _config(execution_mode="dry_run"), headers=op_headers)
    events = client.get(f"/competitions/{comp_id}/events?since_seq=-1").json()
    finalized = next(e for e in events if e["event_type"] == "competition_finalized")
    exec_types = {"policy_result", "execution_submitted", "execution_receipt"}
    exec_seqs = [e["seq"] for e in events if e["event_type"] in exec_types]
    assert exec_seqs, "expected an execution block"
    # Every execution event sits strictly after the COMPETITION_FINALIZED 2A-tail event.
    assert min(exec_seqs) > finalized["seq"]
    # seqs are contiguous and ascending overall.
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs)
    assert seqs == list(range(0, len(seqs)))


# ---------------------------------------------------------------------------
# receipt ≠ skill: leaderboard + proof-card skill block byte-identical w/ or w/o fills
# ---------------------------------------------------------------------------


def test_leaderboard_identical_with_or_without_execution(
    client: TestClient, op_headers: dict[str, str]
) -> None:  # AC-2B-05
    paper_id = _run(client, _config(execution_mode="paper"))
    dry_id = _run(client, _config(execution_mode="dry_run"), headers=op_headers)

    def board(comp_id: str) -> list[dict[str, object]]:
        rows = client.get(f"/competitions/{comp_id}").json()["leaderboard"]
        return [
            {k: r[k] for k in ("rank", "agent_id", "total_clv_bps", "mean_clv_bps", "valid_count", "proof_mode")}
            for r in rows
        ]

    assert board(paper_id) == board(dry_id)


def test_proof_card_skill_block_byte_identical_with_fills(
    client: TestClient, op_headers: dict[str, str]
) -> None:  # AC-2B-16
    paper_id = _run(client, _config(execution_mode="paper"))
    dry_id = _run(client, _config(execution_mode="dry_run"), headers=op_headers)

    paper_state = client.get(f"/competitions/{paper_id}").json()
    dry_state = client.get(f"/competitions/{dry_id}").json()

    # The skill/scoring block (evidence + checks) is byte-identical regardless of fills.
    assert paper_state["proof_card"]["evidence"] == dry_state["proof_card"]["evidence"]
    assert paper_state["proof_card"]["checks"] == dry_state["proof_card"]["checks"]

    # Execution receipts appear ONLY on the dry_run side, under a separate non-scoring key.
    assert dry_state["execution"] is not None
    assert dry_state["execution"]["non_scoring"] is True
    assert dry_state["execution"]["derived"] is True
    assert paper_state["execution"] is None or paper_state["execution"]["receipts"] == []


def test_execution_receipt_is_offchain_artifact_not_memo_anchor(
    client: TestClient, op_headers: dict[str, str]
) -> None:  # AC-2B-12
    comp_id = _run(client, _config(execution_mode="dry_run"), headers=op_headers)
    state = client.get(f"/competitions/{comp_id}").json()
    execution = state["execution"]
    assert execution is not None and execution["receipts"]
    # The receipts are an off-chain venue artifact, explicitly distinct from the Memo anchor.
    assert execution["venue_artifact"] is True
    assert "anchor" not in execution  # no on-chain anchor block hiding inside the receipts
    # The Phase-1 Memo anchor lives separately on the skill-block proof card.
    assert "anchor" in state["proof_card"]
    receipt = execution["receipts"][0]
    assert receipt["venue"] == "fake"


# ---------------------------------------------------------------------------
# Authorized human-approval flow: re-check then submit-or-reject
# ---------------------------------------------------------------------------


def _awaiting_human_competition(client: TestClient, op_headers: dict[str, str]) -> tuple[str, str]:
    """Start a dry_run competition whose envelope escalates every clean action to awaiting_human."""
    cfg = _config(execution_mode="dry_run", envelope=_envelope(human_approval_threshold=0.0))
    comp_id = _run(client, cfg, headers=op_headers)
    recs = client.get(f"/competitions/{comp_id}/executions").json()
    pending = next(r for r in recs if r["status"] == "awaiting_human")
    return comp_id, pending["execution_id"]


def test_authorized_approve_rechecks_and_submits(client: TestClient, op_headers: dict[str, str]) -> None:
    comp_id, exec_id = _awaiting_human_competition(client, op_headers)
    resp = client.post(f"/executions/{exec_id}/approve", headers=op_headers, json={"note": "ok"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["decision"] == "approved"
    # The record advanced past awaiting_human and carries a receipt.
    rec = client.get(f"/executions/{exec_id}").json()
    assert rec["status"] in ("submitted", "accepted", "filled")
    assert rec["receipt"] is not None
    # A non-scoring approval audit event was appended.
    events = client.get(f"/competitions/{comp_id}/events?since_seq=-1").json()
    audit = [e for e in events if e["event_type"] == "approval_audit"]
    assert audit and audit[-1]["payload"]["approver_id"] == _OP_ID
    assert audit[-1]["evidence"] is False


def test_authorized_approve_fails_closed_if_killswitch_flipped(client: TestClient, op_headers: dict[str, str]) -> None:
    comp_id, exec_id = _awaiting_human_competition(client, op_headers)
    # Flip the kill switch (control-plane write, persisted on the envelope).
    ks = client.post(f"/competitions/{comp_id}/kill-switch", headers=op_headers)
    assert ks.status_code == 200, ks.text
    # Re-check now denies; the record is rejected and never submitted.
    resp = client.post(f"/executions/{exec_id}/approve", headers=op_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["decision"] == "rejected"
    rec = client.get(f"/executions/{exec_id}").json()
    assert rec["status"] == "rejected"
    assert rec["receipt"] is None


def test_wrong_owner_approve_403_no_mutation(op_headers: dict[str, str]) -> None:  # AC-2B-15
    """A foreign-owned awaiting_human record cannot be approved by a non-owner principal."""
    from veridex.competition.models import (
        Competition,
        CompetitionConfig,
        CompetitionStatus,
        CompetitionType,
    )
    from veridex.execution.models import ExecutionRecord, ExecutionStatus

    store = InMemoryStore()
    client = TestClient(create_app(store=store, settings=_settings()))

    comp = Competition(
        competition_id="c_foreign",
        config=CompetitionConfig(
            competition_type=CompetitionType.REPLAY_ARENA,
            source_mode="replay",
            market_scope="WC:TEST",
            roster_size=2,
            operator_id="someone-else",  # NOT op-main
        ),
        status=CompetitionStatus.FINALIZED,
        entries=[],
        run_id="r_foreign",
    )
    record = ExecutionRecord(
        execution_id="r_foreign:1",
        competition_id="c_foreign",
        run_id="r_foreign",
        agent_id="agent-alpha",
        source_sequence_no=1,
        status=ExecutionStatus.AWAITING_HUMAN,
        policy_hash="deadbeef",
    )

    async def _seed() -> None:
        await store.create_competition(comp)
        await store.append_execution_record(record)

    asyncio.run(_seed())

    resp = client.post("/executions/r_foreign:1/approve", headers=op_headers)
    assert resp.status_code == 403
    # No mutation: the record stays awaiting_human with no receipt.
    rec = client.get("/executions/r_foreign:1").json()
    assert rec["status"] == "awaiting_human"
    assert rec["receipt"] is None


def test_approve_unknown_execution_404(client: TestClient, op_headers: dict[str, str]) -> None:
    assert client.post("/executions/does-not-exist/approve", headers=op_headers).status_code == 404


def test_approve_non_awaiting_409(client: TestClient, op_headers: dict[str, str]) -> None:
    # A normal dry_run (auto-approve) leaves no awaiting_human record; approving a filled one → 409.
    comp_id = _run(client, _config(execution_mode="dry_run"), headers=op_headers)
    recs = client.get(f"/competitions/{comp_id}/executions").json()
    terminal = next(r for r in recs if r["status"] != "awaiting_human")
    resp = client.post(f"/executions/{terminal['execution_id']}/approve", headers=op_headers)
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Phase-1 + 2A endpoints still green
# ---------------------------------------------------------------------------


def test_phase1_and_2a_endpoints_still_green(client: TestClient) -> None:  # AC-2B-13
    assert client.post("/demo/run").status_code == 200
    assert client.get("/leaderboard").status_code == 200


# ---------------------------------------------------------------------------
# WS overflow: a slow client is disconnected (not silently dropped); broadcast error-isolated
# ---------------------------------------------------------------------------


def _evidence_event(seq: int) -> CompetitionEvent:
    run_event = {
        "sequence_no": seq,
        "event_type": "tick",
        "state_snapshot_json": '{"tick_seq": 0, "ts": 0, "phase": 0, "markets": {}}',
    }
    event, _ = build_evidence_event(competition_id="c", run_id="r", run_event=run_event, current_tick_ts=0)
    return event


async def test_ws_broadcast_disconnects_slow_client_keeps_healthy() -> None:  # REQ-2B-30
    manager = ArenaConnectionManager(max_queue_size=1)
    slow = manager.connect("c")
    healthy = manager.connect("c")
    # Fill the slow client's bounded queue to capacity.
    slow.put_nowait(_evidence_event(0))

    await manager.broadcast("c", _evidence_event(1))

    # The slow client is dropped from the registry (gap-signalled), NOT silently skipped.
    assert slow not in manager._clients.get("c", set())
    # The healthy client still received the event — the run is unaffected.
    assert healthy.qsize() == 1


async def test_ws_broadcast_error_isolated_per_client() -> None:  # carried from Task 6
    manager = ArenaConnectionManager(max_queue_size=10)

    class _Boom:
        def put_nowait(self, _event: object) -> None:
            raise RuntimeError("dead client")

    healthy = manager.connect("c")
    # Inject a raising pseudo-queue alongside a healthy one.
    manager._clients["c"].add(_Boom())  # type: ignore[arg-type]

    # A raising client must NOT abort the broadcast for healthy peers.
    await manager.broadcast("c", _evidence_event(1))
    assert healthy.qsize() == 1


def test_smoke_event_loop_available() -> None:
    # Guard: ensure the module imports cleanly under asyncio_mode=auto.
    assert asyncio.get_event_loop_policy() is not None
