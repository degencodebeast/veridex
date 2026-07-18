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


# ---------------------------------------------------------------------------
# Extra test doubles — already-done + async-completing model handles
# ---------------------------------------------------------------------------


class _DoneHandle:
    """A handle that is ALREADY done, returning one raw model output on ``result()``."""

    def __init__(self, raw: object) -> None:
        self._raw = raw

    def done(self) -> bool:
        return True

    def cancel(self) -> None:
        pass

    def cancelled(self) -> bool:
        return False

    def exception(self) -> BaseException | None:
        return None

    def result(self) -> object:
        return self._raw


class _DoneLauncher:
    """A model seam whose every launch returns an already-done handle carrying ``raw``."""

    def __init__(self, raw: object) -> None:
        self._raw = raw

    def launch(self, prompt: str) -> _DoneHandle:
        return _DoneHandle(self._raw)


# ---------------------------------------------------------------------------
# MAJOR 1 RED — the PRODUCTION competition path attaches the arena comparison
# ---------------------------------------------------------------------------


async def test_production_competition_path_attaches_arena_comparison() -> None:
    """MAJOR 1 — a det-Drift vs LLM-Drift competition driven through the PRODUCTION competition path
    (``run_demo_competition``) ACTUALLY produces and attaches the honest ``ArenaComparisonReport``
    (never ``None``) reaching the API/UI — not a test-harness-only leaf."""
    from veridex.api.demo_fixtures import build_demo_ticks, contrarian_agent
    from veridex.runtime.competition import ArenaSpec, run_demo_competition
    from veridex.runtime.orchestrator import deterministic_agent

    tape = build_demo_ticks()
    spec = ArenaSpec(
        model=FakeModelLauncher(),
        det_policy=_policy(cadence_s=1.0),
        det_kwargs=dict(_DET_KW),
    )
    result = await run_demo_competition(
        tape,
        [deterministic_agent("agent-alpha"), contrarian_agent("agent-beta")],
        source_mode="replay",
        anchor_fn=None,  # offline
        arena=spec,
    )

    assert result.arena_comparison is not None, (
        "the production competition path must RUN and ATTACH the arena comparison, not leave it None"
    )
    payload = result.arena_comparison
    for f in ("eligible_checkpoints", "identical_opportunities", "scoreable_decisions",
              "fixture_count", "contestants", "clustered_uncertainty"):
        assert f in payload, f"missing honest arena field: {f}"
    # NEVER a bare average CLV headline (addendum §3).
    assert {"average_clv", "avg_clv", "mean_clv", "clv"}.isdisjoint(payload.keys())
    # The honest run-level context is mirrored onto the ranked leaderboard rows reaching the UI.
    assert result.leaderboard, "the competition produced a ranked leaderboard"
    for row in result.leaderboard:
        assert "identical_opportunities" in row and "arena_scoreable_decisions" in row


# ---------------------------------------------------------------------------
# MAJOR 2 RED — scoreable_decisions is AUTHORITATIVE (law-valid), not a bare non-WAIT count
# ---------------------------------------------------------------------------


async def test_scoreable_decisions_excludes_law_invalid_non_wait_actions() -> None:
    """MAJOR 2 — a schema-valid but LAW-INVALID non-WAIT action (no market/side) is NOT scoreable;
    a law-valid non-WAIT action IS. ``scoreable_decisions`` must be authoritative, never a bare
    ``action_type != WAIT`` count."""
    from veridex.runtime.schemas import AgentAction, SportsActionType

    tape = _rising_tape()
    # The LLM emits FOLLOW_MOMENTUM with EMPTY params → market_key=None, side=None (law-invalid).
    empty = AgentAction(type=SportsActionType.FOLLOW_MOMENTUM, params={})
    arena = await run_arena_comparison(tape, model=_DoneLauncher(empty), det_policy=_policy(), **_DET_KW)
    report = arena.report()

    llm = report.contestants[LLM_DRIFT_CONTESTANT]
    assert llm["actions"] >= 1, "the LLM emitted at least one non-WAIT action"
    assert llm["scoreable_decisions"] == 0, (
        "a non-WAIT action with no valid market/side is NOT a scoreable decision"
    )

    # det-Drift emits law-valid actions (market_key + side present) → scoreable == its non-WAIT count.
    det = report.contestants[DET_DRIFT_CONTESTANT]
    assert det["actions"] >= 1
    assert det["scoreable_decisions"] == det["actions"]

    # The run-level total is the authoritative sum (the invalid LLM action is excluded).
    assert report.scoreable_decisions == det["scoreable_decisions"] + llm["scoreable_decisions"]
    assert report.scoreable_decisions == det["scoreable_decisions"]


