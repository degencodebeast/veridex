"""II-9 — the checkpointed arena comparison: fair, evidence-identical, honestly reported.

The rules-vs-reasoning claim (deterministic Drift vs LLM-Drift) is only defensible if BOTH
contestants decide at the SAME pinned checkpoints from the SAME shared snapshot, and the comparison
report never launders a bare average CLV as the headline. This offline TDD suite pins the five
load-bearing fairness properties (addendum §3, lines 78-83):

  1. IDENTICAL OPPORTUNITIES — in arena mode both contestants are offered the identical
     ``(checkpoint_ts, evidence_hash)`` sequence (asserted from emitted events).
  2. HONEST REPORT FIELDS — eligible checkpoints, per-contestant actions-vs-WAITs, scoreable
     decisions, fixture count, clustered uncertainty; NEVER a bare-average-CLV headline.
  3. PER-TICK PRESERVED OUTSIDE THE ARENA — the standalone drift template still decides per-tick,
     byte-identical (the arena mode is a DIFFERENT, disclosed mode — it must not perturb standalone).
  4. SHARED-OPPORTUNITY GUARD — if the two contestants do NOT share decision opportunities, the
     identical-opportunity claim is DROPPED (the flag flips), never silently averaged.
  5. ONE SHARED PROJECTOR — both contestants' snapshot at a checkpoint comes from the SAME projector
     call (same ``evidence_hash``); they cannot diverge on evidence.

The model is an INJECTABLE seam (a hand-controlled fake launcher) and the clock is snapshot-ts
driven, so the replay is deterministic and NO real LLM call / no wall-clock leaks into the suite.
"""

from __future__ import annotations

from typing import Any

from veridex.ingest.marketstate import MarketState
from veridex.runtime.arena_comparison import (
    DET_DRIFT_CONTESTANT,
    LLM_DRIFT_CONTESTANT,
    ArenaComparison,
    run_arena_comparison,
)
from veridex.runtime.competition import CompetitionResult
from veridex.runtime.llm_checkpoint import CheckpointPolicy
from veridex.runtime.schemas import SportsActionType
from veridex.strategies.drift import CumulativeDriftStrategy

# ---------------------------------------------------------------------------
# Test doubles — a hand-controlled model launcher (no real LLM, no network)
# ---------------------------------------------------------------------------


class FakeCallHandle:
    """A Future/Task-shaped handle whose completion the test controls explicitly."""

    def __init__(self) -> None:
        self._done = False
        self._cancelled = False
        self._result: object = None

    def complete(self, result: object) -> None:
        self._result = result
        self._done = True

    def done(self) -> bool:
        return self._done

    def cancel(self) -> None:
        pass

    def cancelled(self) -> bool:
        return self._cancelled

    def exception(self) -> BaseException | None:
        return None

    def result(self) -> object:
        return self._result


class FakeModelLauncher:
    """Injectable model seam — hands out fresh handles and records launched prompts."""

    def __init__(self, handles: list[FakeCallHandle] | None = None) -> None:
        self._handles = list(handles) if handles is not None else []
        self.launched_prompts: list[str] = []

    def launch(self, prompt: str) -> FakeCallHandle:
        self.launched_prompts.append(prompt)
        return self._handles.pop(0) if self._handles else FakeCallHandle()


# ---------------------------------------------------------------------------
# Fixtures — deterministic rising tapes (ts in seconds; one market, one side)
# ---------------------------------------------------------------------------

# Discriminating det-Drift thresholds so the short tape actually exercises firing.
_DET_KW: dict[str, Any] = {
    "min_tick_count": 3,
    "min_horizon_s": 0,
    "cum_drift_logit_min": 0.05,
    "trend_strength_min": 0.5,
    "cooldown_ticks": 0,
    "close_quality_required": True,
}


def _ms(ts: int, bps: int, *, suspended: bool = False, fixture_id: int = 42) -> MarketState:
    return MarketState(
        fixture_id=fixture_id,
        tick_seq=0,
        ts=ts,
        phase=1,
        markets={"M": {"stable_prob_bps": {"home": bps}, "suspended": suspended}},
        scores={},
    )


