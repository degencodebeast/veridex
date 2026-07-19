"""II-8b — directional AgentOS adapters (deterministic Cumulative-Drift / Sharp-Momentum, and LLM-Drift).

Mirrors II-4's :class:`~veridex.runtime.mm_agent_adapter.VeridexAgentAdapter` so EVERY directional
contestant is hosted as a real AgentOS session/run rather than an in-process callable. Both classes
here SUBCLASS ``VeridexAgentAdapter`` and therefore INHERIT — never re-implement — the trust-critical
core it already validated:

* **AC-26 — ZERO construct side effects.** ``__init__`` only wires references + an empty registry.
* **AC-16 — owner-first, exactly-once cancel.** The inherited :meth:`acancel_run` owner-checks BEFORE
  any effect and trips the run's :class:`~veridex.mm_strategy.orchestration.StopSignal` exactly once;
  our per-tick driver loop reads ``stop.is_set()`` at the top of each tick, so a cancel promptly
  breaks the decision loop (returns — never a 501/no-op).
* **AC-27/AC-29 — the deny-by-default boundary.** These adapters are composed into the SAME
  ``AgentOS(agents=[...])`` as the MM adapter (see :func:`agentos_service.build_agentos_app`'s new
  ``extra_agents`` seam), so their agno-native run/cancel routes inherit the 401-anonymous guard.

The ONLY thing each subclass supplies is the ``run_driver`` seam: a driver that resolves the run's
tape from server-owned state (the ``tape_resolver`` — never caller metadata, mirroring II-4's
``session_factory``) and drives a directional :class:`~veridex.runtime.orchestrator.Agent` over it,
recording every decision bound to full run/session/config/snapshot provenance.

Two adapters, because deterministic and LLM contestants differ in ONE trust-critical way:

* :class:`VeridexDeterministicAgentAdapter` — a deterministic agent (Cumulative-Drift, admitted
  Sharp-Momentum) is a PURE function of the tape, so its hosted action sequence is BYTE-IDENTICAL to
  a local replay on the same tape. That equivalence is the deterministic-equivalence control.
* :class:`VeridexLLMAgentAdapter` — an LLM contestant's response is canonical only ONCE. The hosted
  run IS the model call: it wraps II-8's injectable ``ModelLauncher`` seam in a RECORDING launcher
  that SEALS every raw response into a transcript. A replay rebuilds the agent with a REPLAYING
  launcher over that sealed transcript and NEVER re-invokes the model (a second physical call would
  yield a different action and drift the sequence).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from veridex.runtime.evidence import serialize_payload
from veridex.runtime.mm_agent_adapter import RunContext, VeridexAgentAdapter
from veridex.runtime.schemas import AgentAction, SportsActionType

if TYPE_CHECKING:
    from veridex.ingest.marketstate import MarketState
    from veridex.mm_strategy.orchestration import StopSignal
    from veridex.runtime.llm_checkpoint import CallHandle, ModelLauncher
    from veridex.runtime.orchestrator import Agent
    from veridex.runtime.runtime_events import RuntimeEventSink


#: Server-owned resolution of the tape a hosted run decides over (never caller-supplied metadata).
TapeResolver = Callable[[RunContext], "list[MarketState]"]

#: Builds a fresh directional :class:`Agent` (own detector/runner state per run).
AgentFactory = Callable[[], "Agent"]

#: Builds an LLM :class:`Agent` bound to an injected model launcher + deterministic clock.
LLMAgentBuilder = Callable[["ModelLauncher", Callable[[], float]], "Agent"]


# ---------------------------------------------------------------------------
# Provenance-bound hosted decision + the canonical run result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HostedAction:
    """One decision produced BY the hosted run, bound to its full provenance (never a recompute).

    Attributes:
        tick_seq / ts: The tape tick that produced the action.
        snapshot_hash: SHA-256 over the exact ``MarketState`` bytes the action decided on (snapshot
            provenance — a verifier re-hashes the tape tick to confirm the binding).
        action: The ACTUAL :class:`AgentAction` the hosted agent emitted at that tick.
        session_id / run_id / runtime_agent_id: The SERVER-pre-allocated AgentOS identity of the run.
        config_hash: The deployed contestant's pinned sealed identity at that snapshot.
    """

    tick_seq: int
    ts: int
    snapshot_hash: str
    action: AgentAction
    session_id: str
    run_id: str
    runtime_agent_id: str
    config_hash: str


@dataclass(frozen=True)
class TranscriptEntry:
    """One SEALED raw model response captured during a hosted LLM run (the replay evidence).

    Attributes:
        prompt: The exact prompt the model was launched on (deterministic given the tape).
        raw: The raw model output (an :class:`AgentAction`, dict, or text) — revalidated identically
            on replay so the canonical action is reproduced without touching the model.
    """

    prompt: str
    raw: Any


@dataclass(frozen=True)
class DirectionalRunResult:
    """The canonical result of one hosted directional run (the base adapter classifies it terminally).

    ``terminal_reason`` is read by the inherited ``_drive_run``: ``"stopped"`` (cancel tripped the
    loop) settles the run to ``CANCELLED``; ``"completed"`` to ``COMPLETED``.

    Attributes:
        run_id / session_id / runtime_agent_id / owner_did: The run's server-owned identity.
        agent_id: The hosted contestant's AgentOS agent id.
        config_hash: The contestant's pinned identity for the run.
        actions: EVERY per-tick :class:`HostedAction` (WAIT + scored), in tape order.
        scored_actions: The non-WAIT subset (the actions the law scores).
        sealed_transcript: The sealed model transcript (LLM only; empty for deterministic runs).
        stopped_early: ``True`` when a cancel StopSignal broke the loop before the tape was exhausted.
        terminal_reason: ``"completed"`` or ``"stopped"`` (never ``"frozen"`` here).
    """

    run_id: str
    session_id: str
    runtime_agent_id: str
    owner_did: str
    agent_id: str
    config_hash: str
    actions: tuple[HostedAction, ...]
    scored_actions: tuple[HostedAction, ...]
    sealed_transcript: tuple[TranscriptEntry, ...]
    stopped_early: bool
    terminal_reason: str


def _snapshot_hash(market_state: MarketState) -> str:
    """SHA-256 over the canonical ``MarketState`` bytes — the snapshot provenance coordinate."""
    import hashlib

    return hashlib.sha256(serialize_payload(market_state.model_dump()).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The shared per-tick decision loop (used by BOTH hosting and local replay)
# ---------------------------------------------------------------------------


@dataclass
class _MutableClock:
    """A deterministic, driver-advanced seconds clock (drives the LLM checkpoint evidence-age).

    The driver sets it to each tick's ``ts`` before ``agent.decide``, so checkpoint cadence +
    evidence-age staleness become a PURE function of the tape — the property that makes an async
    LLM run byte-identically replayable.
    """

    _t: float = 0.0

    def __call__(self) -> float:
        return self._t

    def set(self, t: float) -> None:
        self._t = float(t)


async def _drive_agent_over_tape(
    agent: Agent,
    tape: list[MarketState],
    ctx: RunContext,
    stop: StopSignal,
    *,
    clock: _MutableClock | None = None,
) -> tuple[list[HostedAction], bool]:
    """Drive ``agent`` over ``tape`` one tick at a time, recording provenance-bound decisions.

    Reads ``stop.is_set()`` at the TOP of every tick (so an owner cancel promptly breaks the loop —
    AC-16) and yields the event loop each tick (``asyncio.sleep(0)``) so a concurrent cancel can
    interleave. When a ``clock`` is supplied it is advanced to the tick's ``ts`` before the decision
    (the LLM path). Returns ``(hosted_actions, stopped_early)``.
    """
    import asyncio

    hosted: list[HostedAction] = []
    stopped = False
    for market_state in tape:
        if stop.is_set():
            stopped = True
            break
        if clock is not None:
            clock.set(market_state.ts)
        action = await agent.decide(market_state)
        config_hash = agent.config_hash(market_state) if agent.config_hash is not None else ""
        hosted.append(
            HostedAction(
                tick_seq=market_state.tick_seq,
                ts=market_state.ts,
                snapshot_hash=_snapshot_hash(market_state),
                action=action,
                session_id=ctx.session_id,
                run_id=ctx.run_id,
                runtime_agent_id=ctx.runtime_agent_id,
                config_hash=config_hash,
            )
        )
        await asyncio.sleep(0)  # cooperative: lets an owner cancel trip the StopSignal mid-loop
    return hosted, stopped


def _replay_context(runtime_agent_id: str) -> RunContext:
    """A synthetic RunContext for a LOCAL replay (never a hosted run; provenance is compared by action)."""
    return RunContext(
        run_id="replay", session_id="replay", runtime_agent_id=runtime_agent_id, owner_did="", input=None
    )


def _build_result(
    ctx: RunContext,
    agent_id: str,
    hosted: list[HostedAction],
    stopped: bool,
    transcript: tuple[TranscriptEntry, ...],
) -> DirectionalRunResult:
    """Assemble the canonical :class:`DirectionalRunResult` from a driven tape."""
    scored = tuple(h for h in hosted if h.action.type is not SportsActionType.WAIT)
    config_hash = hosted[0].config_hash if hosted else ""
    return DirectionalRunResult(
        run_id=ctx.run_id,
        session_id=ctx.session_id,
        runtime_agent_id=ctx.runtime_agent_id,
        owner_did=ctx.owner_did,
        agent_id=agent_id,
        config_hash=config_hash,
        actions=tuple(hosted),
        scored_actions=scored,
        sealed_transcript=transcript,
        stopped_early=stopped,
        terminal_reason="stopped" if stopped else "completed",
    )


# ---------------------------------------------------------------------------
# Deterministic adapter — Cumulative-Drift / admitted Sharp-Momentum
# ---------------------------------------------------------------------------


class VeridexDeterministicAgentAdapter(VeridexAgentAdapter):
    """AgentOS-hosted adapter for a DETERMINISTIC directional contestant (Veridex-owned).

    Hosts the II-7-projector-consuming Cumulative-Drift agent and the admitted Sharp-Momentum agent.
    Because the agent is a pure function of the tape, the hosted action sequence is BYTE-IDENTICAL to
    :meth:`replay` on the same tape — the deterministic-equivalence control.

    Construct with ZERO side effects (AC-26): only the ``agent_factory`` + ``tape_resolver`` are
    wired; no run starts and no external resource is touched.
    """

    def __init__(
        self,
        *,
        agent_factory: AgentFactory,
        tape_resolver: TapeResolver,
        name: str = "veridex-cumulative-drift",
        id: str = "veridex-cumulative-drift",
        event_sink: RuntimeEventSink | None = None,
    ) -> None:
        """Wire the adapter. NO run is started and NO external resource is touched here (AC-26).

        Args:
            agent_factory: Builds a FRESH deterministic :class:`Agent` per run (own detector state).
            tape_resolver: Resolves the run's tape from the SERVER-owned :class:`RunContext`.
            name / id: The AgentOS agent name/id (the runtime_agent_id the run attaches under).
            event_sink: Optional default OPS ``RuntimeEvent`` sink for runs that do not supply one.
        """
        super().__init__(run_driver=self._directional_driver, name=name, id=id, event_sink=event_sink)
        self._agent_factory = agent_factory
        self._tape_resolver = tape_resolver

    async def _directional_driver(
        self, ctx: RunContext, stop: StopSignal, event_sink: RuntimeEventSink
    ) -> DirectionalRunResult:
        """The injected :data:`RunDriver`: build a fresh agent + drive it over the resolved tape."""
        agent = self._agent_factory()
        hosted, stopped = await _drive_agent_over_tape(agent, self._tape_resolver(ctx), ctx, stop)
        return _build_result(ctx, self.get_id(), hosted, stopped, transcript=())

    async def replay(self, tape: list[MarketState]) -> tuple[HostedAction, ...]:
        """Re-drive a FRESH agent over ``tape`` locally — the byte-identical equivalence control.

        A pure local replay (no lease, no cancel machinery): builds a fresh agent from the SAME
        factory and drives it over the identical tape. Because the agent is deterministic, the
        returned actions are byte-identical to the hosted run's actions on that tape.
        """
        from veridex.mm_strategy.orchestration import StopSignal

        agent = self._agent_factory()
        hosted, _ = await _drive_agent_over_tape(agent, tape, _replay_context(self.get_id()), StopSignal())
        return tuple(hosted)


# ---------------------------------------------------------------------------
# LLM adapter — hosted run IS the model call; replay consumes the sealed transcript
# ---------------------------------------------------------------------------


class _TranscriptRecorder:
    """Accumulates raw model responses in launch/completion order — the sealed replay evidence."""

    def __init__(self) -> None:
        self._entries: list[TranscriptEntry] = []

    def record(self, prompt: str, raw: Any) -> None:
        self._entries.append(TranscriptEntry(prompt=prompt, raw=raw))

    def sealed(self) -> tuple[TranscriptEntry, ...]:
        return tuple(self._entries)


class _RecordingHandle:
    """Wraps a physical :class:`CallHandle`, sealing its result into the transcript exactly once.

    The checkpoint guard reads ``result()`` once per cleanly-completed call (fresh OR stale), so
    recording there captures every physical model response as canonical evidence — with no change to
    the guard's timing behaviour.
    """

    def __init__(self, inner: CallHandle, prompt: str, recorder: _TranscriptRecorder) -> None:
        self._inner = inner
        self._prompt = prompt
        self._recorder = recorder
        self._recorded = False

    def done(self) -> bool:
        return self._inner.done()

    def cancel(self) -> None:
        self._inner.cancel()

    def cancelled(self) -> bool:
        return self._inner.cancelled()

    def exception(self) -> BaseException | None:
        return self._inner.exception()

    def result(self) -> Any:
        raw = self._inner.result()
        if not self._recorded:
            self._recorder.record(self._prompt, raw)
            self._recorded = True
        return raw


class _RecordingLauncher:
    """Wraps the base :class:`ModelLauncher`, sealing each physical call's response (the hosted run)."""

    def __init__(self, base: ModelLauncher, recorder: _TranscriptRecorder) -> None:
        self._base = base
        self._recorder = recorder

    def launch(self, prompt: str) -> CallHandle:
        return _RecordingHandle(self._base.launch(prompt), prompt, self._recorder)


