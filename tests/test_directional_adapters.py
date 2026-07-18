"""II-8b RED suite — directional AgentOS adapters (deterministic Cumulative-Drift / Sharp-Momentum
and LLM-Drift), mirroring II-4's ``VeridexAgentAdapter``.

OFFLINE: ``agno`` is IMPORTED (the AgentOS db is agno's ``InMemoryDb``), the Veridex store is
``InMemoryStore``, and every model is a hand-controlled fake — NO network, NO real LLM call. Each
test maps to one of the 6 required RED controls in ``brief-ii8b.md``.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime

import pytest
from agno.db.in_memory import InMemoryDb
from fastapi.testclient import TestClient

from tests._sharp_momentum_tapes import TAPE_SHARP
from veridex.api.auth_privy import PrivyPrincipal
from veridex.config import Settings
from veridex.deploy.instance import AgentInstance, DeployStatus
from veridex.ingest.marketstate import MarketState
from veridex.runtime import agentos_service as svc
from veridex.runtime.directional_agent_adapters import (
    DirectionalRunResult,
    HostedAction,
    TranscriptEntry,
    VeridexDeterministicAgentAdapter,
    VeridexLLMAgentAdapter,
)
from veridex.runtime.evidence import serialize_payload
from veridex.runtime.llm_checkpoint import CheckpointPolicy
from veridex.runtime.mm_agent_adapter import (
    OwnerMismatchError,
    RunPhase,
    VeridexAgentAdapter,
)
from veridex.runtime.schemas import AgentAction, SportsActionType
from veridex.store import InMemoryStore
from veridex.strategies.drift import cumulative_drift_agent
from veridex.strategies.drift_features import DriftFeatureSnapshot
from veridex.strategies.llm_drift import llm_drift_agent
from veridex.strategies.momentum import sharp_momentum_agent

# --- identities (mirror II-4) -------------------------------------------------

OWNER_A = "did:privy:ownerA"
OWNER_B = "did:privy:ownerB"
_TOKENS = {"tokenA": OWNER_A, "tokenB": OWNER_B}


def _fake_verifier(token: str, *, app_id: str | None, verification_key: str | None) -> PrivyPrincipal:
    did = _TOKENS.get(token)
    if did is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail="invalid token")
    return PrivyPrincipal(did=did)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _settings() -> Settings:
    return Settings(AUTH_MODE="privy", PRIVY_APP_ID="app", PRIVY_VERIFICATION_KEY="key")


def _instance(instance_id: str, operator_id: str | None) -> AgentInstance:
    now = _now()
    return AgentInstance(
        instance_id=instance_id,
        template_id="drift-template",
        agent_id="agent-1",
        submitted_config={},
        effective_config={},
        config_hash="c" * 8,
        policy_hash="p" * 8,
        source_mode="replay",
        execution_mode="paper",
        run_id="run-seed",
        status=DeployStatus.PENDING,
        operator_id=operator_id,
        created_at=now,
        updated_at=now,
    )


# --- tapes --------------------------------------------------------------------


def _ms(prob_bps: int, *, mk: str = "1X2|home", side: str = "home", tick_seq: int = 0) -> MarketState:
    """A REAL MarketState carrying one non-suspended side (ts advances 60s/tick).

    Mirrors ``tests/test_drift_agent.py::_ms`` so an observation horizon accrues past the gates.
    """
    return MarketState(
        fixture_id=5,
        tick_seq=tick_seq,
        ts=1000 + tick_seq * 60,
        phase=2,
        markets={mk: {"stable_prob_bps": {side: prob_bps}, "stable_price": {side: 2.0}, "suspended": False}},
        scores={},
    )


def _drift_tape(n: int = 25, *, mk: str = "1X2|home", side: str = "home") -> list[MarketState]:
    """A smooth monotone rise over ``n`` ticks — past the drift gates (fires)."""
    return [_ms(3000 + i * 160, mk=mk, side=side, tick_seq=i) for i in range(n)]


def _sharp_tape() -> list[MarketState]:
    """A sharp sustained rise (from the committed operating-curve fixture)."""
    return [_ms(b, tick_seq=i) for i, b in enumerate(TAPE_SHARP)]


def _snapshot_hash(ms: MarketState) -> str:
    return hashlib.sha256(serialize_payload(ms.model_dump()).encode("utf-8")).hexdigest()


def _serialize(actions) -> list[str]:
    """Canonical bytes of each action (for byte-identical equivalence assertions)."""
    return [serialize_payload(a.action.model_dump()) for a in actions]


# --- deterministic adapter factory -------------------------------------------


def _det_adapter(agent_factory, tape, *, name="veridex-cumulative-drift", event_sink=None):
    return VeridexDeterministicAgentAdapter(
        agent_factory=agent_factory,
        tape_resolver=lambda _ctx: tape,
        name=name,
        id=name,
        event_sink=event_sink,
    )


# --- LLM fakes (hand-controlled model seam; no network) ----------------------


class _ResolvedHandle:
    """A Future/Task-shaped handle that is already done, carrying one raw model output."""

    def __init__(self, raw) -> None:
        self._raw = raw

    def done(self) -> bool:
        return True

    def cancel(self) -> None:
        pass

    def cancelled(self) -> bool:
        return False

    def exception(self):
        return None

    def result(self):
        return self._raw


class _CountingLauncher:
    """The base model launcher (test fake): hands out pre-resolved handles, COUNTS launches.

    ``launch`` is the physical model call — the exact seam replay must NEVER touch. Its ``calls``
    counter is how the LLM-canonical test proves the sealed transcript was consumed, not re-invoked.
    """

    def __init__(self, raws) -> None:
        self._raws = list(raws)
        self.calls = 0

    def launch(self, prompt: str) -> _ResolvedHandle:
        raw = self._raws[self.calls % len(self._raws)]
        self.calls += 1
        return _ResolvedHandle(raw)


def _snap(ms: MarketState) -> DriftFeatureSnapshot:
    """A per-tick drift snapshot with a tick-varying evidence coordinate (checkpoint opener)."""
    return DriftFeatureSnapshot(
        first=0.0,
        current=0.5,
        cum_logit_drift=0.5,
        ewma_slope=1.0,
        trend_strength=1.0,
        tick_count=25,
        horizon_s=1200,
        market_quality=True,
        evidence_hash=f"ev{ms.tick_seq}",
    )


def _llm_builder(base_launcher):
    """Return an ``agent_builder(model, clock) -> Agent`` closing over a fixed projector/policy.

    Policy: tiny cadence so every tick opens a checkpoint; huge evidence-age so a completion one
    tick later is always FRESH. Deterministic given (tape, clock) → hosted/replay are identical.
    """

    def _builder(model, clock):
        return llm_drift_agent(
            "llm-drift",
            model=model,
            checkpoint_policy=CheckpointPolicy(cadence_s=1.0, evidence_age_limit_s=10_000.0),
            projector=_snap,
            clock=clock,
            model_id="test-model",
        )

    return _builder


def _llm_action(side: str) -> AgentAction:
    return AgentAction(type=SportsActionType.FOLLOW_MOMENTUM, params={"market_key": "1X2|home", "side": side})


def _llm_adapter(base_launcher, tape, *, name="veridex-llm-drift"):
    return VeridexLLMAgentAdapter(
        agent_builder=_llm_builder(base_launcher),
        base_launcher=base_launcher,
        tape_resolver=lambda _ctx: tape,
        name=name,
        id=name,
    )


# ==============================================================================
# RED #1 — hosted session/run: each directional contestant is a real AgentOS run
# ==============================================================================


async def test_red1_deterministic_run_is_a_visible_hosted_session_run() -> None:
    """A deployed Cumulative-Drift contestant produces a real AgentOS session+run (server ids)."""
    adapter = _det_adapter(cumulative_drift_agent, _drift_tape())
    result = await adapter.start_run(
        run_id="run-det-1", session_id="sess-det-1", runtime_agent_id=adapter.get_id(), owner_did=OWNER_A
    )
    assert isinstance(result, DirectionalRunResult)
    # The hosted run carries the SERVER-pre-allocated session/run identity (not a local recompute).
    assert result.run_id == "run-det-1" and result.session_id == "sess-det-1"
    assert result.runtime_agent_id == adapter.get_id()
    assert result.actions, "the hosted run drove the agent over the tape"
    # The run settled non-cancelled → registry shows the terminal, visible run.
    assert adapter.run_phase("run-det-1") is RunPhase.COMPLETED


async def test_red1_llm_run_is_a_visible_hosted_session_run() -> None:
    """A deployed LLM-Drift contestant produces a real AgentOS session+run backed by model calls."""
    base = _CountingLauncher([_llm_action("home")])
    adapter = _llm_adapter(base, _drift_tape(6))
    result = await adapter.start_run(
        run_id="run-llm-1", session_id="sess-llm-1", runtime_agent_id=adapter.get_id(), owner_did=OWNER_A
    )
    assert isinstance(result, DirectionalRunResult)
    assert result.run_id == "run-llm-1" and result.session_id == "sess-llm-1"
    assert base.calls > 0, "the hosted run IS the model call (launcher was invoked)"
    assert adapter.run_phase("run-llm-1") is RunPhase.COMPLETED


# ==============================================================================
# RED #2 — cancel stops the loop (not 501), exactly-once (AC-16), 2nd cancel no-op
# ==============================================================================


async def test_red2_cancel_stops_loop_exactly_once() -> None:
    """AC-16: an owner cancel trips the loop StopSignal (returns — never 501); exactly-once."""
    adapter = _det_adapter(cumulative_drift_agent, _drift_tape(400))  # long tape → still running on cancel
    task = asyncio.create_task(
        adapter.start_run(run_id="run-c", session_id="s", runtime_agent_id=adapter.get_id(), owner_did=OWNER_A)
    )
    # Let the run register + begin looping.
    for _ in range(3):
        await asyncio.sleep(0)
    assert adapter.is_run_active("run-c")
    # Two concurrent owner cancels → the kill engages EXACTLY once.
    results = await asyncio.gather(
        adapter.acancel_run("run-c", owner_did=OWNER_A),
        adapter.acancel_run("run-c", owner_did=OWNER_A),
    )
    engaged = [r for r in results if r.engaged]
    assert len(engaged) == 1, "cancel-all engages exactly once (AC-16)"
    result = await asyncio.wait_for(task, timeout=2)
    assert result.stopped_early is True, "the loop broke on the StopSignal (did not run the full tape)"
    assert len(result.actions) < 400, "cancel stopped the loop before the tape was exhausted"
    assert adapter.run_phase("run-c") is RunPhase.CANCELLED
    # A 3rd cancel after terminal is a no-op (not re-engaged).
    late = await adapter.acancel_run("run-c", owner_did=OWNER_A)
    assert late.engaged is False and late.phase is RunPhase.CANCELLED


async def test_red2_cancel_is_owner_first_fail_closed() -> None:
    """A non-owner (or None, the agno-native path) cancel is refused BEFORE any effect."""
    adapter = _det_adapter(cumulative_drift_agent, _drift_tape(400))
    task = asyncio.create_task(
        adapter.start_run(run_id="run-c2", session_id="s", runtime_agent_id=adapter.get_id(), owner_did=OWNER_A)
    )
    for _ in range(3):
        await asyncio.sleep(0)
    with pytest.raises(OwnerMismatchError):
        await adapter.acancel_run("run-c2", owner_did=OWNER_B)
    with pytest.raises(OwnerMismatchError):
        await adapter.acancel_run("run-c2", owner_did=None)  # native path carries no principal
    assert adapter.run_phase("run-c2") is RunPhase.ACTIVE  # NO effect from the refused cancels
    await adapter.acancel_run("run-c2", owner_did=OWNER_A)
    await asyncio.wait_for(task, timeout=2)


# ==============================================================================
# RED #3 — auth (inherits II-4 AC-27/AC-29): anonymous native run/cancel → 401
# ==============================================================================


def _build_hosted_app(store, mm_adapter, directional):
    return svc.build_agentos_app(
        store=store,
        settings=_settings(),
        adapter=mm_adapter,
        extra_agents=directional,
        owner_db=InMemoryDb(),
        verifier=_fake_verifier,
        enforce_contract=True,
    )


def test_red3_anonymous_native_routes_for_directional_agents_401() -> None:
    """RED#3: the hosted directional agents live behind the SAME deny-by-default boundary (401 anon)."""
    store = InMemoryStore()

    async def _mm_driver(ctx, stop, sink):
        class _S:
            terminal_reason = "completed"

        return _S()

    mm = VeridexAgentAdapter(run_driver=_mm_driver)
    det = _det_adapter(cumulative_drift_agent, _drift_tape(), name="veridex-cumulative-drift")
    llm = _llm_adapter(_CountingLauncher([_llm_action("home")]), _drift_tape(6), name="veridex-llm-drift")
    guard = _build_hosted_app(store, mm, [det, llm])
    client = TestClient(guard)
    for agent_id in ("veridex-cumulative-drift", "veridex-llm-drift"):
        assert client.post(f"/agents/{agent_id}/runs").status_code == 401
        assert client.post(f"/agents/{agent_id}/runs/r1/cancel").status_code == 401
    # And the composed native surface is UNCHANGED (extra agents add no new route templates).
    composed = guard.app
    veridex_app = svc.create_app(store=InMemoryStore(), settings=_settings())
    svc._register_wrapper_routes(
        veridex_app, store=InMemoryStore(), adapter=mm, require_principal=lambda **k: PrivyPrincipal(did="x"), event_sink=None
    )
    native = svc.agno_native_routes(composed, set(svc._route_table(veridex_app)))
    assert native == set(svc._KNOWN_AGNO_NATIVE_ROUTES)


