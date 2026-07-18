"""II-1 — the ADDITIVE async orchestration bridge (the money-boundary async host adapter).

This module lets an async host (AgentOS/API, II-4) drive the UNCHANGED synchronous money-boundary
core :func:`veridex.mm_strategy.execution_adapter.execute_plan` WITHOUT blocking the event loop and
WITHOUT touching a single byte of the Gate-4-reviewed adapter (plan decision A6, chosen unanimously).

The discipline is "one host loop, one worker per plan":

1. :func:`execute_plan_bridged` captures the currently-running host event loop
   (``asyncio.get_running_loop()`` at call time — control #7, never a cached/global loop).
2. The synchronous ``execute_plan`` runs in a worker via the loop's default ``ThreadPoolExecutor``
   (``asyncio.to_thread`` semantics; an executor SEAM lets a bounded pool be injected later WITHOUT
   changing ``execute_plan``).
3. ``execute_plan``'s injected facade seam is a synchronous ``bridge`` closure that runs IN the worker
   thread. For each leg it submits the async ``propose_mm_execution`` coroutine onto the captured host
   loop with ``asyncio.run_coroutine_threadsafe`` and blocks on ``future.result(timeout_s)`` — so ONLY
   the worker thread blocks; the host loop stays free to execute that R4-A coroutine and other work.

**The honest cancellation/timeout contract (controls #3/#4/#5/#8 — this is a MONEY boundary):**
Cancelling the outer task or the concurrent future does NOT stop the Python worker code and does NOT
retract an in-flight venue action. So the bridge NEVER claims "cancelled ⇒ nothing happened". The
honest contract is: a shared stop-signal (checked before EVERY submission) + a bounded wait + an
uncertain-freeze. A timeout or a facade escape is treated as POSSIBLY-LIVE (an order may be on the
wire) and frozen — never blindly retried. On outer cancellation the worker is DRAINED to terminal
(repeated cancellations absorbed) and any escape is PERSISTED through the session sink BEFORE the
``CancelledError`` propagates. The ONLY not-possibly-live escape is :class:`AbortedByKill`, and it
arises solely from the pre-submission stop-check — i.e. a leg that provably never reached the wire.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from veridex.dust_execution.facade import propose_mm_execution
from veridex.mm_strategy.execution_adapter import PlanExecutionResult, execute_plan

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine
    from concurrent.futures import Future

    from veridex.dust_execution.facade import (
        MMExecutionToolRequest,
        MMExecutionToolResult,
        OperatorInterlock,
    )
    from veridex.dust_execution.manifest import StrategyExperimentManifest
    from veridex.dust_execution.operator_interlock_store import OperatorInterlockStore
    from veridex.dust_execution.runner import ModeBArming, QuoteSource
    from veridex.dust_execution.session_state import DurableSessionStateProvider
    from veridex.dust_execution.signer import Signer
    from veridex.mm_strategy.config import StrategyConfig
    from veridex.mm_strategy.contracts import StrategyDecision, StrategyObservation
    from veridex.mm_strategy.execution_adapter import R4ARequestConfig
    from veridex.policy.envelope import PolicyEnvelope
    from veridex.runtime.runtime_events import RuntimeEventSink
    from veridex.venues.base import VenueAdapter

__all__ = [
    "AbortedByKill",
    "BridgeInstrument",
    "BridgedPlanResult",
    "FacadeDeps",
    "FreezeRecord",
    "FreezeSink",
    "StopSignal",
    "UncertainOutcome",
    "execute_plan_bridged",
]


# ---------------------------------------------------------------------------------------------
# Net-new escape signals (this file defines them — they exist nowhere else).
# ---------------------------------------------------------------------------------------------


class AbortedByKill(Exception):
    """Raised by the bridge when the shared stop-signal is set BEFORE a leg is submitted.

    This is the ONLY escape that is honestly NOT possibly-live: the stop-check precedes EVERY
    submission, so an aborted leg PROVABLY never reached the wire. It carries the leg's built request
    for audit — the request was constructed by the core but never handed to the proposer.
    """

    def __init__(self, request: MMExecutionToolRequest) -> None:
        self.request = request
        super().__init__("bridge: plan killed — leg aborted before submission (proven un-sent)")


class UncertainOutcome(Exception):
    """Raised by the bridge when a leg's bounded ``future.result(timeout_s)`` wait times out.

    Cancelling the timed-out future does NOT prove the order never reached the wire, so the leg is
    POSSIBLY-LIVE: it is frozen and NEVER blindly retried (control #3). Carries the leg's request.
    """

    def __init__(self, request: MMExecutionToolRequest) -> None:
        self.request = request
        super().__init__("bridge: leg timed out — possibly-live, frozen (never retried blindly)")


# ---------------------------------------------------------------------------------------------
# Net-new result / record types.
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FreezeRecord:
    """The audit record for a bridged plan that escaped instead of completing cleanly.

    ``possibly_live`` is the load-bearing honesty flag: ``True`` whenever an order MAY have reached the
    wire (a timeout or any facade escape — fail closed), ``False`` ONLY for an :class:`AbortedByKill`
    whose leg was provably never submitted. ``reason`` is closed-vocab; ``request`` is the leg's built
    request when the escape carries it (``None`` for an opaque facade exception, whose leg identity is
    still recorded by the session/OPS layer).
    """

    reason: str
    possibly_live: bool
    decision: StrategyDecision
    request: MMExecutionToolRequest | None
    error_repr: str

    @classmethod
    def from_escape(cls, decision: StrategyDecision, exc: BaseException) -> FreezeRecord:
        """Classify a worker escape into an honest freeze record (fail closed = possibly-live)."""
        if isinstance(exc, AbortedByKill):
            return cls(
                reason="aborted_by_kill",
                possibly_live=False,  # proven un-sent (stop-check precedes submission)
                decision=decision,
                request=exc.request,
                error_repr=repr(exc),
            )
        if isinstance(exc, UncertainOutcome):
            return cls(
                reason="uncertain_outcome",
                possibly_live=True,  # timeout: an order may be on the wire
                decision=decision,
                request=exc.request,
                error_repr=repr(exc),
            )
        return cls(
            reason="facade_escape",
            possibly_live=True,  # any other escape is treated as possibly-live (fail closed)
            decision=decision,
            request=None,
            error_repr=repr(exc),
        )


@dataclass(frozen=True)
class BridgedPlanResult:
    """The bridge's return value. ``plan`` is the core's :class:`PlanExecutionResult` on a clean run;
    ``escaped`` is a :class:`FreezeRecord` when the worker escaped (mutually exclusive). Outer
    cancellation is NEVER represented here — it propagates as ``CancelledError`` (control #8)."""

    plan: PlanExecutionResult | None
    escaped: FreezeRecord | None
    instrument: BridgeInstrument | None = None


# ---------------------------------------------------------------------------------------------
# Instrumentation (executor-seam telemetry: active/queued bridge runs + timeout/freeze counts).
# ---------------------------------------------------------------------------------------------


@dataclass
class BridgeInstrument:
    """Thread-safe counters for the bridge. Populated from BOTH the host-loop thread (lifecycle) and
    the worker thread (per-leg bridge calls), so every mutation is taken under a lock. This is the
    instrumentation Codex requires alongside the executor seam — active/queued bridge runs plus
    timeout/freeze counts — sized for a bounded dedicated executor to be introduced later."""

    active_bridge_runs: int = 0
    max_active_bridge_runs: int = 0
    completed_bridge_runs: int = 0
    timeouts: int = 0
    futures_cancelled: int = 0
    freezes: int = 0
    aborted_by_kill: int = 0
    worker_thread_finished: bool = False
    host_thread_id: int | None = None
    bridge_thread_ids: set[int] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def bridge_started(self, thread_id: int) -> None:
        with self._lock:
            self.active_bridge_runs += 1
            self.max_active_bridge_runs = max(self.max_active_bridge_runs, self.active_bridge_runs)
            self.bridge_thread_ids.add(thread_id)

    def bridge_finished(self) -> None:
        with self._lock:
            self.active_bridge_runs -= 1
            self.completed_bridge_runs += 1

    def mark_timeout(self) -> None:
        with self._lock:
            self.timeouts += 1
            self.futures_cancelled += 1

    def mark_aborted_by_kill(self) -> None:
        with self._lock:
            self.aborted_by_kill += 1

    def mark_freeze(self) -> None:
        with self._lock:
            self.freezes += 1

    def mark_worker_thread_finished(self) -> None:
        with self._lock:
            self.worker_thread_finished = True


# ---------------------------------------------------------------------------------------------
# Session-owned dependencies + the cross-thread stop signal.
# ---------------------------------------------------------------------------------------------


class FreezeSink(Protocol):
    """The SESSION-owned sink the bridge persists a :class:`FreezeRecord` through on the cancel path
    (control #5). Distinct from R4-A's ``RuntimeEventSink`` (OPS telemetry): this one carries the
    bridge's own freeze audit, not ``RuntimeEvent`` payloads."""

    def emit(self, record: FreezeRecord) -> None: ...


@dataclass(frozen=True)
class FacadeDeps:
    """The FROZEN, session/composition-owned dependency bundle for ``propose_mm_execution``.

    Control #6: ``manifest``, ``envelope``, identity, wallet/execution deps, and the sinks are bound
    from the SESSION/composition layer — NEVER from the decision/request path. No decision field may
    substitute an admitted authority (this is what keeps the Gate-4 caller-forgeable-pin hole closed).
    :meth:`bound` returns exactly the kwargs ``propose_mm_execution`` accepts; ``proposer`` is the
    injectable async proposer seam (the real facade by default, a recording fake offline).
    """

    adapter: VenueAdapter
    signer: Signer
    sources: QuoteSource
    now_fn: Callable[[], int]
    sleep_fn: Callable[[float], Awaitable[None]]
    envelope: PolicyEnvelope
    manifest: StrategyExperimentManifest
    wallet_equity_at_decision: float
    fixed_fraction: float
    freeze_sink: FreezeSink
    event_sink: RuntimeEventSink | None = None
    agent_id: str | None = None
    run_id: str | None = None
    arming: ModeBArming | None = None
    operator_interlock: OperatorInterlock | None = None
    interlock_store: OperatorInterlockStore | None = None
    provider: DurableSessionStateProvider | None = None
    proposer: Callable[..., Coroutine[Any, Any, MMExecutionToolResult]] = propose_mm_execution

    def bound(self) -> dict[str, Any]:
        """The kwargs threaded to ``propose_mm_execution`` (never the freeze_sink / proposer / the
        core's own manifest+envelope path — those are the bridge's, not the proposer's, concern)."""
        kwargs: dict[str, Any] = {
            "adapter": self.adapter,
            "signer": self.signer,
            "sources": self.sources,
            "now_fn": self.now_fn,
            "sleep_fn": self.sleep_fn,
            "envelope": self.envelope,
            "manifest": self.manifest,
            "wallet_equity_at_decision": self.wallet_equity_at_decision,
            "fixed_fraction": self.fixed_fraction,
            "arming": self.arming,
            "operator_interlock": self.operator_interlock,
            "interlock_store": self.interlock_store,
            "provider": self.provider,
            "event_sink": self.event_sink,
            "run_id": self.run_id,
        }
        if self.agent_id is not None:
            kwargs["agent_id"] = self.agent_id
        return kwargs


class StopSignal:
    """ONE kill, two consumers. Wraps an ``asyncio.Event`` (loop-side waits, e.g. II-2's maker loop)
    AND a ``threading.Event`` (checked by II-1's bridge from the WORKER thread). :meth:`set` trips both
    atomically. The bridge reads the threading side — never the asyncio ``Event``, which is not
    thread-safe. :meth:`set` is only ever invoked from the host-loop thread, so the ``asyncio.Event``
    mutation stays loop-confined."""

    def __init__(self) -> None:
        self._async_event = asyncio.Event()
        self._thread_event = threading.Event()

    def set(self) -> None:
        # Trip the thread side FIRST so a worker that reads immediately after a loop-side wait wakes
        # can never observe a half-set signal.
        self._thread_event.set()
        self._async_event.set()

    def is_set(self) -> bool:
        """The WORKER-thread view: the thread-safe ``threading.Event``."""
        return self._thread_event.is_set()

    async def wait(self) -> None:
        """The LOOP-side wait (II-2)."""
        await self._async_event.wait()

    @property
    def threading_event(self) -> threading.Event:
        return self._thread_event

    @property
    def asyncio_event(self) -> asyncio.Event:
        return self._async_event


# ---------------------------------------------------------------------------------------------
# The bridge.
# ---------------------------------------------------------------------------------------------


async def _run_in_worker(
    executor: ThreadPoolExecutor | None,
    fn: Callable[[], PlanExecutionResult],
) -> PlanExecutionResult:
    """Run ``fn`` in a worker thread, mirroring ``asyncio.to_thread`` (contextvars copied) but with an
    injectable executor SEAM: ``executor=None`` uses the loop's default ``ThreadPoolExecutor`` (exactly
    ``to_thread``'s behaviour); a bounded dedicated pool can be supplied later WITHOUT touching
    ``execute_plan``."""
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    call = functools.partial(ctx.run, fn)
    return await loop.run_in_executor(executor, call)


async def execute_plan_bridged(
    decision: StrategyDecision,
    *,
    observation: StrategyObservation,
    config: R4ARequestConfig,
    manifest: StrategyExperimentManifest,
    envelope: PolicyEnvelope,
    strategy_config: StrategyConfig,
    facade_deps: FacadeDeps,
    timeout_s: float,
    stop: StopSignal,
    instrument: BridgeInstrument | None = None,
    executor: ThreadPoolExecutor | None = None,
) -> BridgedPlanResult:
    """Drive the UNCHANGED synchronous ``execute_plan`` from the async host, bridging each leg's async
    ``propose_mm_execution`` onto the captured host loop with escape→freeze semantics.

    Args:
        decision, observation, config, manifest, envelope, strategy_config: the reviewed, session-owned
            inputs threaded verbatim into ``execute_plan`` (the money-boundary core is called AS-IS).
        facade_deps: the frozen session-owned deps + injectable async proposer (control #6).
        timeout_s: the finite, strictly-positive per-leg wait bound (control #2). Rejected otherwise.
        stop: the shared kill signal; the bridge checks its threading side before EVERY submission.
        instrument: telemetry sink (executor seam); a fresh one is created when omitted and returned on
            the result. Pass your own to observe a run mid-flight.
        executor: the worker-thread pool SEAM; ``None`` uses the loop default (``to_thread`` semantics).

    Returns:
        A :class:`BridgedPlanResult` with either the core's ``plan`` (clean) or an ``escaped`` freeze.

    Raises:
        ValueError: ``timeout_s`` is not finite or not strictly positive (fail closed, before any work).
        asyncio.CancelledError: the outer task was cancelled — propagated ONLY after the worker thread
            is terminal and any cancel-path escape has been persisted (never converted to a result).
    """
    # Control #2: bounded, positive, finite timeout — validate BEFORE any worker/bridge runs.
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError(
            f"execute_plan_bridged: timeout_s must be finite and strictly positive, got {timeout_s!r} "
            "— every future.result() wait must be bounded (fail closed)"
        )

    instr = instrument if instrument is not None else BridgeInstrument()

    # Control #7: capture the ACTIVE host loop at call time — never a cached/global reference.
    loop = asyncio.get_running_loop()
    # Control #1: record the host-loop thread identity; the bridge asserts it never runs here (calling
    # future.result() on the host-loop thread would deadlock).
    host_thread_id = threading.get_ident()
    instr.host_thread_id = host_thread_id

    def bridge(request: MMExecutionToolRequest) -> MMExecutionToolResult:
        """The synchronous facade seam ``execute_plan`` calls once per actionable leg — runs in the
        WORKER thread, routes the async proposer onto the host loop, blocks on a bounded wait."""
        current_thread_id = threading.get_ident()
        # Control #1 (defense in depth): the bridge must NEVER run on the host-loop thread.
        if current_thread_id == host_thread_id:
            raise RuntimeError(
                "bridge invoked on the host-loop thread — future.result() would deadlock (control #1)"
            )
        instr.bridge_started(current_thread_id)
        try:
            # Control #4/#8 (cooperative kill): check the stop-signal BEFORE every submission — the only
            # blocking points. A kill submits NO new leg; the leg already in flight is never un-sent.
            if stop.is_set():
                instr.mark_aborted_by_kill()
                raise AbortedByKill(request)
            future: Future[MMExecutionToolResult] = asyncio.run_coroutine_threadsafe(
                facade_deps.proposer(request, **facade_deps.bound()), loop
            )
            try:
                return future.result(timeout_s)
            except FuturesTimeoutError:
                # Control #3: a timeout is POSSIBLY-LIVE. Cancel the future (does NOT prove un-sent) and
                # freeze — never blindly retried.
                future.cancel()
                instr.mark_timeout()
                raise UncertainOutcome(request) from None
        finally:
            instr.bridge_finished()

    def worker_body() -> PlanExecutionResult:
        """The worker-thread entrypoint. Sets the terminal flag from WITHIN the thread (in a finally) so
        the outer wrapper can PROVE the thread is terminal before propagating a cancellation (control
        #4). ``execute_plan`` is called byte-unchanged (A6)."""
        try:
            return execute_plan(
                decision,
                bridge,
                observation=observation,
                config=config,
                manifest=manifest,
                envelope=envelope,
                strategy_config=strategy_config,
            )
        finally:
            instr.mark_worker_thread_finished()

    worker = asyncio.create_task(_run_in_worker(executor, worker_body))

    try:
        result = await asyncio.shield(worker)
        return BridgedPlanResult(plan=result, escaped=None, instrument=instr)
    except asyncio.CancelledError:
        # Control #4/#8: outer cancellation must NEVER become a returned result. Set the stop signal so
        # the worker submits no further legs, then DRAIN the worker to terminal.
        stop.set()
        # Repeated-cancellation-proof: a 2nd/3rd cancel lands on THIS await; asyncio.shield keeps the
        # worker task un-cancelled, so worker.done() is True ONLY once the underlying THREAD is terminal.
        # A live thread is never abandoned.
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue  # keep waiting — never return/propagate while the thread may still be live
            except Exception:
                break  # the worker escaped during cleanup; reconciled below
        # Control #5 (Fable M4): the worker is now terminal. If it escaped (e.g. UncertainOutcome =
        # POSSIBLY-LIVE), PERSIST the freeze through the session sink BEFORE re-raising — a cancel-path
        # escape must NEVER be silently swallowed.
        if not worker.cancelled():
            worker_exc = worker.exception()
            if worker_exc is not None:
                instr.mark_freeze()
                facade_deps.freeze_sink.emit(FreezeRecord.from_escape(decision, worker_exc))
        raise  # cancellation propagates, never swallowed, never a "nothing happened" result
    except Exception as exc:
        # The worker escaped (no PlanExecutionResult exists): every escape is a freeze (fail closed).
        instr.mark_freeze()
        return BridgedPlanResult(
            plan=None, escaped=FreezeRecord.from_escape(decision, exc), instrument=instr
        )