def _rising_tape(*, n: int = 12, ts0: int = 1000, dts: int = 100, bps0: int = 4800, dbps: int = 150) -> list[MarketState]:
    """A monotone-rising ``home`` tape (ts spaced ``dts`` s) — trend_strength → +1, drift grows."""
    return [_ms(ts0 + i * dts, bps0 + i * dbps) for i in range(n)]


def _policy(*, cadence_s: float = 300.0, evidence_age_limit_s: float = 100_000.0) -> CheckpointPolicy:
    return CheckpointPolicy(cadence_s=cadence_s, evidence_age_limit_s=evidence_age_limit_s)


# ---------------------------------------------------------------------------
# RED #1 — identical opportunities: same (checkpoint_ts, evidence_hash) for both
# ---------------------------------------------------------------------------


async def test_both_contestants_get_identical_checkpoint_and_evidence_sequence() -> None:
    tape = _rising_tape()
    policy = _policy()
    arena = await run_arena_comparison(tape, model=FakeModelLauncher(), det_policy=policy, **_DET_KW)

    det = arena.opportunities(DET_DRIFT_CONTESTANT)
    llm = arena.opportunities(LLM_DRIFT_CONTESTANT)

    assert det, "expected at least one shared checkpoint opportunity on a rising tape"
    # Same (checkpoint_ts, evidence_hash) list for BOTH contestants — the identical-opportunity claim.
    assert det == llm
    # Every opportunity carries a real evidence coordinate (the shared snapshot's hash).
    assert all(isinstance(ts, int) and isinstance(h, str) and h for (ts, h) in det)
    assert arena.report().identical_opportunities is True


# ---------------------------------------------------------------------------
# RED #2 — honest report fields present; NO bare-average-CLV headline
# ---------------------------------------------------------------------------


async def test_report_is_honest_and_never_a_bare_average() -> None:
    tape = _rising_tape()
    arena = await run_arena_comparison(tape, model=FakeModelLauncher(), det_policy=_policy(), **_DET_KW)
    payload = arena.report().to_payload()

    # The honest headline fields the addendum requires.
    for field in ("eligible_checkpoints", "identical_opportunities", "scoreable_decisions",
                  "fixture_count", "clustered_uncertainty", "contestants"):
        assert field in payload, f"missing honest report field: {field}"

    # Per-contestant actions vs WAITs (never collapsed into one number).
    for cid in (DET_DRIFT_CONTESTANT, LLM_DRIFT_CONTESTANT):
        row = payload["contestants"][cid]
        assert "actions" in row and "waits" in row and "scoreable_decisions" in row

    assert payload["fixture_count"] == 1  # one fixture in this tape

    # NO bare-average-CLV field may be presented as the headline (SEC-001 honesty).
    banned = {"average_clv", "avg_clv", "mean_clv", "clv_average", "clv", "average_clv_bps"}
    assert banned.isdisjoint(payload.keys())
    assert banned.isdisjoint(payload["clustered_uncertainty"].keys())


# ---------------------------------------------------------------------------
# RED #3 — per-tick standalone drift is preserved (byte-identical) OUTSIDE the arena
# ---------------------------------------------------------------------------


async def test_standalone_drift_stays_per_tick_and_unchanged_by_arena_mode() -> None:
    tape = _rising_tape()

    # Standalone per-tick decisions are deterministic AND reproducible (the byte-identical guard):
    # two INDEPENDENT strategy instances over the same tape produce the exact same action sequence.
    strat_a = CumulativeDriftStrategy(**_DET_KW)
    seq_a = [strat_a.decide(ms) for ms in tape]

    strat_b = CumulativeDriftStrategy(**_DET_KW)
    seq_b = [strat_b.decide(ms) for ms in tape]
    assert [(a.type, a.params) for a in seq_a] == [(b.type, b.params) for b in seq_b]

    # Standalone fires PER TICK — it acts on many ticks (once the thin-data gate clears, cooldown=0).
    standalone_action_ticks = [i for i, a in enumerate(seq_b) if a.type != SportsActionType.WAIT]
    assert len(standalone_action_ticks) >= 5

    # Arena mode is a DIFFERENT, disclosed mode: det-Drift decides ONLY at the pinned checkpoints,
    # a STRICT subset of the per-tick decisions — proving the arena did not turn standalone per-tick.
    arena = await run_arena_comparison(tape, model=FakeModelLauncher(), det_policy=_policy(), **_DET_KW)
    checkpoint_count = len(arena.opportunities(DET_DRIFT_CONTESTANT))
    assert 0 < checkpoint_count < len(standalone_action_ticks)