class _ReplayedHandle:
    """An already-done handle that returns one SEALED transcript response (no physical call)."""

    def __init__(self, raw: Any) -> None:
        self._raw = raw

    def done(self) -> bool:
        return True

    def cancel(self) -> None:
        pass

    def cancelled(self) -> bool:
        return False

    def exception(self) -> BaseException | None:
        return None

    def result(self) -> Any:
        return self._raw


class SealedEvidenceMismatch(BaseException):
    """A sealed transcript was replayed against evidence it was NOT sealed on — FAIL CLOSED.

    Deliberately a :class:`BaseException` (not :class:`Exception`): the LLM checkpoint runner's
    ``step`` degrades any launch ``Exception`` to a fail-closed WAIT, which would silently LAUNDER an
    evidence-integrity violation into an abstention. A ``BaseException`` bypasses that ``except
    Exception`` and propagates out of :meth:`VeridexLLMAgentAdapter.replay`, so a transcript can only
    ever reproduce against its ORIGINATING evidence — a mismatch (or an unconsumed/missing entry) hard
    fails rather than emitting a stale response against the wrong snapshot.
    """


class _ReplayingLauncher:
    """Serves the SEALED transcript in launch order — NEVER invokes the underlying model.

    Because the sealed run has a single physical call in flight at a time (the ``InflightGuard``
    one-in-flight invariant), launch order equals completion order, so consuming the transcript as an
    ordered queue reproduces the canonical response for each checkpoint without any model call.

    SEALED-EVIDENCE INTEGRITY (II-8b): each launch's ``prompt`` — the deterministic evidence coordinate
    the model was sealed on (it embeds the snapshot + ``evidence_hash``) — is compared to the sealed
    entry's stored prompt. ANY divergence raises :class:`SealedEvidenceMismatch` (fail closed), and a
    replay that does not consume EXACTLY the sealed entries (an over-run, or leftover entries at
    completion) fails closed too — so a transcript is bound to, and consumed exactly against, the
    evidence it originated from.
    """

    def __init__(self, transcript: tuple[TranscriptEntry, ...]) -> None:
        self._queue = list(transcript)
        self._index = 0

    def launch(self, prompt: str) -> CallHandle:
        if self._index >= len(self._queue):
            raise SealedEvidenceMismatch(
                "replay over-ran the sealed transcript: a checkpoint has NO sealed entry (missing evidence)"
            )
        entry = self._queue[self._index]
        if prompt != entry.prompt:
            raise SealedEvidenceMismatch(
                "replay evidence mismatch: this checkpoint's prompt/evidence coordinate differs from the "
                "sealed entry — the transcript was not sealed on this tape's snapshot"
            )
        self._index += 1
        return _ReplayedHandle(entry.raw)

    def assert_fully_consumed(self) -> None:
        """At completion, every sealed entry must have been consumed exactly (no leftovers)."""
        if self._index != len(self._queue):
            raise SealedEvidenceMismatch(
                f"replay left {len(self._queue) - self._index} sealed transcript entr(y/ies) unconsumed — "
                "the tape did not replay the exact sealed evidence sequence"
            )


