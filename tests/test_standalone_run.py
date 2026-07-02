"""WD-3 — the decoupled standalone-run core (one agent, no competition container)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from veridex.chain.anchor import run_manifest_hash
from veridex.checks.build import build_check_results, check_results_to_proof_block
from veridex.ingest.marketstate import MarketState, replay_marketstates
from veridex.policy.envelope import PolicyEnvelope
from veridex.runtime.competition import SCHEMA_VERSIONS
from veridex.runtime.orchestrator import Agent, run_competition
from veridex.runtime.schemas import AgentAction, SportsActionType
from veridex.runtime.window import RunWindow
from veridex.scoring import score_run
from veridex.strategies.momentum import momentum_agent
from veridex.venues.sx_bet import FakeVenueAdapter
from veridex.verifier.recompute import (
    fixture_or_window_id_from_events,
    manifest_from_run,
    recompute_score_root,
)
from veridex_agent.run import StandaloneRunResult, standalone_run

FIXTURE = str(Path(__file__).parent / "fixtures" / "wd2_momentum_replay.json")

# --- T20 live-launch-path helpers (offline; injected stream + FakeVenueAdapter, ZERO network) ---
# The TxLINE market key the normalizer derives for SuperOddsType=OU / MarketPeriod=FT /
# MarketParameters=2.5 — stream ticks + the reconstructed close both align on it.
_LIVE_KEY = "OU|FT|2.5"


def _live_market(over_bps: int) -> dict[str, Any]:
    """A single-market snapshot for the flagged ``over`` side (entry de-vigged prob = ``over_bps``)."""
    return {"stable_prob_bps": {"over": over_bps}, "stable_price": {"over": 1.6, "under": 2.4}, "suspended": False}


def _live_ms(over_bps: int, *, tick_seq: int, ts: int, phase: int = 0, fixture_id: int = 1) -> MarketState:
    return MarketState(
        fixture_id=fixture_id, tick_seq=tick_seq, ts=ts, phase=phase, markets={_LIVE_KEY: _live_market(over_bps)}, scores={}
    )


async def _live_stream(items: list[MarketState]) -> AsyncIterator[MarketState]:
    for item in items:
        yield item


def _over_flagger(agent_id: str = "flagger") -> Agent:
    """An agent that always FLAG_VALUEs ``over`` — a scored, numeric-CLV action that routes to exec."""

    async def decide(_market_state: MarketState) -> AgentAction:
        return AgentAction(type=SportsActionType.FLAG_VALUE, params={"market_key": _LIVE_KEY, "side": "over"})

    return Agent(agent_id=agent_id, proof_mode="reproducible", decide=decide)


def _close_upd(over_pct: float, under_pct: float, *, ts_ms: int) -> dict[str, Any]:
    """A TxLINE-native ``/odds/updates`` row foldable by the normalizer into the CON-040 close."""
    return {
        "FixtureId": 1,
        "Ts": ts_ms,
        "InRunning": 0,
        "SuperOddsType": "OU",
        "MarketPeriod": "FT",
        "MarketParameters": "2.5",
        "PriceNames": ["over", "under"],
        "Prices": [1600, 2400],
        "Pct": [over_pct, under_pct],
    }


def _live_window(end_rule: str = "pre_match", **kw: Any) -> RunWindow:
    base: dict[str, Any] = {"window_id": "w1", "fixture_id": 1, "market_allowlist": ["OU"], "end_rule": end_rule}
    base.update(kw)
    return RunWindow(**base)


def _exec_envelope() -> PolicyEnvelope:
    """A permissive envelope that clears the ``fake`` venue + the OU market for a clean APPROVED path."""
    return PolicyEnvelope(
        max_stake=50.0,
        max_orders_per_run=100,
        max_orders_per_session=100,
        max_orders_per_day=100,
        venue_allowlist=["fake"],
        market_allowlist=[_LIVE_KEY],
        min_edge_bps=0,
        max_slippage_bps=10_000,
        max_price=1.0e9,
        max_quote_age_s=10**9,
        cooldown_s=0,
        human_approval_threshold=1.0e12,  # stake below -> APPROVED (never REQUIRES_HUMAN)
        kill_switch=False,
    )


def _live_ticks() -> list[MarketState]:
    """Two pre-kickoff ticks (``over`` prob 5000/5200 → positive executable edge at fake 2.05), then kickoff."""
    return [
        _live_ms(5000, tick_seq=0, ts=1000, phase=0),
        _live_ms(5200, tick_seq=1, ts=1100, phase=0),
        _live_ms(5300, tick_seq=2, ts=9999, phase=1),  # kickoff -> terminates pre_match, NOT fed
    ]


async def _live_close(_fixture_id: int) -> list[dict[str, Any]]:
    """A complete pre-InRunning close where ``over`` drifts UP to ~66% (positive CLV vs the 5000/5200 entries)."""
    return [_close_upd(66, 34, ts_ms=1_200_000)]


async def test_standalone_run_produces_a_verified_proof() -> None:
    ticks = replay_marketstates(FIXTURE)
    result = await standalone_run(ticks, momentum_agent("mom"), source_mode="replay", anchor_fn=None)
    assert isinstance(result, StandaloneRunResult)
    assert result.source_mode == "replay"
    assert result.anchor_status == "not_anchored"  # anchor_fn=None → offline
    assert result.verified is True
    assert result.verify_report["evidence_match"] is True
    assert "checks" in result.proof_card
    assert len(result.scores) == 1  # exactly one agent — no competition/ranking framing
    # FULL arena parity: the manifest is passed so MANIFEST_BOUND gets a real verdict (not n/a);
    # offline replay → ANCHOR is honestly not_applicable.
    checks = result.proof_card["checks"]
    assert checks["manifest_bound"]["result"] == "pass"
    assert checks["anchor"]["result"] == "not_applicable"


async def test_standalone_run_anchors_when_anchor_fn_supplied() -> None:
    ticks = replay_marketstates(FIXTURE)

    async def fake_anchor(manifest_hash: str) -> str:
        assert len(manifest_hash) == 64
        return "FAKESIG"

    result = await standalone_run(ticks, momentum_agent("mom"), source_mode="replay", anchor_fn=fake_anchor)
    assert result.anchor_status == "anchored"
    assert result.signature == "FAKESIG"
    assert result.proof_card["anchor"]["signature"] == "FAKESIG"
    # Anchored → ANCHOR and MANIFEST_BOUND both pass (real arena-parity verdicts).
    checks = result.proof_card["checks"]
    assert checks["anchor"]["result"] == "pass"
    assert checks["manifest_bound"]["result"] == "pass"


async def test_standalone_manifest_bound_is_falsifiable() -> None:
    # Proves MANIFEST_BOUND is HONEST (not a tautological pass): it independently recomputes the
    # score-root + manifest hash from this run+scores, so a TAMPERED manifest FAILS the check. This
    # is the scoring-time binding (the arena pattern), distinct from the WD-1 verify false-pass case.
    ticks = replay_marketstates(FIXTURE)
    run = await run_competition(ticks, [momentum_agent("mom")], source_mode="replay")
    scores = score_run(run)
    manifest = manifest_from_run(
        run,
        fixture_or_window_id=fixture_or_window_id_from_events(run.run_events),
        score_root=recompute_score_root(scores),
        schema_versions=dict(SCHEMA_VERSIONS),
    )
    manifest_hash = run_manifest_hash(manifest)
    anchor = {"status": "anchored", "signature": "S", "cluster": "devnet"}

    ok = check_results_to_proof_block(
        build_check_results(
            scores=scores, run=run, manifest=manifest, manifest_hash=manifest_hash, anchor=anchor, source_mode="replay"
        )
    )
    assert ok["manifest_bound"]["result"] == "pass"
    assert ok["anchor"]["result"] == "pass"

    # Tamper the manifest's evidence root → the independent recompute diverges → MANIFEST_BOUND FAILS.
    tampered = {**manifest, "action_evidence_root": "0" * 64}
    bad = check_results_to_proof_block(
        build_check_results(
            scores=scores, run=run, manifest=tampered, manifest_hash=manifest_hash, anchor=anchor, source_mode="replay"
        )
    )
    assert bad["manifest_bound"]["result"] == "fail"


# ===========================================================================
# T20 — the LAUNCH PATH: standalone core runs a live window + the execution lane
# (dry_run/paper). The execution lane's receipts are NON-SCORING (evidence=false):
# they never enter the sealed prefix and never alter the score/proof (SEC-004/SEC-2D-401).
# ===========================================================================


async def test_standalone_live_window_runs_the_source() -> None:
    # source_mode="live" via an injected stream produces a verified windowed run (no network).
    result = await standalone_run(
        [],
        _over_flagger(),
        window=_live_window(),
        stream=_live_stream(_live_ticks()),
        fetch_updates=_live_close,
        execution_mode="paper",  # no execution lane in paper
        anchor_fn=None,
    )
    assert isinstance(result, StandaloneRunResult)
    assert result.source_mode == "live"
    assert result.verified is True
    assert result.verify_report["evidence_match"] is True
    assert result.receipts == []  # paper mode never runs the execution lane


async def test_standalone_dry_run_emits_nonscoring_receipts_leaving_proof_unchanged() -> None:
    # PAPER baseline (no lane) over the stream, then DRY_RUN (envelope + FakeVenueAdapter) over an
    # identical FRESH stream. The dry-run receipts are labeled dry_run and are NON-SCORING: the
    # sealed evidence + the ranked scores are byte-structurally identical to the paper run.
    paper = await standalone_run(
        [],
        _over_flagger(),
        window=_live_window(),
        stream=_live_stream(_live_ticks()),
        fetch_updates=_live_close,
        execution_mode="paper",
        anchor_fn=None,
    )
    dry = await standalone_run(
        [],
        _over_flagger(),
        window=_live_window(),
        stream=_live_stream(_live_ticks()),
        fetch_updates=_live_close,
        policy_envelope=_exec_envelope(),
        execution_mode="dry_run",
        adapter=FakeVenueAdapter(),
        anchor_fn=None,
    )

    # Receipts present + labeled dry_run (dry-run LABELED as dry-run).
    assert dry.execution_mode == "dry_run"
    assert paper.execution_mode == "paper"
    assert len(dry.receipts) >= 1
    assert all(receipt["mode"] == "dry_run" for receipt in dry.receipts)

    # NON-SCORING: the sealed evidence block + the ranked scores are byte-structurally unchanged
    # whether or not the execution lane ran (run_id-independent — the lane never mutates the seal).
    assert dry.scores == paper.scores
    assert dry.proof_card["evidence"] == paper.proof_card["evidence"]
    assert dry.proof_card["checks"].keys() == paper.proof_card["checks"].keys()


async def test_standalone_run_manifest_pins_the_launched_instance() -> None:
    # AGENT-INSTANCE pinning: config_hash + policy_hash + the window are pinned into the run
    # manifest, so the launched run is a PINNED instance (not loose state).
    envelope = _exec_envelope()
    result = await standalone_run(
        [],
        _over_flagger(),
        window=_live_window(),
        stream=_live_stream(_live_ticks()),
        fetch_updates=_live_close,
        policy_envelope=envelope,
        execution_mode="dry_run",
        adapter=FakeVenueAdapter(),
        config_hash="deadbeefcafe",
        anchor_fn=None,
    )
    pin = result.run_manifest
    assert pin["config_hash"] == "deadbeefcafe"
    assert pin["policy_hash"] == envelope.policy_hash()
    assert pin["execution_mode"] == "dry_run"
    assert pin["source_mode"] == "live"
    assert pin["window"]["window_id"] == "w1"
    assert pin["window"]["fixture_id"] == 1
    assert pin["window"]["end_rule"] == "pre_match"