# ==============================================================================
# RED #4 — hosted-action scoring: the scored action IS the hosted-run action,
#          bound to session/run/config/snapshot provenance
# ==============================================================================


async def test_red4_scored_action_is_provenance_bound_hosted_action() -> None:
    """Each scored action IS the actual hosted-run action, bound to run/session/config/snapshot."""
    tape = _drift_tape()
    adapter = _det_adapter(cumulative_drift_agent, tape)
    result = await adapter.start_run(
        run_id="run-p", session_id="sess-p", runtime_agent_id=adapter.get_id(), owner_did=OWNER_A
    )
    assert result.scored_actions, "the drift tape fires at least one non-WAIT action"
    by_tick = {ms.tick_seq: ms for ms in tape}
    fresh_agent = cumulative_drift_agent()
    for scored in result.scored_actions:
        assert isinstance(scored, HostedAction)
        assert scored.action.type is not SportsActionType.WAIT
        # Provenance: bound to THIS hosted run/session/agent, not a bare recompute.
        assert scored.run_id == "run-p" and scored.session_id == "sess-p"
        assert scored.runtime_agent_id == adapter.get_id()
        # Snapshot provenance: the exact tape tick that produced the action.
        ms = by_tick[scored.tick_seq]
        assert scored.snapshot_hash == _snapshot_hash(ms)
        # Config provenance: the pinned sealed identity of the deployed contestant at that snapshot.
        assert scored.config_hash == fresh_agent.config_hash(ms)


