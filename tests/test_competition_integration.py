"""B11a — competition harness end-to-end integration tests (REQ-115 / AC-115, gate CON-008).

Strict TDD: every assertion below was watched RED before ``veridex/runtime/competition.py``
existed, then turned GREEN by the minimal harness. The suite is fully OFFLINE — the anchor is
MOCKED (no network), the LLM agent's Agno seam is a fixed async stub, and persistence uses the
in-memory store. This is the AC-115 wire-up: ONE fixture, ≥2 agents, ONE scored / proof-carded /
anchored / leaderboard-ranked run, with every link in the chain (evidence → manifest → anchor)
bound by hash.
"""

from __future__ import annotations

from pathlib import Path

from tests._arena_fixtures import finished_run_result
from veridex.chain.anchor import run_manifest, run_manifest_hash
from veridex.ingest.marketstate import MarketState
from veridex.runtime.agent import emit_agent_action_async  # noqa: F401  (documented stub seam)
from veridex.runtime.competition import (
    CompetitionResult,
    read_path_check_block,
    run_demo_competition,
)
from veridex.runtime.orchestrator import Agent, deterministic_agent, llm_agent
from veridex.runtime.schemas import AgentAction, SportsActionType
from veridex.scoring import score_run
from veridex.store import InMemoryStore

KEY = "OU_2_5"


# ---------------------------------------------------------------------------
# Fixtures / helpers (dict-form stable_prob_bps — the B1 normalized contract)
# ---------------------------------------------------------------------------


def _market(prob_bps: dict[str, int], *, suspended: bool = False) -> dict:
    return {
        "stable_prob_bps": dict(prob_bps),
        "stable_price": {"over": 1.6, "under": 2.4},
        "suspended": suspended,
    }


def _ms(prob_bps: dict[str, int], *, tick_seq: int) -> MarketState:
    return MarketState(
        fixture_id=42,
        tick_seq=tick_seq,
        ts=1000 + tick_seq,
        phase=2,
        markets={KEY: _market(prob_bps)},
        scores={},
    )


def _marketstates() -> list[MarketState]:
    """Two ticks; OU 'over' drifts 6000 -> 6300 bps (entry-vs-close CLV +300, positive)."""
    return [
        _ms({"over": 6000, "under": 4000}, tick_seq=0),
        _ms({"over": 6300, "under": 3700}, tick_seq=1),
    ]


class _FakeResponse:
    def __init__(self, content: object) -> None:
        self.content = content


class _FakeAsyncAgent:
    """Stub for Agno's Agent — exposes ``arun`` only (injected via the factory seam)."""

    def __init__(self, content: object) -> None:
        self._content = content

    async def arun(self, prompt: str, **kwargs: object) -> _FakeResponse:
        return _FakeResponse(self._content)


_SENTINEL_MODEL = object()


def _stub_llm_agent(agent_id: str = "llm-stub") -> Agent:
    """A real ``llm_agent`` whose Agno seam is a fixed, offline async stub.

    Returns a scorable FLAG_VALUE on the same market 'over' side, carrying a numeric
    ``confidence`` so the agent contributes a Brier (which breaks the avg-CLV tie).
    """
    action = AgentAction(
        type=SportsActionType.FLAG_VALUE,
        params={"market_key": KEY, "side": "over", "confidence": 0.7},
    )

    def factory(**kwargs: object) -> _FakeAsyncAgent:
        return _FakeAsyncAgent(action)

    return llm_agent(agent_id, model=_SENTINEL_MODEL, agent_factory=factory)


def _agents() -> list[Agent]:
    return [deterministic_agent(), _stub_llm_agent()]


async def _mock_anchor(manifest_hash: str) -> str:
    """Deterministic fake signature derived from the manifest hash (NO network)."""
    assert len(manifest_hash) == 64  # the anchor receives the manifest hash, nothing else
    return f"FAKE_SIG_{manifest_hash[:16]}"


# ---------------------------------------------------------------------------
# B11a-1: the AC-115 end-to-end — one fixture, 2 agents, one scored/anchored run
# ---------------------------------------------------------------------------


