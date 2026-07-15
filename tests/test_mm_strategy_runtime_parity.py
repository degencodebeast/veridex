"""E7-T4 (REQ-120/121/122, SEC-002, AC-034): the runtime-neutral ``InProcessRuntime`` seam.

Four load-bearing properties of the thin neutral seam that wraps the pure ``decide()`` behind a
lifecycle façade (``veridex.mm_strategy.runtime``):

  (1) PARITY (AC-034 / REQ-120): ``decide()`` under a DIRECT pure call and under the
      ``InProcessRuntime`` seam yields BYTE-IDENTICAL ``(decision, next_state)`` — the seam adds a
      telemetry side-channel and a lifecycle façade, never a policy delta. Proven for BOTH ablation
      arms (guard-off ``baseline`` and guard-on ``guarded``).
  (2) TELEMETRY IS OPS-ONLY (REQ-122): the decision projection (kind, reason codes, the four causal
      hashes, arm id) flows into a ``RuntimeEvent`` on the OPS channel ONLY — structurally
      non-evidence (no ``sequence_no`` / ``payload_hash`` / ``evidence`` field) — and CANNOT move the
      decision. Varying the untrusted telemetry identity (agent_id / run_id / sink) leaves the policy
      byte-identical, and the projection carries no channel back into ``decide``.
  (3) FACTORY RECONSTRUCT, ZERO SIDE EFFECTS (REQ-121): the factory rebuilds the seam from a pinned
      ``(instance, config, state)`` triple with NO telemetry emission, NO clock/RNG/I-O, and NO
      mutation of the pinned inputs; the reconstructed seam replays a byte-identical decision.
  (4) NO AGENT TOOLS REGISTRATION (SEC-002): a static AST scan of ``runtime.py`` + the execution
      adapter finds no ``tools=[...]`` kwarg registering venue-write / sign / facade primitives as
      LLM-invokable tools — the strategy proposes via ``decide()``, never a callable tool surface.

The SEC-003 runtime leg (``runtime.py`` imports no raw-write/signer/vendored-CLOB surface) is proven
in ``tests/test_dust_execution_sec_isolation.py`` via the extended ``_AGENT_FACING_MODULES`` audit.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Callable

# Reuse the REAL pure fixtures (same pattern as the SEC file importing facade drivers) so the seam
# is exercised against the identical (observation, state, config) the core-policy suite decides over.
from tests.test_mm_strategy_core_policy import _config, _obs, _warm_state
from veridex.mm_strategy.contracts import StrategyDecision
from veridex.mm_strategy.core import decide
from veridex.mm_strategy.runtime import InProcessRuntime, reconstruct
from veridex.runtime.runtime_events import RuntimeEvent


def _capture() -> tuple[list[RuntimeEvent], Callable[[RuntimeEvent], None]]:
    """A recording OPS sink: returns ``(events, sink)`` where ``sink`` appends each RuntimeEvent."""
    events: list[RuntimeEvent] = []
    return events, events.append


# --- Property (1): direct == seam decision parity (AC-034) ---------------------------------


def test_direct_and_inprocess_runtime_identical_decision() -> None:
    # AC-034 / REQ-120: for BOTH ablation arms, the seam's (decision, next_state) is byte-identical
    # to the direct pure call. The seam calls decide() as a BLACK BOX — it neither imports the core's
    # internals nor perturbs its output; it only adds an OPS side-channel.
    observation = _obs()
    for guard_enabled in (False, True):
        config = _config(guard_enabled=guard_enabled)
        state = _warm_state()

        direct_decision, direct_state = decide(observation, state, config, session_id="s1")

        events, sink = _capture()
        runtime = InProcessRuntime(sink=sink)
        seam_decision, seam_state = runtime.decide(
            observation, state, config, session_id="s1", run_id="r1"
        )

        # Byte-identity of the whole decision AND next state — not merely ``kind`` equality.
        assert seam_decision.model_dump() == direct_decision.model_dump()
        assert seam_state.model_dump() == direct_state.model_dump()
        # Non-vacuity: the seam DID run the policy (a real, hash-stamped decision came back).
        assert seam_decision.observation_hash and seam_decision.config_hash
        # And the telemetry side-channel fired (proving parity is not achieved by doing nothing).
        assert events, "the seam must emit OPS telemetry alongside the identical decision"


def test_seam_decide_matches_direct_for_the_placement_arm_exactly() -> None:
    # A focused re-assertion on the guard-off placement arm (QUOTE_TWO_SIDED): identical decision_id
    # too — the seam never re-stamps identity, so the deterministic decision_id is preserved.
    observation = _obs()
    config = _config(guard_enabled=False)
    state = _warm_state()

    direct_decision, _ = decide(observation, state, config, session_id="sess-x")
    runtime = InProcessRuntime()
    seam_decision, _ = runtime.decide(observation, state, config, session_id="sess-x")

    assert seam_decision.decision_id == direct_decision.decision_id
    assert seam_decision.kind == direct_decision.kind == "QUOTE_TWO_SIDED"
    assert seam_decision.reason_codes == direct_decision.reason_codes


# --- Property (2): telemetry is OPS-only, non-authoritative, cannot move policy (REQ-122) ---


def _assert_ops_non_evidence(event: RuntimeEvent) -> None:
    """A RuntimeEvent must be OPS-channel and STRUCTURALLY non-evidence (SEC-003 shape)."""
    assert event.channel == "OPS"
    # The three fields the evidence/competition-event path requires are ABSENT from the model — so an
    # OPS telemetry event can never masquerade as a sealed/ranked RunEvent.
    for forbidden in ("sequence_no", "payload_hash", "evidence"):
        assert forbidden not in type(event).model_fields, (
            f"a RuntimeEvent must not carry the evidence field {forbidden!r}"
        )


def test_telemetry_ops_only_cannot_affect_policy() -> None:
    # REQ-122: the decision projection is OPS-only and NON-AUTHORITATIVE. Two seam runs over the SAME
    # (observation, state, config) but with DIFFERENT untrusted telemetry identity (agent_id / run_id
    # / sink) must produce the byte-identical decision the pure core produces — telemetry has NO
    # channel back into decide(). Mutation teeth: feeding any telemetry field into the returned
    # decision makes decision_a != decision_b (identity differs) or != direct (projection-fed) → RED.
    observation = _obs()
    config = _config(guard_enabled=False)
    state = _warm_state()

    direct_decision, _ = decide(observation, state, config, session_id="s")

    events_a, sink_a = _capture()
    dec_a, _ = InProcessRuntime(sink=sink_a, agent_id="agent-A").decide(
        observation, state, config, session_id="s", run_id="run-1"
    )
    events_b, sink_b = _capture()
    dec_b, _ = InProcessRuntime(sink=sink_b, agent_id="agent-ZZZ").decide(
        observation, state, config, session_id="s", run_id="run-2"
    )

    # Policy is byte-identical across the telemetry delta AND equal to the pure core output.
    assert dec_a.model_dump() == dec_b.model_dump() == direct_decision.model_dump()

    # Non-vacuity: telemetry ACTUALLY flowed and ACTUALLY differed (so equality above is meaningful).
    assert events_a and events_b
    a_decision_events = [e for e in events_a if e.type.value == "action_emitted"]
    b_decision_events = [e for e in events_b if e.type.value == "action_emitted"]
    assert a_decision_events and b_decision_events
    assert a_decision_events[0].agent_id != b_decision_events[0].agent_id  # telemetry varied
    assert a_decision_events[0].run_id != b_decision_events[0].run_id

    # Every emitted event is OPS-channel + structurally non-evidence.
    for event in (*events_a, *events_b):
        _assert_ops_non_evidence(event)

    # The projection reflects the ALREADY-COMPUTED decision (a read-only shadow, never an input):
    # kind, reason codes, the four causal hashes, and the ablation arm id all match the decision.
    payload = a_decision_events[0].payload
    assert payload["decision_kind"] == direct_decision.kind
    assert tuple(payload["reason_codes"]) == direct_decision.reason_codes
    assert payload["observation_hash"] == direct_decision.observation_hash
    assert payload["config_hash"] == direct_decision.config_hash
    assert payload["prior_state_hash"] == direct_decision.prior_state_hash
    assert payload["next_state_hash"] == direct_decision.next_state_hash
    assert payload["arm"] == "baseline"  # guard_enabled=False


def test_telemetry_arm_id_tracks_the_guard_ablation_arm() -> None:
    # The projected arm id is the A/B ablation arm — ``guarded`` iff ``guard_enabled`` — and is a pure
    # shadow of the config, with ZERO effect on the decision (proven identical to direct above).
    observation = _obs()
    state = _warm_state()
    for guard_enabled, expected_arm in ((False, "baseline"), (True, "guarded")):
        events, sink = _capture()
        InProcessRuntime(sink=sink).decide(
            observation, state, _config(guard_enabled=guard_enabled), session_id="s"
        )
        action = next(e for e in events if e.type.value == "action_emitted")
        assert action.payload["arm"] == expected_arm


def test_lifecycle_facade_emits_ops_only_events() -> None:
    # The seam is a LIFECYCLE façade (run_started/completed/failed), mirroring the AgentRuntime shape;
    # every lifecycle event is OPS-channel + non-evidence too — the façade adds observability, never
    # an evidence/policy channel.
    events, sink = _capture()
    runtime = InProcessRuntime(sink=sink, agent_id="agent-lc")
    runtime.run_started(run_id="r1")
    runtime.run_completed(run_id="r1")
    runtime.run_failed(run_id="r1", error="boom")
    assert events
    for event in events:
        _assert_ops_non_evidence(event)
        assert event.agent_id == "agent-lc"


def test_null_sink_is_a_silent_noop() -> None:
    # With no sink wired, the seam is a silent pure pass-through — telemetry emission is best-effort
    # and never required for a correct decision (REQ-122: OPS is non-load-bearing).
    observation, config, state = _obs(), _config(), _warm_state()
    direct, direct_state = decide(observation, state, config, session_id="s")
    seam_decision, seam_state = InProcessRuntime(sink=None).decide(
        observation, state, config, session_id="s"
    )
    assert seam_decision.model_dump() == direct.model_dump()
    assert seam_state.model_dump() == direct_state.model_dump()


# --- Property (3): the factory reconstructs from pins with ZERO side effects (REQ-121) ------


def test_factory_reconstruct_no_side_effects() -> None:
    # REQ-121: rebuilding the seam from a pinned (instance, config, state) triple emits NOTHING, touches
    # no clock/RNG/I-O, and mutates neither pinned input — then replays a byte-identical decision.
    observation = _obs()
    config = _config(guard_enabled=False)
    state = _warm_state()

    events, sink = _capture()
    instance = InProcessRuntime(sink=sink, agent_id="agent-src")

    # Pin snapshots to prove the factory does not mutate its inputs.
    config_before, state_before = config.model_dump(), state.model_dump()

    reconstructed = reconstruct(instance, config, state)

    # (a) ZERO side effects: reconstruction emitted NO telemetry (the sink was never called)...
    assert events == [], "reconstruction must not emit any telemetry (zero side effects)"
    # ...and did not mutate the pinned config / state (frozen models, byte-identical after).
    assert config.model_dump() == config_before
    assert state.model_dump() == state_before
    # ...and produced a DISTINCT seam object bound to the same agent identity (the reconstruction).
    assert isinstance(reconstructed, InProcessRuntime)
    assert reconstructed is not instance
    assert reconstructed.agent_id == "agent-src"

    # (b) FAITHFUL replay: the reconstructed seam replays the pinned decision byte-identically to the
    # direct pure call over the SAME pins — the reproduction guarantee (a valid snapshot replays).
    direct_decision, direct_state = decide(observation, state, config, session_id="sess-r")
    replay_decision, replay_state = reconstructed.replay(observation, session_id="sess-r")
    assert replay_decision.model_dump() == direct_decision.model_dump()
    assert replay_state.model_dump() == direct_state.model_dump()

    # (c) DETERMINISM: a second reconstruction from the same pins replays the identical decision.
    again = reconstruct(instance, config, state)
    again_decision, _ = again.replay(observation, session_id="sess-r")
    assert again_decision.model_dump() == replay_decision.model_dump()


def test_reconstructed_seam_replay_is_silent_by_default() -> None:
    # The reconstruction factory rebuilds a SILENT seam (sink=None): replaying it emits no telemetry,
    # so a reconstruction/replay never perturbs an OPS stream. It still returns the correct decision.
    observation = _obs()
    config = _config()
    state = _warm_state()
    reconstructed = reconstruct(InProcessRuntime(), config, state)
    decision, _ = reconstructed.replay(observation)
    assert isinstance(decision, StrategyDecision)
    assert decision.observation_hash  # a real hash-stamped decision, produced with no sink wired


# --- Property (4): no agent tools=[...] registration on the seam / adapter (SEC-002) -------

_TOOLS_SCANNED_MODULES = (
    "veridex.mm_strategy.runtime",
    "veridex.mm_strategy.execution_adapter",
)


def _nonempty_tools_registrations(source: str) -> list[str]:
    """Every call passing a NON-EMPTY ``tools=`` kwarg (AST — code only).

    A ``tools=[]`` empty-list literal is the sanctioned decision-only HARD invariant (see
    ``runtime.agent._default_agent_factory``); ANY other ``tools=`` value — a non-empty list, or a
    name/expression — is a real tool registration and is flagged. Walks the WHOLE tree, so a lazy /
    nested construction is caught too. Returns ``ast.dump`` of each offending value for a readable
    assert.
    """
    offenders: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "tools":
                    value = kw.value
                    is_empty_list = isinstance(value, ast.List) and not value.elts
                    if not is_empty_list:
                        offenders.append(ast.dump(value))
    return offenders


def test_no_agent_tools_registration() -> None:
    # SEC-002: neither the neutral runtime seam nor the execution adapter builds an agent/runtime whose
    # ``tools=[...]`` list exposes venue-write / sign / facade primitives as LLM-invokable tools. The
    # strategy proposes via decide(); there is NO callable tool surface. Whole-AST per module.
    import importlib

    for modname in _TOOLS_SCANNED_MODULES:
        module = importlib.import_module(modname)
        source = inspect.getsource(module)
        assert source, f"module {modname} yielded empty source (anti-inert)"
        offenders = _nonempty_tools_registrations(source)
        assert not offenders, (
            f"{modname} must register NO non-empty agent tools=[...] (SEC-002): {offenders}"
        )

    # Positive control (teeth): a synthetic agent construction that REALLY registers venue-write tools
    # IS caught by the same detector — a scan that can catch nothing is worthless.
    leak_src = (
        "from veridex.venues.base import submit_order, cancel_order\n"
        "agent = Agent(model=m, tools=[submit_order, cancel_order], output_schema=None)\n"
    )
    assert _nonempty_tools_registrations(leak_src), (
        "the detector must flag a real tools=[submit_order, ...] agent registration"
    )

    # Negative control: the sanctioned decision-only ``tools=[]`` empty registration is NOT flagged
    # (mirrors runtime.agent's HARD ``tools=[]`` invariant) — the bar keys on a NON-EMPTY tool list.
    benign_src = "agent = Agent(model=m, tools=[], output_schema=Schema)\n"
    assert not _nonempty_tools_registrations(benign_src), (
        "an empty tools=[] (decision-only) registration must not be flagged"
    )
