"""II-2 — the replay/dry-run MM composition root suite (the Studio→R4-B→R4-A→receipt chain).

OFFLINE ONLY, ZERO wire primitives. Every test drives :func:`run_market_maker` against a RECORDING
FAKE async proposer (injected into :class:`FacadeDeps.proposer`) and POISON wire deps — proof the loop
folds a pinned tape into decisions, drives II-1's async bridge onto the UNCHANGED sync ``execute_plan``,
and produces receipts + durable OPS telemetry WITHOUT ever touching a venue adapter / signer / socket.

Reuses the E6/integration tape idiom (``tests/test_mm_strategy_integration.py``): ``load_tape`` →
``LiveRecorder`` → ``run_cadence`` folded from a WARM seed state so the lane genuinely quotes.

The RED set (each observed FAILING before the composition module existed):
  #1  pinned tape → ≥1 decision → EXACTLY the receipts the decisions imply (recording-fake proposer;
      no wire primitive touched).
  #1b AC-24 static import audit: the composition module + the ``InProcessRuntime`` decision path import
      NO ``agno`` / AgentOS symbol (AST scan; mirrors the SEC-002/003 guards).
  #2  determinism: the SAME tape run twice → an IDENTICAL decision/receipt sequence.
  #3  stop halts the loop: ``stop.set()`` → the loop halts + a TERMINAL OPS event, no fresh placement.
  #4  mode router fail-closed: ``replay+live_guarded`` (any live) → rejected with an explicit reason.
Plus the II-1 integration contract (same-session manifest/envelope identity assert + bound() shape) and
the freeze-halts-fresh-placements + synthetic-inventory-labeling invariants.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import importlib
import inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.mm_strategy_ablation_harness import (
    arm_configs,
    load_base_config_overrides,
    load_tape,
)
from tests.test_mm_strategy_adapter import (
    _pinned_config,
    _pinned_envelope,
    _pinned_manifest,
    _result,
)
from tests.test_mm_strategy_integration import _session_meta, _warm_seed_state
from veridex.api.maker_router import build_live_ab_projection, register_maker_routes
from veridex.dust_execution.facade import (
    MMExecutionToolRequest,
    MMExecutionToolResult,
    propose_mm_execution,
)
from veridex.mm_strategy.composition import (
    GUARD_ABLATION_LABEL,
    MakerInstanceConfig,
    ModeRejectedError,
    SessionSummary,
    run_guard_ablation,
    run_market_maker,
)
from veridex.mm_strategy.contracts import StrategyState
from veridex.mm_strategy.orchestration import FacadeDeps, StopSignal
from veridex.runtime.runtime_events import RuntimeEvent, RuntimeEventType

# ---------------------------------------------------------------------------------------------
# Offline doubles — a recording-fake async proposer, poison wire deps, list sinks.
# ---------------------------------------------------------------------------------------------


class _PoisonWire:
    """ANY attribute access = a wire primitive was touched. The recording proposer must NEVER reach it.

    Passed as the ``adapter`` / ``signer`` / ``sources`` on the dry-run ``FacadeDeps`` (they are handed
    to the proposer via ``bound()`` as opaque VALUES). Because the recording fake ignores them, the run
    completes without ever tripping ``__getattr__`` — executable evidence no wire primitive was used.
    """

    def __init__(self, label: str) -> None:
        self._label = label

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"wire primitive touched on {object.__getattribute__(self, '_label')!r}: {name!r}")


@dataclass
class _RecordingProposer:
    """OFFLINE recording-fake async proposer — records each typed request, returns a scripted result.

    NEVER a wire primitive: no network, no signer, no socket. It consumes the SAME typed
    :class:`MMExecutionToolRequest` the money core built + pin-cross-checked (never a raw intent) and
    records it, so ``calls`` is executable evidence of exactly the legs the decisions implied.
    """

    result_factory: Any = _result
    on_call: Any = None
    calls: list[MMExecutionToolRequest] = field(default_factory=list)

    async def __call__(
        self,
        request: MMExecutionToolRequest,
        *,
        adapter: Any,
        signer: Any,
        sources: Any,
        **kwargs: Any,
    ) -> MMExecutionToolResult:
        # adapter/signer/sources are bound but DELIBERATELY never touched (no wire primitive).
        self.calls.append(request)
        if self.on_call is not None:
            self.on_call(request)
        return self.result_factory()


@dataclass
class _ListFreezeSink:
    """The session-owned durable freeze sink — records every persisted :class:`FreezeRecord`."""

    records: list[Any] = field(default_factory=list)

    def emit(self, record: Any) -> None:
        self.records.append(record)


async def _no_sleep(_seconds: float) -> None:
    return None


def _make_deps(
    manifest: Any,
    envelope: Any,
    proposer: Any,
    freeze_sink: _ListFreezeSink,
) -> FacadeDeps:
    """Build the DRY-RUN FacadeDeps: poison wire deps + a recording-fake proposer + the SAME session
    manifest/envelope the composition threads into ``execute_plan_bridged`` (II-1 identity contract)."""
    return FacadeDeps(
        adapter=_PoisonWire("adapter"),
        signer=_PoisonWire("signer"),
        sources=_PoisonWire("sources"),
        now_fn=lambda: 1,
        sleep_fn=_no_sleep,
        envelope=envelope,
        manifest=manifest,
        wallet_equity_at_decision=1000.0,
        fixed_fraction=0.001,
        freeze_sink=freeze_sink,
        proposer=proposer,
    )


def _make_cfg(
    *,
    session_dir: Path,
    manifest: Any,
    envelope: Any,
    deps: FacadeDeps,
    arm: Any,
) -> MakerInstanceConfig:
    return MakerInstanceConfig(
        strategy_config=arm,
        request_config=_pinned_config(),
        manifest=manifest,
        envelope=envelope,
        facade_deps=deps,
        seed_state=_warm_seed_state(),
        session_meta_factory=lambda tape: _session_meta(arm, tape.identity.fixture_id),
        session_dir=session_dir,
        agent_id="mm-composition-test",
        run_id="run-1",
        session_id="sess-1",
    )


def _baseline_arm() -> Any:
    return arm_configs(load_base_config_overrides()).baseline


@dataclass
class _Harness:
    summary: SessionSummary
    proposer: _RecordingProposer
    freeze_sink: _ListFreezeSink
    events: list[RuntimeEvent]


def _run(
    *,
    session_dir: Path,
    mode: str = "replay_dry_run",
    tape_health: str = "healthy",
    proposer: _RecordingProposer | None = None,
    stop: StopSignal | None = None,
    pre_set_stop: bool = False,
) -> _Harness:
    """Drive ONE offline maker session end-to-end and capture its artifacts."""
    arm = _baseline_arm()
    manifest = _pinned_manifest()
    envelope = _pinned_envelope()
    proposer = proposer if proposer is not None else _RecordingProposer()
    freeze_sink = _ListFreezeSink()
    deps = _make_deps(manifest, envelope, proposer, freeze_sink)
    cfg = _make_cfg(session_dir=session_dir, manifest=manifest, envelope=envelope, deps=deps, arm=arm)
    tape = load_tape(tape_health)
    events: list[RuntimeEvent] = []
    stop = stop if stop is not None else StopSignal()
    if pre_set_stop:
        stop.set()
    summary = asyncio.run(
        run_market_maker(
            cfg,
            tape,
            mode=mode,
            guard_enabled=arm.guard_enabled,
            event_sink=events.append,
            stop=stop,
        )
    )
    return _Harness(summary=summary, proposer=proposer, freeze_sink=freeze_sink, events=events)


def _attempted_legs(summary: SessionSummary) -> int:
    return sum(
        1
        for receipt in summary.receipts
        if receipt.plan is not None
        for outcome in receipt.plan.outcomes
        if outcome.attempted
    )


# =====================================================================================
# RED #1 — pinned tape → decisions → EXACTLY the receipts the decisions imply (no wire).
# =====================================================================================


def test_pinned_tape_yields_decisions_and_exact_receipts_no_wire(tmp_path: Path) -> None:
    h = _run(session_dir=tmp_path / "sess")
    summary = h.summary

    # ≥1 decision, and the lane genuinely quotes (non-vacuous — a cold lane would make this trivial).
    assert len(summary.decisions) >= 1
    assert any(d.kind.startswith("QUOTE") for d in summary.decisions), "the lane must actually quote"

    # EXACTLY the receipts the decisions imply: one bridged receipt per decision, and the recording-fake
    # proposer was called EXACTLY once per attempted (actionable) leg — no more, no fewer.
    assert len(summary.receipts) == len(summary.decisions)
    attempted = _attempted_legs(summary)
    assert attempted >= 1, "the lane must attempt at least one actionable leg"
    assert len(h.proposer.calls) == attempted

    # ZERO wire primitives: the proposer only ever saw TYPED, pin-cross-checked requests (never a raw
    # intent), and the poison adapter/signer/sources were never touched (else the run would have raised).
    assert all(isinstance(c, MMExecutionToolRequest) for c in h.proposer.calls)

    # Clean completion + the durable OPS channel actually carried telemetry.
    assert summary.terminal_reason == "completed"
    assert summary.freezes == ()
    assert summary.ops_events_emitted > 0
    assert h.events, "the OPS event_sink must durably receive telemetry"


def test_every_synthetic_inventory_ops_event_is_labeled_synthetic(tmp_path: Path) -> None:
    """A5 honesty: EVERY inventory OPS event carries the SYNTHETIC label (it is a dry-run stub)."""
    h = _run(session_dir=tmp_path / "sess")
    inv_events = [
        e for e in h.events if e.payload.get("telemetry") == "synthetic_inventory_projection"
    ]
    assert inv_events, "the composition must emit synthetic inventory OPS events"
    for e in inv_events:
        assert e.payload.get("synthetic") is True
        assert e.payload.get("inventory_source") == "SYNTHETIC"
        assert e.payload.get("net_position") == 0.0  # deterministic FLAT projection


# =====================================================================================
# RED #1b — AC-24 static import audit: the composition + decision path is framework-free.
# =====================================================================================

_BANNED_FRAMEWORK = {"agno", "AgentOS", "agentos", "AgnoRuntime", "agent_os"}

#: The composition module + the InProcessRuntime decision path (the deterministic core).
_FRAMEWORK_FREE_MODULES = (
    "veridex.mm_strategy.composition",
    "veridex.mm_strategy.inventory_stub",
    "veridex.mm_strategy.runtime",
    "veridex.mm_strategy.core",
)


def _imported_surface(modname: str) -> set[str]:
    """Every imported module path AND imported/alias symbol name in ``modname`` (AST — code only).

    Inspects only ``import`` / ``from ... import`` nodes (incl. lazy/in-function + TYPE_CHECKING), so a
    prose mention of a framework in a docstring/comment can never false-trip the bar.
    """
    mod = importlib.import_module(modname)
    tree = ast.parse(inspect.getsource(mod))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
                names.add(alias.name.split(".")[0])
                if alias.asname:
                    names.add(alias.asname)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
                names.add(node.module.split(".")[0])
            for alias in node.names:
                names.add(alias.name)
                if alias.asname:
                    names.add(alias.asname)
    return names


def test_ac24_composition_and_decision_path_import_no_agno() -> None:
    # GREEN on the real modules: none imports an agno/AgentOS symbol (the core stays framework-free).
    offenders = {
        m: sorted(_imported_surface(m) & _BANNED_FRAMEWORK)
        for m in _FRAMEWORK_FREE_MODULES
        if _imported_surface(m) & _BANNED_FRAMEWORK
    }
    assert not offenders, f"the composition + decision path must import NO agno/AgentOS symbol: {offenders}"

    # Anti-inert: the modules are actually scanned (a non-trivial import surface, not an empty parse).
    assert "veridex.mm_strategy.orchestration" in _imported_surface("veridex.mm_strategy.composition")

    # Positive control (teeth): a synthetic module that REALLY imports agno IS caught by the same detector.
    fake = "import agno\nfrom agno.agent import AgentOS\nVALUE = 1\n"
    caught: set[str] = set()
    for node in ast.walk(ast.parse(fake)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                caught.add(alias.name)
                caught.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                caught.add(node.module.split(".")[0])
            for alias in node.names:
                caught.add(alias.name)
    assert caught & _BANNED_FRAMEWORK == {"agno", "AgentOS"}


# =====================================================================================
# RED #2 — determinism: the same tape run twice → an identical decision/receipt sequence.
# =====================================================================================


def test_same_tape_twice_is_byte_deterministic(tmp_path: Path) -> None:
    a = _run(session_dir=tmp_path / "a")
    b = _run(session_dir=tmp_path / "b")

    # Non-vacuous.
    assert len(a.summary.decisions) >= 1

    # Identical decisions AND identical receipt digest (the digest excludes the non-deterministic
    # bridge instrument: thread ids / timing counters).
    assert a.summary.decisions == b.summary.decisions
    assert a.summary.digest() == b.summary.digest()

    # The recording-fake proposer saw the byte-identical typed request sequence across both runs.
    assert a.proposer.calls == b.proposer.calls


# =====================================================================================
# RED #3 — stop halts the loop: no fresh placement after stop + a TERMINAL OPS event.
# =====================================================================================


def _terminal_events(events: list[RuntimeEvent]) -> list[RuntimeEvent]:
    return [e for e in events if e.payload.get("terminal") is True]


def test_stop_preset_halts_before_any_placement(tmp_path: Path) -> None:
    h = _run(session_dir=tmp_path / "sess", pre_set_stop=True)

    # A kill set BEFORE the loop → zero decisions, zero placements, a terminal OPS event marked stopped.
    assert h.summary.decisions == ()
    assert h.proposer.calls == []
    assert h.summary.terminal_reason == "stopped"
    terminals = _terminal_events(h.events)
    assert len(terminals) == 1
    assert terminals[0].payload.get("terminal_reason") == "stopped"


def test_stop_mid_run_places_no_fresh_leg_after_kill(tmp_path: Path) -> None:
    """A kill tripped DURING a leg → the in-flight leg resolves, but NO fresh leg is submitted after."""
    stop = StopSignal()
    proposer = _RecordingProposer(on_call=lambda _req: stop.set())  # trip the kill on the first leg
    h = _run(session_dir=tmp_path / "sess", proposer=proposer, stop=stop)

    # Exactly ONE leg reached the proposer; the kill aborted every subsequent submission (bridge
    # pre-submission stop-check → AbortedByKill, proven un-sent).
    assert len(proposer.calls) == 1
    assert h.summary.terminal_reason == "stopped"
    # A stop is NOT a possibly-live freeze: the aborted leg was proven un-sent.
    assert all(not f.possibly_live for f in h.summary.freezes)
    terminals = _terminal_events(h.events)
    assert len(terminals) == 1
    assert terminals[0].payload.get("terminal_reason") == "stopped"


# =====================================================================================
# RED #4 — mode router fail-closed: any live mode rejected with an explicit reason, no execution.
# =====================================================================================


@pytest.mark.parametrize("bad_mode", ["replay+live_guarded", "live_guarded", "live", "dry_run", ""])
def test_mode_router_rejects_live_modes_fail_closed(tmp_path: Path, bad_mode: str) -> None:
    proposer = _RecordingProposer()
    with pytest.raises(ModeRejectedError) as excinfo:
        _run(session_dir=tmp_path / "sess", mode=bad_mode, proposer=proposer)

    reason = str(excinfo.value)
    assert bad_mode in reason or "not a permitted" in reason
    assert "replay" in reason  # names the permitted set explicitly
    # Fail-closed: no execution whatsoever — the proposer was never called.
    assert proposer.calls == []


def test_replay_mode_replays_decisions_without_placement(tmp_path: Path) -> None:
    """``replay`` is a pure decision replay: decisions are produced, but NO dry-run placement occurs."""
    proposer = _RecordingProposer()
    h = _run(session_dir=tmp_path / "sess", mode="replay", proposer=proposer)
    assert len(h.summary.decisions) >= 1
    assert h.summary.receipts == ()
    assert proposer.calls == []
    assert h.summary.terminal_reason == "completed"


# =====================================================================================
# Freeze halts fresh placements — any bridge FreezeRecord ends the loop + is durably persisted.
# =====================================================================================


def test_bridge_freeze_halts_fresh_placements_and_persists(tmp_path: Path) -> None:
    """A possibly-live facade escape → the loop halts fresh placements, the freeze is durably persisted
    through the session-owned freeze_sink, and the terminal OPS event is marked frozen."""

    def _boom(_request: MMExecutionToolRequest) -> None:
        raise RuntimeError("facade blew up mid-leg (possibly-live)")

    proposer = _RecordingProposer(on_call=_boom)
    h = _run(session_dir=tmp_path / "sess", proposer=proposer)

    assert h.summary.terminal_reason == "frozen"
    assert len(h.summary.freezes) == 1
    freeze = h.summary.freezes[0]
    assert freeze.possibly_live is True  # any facade escape is fail-closed possibly-live
    assert freeze.reason == "facade_escape"
    # Durably persisted through the SESSION-owned freeze sink (never swallowed).
    assert h.freeze_sink.records == [freeze]
    terminals = _terminal_events(h.events)
    assert len(terminals) == 1
    assert terminals[0].payload.get("terminal_reason") == "frozen"
    assert terminals[0].type == RuntimeEventType.RUN_FAILED


# =====================================================================================
# The II-1 integration contract — same-session manifest/envelope identity + bound() shape.
# =====================================================================================


def test_manifest_identity_mismatch_is_localized_error(tmp_path: Path) -> None:
    """A manifest that is NOT the SAME object on FacadeDeps is an immediate localized error at the
    composition site (II-1 fu-ii1-composition-asserts) — never a downstream fail-closed surprise."""
    arm = _baseline_arm()
    envelope = _pinned_envelope()
    deps = _make_deps(_pinned_manifest(), envelope, _RecordingProposer(), _ListFreezeSink())
    # A DISTINCT manifest object (equal value, different identity) wired into the config only.
    cfg = _make_cfg(
        session_dir=tmp_path / "sess",
        manifest=_pinned_manifest(),
        envelope=envelope,
        deps=deps,
        arm=arm,
    )
    with pytest.raises(ValueError, match="same object as .*facade_deps.manifest|manifest"):
        asyncio.run(
            run_market_maker(
                cfg,
                load_tape("healthy"),
                mode="replay_dry_run",
                guard_enabled=arm.guard_enabled,
                event_sink=[].append,
                stop=StopSignal(),
            )
        )


def test_envelope_identity_mismatch_is_localized_error(tmp_path: Path) -> None:
    arm = _baseline_arm()
    manifest = _pinned_manifest()
    deps = _make_deps(manifest, _pinned_envelope(), _RecordingProposer(), _ListFreezeSink())
    cfg = _make_cfg(
        session_dir=tmp_path / "sess",
        manifest=manifest,
        envelope=_pinned_envelope(),  # distinct envelope object
        deps=deps,
        arm=arm,
    )
    with pytest.raises(ValueError, match="envelope"):
        asyncio.run(
            run_market_maker(
                cfg,
                load_tape("healthy"),
                mode="replay_dry_run",
                guard_enabled=arm.guard_enabled,
                event_sink=[].append,
                stop=StopSignal(),
            )
        )


def test_facade_deps_bound_matches_propose_mm_execution_signature() -> None:
    """bound() cross-check: every kwarg FacadeDeps.bound() supplies IS a real parameter of
    ``propose_mm_execution`` (a drift here would fail-closed downstream — catch it locally)."""
    deps = _make_deps(_pinned_manifest(), _pinned_envelope(), _RecordingProposer(), _ListFreezeSink())
    bound_keys = set(deps.bound().keys())
    params = set(inspect.signature(propose_mm_execution).parameters)
    assert bound_keys <= params, f"bound() supplies non-parameters: {sorted(bound_keys - params)}"
    # Non-vacuous: bound() actually threads the session authorities.
    assert {"manifest", "envelope", "adapter", "signer"} <= bound_keys


# =====================================================================================
# II-3 — the guard ON/OFF biting beat: a JUDGE-FACING behavior ABLATION (NOT a rank).
# =====================================================================================
# ``run_guard_ablation`` runs ``run_market_maker`` TWICE on the SAME pinned tape — the SAME strategy
# with the guard OFF then ON (only the guard arm differs) — and returns the two arms' decision streams
# + the pinned-frame divergence. It DEMONSTRATES that TxLINE changes maker behavior; it EMITS NO rank /
# toxicity / performance ordering / winner (that would be a false-edge honesty violation, conflating a
# behavior-change with the sealed HISTORICAL maker leaderboard). Reuses the E6 divergence fixtures.

#: Field/key names that would smuggle a rank / performance ordering into the honest ablation surface. The
#: ablation carries the two arms' BEHAVIOR (decisions) + divergence ONLY — never any of these.
_BANNED_RANK_TOKENS = (
    "rank",
    "toxic",
    "winner",
    "ordering",
    "score",
    "leaderboard",
    "edge",
    "pnl",
    "clv",
    "better",
    "worst",
    "best",
)


def _control_overrides() -> dict[str, Any]:
    """The E6 control overrides: compact warmup so the guard is genuinely LIVE by the trigger frame.

    ``ref_min_samples=2`` / ``basis_min_samples=2`` let the 4-tick canned divergence tapes warm BOTH
    the venue references and the guarded basis, so arm B really saw a fair value at the trigger.
    ``guard_enabled`` is NOT set here — :func:`run_guard_ablation` flips ONLY that knob between the arms.
    """
    return {**load_base_config_overrides(), "ref_min_samples": 2, "basis_min_samples": 2}


def _ablation_cfg(*, session_dir: Path, arm: Any) -> MakerInstanceConfig:
    """A maker instance bound to ``arm`` from a FRESH ``StrategyState`` seed (the E6 replay_arm idiom).

    The divergence tapes carry their own byte-identical warmup prefix and warm the references from cold
    under the control overrides, so the seed MUST be a fresh ``StrategyState()`` (a warm seed would
    pre-warm the references and change the warmup disposition). ``run_guard_ablation`` derives BOTH arms
    from this one config by flipping ``guard_enabled`` — the arm passed here is only the base strategy.
    """
    manifest = _pinned_manifest()
    envelope = _pinned_envelope()
    deps = _make_deps(manifest, envelope, _RecordingProposer(), _ListFreezeSink())
    return MakerInstanceConfig(
        strategy_config=arm,
        request_config=_pinned_config(),
        manifest=manifest,
        envelope=envelope,
        facade_deps=deps,
        seed_state=StrategyState(),
        session_meta_factory=lambda tape: _session_meta(arm, tape.identity.fixture_id),
        session_dir=session_dir,
        agent_id="mm-ablation-test",
        run_id="run-1",
        session_id="sess-1",
    )


def _collect_keys(obj: Any) -> set[str]:
    """Every mapping KEY reachable in a (possibly nested) JSON-ish structure (dicts/lists)."""
    keys: set[str] = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            keys.add(key)
            keys |= _collect_keys(value)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            keys |= _collect_keys(value)
    return keys


# --- RED #1 — the guard flip CHANGES the decision on the pinned adversarial trigger frame ------


@pytest.mark.parametrize(
    "health,on_kind,on_reasons",
    [
        ("divergence_extreme", "NO_QUOTE", ("residual_extreme",)),
        ("divergence_pull", "QUOTE_ONE_SIDED", ("residual_pull_ask",)),
        ("divergence_stale", "NO_QUOTE", ("txline_stale",)),
    ],
)
def test_guard_flip_changes_decision(
    tmp_path: Path, health: str, on_kind: str, on_reasons: tuple[str, ...]
) -> None:
    """Guard OFF vs ON diverge on the PINNED trigger frame ONLY; the quiescent warmup stretch matches.

    The SAME strategy on the SAME pinned tape, guard OFF then ON, produces a DIFFERENT decision on the
    last (pinned adversarial) frame — arm A is FV-blind and quotes both sides, arm B takes the guarded
    action. The divergence is asserted ONLY on that pinned frame (the warmup prefix is byte-identical):
    honest — a behavior-change on the trigger, never a promise the guard always changes the decision.
    Zero wire primitives (``replay`` mode; no dry-run bridge, no placement).
    """
    arm = arm_configs(_control_overrides()).baseline
    cfg = _ablation_cfg(session_dir=tmp_path / "sess", arm=arm)
    result = asyncio.run(
        run_guard_ablation(cfg, load_tape(health), mode="replay", event_sink=[].append)
    )

    off = result.guard_off.decisions
    on = result.guard_on.decisions
    # The two arms folded the SAME tape → the same frame count (a real, non-trivial stream).
    assert len(off) == len(on) >= 2, health
    assert result.guard_off.guard_enabled is False and result.guard_on.guard_enabled is True

    # Divergence is EXACTLY the pinned trigger frame (the last tick) — the warmup prefix is identical.
    pinned = len(off) - 1
    assert result.divergent_frame_indices == (pinned,), health
    assert result.diverges is True

    # The pinned frame: arm A (guard OFF) quotes BOTH sides; arm B (guard ON) takes the guarded action.
    assert off[pinned].kind == "QUOTE_TWO_SIDED" and off[pinned].reason_codes == (), health
    assert on[pinned].kind == on_kind, health
    assert on[pinned].reason_codes == on_reasons, health

    # Honest: divergence is NOT promised globally — every warmup frame's substance matches across arms.
    for i in range(pinned):
        assert off[i].kind == on[i].kind, (health, i)
        assert off[i].reason_codes == on[i].reason_codes, (health, i)


def test_guard_flip_leaves_quiescent_tape_identical(tmp_path: Path) -> None:
    """Honesty counterweight: on a no-trigger tape the guard (genuinely LIVE) changes NOTHING.

    The quiescent tape keeps the guard ON and fresh but the FV agrees with the venue mid (residual 0),
    so the guard fires on nothing — the two arms' decisions match and there is NO divergence frame. This
    is why divergence is only ever asserted on the pinned adversarial fixture, never promised globally.
    """
    arm = arm_configs(_control_overrides()).baseline
    cfg = _ablation_cfg(session_dir=tmp_path / "sess", arm=arm)
    result = asyncio.run(
        run_guard_ablation(cfg, load_tape("quiescent"), mode="replay", event_sink=[].append)
    )

    assert result.divergent_frame_indices == ()
    assert result.diverges is False
    # Non-vacuous: the guard arm was genuinely live (it saw a fair value) yet changed nothing.
    assert result.guard_on.guard_enabled is True and result.guard_off.guard_enabled is False
    assert len(result.guard_on.decisions) >= 2
    assert any(d.kind == "QUOTE_TWO_SIDED" for d in result.guard_on.decisions)


# --- RED #2 — the ablation result + the /maker/live-ab projection emit NO rank -----------------


def test_ablation_result_and_projection_emit_no_rank(tmp_path: Path) -> None:
    """The ``GuardAblationResult`` + the ``/maker/live-ab`` projection carry NO rank/ordering/winner.

    This is the make-or-break honesty rule: the ablation demonstrates a BEHAVIOR change (guard on vs
    off), never a maker ranking / performance ordering / proven edge. Asserted structurally — no field
    or JSON key anywhere on the result or its projection names a rank/toxicity/ordering/edge/winner.
    """
    arm = arm_configs(_control_overrides()).baseline
    cfg = _ablation_cfg(session_dir=tmp_path / "sess", arm=arm)
    result = asyncio.run(
        run_guard_ablation(cfg, load_tape("divergence_extreme"), mode="replay", event_sink=[].append)
    )

    # (a) the result dataclass (and the SessionSummary arms it carries) expose NO rank-bearing field.
    field_names = {f.name for f in dataclasses.fields(result)}
    field_names |= {f.name for f in dataclasses.fields(result.guard_off)}
    field_names |= {f.name for f in dataclasses.fields(result.guard_on)}
    for name in field_names:
        assert not any(tok in name.lower() for tok in _BANNED_RANK_TOKENS), name

    # It is LABELED a behavior ablation (guard on vs off), never a winner/ranking.
    assert result.ablation_label == GUARD_ABLATION_LABEL
    assert "ablation" in GUARD_ABLATION_LABEL

    # (b) the /maker/live-ab projection carries NO rank key ANYWHERE in its (nested) envelope.
    projection = build_live_ab_projection(result, instance_id="inst-1").model_dump()
    for key in _collect_keys(projection):
        assert not any(tok in key.lower() for tok in _BANNED_RANK_TOKENS), key
    assert projection["is_ablation"] is True
    assert projection["panel"] == "guard_on_off_ablation"


def test_live_ab_route_projects_ablation_no_rank(tmp_path: Path) -> None:
    """The read-only ``GET /maker/live-ab/{instance_id}`` route surfaces the ablation (no rank), 404s absent."""
    arm = arm_configs(_control_overrides()).baseline
    cfg = _ablation_cfg(session_dir=tmp_path / "sess", arm=arm)
    result = asyncio.run(
        run_guard_ablation(cfg, load_tape("divergence_extreme"), mode="replay", event_sink=[].append)
    )

    app = FastAPI()
    register_maker_routes(
        app, live_ab_provider=lambda iid: result if iid == "inst-1" else None
    )
    client = TestClient(app)

    resp = client.get("/maker/live-ab/inst-1")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_ablation"] is True
    assert body["diverges"] is True
    for key in _collect_keys(body):
        assert not any(tok in key.lower() for tok in _BANNED_RANK_TOKENS), key

    # Read-only projection: no ablation for an unknown instance → 404 (never a fabricated result).
    assert client.get("/maker/live-ab/unknown").status_code == 404


# --- RED #3 — the paired run is deterministic (same tape → identical ablation) -----------------


def test_guard_ablation_is_deterministic(tmp_path: Path) -> None:
    """The SAME pinned tape run through ``run_guard_ablation`` twice yields the identical ablation."""
    arm = arm_configs(_control_overrides()).baseline
    a = asyncio.run(
        run_guard_ablation(
            _ablation_cfg(session_dir=tmp_path / "a", arm=arm),
            load_tape("divergence_extreme"),
            mode="replay",
            event_sink=[].append,
        )
    )
    b = asyncio.run(
        run_guard_ablation(
            _ablation_cfg(session_dir=tmp_path / "b", arm=arm),
            load_tape("divergence_extreme"),
            mode="replay",
            event_sink=[].append,
        )
    )
    assert a.guard_off.decisions == b.guard_off.decisions
    assert a.guard_on.decisions == b.guard_on.decisions
    assert a.divergent_frame_indices == b.divergent_frame_indices
    assert a.digest() == b.digest()