# ==============================================================================
# RED #5 — deterministic-equivalence: hosted action BYTE-IDENTICAL to local replay
# ==============================================================================


async def test_red5_deterministic_hosted_equals_local_replay_byte_identical() -> None:
    """The deterministic hosted run is BYTE-IDENTICAL to the local replay on the same tape."""
    tape = _drift_tape()
    adapter = _det_adapter(cumulative_drift_agent, tape)
    result = await adapter.start_run(
        run_id="run-eq", session_id="s", runtime_agent_id=adapter.get_id(), owner_did=OWNER_A
    )
    replay = await adapter.replay(tape)
    assert _serialize(result.actions) == _serialize(replay), "hosted vs local replay must be byte-identical"
    assert [a.action.type for a in result.scored_actions] == [
        a.action.type for a in replay if a.action.type is not SportsActionType.WAIT
    ]


async def test_red5_deterministic_equivalence_holds_for_sharp_momentum() -> None:
    """The SAME deterministic adapter hosts admitted Sharp-Momentum with byte-identical replay."""
    tape = _sharp_tape()
    adapter = _det_adapter(sharp_momentum_agent, tape, name="veridex-sharp-momentum")
    result = await adapter.start_run(
        run_id="run-sm", session_id="s", runtime_agent_id=adapter.get_id(), owner_did=OWNER_A
    )
    replay = await adapter.replay(tape)
    assert _serialize(result.actions) == _serialize(replay)