async def test_end_to_end_competition_is_scored_anchored_and_ranked() -> None:
    result = await run_demo_competition(
        _marketstates(),
        _agents(),
        source_mode="replay",
        store=InMemoryStore(),
        anchor_fn=_mock_anchor,
    )

    assert isinstance(result, CompetitionResult)

    # --- ranked leaderboard: >=2 rows, contiguous ranks 1..N -----------------------
    assert len(result.leaderboard) >= 2
    ranks = [row["rank"] for row in result.leaderboard]
    assert ranks == list(range(1, len(result.leaderboard) + 1))
    assert {row["agent_id"] for row in result.leaderboard} == {"deterministic-baseline", "llm-stub"}

    # --- proof card: lineage.proof_mode_map + checks + anchored anchor w/ signature -
    card = result.proof_card
    assert card["lineage"]["proof_mode_map"] == result.run.proof_mode_map
    assert "checks" in card
    # SEC-001: CLV is NOT a check — it lives in the separate Performance-Metrics block.
    assert "clv" not in card["checks"]
    assert card["metrics"]["clv"] > 0  # top agent has positive avg CLV (over drifts +300)
    assert card["checks"]["metrics_recomputed"]["result"] == "pass"
    assert card["anchor"]["status"] == "anchored"
    assert card["anchor"]["signature"] == result.signature
    assert result.signature is not None and result.signature.startswith("FAKE_SIG_")
    assert result.anchor_status == "anchored"


# ---------------------------------------------------------------------------
# B11a-2: source_mode flows through to the leaderboard rows
# ---------------------------------------------------------------------------


async def test_source_mode_flows_to_leaderboard_rows() -> None:
    result = await run_demo_competition(
        _marketstates(), _agents(), source_mode="replay", store=InMemoryStore(), anchor_fn=_mock_anchor
    )

    # leaderboard summarizes per-run source_mode across runs: one replay run -> "all-replay".
    assert result.leaderboard  # non-empty
    assert all(row["source_mode"] == "all-replay" for row in result.leaderboard)
    # every run is anchored -> the cross-run summary reflects it.
    assert all(row["anchor_status"] == "all-anchored" for row in result.leaderboard)


# ---------------------------------------------------------------------------
# B11a-3: the chain is bound — evidence_hash consistent run/card/manifest
# ---------------------------------------------------------------------------


async def test_evidence_hash_binds_run_card_and_manifest() -> None:
    result = await run_demo_competition(
        _marketstates(), _agents(), source_mode="replay", store=InMemoryStore(), anchor_fn=_mock_anchor
    )

    run_hash = result.run.evidence_hash
    assert len(run_hash) == 64
    # run.evidence_hash == proof_card evidence == manifest action_evidence_root (the chain binds).
    assert result.proof_card["evidence"]["evidence_hash"] == run_hash
    assert result.manifest["action_evidence_root"] == run_hash
    # the anchored manifest_hash is exactly the hash of the bound manifest.
    assert result.manifest_hash == run_manifest_hash(result.manifest)


# ---------------------------------------------------------------------------
# B11a-4: deterministic — same inputs (incl. run_id) -> same ranking + manifest_hash
# ---------------------------------------------------------------------------


async def test_deterministic_ranking_and_manifest_hash() -> None:
    r1 = await run_demo_competition(
        _marketstates(),
        _agents(),
        source_mode="replay",
        store=InMemoryStore(),
        anchor_fn=_mock_anchor,
        run_id="fixed-1",
    )
    r2 = await run_demo_competition(
        _marketstates(),
        _agents(),
        source_mode="replay",
        store=InMemoryStore(),
        anchor_fn=_mock_anchor,
        run_id="fixed-1",
    )

    assert [row["agent_id"] for row in r1.leaderboard] == [row["agent_id"] for row in r2.leaderboard]
    assert [row["rank"] for row in r1.leaderboard] == [row["rank"] for row in r2.leaderboard]
    assert r1.manifest_hash == r2.manifest_hash
    assert r1.signature == r2.signature  # fixed anchor mock + fixed manifest hash


# ---------------------------------------------------------------------------
# B11a-5: no-anchor path -> anchor_status "not_anchored", signature None
# ---------------------------------------------------------------------------


