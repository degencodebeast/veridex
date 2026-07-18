"""II-2 — the replay/dry-run market-maker COMPOSITION ROOT (the Studio→R4-B→R4-A→receipt chain).

This is the continuous deterministic maker loop that ties the whole Gate-II runtime together WITHOUT
touching a byte of the money core (A6) or II-1's frozen bridge. It composes, in one place:

    pinned tape
        -> run_cadence  (E3-T4 fold: the deterministic observation stream)
        -> InProcessRuntime.decide  (R4-B: the pure policy behind the neutral runtime seam, + OPS)
        -> execute_plan_bridged  (II-1: the async bridge onto the UNCHANGED sync execute_plan)
             with DRY-RUN FacadeDeps  (a recording-fake async proposer + local/offline deps — no wire)
        -> receipts + durable OPS events (R4-A Agent-Ops telemetry) + durable freeze audit

Load-bearing trust properties (each proven by a test in ``tests/test_mm_composition.py``):

  * **Deterministic (RED#2).** The same pinned tape folded twice yields the byte-identical decision /
    receipt sequence — the loop reads a pinned tape, a pure ``decide``, and a deterministic dry-run
    proposer; nothing consults a clock/RNG on the policy path.
  * **Framework-free core (RED#1b / AC-24).** This module and the ``InProcessRuntime`` decision path
    import NO ``agno`` / AgentOS symbol (a static AST import audit) — the deterministic core stays
    neutral; a framework host wraps this loop from OUTSIDE (II-4), never from within.
  * **Fail-closed mode router (RED#4, wiring req 7).** Only ``replay`` / ``replay_dry_run`` are
    accepted; ANY live / live-guarded mode is rejected with an EXPLICIT reason BEFORE any work — a
    possibly-live order can never be placed from the replay loop.
  * **Stop halts promptly (RED#3).** ``stop.set()`` halts the loop before the next placement and emits
    a TERMINAL OPS event; the already-in-flight leg (if any) resolves through II-1's honest bridge
    semantics — an order is never un-sent, only never freshly re-placed.
  * **Freeze halts fresh placements.** Any bridge ``FreezeRecord`` (a possibly-live escape) halts fresh
    placements, is PERSISTED through the session-owned ``freeze_sink`` (never swallowed), and ends the
    loop — the replay maker never keeps placing after a possibly-live freeze.

The II-1 integration contract (fu-ii1-composition-asserts): the SAME session-layer ``manifest`` +
``envelope`` are wired into BOTH ``execute_plan_bridged``'s params AND ``FacadeDeps`` — asserted by
identity at the composition site so a mismatch is a localized error, never a downstream fail-closed
surprise. ``FacadeDeps`` is bound from the SESSION layer (:class:`MakerInstanceConfig`), never the
decision/request path (the Gate-4 forgeable-pin invariant).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from veridex.mm_strategy.inventory_stub import synthetic_inventory, synthetic_inventory_event
from veridex.mm_strategy.orchestration import (
    BridgedPlanResult,
    FacadeDeps,
    FreezeRecord,
    StopSignal,
    execute_plan_bridged,
)
from veridex.mm_strategy.runtime import DEFAULT_AGENT_ID, InProcessRuntime
from veridex.runtime.runtime_events import (
    RuntimeEvent,
    RuntimeEventSink,
    RuntimeEventType,
    runtime_event,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from veridex.dust_execution.manifest import StrategyExperimentManifest
    from veridex.live_recorder.contracts import LiveRecorderSessionMeta
    from veridex.mm_strategy.config import StrategyConfig
    from veridex.mm_strategy.contracts import (
        StrategyDecision,
        StrategyObservation,
        StrategyState,
    )
    from veridex.mm_strategy.execution_adapter import R4ARequestConfig
    from veridex.mm_strategy.runtime import DecideFn
    from veridex.policy.envelope import PolicyEnvelope

__all__ = [
    "MakerInstanceConfig",
    "ModeRejectedError",
    "SessionSummary",
    "run_market_maker",
]

#: OPS-boundary logger for CONTAINED sink failures on the composition's OWN emissions (distinct from
#: the runtime seam's logger). Composition OPS telemetry is best-effort: a throwing sink can never
#: gate the deterministic loop (mirrors ``InProcessRuntime._emit``).
logger = logging.getLogger(__name__)

#: The modes the replay/dry-run composition root ACCEPTS. Both are OFFLINE; neither can place a live
#: order. ``replay`` folds+decides (a pure decision replay, no execution); ``replay_dry_run`` ALSO
#: drives the dry-run bridge so receipts are produced.
_ACCEPTED_MODES: frozenset[str] = frozenset({"replay", "replay_dry_run"})

#: The subset of accepted modes that actually drive the dry-run bridge (produce receipts).
_EXECUTING_MODES: frozenset[str] = frozenset({"replay_dry_run"})


class ModeRejectedError(ValueError):
    """Raised when a mode outside :data:`_ACCEPTED_MODES` is requested (wiring req 7, fail-closed).

    A ``ValueError`` subclass so callers can catch either. The message names the requested mode and the
    permitted set explicitly — the rejection is never a silent no-op.
    """


@dataclass(frozen=True)
class MakerInstanceConfig:
    """The SESSION-layer authority bundle the composition root binds every run from (control #6).

    Every field here is SESSION/composition-owned — NEVER sourced from a decision or request path — so
    the Gate-4 caller-forgeable-pin hole stays closed. ``manifest`` / ``envelope`` MUST be the SAME
    objects carried on ``facade_deps`` (asserted by identity in :func:`run_market_maker`).

    Attributes:
        strategy_config: The pinned, hash-bound arm config (its ``guard_enabled`` is cross-checked
            against the ``guard_enabled`` run argument — arm drift fails closed).
        request_config: The pinned R4-A request config the adapter DECLARES every leg under.
        manifest / envelope: The session-supplied admitted authorities threaded into BOTH
            ``execute_plan_bridged`` and ``facade_deps`` (the same objects — identity-asserted).
        facade_deps: The frozen dry-run dependency bundle + injectable recording-fake proposer.
        seed_state: The warm ``StrategyState`` the fold starts from (a live session mid-run).
        session_meta_factory: Builds the durable recorder session meta from the pinned tape.
        session_dir: The durable recorder session directory (the replay tape is sealed here).
        session_ended_ts: The wall-clock the recorder session is finalized at (durable seal bound).
        timeout_s: The finite, strictly-positive per-leg bridge wait bound (II-1 validates it).
        agent_id / run_id / session_id: OPS telemetry identity/correlation (never a policy input).
        decide_fn: Optional black-box decision seam override (defaults to the pure core ``decide``).
    """

    strategy_config: StrategyConfig
    request_config: R4ARequestConfig
    manifest: StrategyExperimentManifest
    envelope: PolicyEnvelope
    facade_deps: FacadeDeps
    seed_state: StrategyState
    session_meta_factory: Callable[[Any], LiveRecorderSessionMeta]
    session_dir: Path
    session_ended_ts: int = 10_000_000
    timeout_s: float = 5.0
    agent_id: str = DEFAULT_AGENT_ID
    run_id: str | None = None
    session_id: str = ""
    decide_fn: DecideFn | None = None


@dataclass(frozen=True)
class SessionSummary:
    """The captured artifacts of ONE replay/dry-run maker session (the composition's return value).

    Attributes:
        mode / guard_enabled: The run's mode and ablation arm.
        decisions: The ordered decisions the fold produced (one per consumed observation).
        receipts: The ordered bridged plan results (one per decision in an executing mode; empty in a
            pure ``replay``).
        freezes: The bridge freeze records encountered (each halts fresh placements; each persisted).
        terminal_reason: Why the loop ended — ``"completed"`` / ``"stopped"`` / ``"frozen"``.
        observations_consumed: How many observations the loop consumed before terminating.
        ops_events_emitted: How many OPS ``RuntimeEvent``s were handed to the sink (best-effort count).
    """

    mode: str
    guard_enabled: bool
    decisions: tuple[StrategyDecision, ...]
    receipts: tuple[BridgedPlanResult, ...]
    freezes: tuple[FreezeRecord, ...]
    terminal_reason: str
    observations_consumed: int
    ops_events_emitted: int

    def digest(self) -> str:
        """A byte-deterministic digest of the decision + receipt sequence (the determinism artifact).

        Excludes the non-deterministic bridge ``instrument`` (thread ids / timing counters) so the same
        pinned tape run twice produces an IDENTICAL digest (RED#2). Built from the pydantic model dumps
        of the decisions and each leg's request/result plus the freeze classifications.
        """
        payload = {
            "mode": self.mode,
            "guard_enabled": self.guard_enabled,
            "terminal_reason": self.terminal_reason,
            "decisions": [d.model_dump() for d in self.decisions],
            "receipts": [_receipt_signature(r) for r in self.receipts],
            "freezes": [(f.reason, f.possibly_live, f.error_repr) for f in self.freezes],
        }
        canonical = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class _CountingSink:
    """Wraps an OPS sink to count handed-off events while forwarding each to the inner sink."""

    inner: RuntimeEventSink
    count: int = 0

    def __call__(self, event: RuntimeEvent) -> None:
        self.count += 1
        self.inner(event)


def _receipt_signature(result: BridgedPlanResult) -> dict[str, Any]:
    """The DETERMINISTIC projection of a bridged result (excludes the non-deterministic instrument)."""
    plan: list[dict[str, Any]] | None = None
    if result.plan is not None:
        plan = [
            {
                "leg": outcome.leg.model_dump(),
                "attempted": outcome.attempted,
                "result": outcome.result.model_dump() if outcome.result is not None else None,
            }
            for outcome in result.plan.outcomes
        ]
    escaped: dict[str, Any] | None = None
    if result.escaped is not None:
        escaped = {"reason": result.escaped.reason, "possibly_live": result.escaped.possibly_live}
    return {"plan": plan, "escaped": escaped}


def _validate_mode(mode: str) -> None:
    """Fail-closed mode router (wiring req 7): reject any non-replay/dry-run mode with an EXPLICIT reason.

    Rejects ``live`` / ``live_guarded`` / ``replay+live_guarded`` / any unknown mode BEFORE any fold,
    decision, or placement — a possibly-live order must never be placed from the replay composition
    root. Raising (not returning) makes the rejection impossible to swallow.
    """
    if mode not in _ACCEPTED_MODES:
        raise ModeRejectedError(
            f"run_market_maker: mode {mode!r} is not a permitted replay/dry-run mode. This is the "
            "OFFLINE replay/dry-run composition root; it REFUSES any live or live-guarded execution "
            "path fail-closed (wiring req 7 invalid-combination rule) — a possibly-live order must "
            f"never be placed from the replay loop. Permitted modes: {sorted(_ACCEPTED_MODES)}."
        )


def _emit_ops(sink: RuntimeEventSink, event: RuntimeEvent) -> None:
    """Best-effort OPS emission for the composition's OWN events (mirrors ``InProcessRuntime._emit``).

    A throwing telemetry sink is CONTAINED here so it can never abort the deterministic loop; the
    failure is reported on the module logger, never re-raised. (The SESSION-owned ``freeze_sink`` is
    DELIBERATELY not routed through here — a freeze is safety-critical and must NOT be swallowed.)
    """
    try:
        sink(event)
    except Exception:
        logger.warning(
            "composition OPS telemetry sink raised on emit; dropping event (best-effort)",
            exc_info=True,
        )


def _receipt_event(
    result: BridgedPlanResult,
    decision: StrategyDecision,
    *,
    agent_id: str,
    run_id: str | None,
    session_id: str | None,
) -> RuntimeEvent:
    """Durably record ONE bridged decision's per-leg admission/execution/reconciliation on the OPS channel.

    This is the I-4 durable Agent-Ops path for the dry-run proposer (which, being a recording fake,
    emits nothing itself): the composition projects each leg's disposition into telemetry so an
    admission/execution/reconciliation is recorded, never dropped.
    """
    legs: list[dict[str, Any]] = []
    if result.plan is not None:
        for outcome in result.plan.outcomes:
            legs.append(
                {
                    "kind": outcome.leg.kind,
                    "attempted": outcome.attempted,
                    "admission": outcome.result.admission if outcome.result is not None else None,
                    "execution_status": (
                        outcome.result.execution_status if outcome.result is not None else None
                    ),
                    "frozen": outcome.frozen,
                    "possibly_unresolved": outcome.possibly_unresolved,
                }
            )
    return runtime_event(
        RuntimeEventType.TOOL_CALL,
        agent_id=agent_id,
        run_id=run_id,
        session_id=session_id,
        telemetry="leg_receipt",
        decision_kind=decision.kind,
        plan_frozen=result.plan.frozen if result.plan is not None else None,
        awaiting_reconciliation=(
            result.plan.awaiting_reconciliation if result.plan is not None else None
        ),
        escaped_reason=result.escaped.reason if result.escaped is not None else None,
        escaped_possibly_live=(
            result.escaped.possibly_live if result.escaped is not None else None
        ),
        legs=legs,
    )


def _terminal_event(
    terminal_reason: str,
    *,
    agent_id: str,
    run_id: str | None,
    session_id: str | None,
    decisions: int,
    receipts: int,
    freezes: int,
) -> RuntimeEvent:
    """The single TERMINAL OPS event marking loop end (RED#3). ``terminal=True`` makes it findable.

    A clean/stopped run is ``RUN_COMPLETED``; a freeze halt is ``RUN_FAILED`` (the loop did NOT complete
    cleanly — a possibly-live order froze fresh placements).
    """
    event_type = (
        RuntimeEventType.RUN_FAILED if terminal_reason == "frozen" else RuntimeEventType.RUN_COMPLETED
    )
    return runtime_event(
        event_type,
        agent_id=agent_id,
        run_id=run_id,
        session_id=session_id,
        terminal=True,
        terminal_reason=terminal_reason,
        decisions=decisions,
        receipts=receipts,
        freezes=freezes,
    )


async def run_market_maker(
    instance_cfg: MakerInstanceConfig,
    tape: Any,
    *,
    mode: str,
    guard_enabled: bool,
    event_sink: RuntimeEventSink,
    stop: StopSignal,
) -> SessionSummary:
    """Run ONE replay/dry-run maker session: fold a pinned tape → decide → dry-run bridge → receipts + OPS.

    The continuous deterministic loop (see the module docstring for the trust properties). Fail-closed
    at every boundary: the mode router rejects live modes BEFORE any work; the session-layer
    manifest/envelope identity is asserted against ``facade_deps``; a ``stop`` halts fresh placements
    promptly; any bridge freeze halts fresh placements and is durably persisted.

    Args:
        instance_cfg: The SESSION-layer authority bundle (control #6 — never a decision/request path).
        tape: The pinned replay tape; only ``tape.events`` is folded here, and the whole tape is passed
            to ``instance_cfg.session_meta_factory`` to build the durable recorder session meta.
        mode: One of ``replay`` / ``replay_dry_run`` (any live mode is rejected fail-closed).
        guard_enabled: The ablation arm; cross-checked against ``strategy_config.guard_enabled``.
        event_sink: The R4-A OPS ``RuntimeEvent`` sink (durably persists lifecycle/decision/receipt/
            inventory/terminal telemetry).
        stop: The shared kill signal (one kill, two consumers — II-1's ``StopSignal``).

    Returns:
        A :class:`SessionSummary` capturing the decisions, receipts, freezes, and terminal reason.

    Raises:
        ModeRejectedError: ``mode`` is not a permitted replay/dry-run mode (fail-closed, before work).
        ValueError: the session-layer manifest/envelope are not the SAME objects on ``facade_deps``, or
            the ``guard_enabled`` arm disagrees with ``strategy_config.guard_enabled``.
    """
    # (1) Mode router — fail closed BEFORE any fold, decision, or placement (wiring req 7).
    _validate_mode(mode)

    facade_deps = instance_cfg.facade_deps
    manifest = instance_cfg.manifest
    envelope = instance_cfg.envelope

    # (2) II-1 integration contract: the SAME session-layer manifest + envelope must be wired into BOTH
    # execute_plan_bridged's params AND FacadeDeps. Assert by IDENTITY at the composition site so a
    # mismatch is an immediate localized error, never a downstream fail-closed surprise.
    if manifest is not facade_deps.manifest:
        raise ValueError(
            "run_market_maker: the session-layer manifest is not the SAME object as "
            "facade_deps.manifest — the core pin cross-check and the proposer cross-check MUST bind the "
            "identical admitted authority (II-1 fu-ii1-composition-asserts). Bind both from the session "
            "layer (MakerInstanceConfig), never the decision/request path (Gate-4 forgeable-pin)."
        )
    if envelope is not facade_deps.envelope:
        raise ValueError(
            "run_market_maker: the session-layer envelope is not the SAME object as "
            "facade_deps.envelope — bind the identical admitted policy authority into BOTH the bridge "
            "params and FacadeDeps (II-1 fu-ii1-composition-asserts)."
        )

    config = instance_cfg.strategy_config
    if config.guard_enabled != guard_enabled:
        raise ValueError(
            f"run_market_maker: guard_enabled arg ({guard_enabled!r}) disagrees with "
            f"strategy_config.guard_enabled ({config.guard_enabled!r}) — the ablation arm must be "
            "pinned by the session config, never a divergent run argument (fail closed)."
        )

    executing = mode in _EXECUTING_MODES
    agent_id = instance_cfg.agent_id
    run_id = instance_cfg.run_id
    session_id = instance_cfg.session_id
    session_id_or_none = session_id or None

    # (3) Fold the pinned tape into the deterministic observation stream (the run_cadence idiom). Import
    # the recorder/assembler LOCALLY so this module's TOP-LEVEL import surface stays minimal for the
    # AC-24 audit (the deterministic decision path imports no framework).
    from veridex.live_recorder.recorder import LiveRecorder
    from veridex.mm_strategy.assembler import run_cadence

    recorder = LiveRecorder(instance_cfg.session_dir, instance_cfg.session_meta_factory(tape))
    cadence = run_cadence(recorder, tape.events, guard_enabled=guard_enabled)
    recorder.finalize(ended_ts=instance_cfg.session_ended_ts)
    recorder.close()
    observations: tuple[StrategyObservation, ...] = cadence.observations

    # (4) The neutral runtime seam wired to the DURABLE OPS sink — decision telemetry persists here.
    counted = _CountingSink(event_sink)
    runtime = InProcessRuntime(sink=counted, agent_id=agent_id, decide_fn=instance_cfg.decide_fn)
    runtime.run_started(run_id=run_id)

    state = instance_cfg.seed_state
    decisions: list[StrategyDecision] = []
    receipts: list[BridgedPlanResult] = []
    freezes: list[FreezeRecord] = []
    terminal_reason = "completed"
    consumed = 0

    for observation in observations:
        # Stop check FIRST (loop-side): a set kill halts BEFORE the next decision/placement, so no fresh
        # placement follows a stop. The threading side is the thread-safe view (II-1's StopSignal).
        if stop.is_set():
            terminal_reason = "stopped"
            break

        consumed += 1
        decision, state = runtime.decide(
            observation, state, config, session_id=session_id, run_id=run_id
        )
        decisions.append(decision)

        # Synthetic inventory OPS event — ALWAYS labeled synthetic (A5: this is a dry-run stub, never
        # reconciled venue truth). The stub does NOT feed the policy path (determinism-safe).
        _emit_ops(
            counted,
            synthetic_inventory_event(
                synthetic_inventory(as_of_ts=observation.as_of_ts),
                agent_id=agent_id,
                run_id=run_id,
                session_id=session_id_or_none,
            ),
        )

        if not executing:
            # ``replay`` mode: a pure decision replay — no dry-run bridge, no receipts.
            continue

        # Drive II-1's async bridge onto the UNCHANGED sync execute_plan with the DRY-RUN FacadeDeps.
        # The SAME session-layer manifest + envelope are threaded here AND on facade_deps (asserted
        # above), never the decision/request path.
        result = await execute_plan_bridged(
            decision,
            observation=observation,
            config=instance_cfg.request_config,
            manifest=manifest,
            envelope=envelope,
            strategy_config=config,
            facade_deps=facade_deps,
            timeout_s=instance_cfg.timeout_s,
            stop=stop,
        )
        receipts.append(result)
        _emit_ops(
            counted,
            _receipt_event(
                result,
                decision,
                agent_id=agent_id,
                run_id=run_id,
                session_id=session_id_or_none,
            ),
        )

        if result.escaped is not None:
            # A bridge escape halts fresh placements. PERSIST it durably through the SESSION-owned
            # freeze_sink (safety-critical — NOT best-effort, never swallowed).
            freeze = result.escaped
            freezes.append(freeze)
            facade_deps.freeze_sink.emit(freeze)
            # aborted_by_kill is PROVEN un-sent (a stop, not a freeze); any possibly-live escape freezes.
            terminal_reason = "frozen" if freeze.possibly_live else "stopped"
            break

        if result.plan is not None and result.plan.frozen:
            # A possibly-live leg froze the plan (SUBMITTED / pending). Halt fresh placements — the
            # replay maker never keeps placing after a possibly-live freeze (freeze semantics).
            terminal_reason = "frozen"
            break

    # (5) The single TERMINAL OPS event (RED#3) — always emitted, regardless of how the loop ended.
    _emit_ops(
        counted,
        _terminal_event(
            terminal_reason,
            agent_id=agent_id,
            run_id=run_id,
            session_id=session_id_or_none,
            decisions=len(decisions),
            receipts=len(receipts),
            freezes=len(freezes),
        ),
    )

    return SessionSummary(
        mode=mode,
        guard_enabled=guard_enabled,
        decisions=tuple(decisions),
        receipts=tuple(receipts),
        freezes=tuple(freezes),
        terminal_reason=terminal_reason,
        observations_consumed=consumed,
        ops_events_emitted=counted.count,
    )