# ---------------------------------------------------------------------------
# MAJOR 3 RED — replay yields to a real async model task + drains the guard at end-of-tape
# ---------------------------------------------------------------------------


async def test_replay_settles_async_model_call_and_drains_guard() -> None:
    """MAJOR 3 — with a launcher whose task needs one ``await asyncio.sleep(0)`` to complete, the
    LLM contestant produces its decision (not all-WAIT) and at end-of-tape the guard is DRAINED
    (no physical call left unobserved / in flight)."""
    import asyncio

    from veridex.runtime.schemas import AgentAction, SportsActionType

    class _AsyncHandle:
        """A handle backed by an asyncio.Task that resolves after ONE event-loop yield."""

        def __init__(self, raw: object) -> None:
            async def _run() -> object:
                await asyncio.sleep(0)
                return raw

            self._task = asyncio.ensure_future(_run())

        def done(self) -> bool:
            return self._task.done()

        def cancel(self) -> None:
            self._task.cancel()

        def cancelled(self) -> bool:
            return self._task.cancelled()

        def exception(self) -> BaseException | None:
            if self._task.done() and not self._task.cancelled():
                return self._task.exception()
            return None

        def result(self) -> object:
            return self._task.result()

    class _AsyncLauncher:
        def __init__(self, raw: object) -> None:
            self._raw = raw

        def launch(self, prompt: str) -> _AsyncHandle:
            return _AsyncHandle(self._raw)

    tape = _rising_tape()
    action = AgentAction(type=SportsActionType.FOLLOW_MOMENTUM, params={"market_key": "M", "side": "home"})
    arena = await run_arena_comparison(tape, model=_AsyncLauncher(action), det_policy=_policy(), **_DET_KW)

    llm = arena.report().contestants[LLM_DRIFT_CONTESTANT]
    assert llm["actions"] >= 1, "the async model task must complete and produce a decision (not all-WAIT)"
    assert arena.guard_busy is False, "end-of-tape drain must leave no call unobserved / in flight"


# ---------------------------------------------------------------------------
# MINOR 1 RED — arena projector tie-break MIRRORS standalone (smallest key)
# ---------------------------------------------------------------------------


def test_arena_tiebreak_mirrors_standalone_smallest_key() -> None:
    """MINOR 1 — at EQUAL drift the arena projector must target the SAME (smallest) key standalone
    det-Drift / ``DefaultDriftProjector`` keep, not the lexicographically largest one."""
    from veridex.runtime.arena_comparison import ArenaSharedProjector

    def _tied_ms(ts: int, bps: int) -> MarketState:
        # Two markets with IDENTICAL rising bps → identical drift → a pure key tie.
        return MarketState(
            fixture_id=7,
            tick_seq=0,
            ts=ts,
            phase=1,
            markets={
                "A": {"stable_prob_bps": {"home": bps}, "suspended": False},
                "B": {"stable_prob_bps": {"home": bps}, "suspended": False},
            },
            scores={},
        )

    tape = [_tied_ms(1000 + i * 100, 4800 + i * 150) for i in range(6)]
    projector = ArenaSharedProjector()
    pick = None
    for ms in tape:
        pick = projector.project(ms)
    assert pick is not None
    # Standalone det-Drift keeps the SMALLEST key ("A","home"); arena must MIRROR it (never "B").
    assert (pick.market_key, pick.side) == ("A", "home")