async def test_no_anchor_path_is_not_anchored() -> None:
    result = await run_demo_competition(
        _marketstates(), _agents(), source_mode="replay", store=InMemoryStore(), anchor_fn=None
    )

    assert result.anchor_status == "not_anchored"
    assert result.signature is None
    assert result.proof_card["anchor"]["status"] == "not_anchored"
    assert result.proof_card["anchor"]["signature"] is None
    # leaderboard reflects the unanchored run.
    assert all(row["anchor_status"] == "none-anchored" for row in result.leaderboard)
    # the run is still fully scored + a manifest hash is still produced (anchoring is optional).
    assert len(result.manifest_hash) == 64
    assert result.scores


# ---------------------------------------------------------------------------
# B11a-6: default checks builder composition (judgment call surfaced)
# ---------------------------------------------------------------------------


def testread_path_check_block_summary_shape() -> None:
    run = finished_run_result()
    scores = score_run(run)
    checks = read_path_check_block(scores, run)
    # SEC-001: exactly the 7 CheckIds, and CLV is NOT one of them.
    assert set(checks) == {
        "evidence_integrity",
        "llm_boundary",
        "metrics_recomputed",
        "manifest_bound",
        "policy_obeyed",
        "receipt_separation",
        "anchor",
    }
    assert "clv" not in checks
    assert checks["evidence_integrity"]["result"] == "pass"
    assert checks["llm_boundary"]["result"] == "pass"
    assert checks["metrics_recomputed"]["result"] == "pass"
    assert checks["anchor"]["result"] == "not_applicable"  # offline replay


async def test_checks_fn_is_injectable() -> None:
    sentinel = {"custom": {"result": "pass"}}

    def _checks_fn(scores: list[dict], run: object) -> dict:
        return sentinel

    result = await run_demo_competition(
        _marketstates(),
        _agents(),
        source_mode="replay",
        store=InMemoryStore(),
        anchor_fn=_mock_anchor,
        checks_fn=_checks_fn,
    )

    assert result.proof_card["checks"] == sentinel


# ---------------------------------------------------------------------------
# B11a-7: import-audit note — competition.py is a SHELL (may import agent), but the
# trust path (checks/verifier/law/ingest + scoring.py) MUST stay LLM-SDK-free.
# ---------------------------------------------------------------------------


def test_trust_path_import_audit_still_clean() -> None:
    from veridex.verifier.import_audit import assert_no_llm_imports

    root = Path(__file__).parent.parent / "veridex"
    for subdir in ("checks", "verifier", "law", "ingest"):
        assert_no_llm_imports(root / subdir)
    assert_no_llm_imports(root / "scoring.py")
    assert_no_llm_imports(root / "leaderboard.py")


def test_run_manifest_uses_run_fields() -> None:
    """The harness manifest is built from the run's own fields (not re-derived elsewhere)."""

    async def _run() -> CompetitionResult:
        return await run_demo_competition(
            _marketstates(),
            _agents(),
            source_mode="replay",
            store=InMemoryStore(),
            anchor_fn=_mock_anchor,
            run_id="manifest-check",
        )

    import asyncio

    result = asyncio.run(_run())
    manifest = result.manifest
    assert manifest["run_id"] == "manifest-check"
    assert manifest["agent_ids"] == result.run.agent_ids
    assert manifest["proof_mode_map"] == result.run.proof_mode_map
    assert manifest["action_evidence_root"] == result.run.evidence_hash
    # rebuilding the manifest from the same fields reproduces the same hash.
    rebuilt = run_manifest(
        run_id=manifest["run_id"],
        fixture_or_window_id=manifest["fixture_or_window_id"],
        agent_ids=manifest["agent_ids"],
        action_evidence_root=manifest["action_evidence_root"],
        score_root=manifest["score_root"],
        proof_mode_map=manifest["proof_mode_map"],
        code_prompt_schema_versions=manifest["code_prompt_schema_versions"],
    )
    # The per-domain root forest (Pre-2C) is bound into the canonical manifest before hashing,
    # so a faithful rebuild must carry it too (it is part of what manifest_hash commits to).
    rebuilt["root_forest"] = manifest["root_forest"]
    assert run_manifest_hash(rebuilt) == result.manifest_hash
