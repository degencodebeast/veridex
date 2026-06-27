"""Phase 0 — T1 first-failing tests (RED).

Every test calls real behavior and currently errors on the `NotImplementedError` stub
(feature-missing RED). Each GREENs in its tagged task (T2–T8). Tags map to the spike's
REQ-001..007 / KILL-1..6 (see `.omc/plans/phase0-spike-task-plan.md`).

NOTE on test 1: `test_verifier_imports_without_agno` is partly a standing GUARD (the trust
path must never import an LLM SDK), but it is RED here because the AST-audit machinery
(`assert_no_llm_imports`) is not yet implemented (T3).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "txline_odds_sample.json"


# 1 — REQ-003 · gate 2/7 · KILL-2
def test_verifier_imports_without_agno():
    from veridex.verifier.import_audit import assert_no_llm_imports
    import veridex.checks as checks_pkg
    import veridex.verifier as verifier_pkg

    # Trust-path packages must import cleanly AND contain no LLM SDK imports.
    assert_no_llm_imports(Path(checks_pkg.__file__).parent)
    assert_no_llm_imports(Path(verifier_pkg.__file__).parent)


# 2 — REQ-001 · gate 3 · KILL-1
def test_evidence_hash_stable_cross_process():
    from veridex.runtime.evidence import compute_evidence_hash

    events = [
        {"sequence_no": 2, "event_type": "decision", "action_payload_json": '{"type":"WAIT"}'},
        {"sequence_no": 1, "event_type": "tick", "state_snapshot_json": '{"tick":1}'},
    ]
    h1 = compute_evidence_hash(events)
    h2 = compute_evidence_hash(list(reversed(events)))  # order-independent (sorted by sequence_no)
    assert h1 == h2
    assert isinstance(h1, str) and len(h1) == 64  # sha256 hex


# 3 — REQ-006 · gate 6/8 · KILL-6
def test_proof_card_public_json_uses_checks_not_cats():
    from veridex.verifier.proof_card import build_proof_card

    card = build_proof_card(
        run={"run_id": 1, "status": "complete"},
        evidence={"run_log_hash": "ab12", "run_event_count": 2},
        checks={"clv": {"result": "pass"}},
        proof_mode="LLM/evidence-verified",
    )
    assert "checks" in card
    assert "cats" not in card  # public field names, not value substrings


# 4 — REQ-005 · gate 4 · KILL-4
def test_memo_manifest_hash_matches_anchor_payload():
    from veridex.chain.anchor import memo_payload_for_manifest, run_manifest, run_manifest_hash

    manifest = run_manifest(
        run_id="r1",
        fixture_or_window_id="17952170",
        agent_ids=["agno-1", "det-baseline"],
        action_evidence_root="ev_root",
        score_root="sc_root",
        proof_mode_map={"agno-1": "LLM/evidence-verified"},
        code_prompt_schema_versions={"action_schema": "sports_v0"},
    )
    h = run_manifest_hash(manifest)
    assert isinstance(h, str) and len(h) == 64
    # the payload anchored in the Memo tx MUST equal the manifest hash (not something unrelated)
    assert memo_payload_for_manifest(manifest) == h


# 5 — REQ-007 · gate 5 · KILL-5
def test_live_and_replay_yield_same_marketstate_shape():
    from veridex.ingest.marketstate import marketstate_from_fixture, marketstate_from_sse

    tick = json.loads(FIXTURE.read_text())["ticks"][0]
    ms_replay = marketstate_from_fixture(tick, tick_seq=0, fixture_id=17952170)
    ms_live = marketstate_from_sse(tick, tick_seq=0, fixture_id=17952170)
    assert set(ms_replay.model_dump().keys()) == set(ms_live.model_dump().keys())


# 6 — gate 1
def test_llm_claimed_edge_is_ignored():
    from veridex.checks.clv import compute_clv_check

    # LLM claims a fat +600bps edge; deterministic recompute says -100bps → must FAIL on recomputed.
    res = compute_clv_check(recomputed_edge_bps=-100, claimed_edge_bps=600)
    assert res.result == "fail"  # scored on recomputed, not the claimed edge


# 7 — gate 3
def test_raw_prescore_record_written_before_score_row():
    from veridex.runtime.evidence import build_raw_prescore_record, score_row_from_prescore

    rec = build_raw_prescore_record(
        evidence_hash="ev",
        raw_action={"type": "WAIT", "params": {}},
        action_schema_version="sports_v0",
        agent_id="agno-1",
        model_prompt_config_hash="mpc",
        tick_seq=3,
        proof_mode="LLM/evidence-verified",
    )
    assert rec["record_kind"] == "raw_prescore"
    assert "raw_prescore_hash" in rec
    for k in ("evidence_hash", "raw_action", "model_prompt_config_hash", "tick_seq", "proof_mode"):
        assert k in rec
    # the score row must derive ONLY from the bound raw record hash + recomputed values (gate 3 ordering)
    row = score_row_from_prescore(raw_prescore_hash=rec["raw_prescore_hash"], recomputed_edge_bps=-100)
    assert row["raw_prescore_hash"] == rec["raw_prescore_hash"]


# 8 — gate 4
def test_no_validate_stat_claim_for_odds_without_confirmed_odds_proof():
    from veridex.chain.txline_authenticity import txline_native_authenticity

    assert txline_native_authenticity("odds", odds_proof_confirmed=False) == "recorded_evidence"
    assert txline_native_authenticity("scores") == "validateStat"


# 8b — gate 4 honesty (Codex/spec review): even a CONFIRMED odds-proof must NOT be relabeled
# `validateStat` (that is TxLINE's scores-only on-chain mechanism — reusing it for odds overclaims).
def test_confirmed_odds_proof_does_not_claim_validate_stat():
    from veridex.chain.txline_authenticity import txline_native_authenticity

    label = txline_native_authenticity("odds", odds_proof_confirmed=True)
    assert label != "validateStat"
    assert label == "odds_proof_confirmed"


# 9 — REQ-003
def test_agno_output_schema_fallback_json_parse_produces_agent_action():
    from veridex.runtime.agent import parse_agent_action_json
    from veridex.runtime.schemas import AgentAction, SportsActionType

    action = parse_agent_action_json('{"type": "WAIT", "params": {}}')
    assert isinstance(action, AgentAction) and action.type == SportsActionType.WAIT


# 10 — REQ-007
def test_live_stream_parser_tolerates_plain_lines_and_sse_heartbeat_if_present():
    from veridex.ingest.marketstate import parse_sse_line

    assert parse_sse_line("event: heartbeat") is None  # heartbeats ignored
    assert parse_sse_line("") is None  # blank lines ignored
    rec = parse_sse_line('data: {"ts": 1718000000, "phase": 2}')
    assert rec is not None and rec.get("ts") == 1718000000


# 11 — REQ-004
def test_deterministic_baseline_emits_same_schema_reproducibly():
    from veridex.ingest.marketstate import marketstate_from_fixture
    from veridex.runtime.baseline import deterministic_baseline_action
    from veridex.runtime.schemas import AgentAction

    tick = json.loads(FIXTURE.read_text())["ticks"][0]
    ms = marketstate_from_fixture(tick, tick_seq=0, fixture_id=17952170)
    a1 = deterministic_baseline_action(ms)
    a2 = deterministic_baseline_action(ms)
    assert isinstance(a1, AgentAction) and a1 == a2  # reproducible


# 12 — REQ-001 · REQ-004
def test_marketstate_replay_is_deterministic():
    from veridex.ingest.marketstate import replay_marketstates

    run_a = replay_marketstates(str(FIXTURE))
    run_b = replay_marketstates(str(FIXTURE))
    assert [m.model_dump() for m in run_a] == [m.model_dump() for m in run_b]
    assert len(run_a) == 2  # two ticks in the fixture


# 13 — MarketState immutability (Codex T0/T1 review; trust-boundary invariant)
def test_marketstate_is_immutable_snapshot():
    from veridex.ingest.marketstate import MarketState

    ms = MarketState(fixture_id=1, tick_seq=0, ts=1, phase=2, markets={}, scores={})
    try:
        ms.fixture_id = 999  # top-level mutation must NOT take effect
    except Exception:
        pass
    assert ms.fixture_id == 1


# 14 — T3 durability (Codex T3 review): the import audit must FIRE on planted forbidden
# imports — incl. `from google import generativeai`, where the SDK name is in alias.name not
# module — and must NOT false-positive on sibling namespaces / relative imports. gate 2/7 · KILL-2
@pytest.mark.parametrize(
    "src",
    [
        "import agno\n",
        "from agno.os import AgentOS\n",
        "import google.generativeai as genai\n",
        "from google.generativeai.types import Tool\n",
        "from google import generativeai\n",  # <- the bypass form
        "import openai\n",
        "import litellm as llm\n",
    ],
)
def test_import_audit_fires_on_planted_llm_import(tmp_path, src):
    from veridex.verifier.import_audit import assert_no_llm_imports

    (tmp_path / "mod.py").write_text(src)
    with pytest.raises(AssertionError, match="Forbidden"):
        assert_no_llm_imports(tmp_path)


def test_import_audit_allows_sibling_and_relative_imports(tmp_path):
    from veridex.verifier.import_audit import assert_no_llm_imports

    (tmp_path / "mod.py").write_text(
        "from google.cloud import storage\nfrom . import sibling\nimport json\n"
    )
    assert_no_llm_imports(tmp_path)  # must not raise


# 15 — REQ-004: the deterministic baseline must be a real rules agent that READS MarketState
# (not a degenerate constant), else it isn't a meaningful reproducible-proof contestant.
def test_deterministic_baseline_responds_to_marketstate_not_constant():
    from veridex.ingest.marketstate import MarketState
    from veridex.runtime.baseline import deterministic_baseline_action
    from veridex.runtime.schemas import SportsActionType

    base = dict(fixture_id=1, tick_seq=0, ts=1, phase=2, scores={})
    suspended = MarketState(
        markets={"OU_2_5": {"stable_prob_bps": 5800, "stable_price": 1.72, "suspended": True}}, **base
    )
    flaggable = MarketState(
        markets={"OU_2_5": {"stable_prob_bps": 5800, "stable_price": 1.72, "suspended": False}}, **base
    )
    quiet = MarketState(
        markets={"OU_2_5": {"stable_prob_bps": 4200, "stable_price": 2.38, "suspended": False}}, **base
    )
    assert deterministic_baseline_action(suspended).type == SportsActionType.WIDEN_OR_SUSPEND
    assert deterministic_baseline_action(flaggable).type == SportsActionType.FLAG_VALUE
    assert deterministic_baseline_action(quiet).type == SportsActionType.WAIT