class VeridexLLMAgentAdapter(VeridexAgentAdapter):
    """AgentOS-hosted adapter for the LLM-Drift contestant (Veridex-owned).

    The HOSTED run IS the model call: II-8's injectable ``ModelLauncher`` seam is wrapped in a
    RECORDING launcher that seals every raw response into a transcript. :meth:`replay` rebuilds the
    agent with a REPLAYING launcher over that sealed transcript and NEVER re-invokes the model.

    Construct with ZERO side effects (AC-26): only the ``agent_builder`` + ``base_launcher`` +
    ``tape_resolver`` are wired; no model is launched and no run starts.
    """

    def __init__(
        self,
        *,
        agent_builder: LLMAgentBuilder,
        base_launcher: ModelLauncher,
        tape_resolver: TapeResolver,
        name: str = "veridex-llm-drift",
        id: str = "veridex-llm-drift",
        event_sink: RuntimeEventSink | None = None,
    ) -> None:
        """Wire the adapter. NO model call and NO run are made here (AC-26).

        Args:
            agent_builder: ``builder(model, clock) -> Agent`` — builds the LLM-Drift agent bound to
                an injected launcher + deterministic clock (so hosted + replay are reproducible).
            base_launcher: The physical model launcher (production Agno; a fake offline). Wrapped for
                recording during a hosted run; NEVER touched during a replay.
            tape_resolver: Resolves the run's tape from the SERVER-owned :class:`RunContext`.
            name / id: The AgentOS agent name/id.
            event_sink: Optional default OPS ``RuntimeEvent`` sink.
        """
        super().__init__(run_driver=self._directional_driver, name=name, id=id, event_sink=event_sink)
        self._agent_builder = agent_builder
        self._base_launcher = base_launcher
        self._tape_resolver = tape_resolver

    async def _directional_driver(
        self, ctx: RunContext, stop: StopSignal, event_sink: RuntimeEventSink
    ) -> DirectionalRunResult:
        """The injected :data:`RunDriver`: drive the LLM agent over the tape, sealing the transcript."""
        recorder = _TranscriptRecorder()
        launcher = _RecordingLauncher(self._base_launcher, recorder)
        clock = _MutableClock()
        agent = self._agent_builder(launcher, clock)
        hosted, stopped = await _drive_agent_over_tape(
            agent, self._tape_resolver(ctx), ctx, stop, clock=clock
        )
        return _build_result(ctx, self.get_id(), hosted, stopped, transcript=recorder.sealed())

    async def replay(
        self, tape: list[MarketState], sealed_transcript: tuple[TranscriptEntry, ...]
    ) -> tuple[HostedAction, ...]:
        """Re-drive the LLM agent over ``tape`` from the SEALED transcript — NO model re-invocation.

        Rebuilds the agent with a REPLAYING launcher (the ``base_launcher`` is never touched) and the
        SAME tape-driven clock, so the checkpoint machine makes identical accept/drop decisions and
        the canonical hosted action sequence is reproduced byte-for-byte.
        """
        from veridex.mm_strategy.orchestration import StopSignal

        launcher = _ReplayingLauncher(sealed_transcript)
        clock = _MutableClock()
        agent = self._agent_builder(launcher, clock)
        hosted, _ = await _drive_agent_over_tape(
            agent, tape, _replay_context(self.get_id()), StopSignal(), clock=clock
        )
        # Fail closed if the tape did not consume EXACTLY the sealed entries (leftover/missing evidence).
        launcher.assert_fully_consumed()
        return tuple(hosted)
