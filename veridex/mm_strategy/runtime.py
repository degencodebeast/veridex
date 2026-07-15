"""Runtime-neutral ``InProcessRuntime`` seam for the pure MM strategy (REQ-120/121/122, SEC-002/003).

This is the thin, provider-neutral runtime seam the E5-T6 audit deferred: it wraps the pure
:func:`veridex.mm_strategy.core.decide` behind a lifecycle façade (``run_started`` /
``run_completed`` / ``run_failed`` / ``decide``) that mirrors the :class:`AgentRuntime` shape, WITHOUT
binding to any concrete runtime implementation.

Load-bearing trust properties (each proven by a test):

  * **Neutral by construction (REQ-120).** The seam calls ``decide()`` as a BLACK BOX. It imports
    NEITHER a concrete ``AgentRuntime`` impl (no ``AgnoRuntime``/BYOA/LLM SDK) NOR the pure core's
    internals (no ``_decide_raw`` / ``_classify_row`` / ``_accept``) — only the public ``decide``
    entry point + the frozen contract types. So ``decide()`` direct and ``decide()`` via the seam are
    byte-identical: the seam adds observability, never a policy delta (AC-034).
  * **Telemetry is OPS-only, non-authoritative (REQ-122).** After ``decide()`` returns, the seam
    projects the ALREADY-COMPUTED decision (kind, reason codes, the four causal hashes, the A/B arm
    id) into ONE :class:`~veridex.runtime.runtime_events.RuntimeEvent` on the OPS channel and hands it
    to an injected sink. A ``RuntimeEvent`` carries NO ``sequence_no`` / ``payload_hash`` / evidence
    flag (see its module docstring), so it is STRUCTURALLY non-evidence and can never be sealed,
    scored, or ranked. The projection is a read-only shadow of the output — it has no channel back
    into ``decide()``, so it cannot move policy.
  * **Reconstruction factory, zero side effects (REQ-121).** :func:`reconstruct` rebuilds the seam
    from a pinned ``(instance, config, state)`` triple emitting NOTHING (a SILENT ``sink=None`` seam),
    touching no clock/RNG/I-O, and mutating neither pinned input; :meth:`InProcessRuntime.replay`
    then reproduces a byte-identical decision from those pins.
  * **No agent tool surface (SEC-002).** The seam registers NO agent ``tools=[...]`` array — it
    proposes via ``decide()`` only, never an LLM-invokable tool that could reach a venue write / signer
    / facade primitive.

SEC-003 (runtime leg): this module imports NO raw venue write (``submit_order`` / ``cancel_order``),
NO signer / local-key crypto, and NO vendored CLOB client (``_vendor`` / ``polymarket_clob``) — nor
any concrete venue adapter (``veridex.venues.base`` / ``veridex.venues.sx_bet``). It is held to the
SAME import bar as the adapter/assembler by the ``_AGENT_FACING_MODULES`` audit in
``tests/test_dust_execution_sec_isolation.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from veridex.mm_strategy.config import StrategyConfig
from veridex.mm_strategy.contracts import (
    StrategyDecision,
    StrategyObservation,
    StrategyState,
)
from veridex.mm_strategy.core import decide as _pure_decide
from veridex.runtime.runtime_events import (
    RuntimeEvent,
    RuntimeEventSink,
    RuntimeEventType,
    RuntimeStatus,
    runtime_event,
)

#: The default agent identity a seam stamps its OPS telemetry with when none is supplied — the pinned
#: strategy id (matches ``StrategyConfig.strategy_id``). It is telemetry identity ONLY, never a policy
#: input.
DEFAULT_AGENT_ID = "venue-anchored-txline-guarded-maker"

#: The two A/B ablation arm ids the guard toggle selects (REQ-120). Projected as OPS telemetry only.
ArmId = Literal["guarded", "baseline"]

#: The black-box decision seam: ``(observation, state, config, *, session_id) -> (decision, state)``.
#: Typed against the PUBLIC ``decide`` signature so the seam can never bind a core internal.
DecideFn = Callable[..., tuple[StrategyDecision, StrategyState]]


def arm_id(config: StrategyConfig) -> ArmId:
    """The A/B ablation arm id — ``"guarded"`` iff ``config.guard_enabled`` else ``"baseline"``.

    A pure function of the frozen config; used ONLY to enrich OPS telemetry, never to steer policy.
    """
    return "guarded" if config.guard_enabled else "baseline"


def project_decision_telemetry(
    decision: StrategyDecision,
    config: StrategyConfig,
    *,
    agent_id: str,
    run_id: str | None = None,
    session_id: str | None = None,
) -> RuntimeEvent:
    """Project an ALREADY-COMPUTED decision into ONE OPS ``RuntimeEvent`` (non-authoritative).

    Reads the decision's own decided fields (kind, ordered reason codes, the four causal hashes) plus
    the config's ablation arm id and packs them into an ``ACTION_EMITTED`` OPS event. Pure and total:
    it constructs telemetry FROM the decision — there is no path from the returned event back into the
    decision, so this projection can never affect policy (REQ-122). The event is structurally
    non-evidence (no ``sequence_no`` / ``payload_hash`` / evidence flag exist on the model).
    """
    return runtime_event(
        RuntimeEventType.ACTION_EMITTED,
        agent_id=agent_id,
        run_id=run_id,
        session_id=session_id,
        decision_kind=decision.kind,
        reason_codes=list(decision.reason_codes),
        observation_hash=decision.observation_hash,
        config_hash=decision.config_hash,
        prior_state_hash=decision.prior_state_hash,
        next_state_hash=decision.next_state_hash,
        arm=arm_id(config),
    )


class InProcessRuntime:
    """A thin neutral runtime seam wrapping the pure ``decide()`` behind a lifecycle façade.

    The seam is provider-neutral: it holds an injectable black-box ``decide_fn`` (defaulting to the
    public :func:`veridex.mm_strategy.core.decide`) and an optional OPS ``sink``. It exposes the
    :class:`AgentRuntime`-shaped lifecycle (``run_started`` / ``run_completed`` / ``run_failed``) plus
    :meth:`decide`, which runs the policy and projects the outcome into OPS telemetry — never the
    other way round.

    Args:
        sink: OPS-channel callback invoked with every emitted ``RuntimeEvent`` (``None`` ⇒ silent).
        agent_id: Telemetry identity stamped on emitted events (NOT a policy input).
        decide_fn: The black-box decision seam; defaults to the pure ``decide`` (injectable for the
            reconstruction factory and for tests). Called as a black box — never a core internal.
        pinned_config / pinned_state: Optional pins captured by :func:`reconstruct`; :meth:`replay`
            reproduces a decision from them. Left ``None`` for a live (non-reconstructed) seam.
    """

    def __init__(
        self,
        *,
        sink: RuntimeEventSink | None = None,
        agent_id: str = DEFAULT_AGENT_ID,
        decide_fn: DecideFn | None = None,
        pinned_config: StrategyConfig | None = None,
        pinned_state: StrategyState | None = None,
    ) -> None:
        self._sink = sink
        self._agent_id = agent_id
        self._decide_fn: DecideFn = decide_fn if decide_fn is not None else _pure_decide
        self._pinned_config = pinned_config
        self._pinned_state = pinned_state

    @property
    def agent_id(self) -> str:
        """The telemetry identity this seam stamps on emitted OPS events."""
        return self._agent_id

    def _emit(self, event: RuntimeEvent) -> None:
        """Best-effort OPS emission: hand the event to the sink, or drop it if none is wired.

        Emission is NON-load-bearing — a correct decision never depends on the sink (REQ-122).
        """
        if self._sink is not None:
            self._sink(event)

    def run_started(self, *, run_id: str | None = None) -> None:
        """Lifecycle façade: emit ``RUN_STARTED`` + ``STATUS_CHANGED(running)`` (OPS only)."""
        self._emit(runtime_event(RuntimeEventType.RUN_STARTED, agent_id=self._agent_id, run_id=run_id))
        self._emit(
            runtime_event(
                RuntimeEventType.STATUS_CHANGED,
                agent_id=self._agent_id,
                run_id=run_id,
                status=RuntimeStatus.RUNNING.value,
            )
        )

    def run_completed(self, *, run_id: str | None = None) -> None:
        """Lifecycle façade: emit ``RUN_COMPLETED`` + ``STATUS_CHANGED(completed)`` (OPS only)."""
        self._emit(runtime_event(RuntimeEventType.RUN_COMPLETED, agent_id=self._agent_id, run_id=run_id))
        self._emit(
            runtime_event(
                RuntimeEventType.STATUS_CHANGED,
                agent_id=self._agent_id,
                run_id=run_id,
                status=RuntimeStatus.COMPLETED.value,
            )
        )

    def run_failed(self, *, run_id: str | None = None, error: str) -> None:
        """Lifecycle façade: emit ``RUN_FAILED`` + ``STATUS_CHANGED(failed)`` (OPS only)."""
        self._emit(
            runtime_event(RuntimeEventType.RUN_FAILED, agent_id=self._agent_id, run_id=run_id, error=error)
        )
        self._emit(
            runtime_event(
                RuntimeEventType.STATUS_CHANGED,
                agent_id=self._agent_id,
                run_id=run_id,
                status=RuntimeStatus.FAILED.value,
            )
        )

    def decide(
        self,
        observation: StrategyObservation,
        state: StrategyState,
        config: StrategyConfig,
        *,
        session_id: str = "",
        run_id: str | None = None,
    ) -> tuple[StrategyDecision, StrategyState]:
        """Run the pure policy via the black-box seam, then project the outcome into OPS telemetry.

        The decision + next state are EXACTLY the black-box ``decide()`` output — returned unmodified,
        so the seam is byte-identical to a direct call (AC-034). Telemetry is emitted AFTER the
        decision is computed and is a read-only projection of it (REQ-122): it can never feed back
        into the returned decision.
        """
        decision, next_state = self._decide_fn(observation, state, config, session_id=session_id)
        # TERMINAL projection: build + emit OPS telemetry FROM the decided output. Deliberately the
        # last step and deliberately not consulted below — the returned tuple is the untouched policy
        # result. Wiring any telemetry value into `decision`/`next_state` would break the OPS-only /
        # parity guarantees (and the mutation tests that guard them).
        self._emit(
            project_decision_telemetry(
                decision, config, agent_id=self._agent_id, run_id=run_id, session_id=session_id or None
            )
        )
        return decision, next_state

    def replay(
        self,
        observation: StrategyObservation,
        *,
        session_id: str = "",
        run_id: str | None = None,
    ) -> tuple[StrategyDecision, StrategyState]:
        """Reproduce a decision from the pinned ``(config, state)`` captured by :func:`reconstruct`.

        Uses the reconstruction pins as the frozen reproduction context, so feeding the same
        observation replays the byte-identical decision (REQ-121). Raises if this seam was not built
        from pins (a live seam must call :meth:`decide` with explicit inputs).
        """
        if self._pinned_config is None or self._pinned_state is None:
            raise ValueError(
                "replay() requires a reconstructed seam pinned to (config, state); "
                "use reconstruct(instance, config, state) or call decide() with explicit inputs"
            )
        return self.decide(
            observation, self._pinned_state, self._pinned_config, session_id=session_id, run_id=run_id
        )


def reconstruct(
    instance: InProcessRuntime,
    config: StrategyConfig,
    state: StrategyState,
) -> InProcessRuntime:
    """Rebuild the seam pinned to ``(instance, config, state)`` with ZERO side effects (REQ-121).

    Returns a NEW ``InProcessRuntime`` carrying the SAME black-box ``decide_fn`` and agent identity as
    ``instance`` but pinned to the given ``config`` / ``state`` and wired to NO sink — so the
    reconstruction is SILENT (emits nothing), touches no clock/RNG/I-O, and mutates neither pinned
    input (both are frozen models). Feeding the reconstructed seam the same observation via
    :meth:`InProcessRuntime.replay` reproduces a byte-identical decision: the reproduction guarantee,
    not a re-run with fresh side effects.
    """
    return InProcessRuntime(
        sink=None,  # reconstruction is SILENT — no telemetry side effect (REQ-121)
        agent_id=instance.agent_id,
        decide_fn=instance._decide_fn,
        pinned_config=config,
        pinned_state=state,
    )