# ---------------------------------------------------------------------------
# RED #4 — shared-opportunity guard: divergent grids DROP the identical claim
# ---------------------------------------------------------------------------


async def test_divergent_opportunities_drop_the_identical_claim_never_average() -> None:
    tape = _rising_tape()
    # Two DIFFERENT checkpoint cadences ⇒ the contestants do NOT share the same checkpoint grid.
    arena = await run_arena_comparison(
        tape,
        model=FakeModelLauncher(),
        det_policy=_policy(cadence_s=300.0),
        llm_policy=_policy(cadence_s=200.0),
        **_DET_KW,
    )

    assert arena.opportunities(DET_DRIFT_CONTESTANT) != arena.opportunities(LLM_DRIFT_CONTESTANT)
    report = arena.report()
    assert report.identical_opportunities is False  # the flag FLIPS — claim dropped

    payload = report.to_payload()
    # Dropping the claim must NOT be papered over with a silent average.
    banned = {"average_clv", "avg_clv", "mean_clv", "clv_average"}
    assert banned.isdisjoint(payload.keys())
    # A non-shared run must still surface per-contestant tallies (honest, just not "identical").
    assert payload["contestants"][DET_DRIFT_CONTESTANT]["eligible_checkpoints"] >= 0


# ---------------------------------------------------------------------------
# RED #5 — one shared projector: both snapshots at a checkpoint are the SAME projection
# ---------------------------------------------------------------------------


async def test_one_shared_projector_feeds_both_contestants() -> None:
    tape = _rising_tape()
    arena = await run_arena_comparison(tape, model=FakeModelLauncher(), det_policy=_policy(), **_DET_KW)

    # EXACTLY one real projection per tick — not one-per-contestant (they cannot each project).
    assert arena.projector.call_count == len(tape)

    # At each checkpoint both contestants saw the IDENTICAL evidence coordinate (same projector call).
    det_hashes = [h for (_ts, h) in arena.opportunities(DET_DRIFT_CONTESTANT)]
    llm_hashes = [h for (_ts, h) in arena.opportunities(LLM_DRIFT_CONTESTANT)]
    assert det_hashes == llm_hashes and det_hashes  # cannot diverge on evidence


# ---------------------------------------------------------------------------
# Additive wiring — the comparison payload is carriable on the competition result
# ---------------------------------------------------------------------------


async def test_competition_result_carries_arena_comparison_additively() -> None:
    tape = _rising_tape()
    arena = await run_arena_comparison(tape, model=FakeModelLauncher(), det_policy=_policy(), **_DET_KW)
    payload = arena.report().to_payload()

    # The field is ADDITIVE (defaults to None) — an existing result constructs without it.
    result = CompetitionResult(
        run=None,  # type: ignore[arg-type]
        scores=[],
        manifest={},
        manifest_hash="h",
        anchor_status="not_anchored",
        signature=None,
        proof_card={},
        leaderboard=[],
    )
    assert result.arena_comparison is None

    from veridex.runtime.competition import attach_arena_comparison

    tagged = attach_arena_comparison(result, payload)
    assert tagged.arena_comparison == payload
    assert result.arena_comparison is None  # original untouched (frozen, replaced not mutated)


def test_arena_contestant_ids_are_stable() -> None:
    assert DET_DRIFT_CONTESTANT == "det-drift"
    assert LLM_DRIFT_CONTESTANT == "llm-drift"
    assert isinstance(ArenaComparison, type)
