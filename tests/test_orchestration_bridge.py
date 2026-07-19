"""II-1 — the additive async orchestration bridge (MONEY BOUNDARY, offline TDD).

OFFLINE ONLY. Every test drives a RECORDING-FAKE async proposer (a stand-in for R4-A's
:func:`~veridex.dust_execution.facade.propose_mm_execution`) — ZERO wire primitives: no network,
no wallet/signer/venue, no submit/cancel. The UNCHANGED synchronous money-boundary core
``execute_plan`` (``veridex/mm_strategy/execution_adapter.py``) runs in a worker thread; each leg's
async facade call is routed onto the ONE captured host loop via
``asyncio.run_coroutine_threadsafe``. These tests prove the honest cancellation/timeout contract:
a possibly-live order is NEVER claimed un-sent, no leg is submitted after a kill, and an escape on
the cancel path is persisted (never silently swallowed) before ``CancelledError`` propagates.

The reviewed (observation, decision, config, manifest, envelope, strategy_config) inputs are reused
verbatim from the E5-T4 adapter suite so ``execute_plan`` builds real, pin-cross-checked
``MMExecutionToolRequest`` legs — the bridge is exercised on the SAME path production drives.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

# Reuse the E5-T4 reviewed fixtures verbatim (offline, pure constructors — no wire).
from tests.test_mm_strategy_adapter import (
    _fresh_write,
    _pinned_config,
    _pinned_envelope,
    _pinned_manifest,
    _pinned_strategy_config,
    _result,
    _reviewed_pair,
)
from veridex.mm_strategy.orchestration import (
    BridgedPlanResult,
    BridgeInstrument,
    FacadeDeps,
    FreezeRecord,
    StopSignal,
    execute_plan_bridged,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from veridex.dust_execution.facade import MMExecutionToolRequest, MMExecutionToolResult

# --- offline harness ---------------------------------------------------------------------------


@dataclass
class _RecordingFreezeSink:
    """The SESSION-owned FreezeRecord sink (a recording fake). Control #5/#6: the bridge writes an
    escape here BEFORE propagating ``CancelledError`` — the record is executable evidence the
    possibly-live outcome was persisted, never swallowed."""

    records: list[FreezeRecord] = field(default_factory=list)

    def emit(self, record: FreezeRecord) -> None:
        self.records.append(record)


async def _noop_sleep(_seconds: float) -> None:
    """The injected async delay seam — never a real wall-clock wait."""
    return None


def _deps(
    proposer: Callable[..., Awaitable[MMExecutionToolResult]],
    freeze_sink: _RecordingFreezeSink,
) -> FacadeDeps:
    """Build session-owned ``FacadeDeps`` with the injected recording proposer and DUMMY wire deps
    (never touched by the fake — ZERO wire primitives). ``manifest``/``envelope`` are the SAME reviewed
    session authorities ``execute_plan`` cross-checks against (control #6: session-owned, never from
    the decision path)."""
    return FacadeDeps(
        adapter=object(),  # type: ignore[arg-type]  # never called by the recording fake
        signer=object(),  # type: ignore[arg-type]
        sources=object(),  # type: ignore[arg-type]
        now_fn=lambda: 1_000,
        sleep_fn=_noop_sleep,
        envelope=_pinned_envelope(),
        manifest=_pinned_manifest(),
        wallet_equity_at_decision=1_000.0,
        fixed_fraction=0.001,
        freeze_sink=freeze_sink,
        proposer=proposer,
    )


async def _run(
    proposer: Callable[..., Awaitable[MMExecutionToolResult]],
    *legs: Any,
    freeze_sink: _RecordingFreezeSink | None = None,
    instrument: BridgeInstrument | None = None,
    timeout_s: float = 5.0,
    stop: StopSignal | None = None,
) -> tuple[asyncio.Task[BridgedPlanResult], StopSignal, _RecordingFreezeSink, BridgeInstrument]:
    """Start ``execute_plan_bridged`` as a task over ``legs`` and hand back the live handles so a
    test can cancel/observe it mid-flight."""
    sink = freeze_sink if freeze_sink is not None else _RecordingFreezeSink()
    instr = instrument if instrument is not None else BridgeInstrument()
    the_stop = stop if stop is not None else StopSignal()
    observation, decision = _reviewed_pair(*legs)
    task = asyncio.create_task(
        execute_plan_bridged(
            decision,
            observation=observation,
            config=_pinned_config(),
            manifest=_pinned_manifest(),
            envelope=_pinned_envelope(),
            strategy_config=_pinned_strategy_config(),
            facade_deps=_deps(proposer, sink),
            timeout_s=timeout_s,
            stop=the_stop,
            instrument=instr,
        )
    )
    return task, the_stop, sink, instr


# --- RED #1: cooperative kill mid-plan ---------------------------------------------------------


async def test_cooperative_kill_between_legs_aborts_later_leg() -> None:
    """RED #1 (control #4/#8): ``stop`` set after leg-1 → leg-1's outcome recorded, leg-2 NEVER
    submitted, ``escaped`` marks aborted-by-kill (NOT possibly-live: the aborted leg never reached
    the wire because the stop-check precedes EVERY submission)."""
    calls: list[MMExecutionToolRequest] = []
    stop = StopSignal()

    async def proposer(request: MMExecutionToolRequest, **_: Any) -> MMExecutionToolResult:
        calls.append(request)
        stop.set()  # kill AFTER leg-1 (runs on the host loop — asyncio.Event.set is loop-safe)
        return _result()  # clean, non-freezing → the core advances to leg-2

    task, _stop, _sink, instr = await _run(
        proposer, _fresh_write("A"), _fresh_write("B"), stop=stop
    )
    result = await task

    assert result.plan is None
    assert result.escaped is not None
    assert result.escaped.reason == "aborted_by_kill"
    assert result.escaped.possibly_live is False  # honest: a kill-aborted leg is proven un-sent
    assert len(calls) == 1  # leg-2 NEVER submitted
    assert instr.aborted_by_kill == 1


# --- RED #2: outer-task cancellation during in-flight leg-1 (incl. two-cancellation) ------------


async def test_outer_cancel_during_leg1_propagates_and_drains_worker() -> None:
    """RED #2 (control #4/#8): outer cancellation during an in-flight leg-1 must PROPAGATE (never
    become a returned result); ``stop`` is set; the worker thread is PROVEN TERMINAL before
    propagation. INCLUDING the two-cancellation control: a SECOND cancel during cleanup must NOT let
    the wrapper return/propagate while the worker thread is still live."""
    leg1_started = asyncio.Event()
    release_leg1 = asyncio.Event()
    calls: list[MMExecutionToolRequest] = []

    async def proposer(request: MMExecutionToolRequest, **_: Any) -> MMExecutionToolResult:
        calls.append(request)
        leg1_started.set()
        await release_leg1.wait()  # hold leg-1 in-flight
        return _result()  # clean → the core would advance to leg-2 (which the kill aborts)

    instr = BridgeInstrument()
    stop = StopSignal()
    task, _stop, _sink, _instr = await _run(
        proposer, _fresh_write("A"), _fresh_write("B"), instrument=instr, stop=stop, timeout_s=30.0
    )
    await asyncio.wait_for(leg1_started.wait(), timeout=5.0)

    # First cancel — lands while leg-1 is in-flight.
    task.cancel()
    await asyncio.sleep(0.02)
    assert not task.done()  # must NOT return/propagate — worker thread still live
    assert stop.is_set() is True  # cancellation drains via the shared stop signal
    assert instr.worker_thread_finished is False

    # Second cancel — lands DURING cleanup (the two-cancellation control).
    task.cancel()
    await asyncio.sleep(0.02)
    assert not task.done()  # STILL not done — a live thread is never abandoned
    assert instr.worker_thread_finished is False

    # Release leg-1 → worker resumes, leg-2 is aborted-by-kill, thread terminates.
    release_leg1.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert instr.worker_thread_finished is True  # proven terminal before propagation
    assert len(calls) == 1  # leg-2 NEVER submitted


# --- RED #3: happy path ------------------------------------------------------------------------


async def test_happy_path_two_legs_awaited_sequentially() -> None:
    """RED #3: a 2-leg plan → the facade coroutine is awaited twice SEQUENTIALLY on the host loop →
    a ``PlanExecutionResult`` is returned (no escape)."""
    calls: list[MMExecutionToolRequest] = []
    concurrency = {"current": 0, "max": 0}

    async def proposer(request: MMExecutionToolRequest, **_: Any) -> MMExecutionToolResult:
        concurrency["current"] += 1
        concurrency["max"] = max(concurrency["max"], concurrency["current"])
        await asyncio.sleep(0)  # yield — would expose any accidental concurrency
        calls.append(request)
        concurrency["current"] -= 1
        return _result()  # clean, non-freezing → both legs attempted

    task, _stop, _sink, _instr = await _run(proposer, _fresh_write("A"), _fresh_write("B"))
    result = await task

    assert result.escaped is None
    assert result.plan is not None
    assert len(result.plan.outcomes) == 2
    assert all(outcome.attempted for outcome in result.plan.outcomes)
    assert len(calls) == 2  # awaited exactly twice
    assert concurrency["max"] == 1  # strictly SEQUENTIAL — never concurrent


# --- RED #4: facade exception on leg-1 ---------------------------------------------------------


async def test_facade_exception_freezes_no_further_call() -> None:
    """RED #4: a facade exception on leg-1 → ``escaped`` FreezeRecord (possibly-live: fail closed),
    NO further facade call, decision identity recorded."""
    calls: list[MMExecutionToolRequest] = []

    async def proposer(request: MMExecutionToolRequest, **_: Any) -> MMExecutionToolResult:
        calls.append(request)
        raise RuntimeError("venue boom")

    observation, decision = _reviewed_pair(_fresh_write("A"), _fresh_write("B"))
    sink = _RecordingFreezeSink()
    result = await execute_plan_bridged(
        decision,
        observation=observation,
        config=_pinned_config(),
        manifest=_pinned_manifest(),
        envelope=_pinned_envelope(),
        strategy_config=_pinned_strategy_config(),
        facade_deps=_deps(proposer, sink),
        timeout_s=5.0,
        stop=StopSignal(),
    )

    assert result.plan is None
    assert result.escaped is not None
    assert result.escaped.reason == "facade_escape"
    assert result.escaped.possibly_live is True  # fail closed: an escape may be live
    assert result.escaped.decision is decision  # decision identity recorded
    assert len(calls) == 1  # NO further facade call after the escape


# --- RED #5: timeout ---------------------------------------------------------------------------


async def test_timeout_yields_uncertain_and_cancels_future() -> None:
    """RED #5 (control #2/#3): a leg that outruns ``timeout_s`` → ``fut.cancel()`` is called AND an
    ``UncertainOutcome`` freeze marks the leg possibly-live (NEVER treated as proof no order reached
    the wire; never blindly retried)."""

    async def proposer(_request: MMExecutionToolRequest, **_: Any) -> MMExecutionToolResult:
        await asyncio.sleep(3_600)  # outruns the bounded timeout
        return _result()

    task, _stop, _sink, instr = await _run(proposer, _fresh_write("A"), timeout_s=0.2)
    result = await task

    assert result.plan is None
    assert result.escaped is not None
    assert result.escaped.reason == "uncertain_outcome"
    assert result.escaped.possibly_live is True
    assert instr.timeouts == 1
    assert instr.futures_cancelled == 1  # fut.cancel() was called on the timed-out future


# --- RED #6: timeout-then-outer-cancel ---------------------------------------------------------


async def test_timeout_during_cancel_cleanup_emits_freeze_before_propagating() -> None:
    """RED #6 (control #5 / Fable M4): a leg times out (UncertainOutcome) DURING the cancel-cleanup
    path → a FreezeRecord IS emitted to the session sink BEFORE ``CancelledError`` propagates (the
    possibly-live order is never silently swallowed)."""
    leg1_started = asyncio.Event()

    async def proposer(_request: MMExecutionToolRequest, **_: Any) -> MMExecutionToolResult:
        leg1_started.set()
        await asyncio.sleep(3_600)  # never returns within the test → times out
        return _result()

    sink = _RecordingFreezeSink()
    instr = BridgeInstrument()
    task, _stop, _sink, _instr = await _run(
        proposer, _fresh_write("A"), _fresh_write("B"), freeze_sink=sink, instrument=instr, timeout_s=0.2
    )
    await asyncio.wait_for(leg1_started.wait(), timeout=5.0)

    # Cancel while leg-1 is in-flight; the timeout then fires DURING the cancel-drain.
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(sink.records) == 1  # the possibly-live escape was persisted
    record = sink.records[0]
    assert record.reason == "uncertain_outcome"
    assert record.possibly_live is True
    assert instr.worker_thread_finished is True  # emitted only once the worker thread is terminal


# --- RED #7: worker-only-blocking + bounded-timeout validation ---------------------------------


async def test_bridge_runs_only_on_worker_thread() -> None:
    """RED #7 (control #1): the synchronous bridge callback NEVER executes on the host-loop thread
    (calling ``future.result()`` there would deadlock)."""
    host_thread_id = threading.get_ident()

    async def proposer(_request: MMExecutionToolRequest, **_: Any) -> MMExecutionToolResult:
        return _result()

    task, _stop, _sink, instr = await _run(proposer, _fresh_write("A"))
    result = await task

    assert result.plan is not None
    assert instr.host_thread_id == host_thread_id
    assert instr.bridge_thread_ids  # the bridge actually ran
    assert host_thread_id not in instr.bridge_thread_ids  # NEVER on the host-loop thread


@pytest.mark.parametrize("bad_timeout", [0.0, -1.0, float("inf"), float("-inf"), float("nan")])
async def test_nonpositive_or_nonfinite_timeout_rejected(bad_timeout: float) -> None:
    """RED #7 (control #2): a non-finite / non-positive ``timeout_s`` is rejected fail-closed, BEFORE
    any worker/bridge runs — every ``future.result()`` wait must be bounded."""
    calls: list[MMExecutionToolRequest] = []

    async def proposer(request: MMExecutionToolRequest, **_: Any) -> MMExecutionToolResult:
        calls.append(request)
        return _result()

    observation, decision = _reviewed_pair(_fresh_write("A"))
    with pytest.raises(ValueError):
        await execute_plan_bridged(
            decision,
            observation=observation,
            config=_pinned_config(),
            manifest=_pinned_manifest(),
            envelope=_pinned_envelope(),
            strategy_config=_pinned_strategy_config(),
            facade_deps=_deps(proposer, _RecordingFreezeSink()),
            timeout_s=bad_timeout,
            stop=StopSignal(),
        )
    assert calls == []  # fail closed: rejected before any submission