# ==============================================================================
# RED #6 — LLM-canonical: hosted response canonical; replay consumes the SEALED
#          transcript and NEVER re-invokes the model
# ==============================================================================


async def test_red6_llm_replay_consumes_sealed_transcript_never_reinvokes() -> None:
    """The hosted LLM response is canonical; replay consumes the sealed transcript, no re-invoke."""
    tape = _drift_tape(6)
    # Distinct raw per physical call: a 2nd invocation would change the action → drift the sequence.
    base = _CountingLauncher([_llm_action("home"), _llm_action("away"), _llm_action("draw")])
    adapter = _llm_adapter(base, tape)
    result = await adapter.start_run(
        run_id="run-canon", session_id="s", runtime_agent_id=adapter.get_id(), owner_did=OWNER_A
    )
    assert result.sealed_transcript, "the hosted run sealed the model transcript"
    assert all(isinstance(e, TranscriptEntry) for e in result.sealed_transcript)
    hosted_calls = base.calls
    assert hosted_calls > 0

    # Replay consumes the SEALED transcript — the model launcher is NEVER touched again.
    replay = await adapter.replay(tape, result.sealed_transcript)
    assert base.calls == hosted_calls, "replay re-invoked the model (must consume the sealed transcript)"
    # ...and yields the byte-identical canonical action sequence.
    assert _serialize(result.actions) == _serialize(replay)
    assert result.scored_actions, "at least one canonical hosted action was scored"


