"""II-8 — the LLM-Drift checkpoint state machine + contestant identity (addendum §3).

Offline TDD suite for the §3 "LLM-call contract" that binds EVERY LLM contestant:

  * one in-flight call max (a checkpoint during flight → ``WAIT / skipped_inflight``, never
    supersedes the active call),
  * staleness defined ONLY by the pinned evidence-age limit (never by a newer tick / later
    checkpoint), with the EXPIRY-CONFIRMATION rule — on expiry NOTHING launches until the prior
    physical call's termination/cancellation is CONFIRMED,
  * one model response → at most one scored action; a carried decision is DISPLAYED but never
    re-emitted / re-scored,
  * invalid / malformed / timeout / provider-failure → ``WAIT`` (fail-closed), the loop stays alive,
  * a NEW sealed identity distinct from the generic ``llm_agent`` (the sealed goldens are untouched).

The model is an INJECTABLE seam: a fake launcher hands out hand-controlled ``FakeCallHandle``s and an
injected ``FakeClock`` drives evidence-age — so NO real LLM call and no wall-clock leak into the suite.
"""

from __future__ import annotations

import pytest

from veridex.ingest.marketstate import MarketState
from veridex.runtime.agent import build_decision_prompt
from veridex.runtime.llm_checkpoint import (
    AWAITING_CONFIRMATION,
    COMPLETED_FRESH,
    COMPLETED_STALE,
    CONFIRMED_TERMINATED,
    IN_FLIGHT,
    CheckpointPolicy,
    InflightGuard,
)
from veridex.runtime.orchestrator import PROOF_MODE_LLM, llm_agent
from veridex.runtime.schemas import AgentAction, SportsActionType
from veridex.strategies.drift_features import DriftFeatureSnapshot
from veridex.strategies.llm_drift import (
    LLMDriftCheckpointRunner,
    build_drift_decision_prompt,
    default_model_launcher,
    llm_drift_agent,
)

# ---------------------------------------------------------------------------
# Test doubles — injected clock, hand-controlled call handle, fake launcher
# ---------------------------------------------------------------------------


