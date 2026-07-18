"""VeridexAgentAdapter — the Veridex-owned AgentOS host for the II-2 market-maker composition run.

This adapter subclasses agno's :class:`~agno.agents.base.BaseExternalAgent` so the II-2
``run_market_maker`` composition can be hosted inside AgentOS while Veridex keeps the trust boundary:

* **AC-26 — ZERO construct side effects.** ``__init__`` only wires references + an empty in-process
  run registry. No network, no thread, no task, no db, no run is started at import/construct time.
* **AC-16 — owner-first, exactly-once cancel.** :meth:`acancel_run` verifies the owner BEFORE any
  effect, then makes a per-run atomic ``ACTIVE -> CANCELLING`` transition under a lock — only the ONE
  winner engages the kill (``StopSignal.set``); concurrent repeat cancels return the current state
  without re-engaging.
* **Ownership is never derived from agno's caller-supplied session metadata.** The Veridex wrapper
  route (:mod:`veridex.runtime.agentos_service`) authenticates + owner-gates the persisted
  ``AgentInstance`` and drives :meth:`start_run` with SERVER-pre-allocated ids and the known owner.

The II-2 run itself is reached through an injectable :data:`RunDriver` seam so the security + lease +
cancel behaviour is testable OFFLINE (a fake driver that cooperatively waits on the ``StopSignal``),
while production wires the real ``run_market_maker`` through :func:`build_market_maker_driver`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from agno.agents.base import BaseExternalAgent
from agno.run.agent import RunContentEvent

from veridex.mm_strategy.orchestration import StopSignal
from veridex.runtime.runtime_events import (
    RuntimeEventSink,
    RuntimeEventType,
    RuntimeStatus,
    runtime_event,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)


class RunPhase(str, Enum):
    """The adapter-local lifecycle of ONE hosted run (distinct from the durable lease state).

    ``ACTIVE`` while the driver runs; ``CANCELLING`` after the exactly-once owner-checked cancel
    engages the kill; the run then settles to ``COMPLETED`` / ``FAILED`` / ``CANCELLED``.
    """

    ACTIVE = "active"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


_TERMINAL_PHASES: frozenset[RunPhase] = frozenset(
    {RunPhase.COMPLETED, RunPhase.FAILED, RunPhase.CANCELLED}
)


class OwnerMismatchError(PermissionError):
    """Raised by :meth:`VeridexAgentAdapter.acancel_run` when the caller is not the run's owner.

    Fail-closed: raised BEFORE any cancellation effect (no ``stop.set``, no state transition). A
    ``None`` owner (the agno-native cancel path, which carries no Privy principal) is treated as a
    non-owner — so even if the outer guard were bypassed, the native cancel cannot engage the kill.
    """


@dataclass(frozen=True)
class RunContext:
    """The SERVER-owned identity + input a hosted run is driven under (never caller-forgeable).

    Attributes:
        run_id: SERVER-pre-allocated Veridex run id (authoritative result/evidence identity).
        session_id: SERVER-pre-allocated AgentOS session id.
        runtime_agent_id: SERVER-pre-allocated AgentOS runtime agent id.
        owner_did: The authenticated owner (``AgentInstance.operator_id``), resolved by the wrapper.
        input: The opaque run input forwarded to the driver (never an ownership signal).
    """

    run_id: str
    session_id: str
    runtime_agent_id: str
    owner_did: str
    input: Any = None


@dataclass(frozen=True)
class CancelResult:
    """The outcome of an :meth:`VeridexAgentAdapter.acancel_run` call.

    Attributes:
        run_id: The run the cancel targeted.
        phase: The run's phase AFTER this call.
        engaged: ``True`` for the single winner that engaged the kill (``ACTIVE -> CANCELLING``);
            ``False`` for a repeat/concurrent cancel that observed a non-``ACTIVE`` phase.
    """

    run_id: str
    phase: RunPhase
    engaged: bool


@dataclass
class _RunEntry:
    """Process-local bookkeeping for ONE hosted run (registry value)."""

    run_id: str
    owner_did: str
    stop: StopSignal
    phase: RunPhase


#: The injectable run seam: given the SERVER-owned :class:`RunContext`, the run's own
#: :class:`~veridex.mm_strategy.orchestration.StopSignal`, and the OPS event sink, drive the II-2
#: composition to completion and return its result (a ``SessionSummary`` in production, anything in
#: tests). The adapter owns the ``StopSignal`` so :meth:`acancel_run` can trip it.
RunDriver = Callable[[RunContext, StopSignal, RuntimeEventSink], Awaitable[Any]]


def _noop_sink(_event: Any) -> None:
    """A do-nothing OPS sink (used when no sink is supplied)."""


class VeridexAgentAdapter(BaseExternalAgent):
    """AgentOS-hosted adapter that starts/attaches + cancels the II-2 market-maker run (Veridex-owned).

    Construct with ZERO side effects (AC-26): the only work done here is wiring the injected
    :data:`RunDriver`, an optional default OPS sink, an empty run registry, and an ``asyncio.Lock``
    for the exactly-once cancel transition. ``self.db`` is left ``None`` so AgentOS injects the
    owner-scoped db when it composes.
    """

    def __init__(
        self,
        *,
        run_driver: RunDriver,
        name: str = "veridex-market-maker",
        id: str = "veridex-market-maker",
        event_sink: RuntimeEventSink | None = None,
    ) -> None:
        """Wire the adapter. NO run is started and NO external resource is touched here (AC-26).

        Args:
            run_driver: The injectable seam that actually drives the II-2 composition run.
            name: The AgentOS agent name.
            id: The AgentOS agent id (stable; the runtime_agent_id the run attaches under).
            event_sink: Optional default OPS ``RuntimeEvent`` sink for runs that do not supply one.
        """
        super().__init__(name=name, id=id)
        # self.db stays None — AgentOS injects the owner db at composition (I-9 Q1).
        self._run_driver = run_driver
        self._default_event_sink = event_sink
        self._runs: dict[str, _RunEntry] = {}
        self._cancel_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Veridex-wrapper entrypoint (server-pre-allocated ids + known owner)
    # ------------------------------------------------------------------

    async def start_run(
        self,
        *,
        run_id: str,
        session_id: str,
        runtime_agent_id: str,
        owner_did: str,
        input: Any = None,
        event_sink: RuntimeEventSink | None = None,
    ) -> Any:
        """Drive a run under SERVER-pre-allocated ids + an authenticated owner (the wrapper path).

        Called INTERNALLY by the Veridex wrapper route AFTER it has authenticated + owner-gated the
        persisted ``AgentInstance`` and acquired the lease — never reachable from an agno-native route.

        Args:
            run_id / session_id / runtime_agent_id: The SERVER-pre-allocated identity the run is pinned
                to (the same ids recorded on the lease + instance ``runtime_handle``).
            owner_did: The authenticated owner (``AgentInstance.operator_id``); the ONLY principal that
                may later cancel this run.
            input: Opaque run input forwarded to the driver.
            event_sink: OPS sink for this run (falls back to the adapter default).

        Returns:
            Whatever the injected :data:`RunDriver` returns (a ``SessionSummary`` in production).
        """
        ctx = RunContext(
            run_id=run_id,
            session_id=session_id,
            runtime_agent_id=runtime_agent_id,
            owner_did=owner_did,
            input=input,
        )
        return await self._drive_run(ctx, event_sink=event_sink)

    def is_run_active(self, run_id: str) -> bool:
        """Return whether ``run_id`` is a currently non-terminal hosted run."""
        entry = self._runs.get(run_id)
        return entry is not None and entry.phase not in _TERMINAL_PHASES

    def run_phase(self, run_id: str) -> RunPhase | None:
        """Return the current :class:`RunPhase` of ``run_id``, or ``None`` if unknown."""
        entry = self._runs.get(run_id)
        return entry.phase if entry is not None else None

    # ------------------------------------------------------------------
    # Owner-first, exactly-once cancel (AC-16)
    # ------------------------------------------------------------------

    async def acancel_run(self, run_id: str, *, owner_did: str | None = None) -> CancelResult:
        """Cancel ``run_id`` — OWNER CHECK first, then an exactly-once ``ACTIVE -> CANCELLING`` kill.

        agno's native cancel route calls ``agent.acancel_run(run_id=run_id)`` with NO principal, so
        ``owner_did`` defaults to ``None`` and is treated as a non-owner (fail-closed) — the native
        path can never engage the kill even if the outer guard were bypassed. The Veridex wrapper
        supplies the authenticated ``owner_did`` after resolving ownership from server-owned state.

        Under a per-adapter lock: the owner check completes BEFORE any effect; only the single caller
        that observes ``ACTIVE`` performs the atomic transition + engages the kill exactly once; any
        concurrent/repeat cancel observes ``CANCELLING`` / terminal and returns without re-engaging.

        Args:
            run_id: The run to cancel.
            owner_did: The authenticated owner; must equal the run's recorded owner.

        Returns:
            A :class:`CancelResult`; ``engaged`` is ``True`` only for the exactly-once winner.

        Raises:
            KeyError: If ``run_id`` is unknown (the wrapper maps this to 404 — no existence leak).
            OwnerMismatchError: If ``owner_did`` is ``None`` or not the run's owner (no effect).
        """
        async with self._cancel_lock:
            entry = self._runs.get(run_id)
            if entry is None:
                raise KeyError(run_id)
            # OWNER CHECK FIRST — before any state transition or kill.
            if owner_did is None or owner_did != entry.owner_did:
                raise OwnerMismatchError(
                    f"principal does not own run {run_id!r} (cancel refused before any effect)"
                )
            if entry.phase is not RunPhase.ACTIVE:
                # Already cancelling or terminal: return current state WITHOUT re-engaging the kill.
                return CancelResult(run_id=run_id, phase=entry.phase, engaged=False)
            # Atomic ACTIVE -> CANCELLING under the lock: exactly ONE winner reaches here.
            entry.phase = RunPhase.CANCELLING
            entry.stop.set()  # engage cancel-all exactly once (StopSignal trips thread + loop sides)
            return CancelResult(run_id=run_id, phase=RunPhase.CANCELLING, engaged=True)

    # ------------------------------------------------------------------
    # agno BaseExternalAgent hooks (present for protocol + AC-29; native routes are DENIED publicly)
    # ------------------------------------------------------------------

    async def _arun_adapter(
        self, input: Any, *, history: list[dict[str, Any]] | None = None, **kwargs: Any
    ) -> Any:
        """Non-streaming agno hook. Reached ONLY via the (guard-denied) native run route.

        Drives the run with an EMPTY owner (agno's native path carries no Privy principal), so a run
        started this way can never be cancelled by anyone — defense in depth behind the outer deny.
        """
        ctx = self._native_context(input, kwargs)
        result = await self._drive_run(ctx, event_sink=self._default_event_sink)
        return _content_of(result)

    async def _arun_adapter_stream(
        self, input: Any, *, history: list[dict[str, Any]] | None = None, **kwargs: Any
    ) -> AsyncIterator[Any]:
        """Streaming agno hook. Yields a single ``RunContentEvent`` (base emits Started/Completed)."""
        ctx = self._native_context(input, kwargs)
        result = await self._drive_run(ctx, event_sink=self._default_event_sink)
        yield RunContentEvent(content=_content_of(result))

    def _native_context(self, input: Any, kwargs: dict[str, Any]) -> RunContext:
        """Build a RunContext for the agno-native path — owner is EMPTY (uncancellable, fail-closed)."""
        run_id = kwargs.get("run_id") or str(uuid4())
        session_id = kwargs.get("session_id") or run_id
        return RunContext(
            run_id=run_id,
            session_id=session_id,
            runtime_agent_id=self.get_id(),
            owner_did="",  # no authenticated owner on the native path — never cancellable
            input=input,
        )

    # ------------------------------------------------------------------
    # Shared run core
    # ------------------------------------------------------------------

    async def _drive_run(self, ctx: RunContext, *, event_sink: RuntimeEventSink | None) -> Any:
        """Register, drive, and terminally-classify one run; emit a terminal STATUS_CHANGED.

        Raises:
            RuntimeError: If ``ctx.run_id`` is already an active run (never double-attach a run id).
        """
        if ctx.run_id in self._runs and self._runs[ctx.run_id].phase not in _TERMINAL_PHASES:
            raise RuntimeError(f"run already active: {ctx.run_id!r}")
        stop = StopSignal()
        entry = _RunEntry(run_id=ctx.run_id, owner_did=ctx.owner_did, stop=stop, phase=RunPhase.ACTIVE)
        self._runs[ctx.run_id] = entry
        sink = event_sink or self._default_event_sink or _noop_sink
        try:
            result = await self._run_driver(ctx, stop, sink)
        except Exception:
            entry.phase = RunPhase.FAILED
            self._emit_terminal(sink, ctx, failed=True)
            raise
        # Map the driver outcome to a terminal phase + STATUS_CHANGED, mirroring the composition's own
        # _terminal_event mapping ("frozen" -> failed; "completed"/"stopped" -> completed).
        reason = getattr(result, "terminal_reason", None)
        failed = reason == "frozen"
        if entry.phase is RunPhase.CANCELLING:
            entry.phase = RunPhase.CANCELLED
        else:
            entry.phase = RunPhase.FAILED if failed else RunPhase.COMPLETED
        self._emit_terminal(sink, ctx, failed=failed)
        return result

    def _emit_terminal(self, sink: RuntimeEventSink, ctx: RunContext, *, failed: bool) -> None:
        """Emit the terminal ``STATUS_CHANGED(completed|failed)`` (fu-ii2-minors M3; best-effort).

        A throwing sink can never gate run teardown — OPS telemetry is best-effort here, matching the
        composition's own contained-sink discipline.
        """
        status = RuntimeStatus.FAILED if failed else RuntimeStatus.COMPLETED
        try:
            sink(
                runtime_event(
                    RuntimeEventType.STATUS_CHANGED,
                    agent_id=ctx.runtime_agent_id,
                    run_id=ctx.run_id,
                    session_id=ctx.session_id or None,
                    status=status.value,
                )
            )
        except Exception:  # noqa: BLE001 — OPS telemetry is best-effort; never gate teardown.
            logger.warning("terminal STATUS_CHANGED sink failed for run %s", ctx.run_id, exc_info=True)


def _content_of(result: Any) -> str:
    """Render a driver result into a short content string for the agno-native hook."""
    reason = getattr(result, "terminal_reason", None)
    if reason is not None:
        return f"market-maker run {reason}"
    return "market-maker run completed" if result is None else str(result)


def build_market_maker_driver(
    session_factory: Callable[[RunContext], tuple[Any, Any, str, bool]],
) -> RunDriver:
    """Build the PRODUCTION :data:`RunDriver` that wires the II-2 ``run_market_maker`` composition.

    The returned driver threads the adapter-owned ``StopSignal`` and OPS sink into the frozen II-2
    ``run_market_maker`` so a cancel promptly halts the loop. ``session_factory`` (supplied by II-5)
    resolves the run's ``(MakerInstanceConfig, tape, mode, guard_enabled)`` from the SERVER-owned
    :class:`RunContext` — never from caller-supplied metadata.

    Args:
        session_factory: Resolves the II-2 session bundle for a given :class:`RunContext`.

    Returns:
        A :data:`RunDriver` suitable for :class:`VeridexAgentAdapter`.
    """

    async def _driver(ctx: RunContext, stop: StopSignal, event_sink: RuntimeEventSink) -> Any:
        from veridex.mm_strategy.composition import run_market_maker  # noqa: PLC0415 (lazy, heavy)

        instance_cfg, tape, mode, guard_enabled = session_factory(ctx)
        return await run_market_maker(
            instance_cfg,
            tape,
            mode=mode,
            guard_enabled=guard_enabled,
            event_sink=event_sink,
            stop=stop,
        )

    return _driver