async def test_major4_sealed_transcript_fails_closed_on_evidence_mismatch() -> None:
    """MAJOR 4 — a sealed LLM transcript replays ONLY against its ORIGINATING evidence.

    A tape whose per-tick snapshot (evidence coordinate) differs from the sealed entry FAILS CLOSED
    (raises), never emitting the stale response against the wrong evidence; a matching tape still
    replays canonically with NO model re-invoke; and leftover/unconsumed entries fail closed too.
    """
    from veridex.runtime.directional_agent_adapters import SealedEvidenceMismatch

    tape_a = _drift_tape(6)
    base = _CountingLauncher([_llm_action("home"), _llm_action("away"), _llm_action("draw")])
    adapter = _llm_adapter(base, tape_a)
    result = await adapter.start_run(
        run_id="run-seal", session_id="s", runtime_agent_id=adapter.get_id(), owner_did=OWNER_A
    )
    assert result.sealed_transcript, "the hosted run sealed a transcript"
    hosted_calls = base.calls

    # A DIFFERENT tape → different per-tick evidence coordinate (the projector keys on tick_seq) →
    # the sealed prompt/evidence differs from what this replay would decide on → FAIL CLOSED.
    tape_b = [_ms(3000 + i * 160, tick_seq=i + 50) for i in range(6)]
    with pytest.raises(SealedEvidenceMismatch):
        await adapter.replay(tape_b, result.sealed_transcript)
    assert base.calls == hosted_calls, "a rejected replay must NEVER invoke the model"

    # The MATCHING tape still replays canonically (no re-invoke; byte-identical).
    replay = await adapter.replay(tape_a, result.sealed_transcript)
    assert base.calls == hosted_calls, "a matching replay consumes the sealed transcript, no re-invoke"
    assert _serialize(result.actions) == _serialize(replay)

    # Leftover / unconsumed sealed entries fail closed at completion.
    with pytest.raises(SealedEvidenceMismatch):
        await adapter.replay(
            tape_a, (*result.sealed_transcript, TranscriptEntry(prompt="UNUSED", raw=_llm_action("home")))
        )


async def test_red6_zero_construct_side_effects() -> None:
    """AC-26 (inherited): constructing either adapter starts NO run and touches NO model."""
    base = _CountingLauncher([_llm_action("home")])
    det = _det_adapter(cumulative_drift_agent, _drift_tape())
    llm = _llm_adapter(base, _drift_tape(6))
    assert det.db is None and llm.db is None  # AgentOS injects the owner db later
    assert det._runs == {} and llm._runs == {}
    assert base.calls == 0  # no model launched at construct time