class FakeClock:
    """A monotone clock the test advances explicitly (drives cadence + evidence-age)."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += float(dt)


class FakeCallHandle:
    """Hand-controlled Future/Task-like handle (quacks like ``asyncio.Task``).

    ``cancel()`` only RECORDS the request; the physical termination is confirmed later by the test
    via ``confirm_cancelled()`` — this reproduces the unconfirmed window the expiry rule guards.
    """

    def __init__(self) -> None:
        self._done = False
        self._cancelled = False
        self._result: object = None
        self._exc: BaseException | None = None
        self.cancel_calls = 0

    # --- test controls -------------------------------------------------
    def complete(self, result: object) -> None:
        self._result = result
        self._done = True

    def fail(self, exc: BaseException) -> None:
        self._exc = exc
        self._done = True

    def confirm_cancelled(self) -> None:
        self._cancelled = True
        self._done = True

    # --- Future-like interface consumed by the guard -------------------
    def done(self) -> bool:
        return self._done

    def cancel(self) -> None:
        self.cancel_calls += 1

    def cancelled(self) -> bool:
        return self._cancelled

    def exception(self) -> BaseException | None:
        return self._exc if self._done else None

    def result(self) -> object:
        if self._exc is not None:
            raise self._exc
        return self._result


class FakeModelLauncher:
    """Injectable model seam — hands out pre-seeded handles and records launched prompts."""

    def __init__(self, handles: list[FakeCallHandle]) -> None:
        self._handles = list(handles)
        self.launched_prompts: list[str] = []

    def launch(self, prompt: str) -> FakeCallHandle:
        self.launched_prompts.append(prompt)
        return self._handles.pop(0)


def _snap(*, evidence_hash: str = "ev0", cum: float = 0.5) -> DriftFeatureSnapshot:
    return DriftFeatureSnapshot(
        first=0.0,
        current=cum,
        cum_logit_drift=cum,
        ewma_slope=1.0,
        trend_strength=1.0,
        tick_count=25,
        horizon_s=1200,
        market_quality=True,
        evidence_hash=evidence_hash,
    )


def _ms(ts: int = 1000) -> MarketState:
    return MarketState(fixture_id=5, tick_seq=0, ts=ts, phase=2, markets={}, scores={})


def _runner(
    *,
    clock: FakeClock,
    model: FakeModelLauncher,
    cadence_s: float = 45.0,
    evidence_age_limit_s: float = 30.0,
    feature_delta_threshold: float | None = None,
    projector=None,
) -> LLMDriftCheckpointRunner:
    policy = CheckpointPolicy(
        cadence_s=cadence_s,
        evidence_age_limit_s=evidence_age_limit_s,
        feature_delta_threshold=feature_delta_threshold,
    )
    return LLMDriftCheckpointRunner(
        model=model,
        policy=policy,
        projector=projector if projector is not None else (lambda _ms: _snap()),
        clock=clock,
        model_id="test-model",
    )


# ---------------------------------------------------------------------------
# RED #1 — one in-flight call max; a checkpoint during flight → skipped_inflight
# ---------------------------------------------------------------------------


def test_guard_holds_exactly_one_call_and_never_supersedes() -> None:
    clock = FakeClock(0)
    guard = InflightGuard(evidence_age_limit_s=10_000, clock=clock)

    h = FakeCallHandle()
    guard.launch(h, evidence_hash="ev0")
    assert guard.busy
    assert guard.evidence_coordinate() == "ev0"

    # A burst of checkpoints while the call is in flight: it stays the SAME call, never superseded.
    for _ in range(5):
        clock.advance(5)
        out = guard.service()
        assert out.status == IN_FLIGHT
        assert guard.evidence_coordinate() == "ev0"

    # A second physical call cannot start while one is in flight (starvation-proof, one-in-flight).
    with pytest.raises(RuntimeError):
        guard.launch(FakeCallHandle(), evidence_hash="ev1")


async def test_runner_launches_once_and_records_skipped_inflight_for_the_burst() -> None:
    clock = FakeClock(0)
    model = FakeModelLauncher([FakeCallHandle()])  # ONE handle: a 2nd launch would IndexError
    runner = _runner(clock=clock, model=model, cadence_s=5.0, evidence_age_limit_s=10_000)

    actions = []
    for _ in range(4):
        actions.append(await runner.step(_ms()))
        clock.advance(5)  # every subsequent tick is a fresh checkpoint

    assert len(model.launched_prompts) == 1  # EXACTLY one physical call launched
    assert len(runner.skipped_inflight) == 3  # the 3 in-flight checkpoints were skipped, not launched
    assert all(a.type == SportsActionType.WAIT for a in actions)


# ---------------------------------------------------------------------------
# RED #2 — evidence-age staleness ONLY + the expiry-confirmation rule
# ---------------------------------------------------------------------------


def test_completion_within_evidence_age_is_accepted() -> None:
    clock = FakeClock(0)
    guard = InflightGuard(evidence_age_limit_s=30, clock=clock)
    h = FakeCallHandle()
    guard.launch(h, evidence_hash="evA")
    h.complete(AgentAction(type=SportsActionType.FLAG_VALUE))

    clock.advance(10)  # age 10 <= 30 → still fresh
    out = guard.service()
    assert out.status == COMPLETED_FRESH
    assert isinstance(out.raw, AgentAction)
    assert not guard.busy  # the call terminated → the slot is free again


def test_completion_beyond_evidence_age_is_dropped() -> None:
    clock = FakeClock(0)
    guard = InflightGuard(evidence_age_limit_s=30, clock=clock)
    h = FakeCallHandle()
    guard.launch(h, evidence_hash="evB")
    h.complete(AgentAction(type=SportsActionType.FLAG_VALUE))

    clock.advance(31)  # age 31 > 30 → stale on completion → dropped
    out = guard.service()
    assert out.status == COMPLETED_STALE
    assert not guard.busy


def test_expiry_in_flight_blocks_relaunch_until_termination_confirmed() -> None:
    clock = FakeClock(0)
    guard = InflightGuard(evidence_age_limit_s=30, clock=clock)
    h = FakeCallHandle()
    guard.launch(h, evidence_hash="evX")  # captured at t=0

    clock.advance(31)  # evidence-age limit passes while the call is STILL in flight → EXPIRY
    out = guard.service()
    assert out.status == AWAITING_CONFIRMATION
    assert h.cancel_calls == 1  # cancellation was requested...

    # THE UNCONFIRMED WINDOW: no new physical call may start until termination is confirmed.
    assert guard.busy
    with pytest.raises(RuntimeError):
        guard.launch(FakeCallHandle(), evidence_hash="evY")

    # Servicing again does not double-cancel and does not relaunch — still awaiting confirmation.
    clock.advance(5)
    out = guard.service()
    assert out.status == AWAITING_CONFIRMATION
    assert h.cancel_calls == 1
    assert guard.busy

    # The physical call confirms its cancellation → ONLY NOW may the next call launch.
    h.confirm_cancelled()
    out = guard.service()
    assert out.status == CONFIRMED_TERMINATED
    assert not guard.busy
    guard.launch(FakeCallHandle(), evidence_hash="evZ")  # no raise — relaunch is finally permitted


# ---------------------------------------------------------------------------
# RED #3 — one response → at most one scored action; carried never re-emitted
# ---------------------------------------------------------------------------


async def test_one_response_yields_exactly_one_scored_action() -> None:
    clock = FakeClock(0)
    decided = AgentAction(type=SportsActionType.FOLLOW_MOMENTUM, params={"market_key": "OU_2_5", "side": "over"})
    first = FakeCallHandle()
    # Extra handles for later checkpoints that NEVER complete (so they can never be scored).
    model = FakeModelLauncher([first, FakeCallHandle(), FakeCallHandle(), FakeCallHandle()])
    runner = _runner(clock=clock, model=model, cadence_s=45.0, evidence_age_limit_s=10_000)

    collected = [await runner.step(_ms())]  # t=0 → launch (returns WAIT)
    first.complete(decided)
    clock.advance(5)
    collected.append(await runner.step(_ms()))  # completion tick → the ONE scored action

    for _ in range(10):  # further checkpoints — none of their calls complete
        clock.advance(45)
        collected.append(await runner.step(_ms()))

    scored = [a for a in collected if a.type != SportsActionType.WAIT]
    assert len(scored) == 1  # exactly one scored action from the single accepted response
    assert scored[0].type == SportsActionType.FOLLOW_MOMENTUM
    assert runner.last_decision is not None  # the carried decision is retained for DISPLAY...
    assert runner.last_decision.type == SportsActionType.FOLLOW_MOMENTUM  # ...but was emitted only once


# ---------------------------------------------------------------------------
# RED #4 — fail-closed: exception / malformed → WAIT, the loop stays alive
# ---------------------------------------------------------------------------


async def test_provider_exception_degrades_to_wait_and_loop_survives() -> None:
    clock = FakeClock(0)
    failing = FakeCallHandle()
    model = FakeModelLauncher([failing, FakeCallHandle()])
    runner = _runner(clock=clock, model=model, cadence_s=45.0, evidence_age_limit_s=10_000)

    await runner.step(_ms())  # t=0 → launch failing call
    failing.fail(RuntimeError("provider boom"))
    clock.advance(1)
    a = await runner.step(_ms())
    assert a.type == SportsActionType.WAIT  # fail-closed, NOT raised

    clock.advance(45)  # the loop is still alive: the next eligible checkpoint can act
    b = await runner.step(_ms())
    assert b.type == SportsActionType.WAIT
    assert len(model.launched_prompts) == 2  # a fresh call WAS relaunched after the failure


async def test_malformed_output_degrades_to_wait() -> None:
    clock = FakeClock(0)
    h = FakeCallHandle()
    model = FakeModelLauncher([h])
    runner = _runner(clock=clock, model=model, cadence_s=45.0, evidence_age_limit_s=10_000)

    await runner.step(_ms())  # launch
    h.complete({"type": "EXECUTE_TRADE", "params": {"size": 9999}})  # outside the constrained enum
    clock.advance(1)
    a = await runner.step(_ms())
    assert a.type == SportsActionType.WAIT  # revalidation rejected the over-powered action → WAIT


# ---------------------------------------------------------------------------
# RED #5 — NEW sealed identity distinct from the generic llm_agent
# ---------------------------------------------------------------------------


def test_llm_drift_identity_differs_from_generic_llm_agent() -> None:
    ms = _ms()
    generic = llm_agent("generic", model=object(), model_id="test-model")
    drift = llm_drift_agent(
        "llm-drift",
        model=FakeModelLauncher([]),
        checkpoint_policy=CheckpointPolicy(),
        projector=lambda _m: _snap(),
        model_id="test-model",
    )

    assert drift.proof_mode == PROOF_MODE_LLM
    assert drift.config_hash is not None and generic.config_hash is not None
    # Same model id, SAME snapshot input, but a NEW prompt/policy identity → a different sealed hash.
    assert drift.config_hash(ms) != generic.config_hash(ms)
    assert drift.config_hash(ms) == drift.config_hash(ms)  # stable


def test_llm_drift_config_hash_is_policy_sensitive() -> None:
    ms = _ms()
    a = llm_drift_agent(
        "d", model=FakeModelLauncher([]), checkpoint_policy=CheckpointPolicy(cadence_s=45.0),
        projector=lambda _m: _snap(), model_id="test-model",
    )
    b = llm_drift_agent(
        "d", model=FakeModelLauncher([]), checkpoint_policy=CheckpointPolicy(cadence_s=60.0),
        projector=lambda _m: _snap(), model_id="test-model",
    )
    assert a.config_hash(ms) != b.config_hash(ms)  # the pinned cadence is folded into the identity


def test_drift_prompt_is_distinct_from_generic_and_embeds_evidence() -> None:
    drift_prompt = build_drift_decision_prompt(_snap(evidence_hash="EVIDENCECOORD"))
    generic_prompt = build_decision_prompt(_ms())

    assert "DriftFeatureSnapshot" in drift_prompt
    assert "DriftFeatureSnapshot" not in generic_prompt  # the generic agent never sees the projection
    assert "EVIDENCECOORD" in drift_prompt  # the pre-call evidence coordinate is embedded
    assert "untrusted" in drift_prompt.lower()  # rationale/confidence declared untrusted (gate 1)
    for action in SportsActionType:  # the constrained action vocabulary is advertised
        assert action.value in drift_prompt


async def test_default_model_launcher_constructs_agent_with_empty_tools() -> None:
    """`tools=[]` is a HARD invariant — proven on the DEFAULT production launcher's factory seam."""

    class _FakeResponse:
        def __init__(self, content):
            self.content = content

    class _FakeAsyncAgent:
        async def arun(self, prompt, **kwargs):
            return _FakeResponse(AgentAction(type=SportsActionType.WAIT))

    recorder: dict = {}

    def spy_factory(**kwargs):
        recorder.update(kwargs)
        return _FakeAsyncAgent()

    launcher = default_model_launcher(model=object(), agent_factory=spy_factory)
    handle = launcher.launch("prompt")
    raw = await handle  # the handle is an awaitable Task
    assert recorder["tools"] == []  # HARD invariant
    assert recorder["output_schema"] is AgentAction
    assert isinstance(raw, AgentAction)
