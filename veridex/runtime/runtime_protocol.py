"""§4.4 AgentRuntime — the runtime-neutral seam (SEC-010). AgnoRuntime is the only hackathon impl.

``propose_action`` returns a validated ``AgentAction`` AND emits the normalized required-tier
``RuntimeEvent`` stream on an OPS sink. The adapter SYNTHESIZES the required tier from the
request→response→parse→validate boundary, so even a bare BYOA endpoint (a different ``propose_fn``)
produces baseline telemetry — observability never binds to Agno-specific structures.

The ops sink is DISTINCT from the orchestrator's evidence ``event_sink`` (which carries SEALED
RunEvents): RuntimeEvents are operational only and never feed the deterministic seal (SEC-003).

CON-010: ``emit_agent_action_async`` lazy-imports agno, so this module stays offline-importable;
tests inject ``propose_fn`` and never touch the network.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

from veridex.runtime.runtime_events import (
    RuntimeEvent,
    RuntimeEventSink,
    RuntimeEventType,
    RuntimeStatus,
    runtime_event,
)
from veridex.runtime.schemas import AgentAction

#: A runtime-neutral proposer: ``async (market_state, **kw) -> AgentAction`` — the BYOA seam.
ProposeFn = Callable[..., Awaitable[AgentAction]]


@runtime_checkable
class AgentRuntime(Protocol):
    """Structural contract every runtime adapter satisfies (Agno now; Hermes/BYOA later)."""

    async def propose_action(
        self,
        market_state: Any,
        *,
        agent_id: str,
        run_id: str | None = None,
        config: dict[str, Any] | None = None,
        run_context: dict[str, Any] | None = None,
    ) -> AgentAction: ...

    def run_started(self, *, agent_id: str, run_id: str | None = None) -> None: ...

    def run_completed(self, *, agent_id: str, run_id: str | None = None) -> None: ...

    def run_failed(self, *, agent_id: str, run_id: str | None = None, error: str) -> None: ...


class AgnoRuntime:
    """The hackathon AgentRuntime: wraps the LLM proposer and emits required-tier telemetry.

    Args:
        sink: OPS-channel callback for every emitted RuntimeEvent (``None`` ⇒ no-op).
        propose_fn: The proposer seam; defaults to ``emit_agent_action_async`` (lazy). A BYOA
            runtime passes its own ``async (market_state, **kw) -> AgentAction`` here.
        model / model_id / agent_factory: forwarded to the default Agno proposer.
    """

    def __init__(
        self,
        *,
        sink: RuntimeEventSink | None = None,
        propose_fn: ProposeFn | None = None,
        model: Any = None,
        model_id: str | None = None,
        agent_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._sink = sink
        self._propose_fn = propose_fn
        self._model = model
        self._model_id = model_id
        self._agent_factory = agent_factory

    def _emit(self, event: RuntimeEvent) -> None:
        if self._sink is not None:
            self._sink(event)

    async def _resolve_propose(self, market_state: Any) -> AgentAction:
        if self._propose_fn is not None:
            return await self._propose_fn(market_state)
        from veridex.runtime.agent import emit_agent_action_async  # lazy: keeps module agno-free

        return await emit_agent_action_async(
            market_state, model=self._model, model_id=self._model_id, agent_factory=self._agent_factory
        )

    def run_started(self, *, agent_id: str, run_id: str | None = None) -> None:
        """Emit RUN_STARTED + STATUS_CHANGED(running)."""
        self._emit(runtime_event(RuntimeEventType.RUN_STARTED, agent_id=agent_id, run_id=run_id))
        self._emit(
            runtime_event(
                RuntimeEventType.STATUS_CHANGED,
                agent_id=agent_id,
                run_id=run_id,
                status=RuntimeStatus.RUNNING.value,
            )
        )

    def run_completed(self, *, agent_id: str, run_id: str | None = None) -> None:
        """Emit RUN_COMPLETED + STATUS_CHANGED(completed)."""
        self._emit(runtime_event(RuntimeEventType.RUN_COMPLETED, agent_id=agent_id, run_id=run_id))
        self._emit(
            runtime_event(
                RuntimeEventType.STATUS_CHANGED,
                agent_id=agent_id,
                run_id=run_id,
                status=RuntimeStatus.COMPLETED.value,
            )
        )

    def run_failed(self, *, agent_id: str, run_id: str | None = None, error: str) -> None:
        """Emit RUN_FAILED + STATUS_CHANGED(failed)."""
        self._emit(runtime_event(RuntimeEventType.RUN_FAILED, agent_id=agent_id, run_id=run_id, error=error))
        self._emit(
            runtime_event(
                RuntimeEventType.STATUS_CHANGED,
                agent_id=agent_id,
                run_id=run_id,
                status=RuntimeStatus.FAILED.value,
            )
        )

    async def propose_action(
        self,
        market_state: Any,
        *,
        agent_id: str,
        run_id: str | None = None,
        config: dict[str, Any] | None = None,
        run_context: dict[str, Any] | None = None,
    ) -> AgentAction:
        """Propose one action, synthesizing the required tier from the call boundary.

        Emits MODEL_CALL_STARTED → (proposer) → MODEL_CALL_COMPLETED + LATENCY (optional) →
        ACTION_EMITTED + SCHEMA_VALIDATION(valid=True) on success; SCHEMA_VALIDATION(valid=False)
        + ERROR on failure, then re-raises (the orchestrator's per-decide guard fail-closes).
        """
        started = time.time()
        self._emit(runtime_event(RuntimeEventType.MODEL_CALL_STARTED, agent_id=agent_id, run_id=run_id))
        try:
            action = await self._resolve_propose(market_state)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            self._emit(
                runtime_event(
                    RuntimeEventType.SCHEMA_VALIDATION,
                    agent_id=agent_id,
                    run_id=run_id,
                    valid=False,
                    error=detail,
                )
            )
            self._emit(runtime_event(RuntimeEventType.ERROR, agent_id=agent_id, run_id=run_id, error=detail))
            raise
        latency_ms = int((time.time() - started) * 1000)
        self._emit(runtime_event(RuntimeEventType.MODEL_CALL_COMPLETED, agent_id=agent_id, run_id=run_id))
        self._emit(runtime_event(RuntimeEventType.LATENCY, agent_id=agent_id, run_id=run_id, latency_ms=latency_ms))
        self._emit(
            runtime_event(
                RuntimeEventType.ACTION_EMITTED,
                agent_id=agent_id,
                run_id=run_id,
                action=action.type.value,
                params=dict(action.params),
            )
        )
        self._emit(runtime_event(RuntimeEventType.SCHEMA_VALIDATION, agent_id=agent_id, run_id=run_id, valid=True))
        return action
