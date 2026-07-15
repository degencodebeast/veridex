"""E8-T1 (§6 whole-lane gate): the WHOLE R4-B strategy lane, driven OFFLINE end-to-end.

This is the capstone whole-lane proof for the R4-B experimental market-maker STRATEGY lane. It
drives ONE offline flow through EVERY R4-B tier in one composition — typed recorder events →
``assembler`` → pure ``core`` → decision plan → ``execution_adapter`` → a RECORDING R4-A facade
fake — and asserts the cross-cutting trust invariants the individual tiers each pin locally.

    typed recorder cadence events
        -> assembler.run_cadence           (E3-T4: global mint boundary, guard-off projection,
                                            status/match joins — arm-identical venue facts)
        -> live_recorder seal + re-read     (replay.py: byte-deterministic reproduction)
        -> core.decide  (warm-seeded fold)  (transition table, anchor, guard, quote math — the ONE
                                            pure reducer; clock is the explicit as_of_ts input)
        -> StrategyDecision.intent_plan     (single-phase: cancel XOR place)
        -> execution_adapter.build_r4a_request(leg, config, observation=obs, decision=dec)
                                            (CRITICAL-1: the request is DERIVED from the reviewed
                                            observation's stream identity and BOUND to the stamped
                                            decision — never a caller bare-token channel)
        -> execution_adapter.execute_plan   (fires each actionable leg IN ORDER)
        -> _RecordingFakeFacade             (OFFLINE: counts typed calls, returns a scripted typed
                                            result; NEVER a wire primitive / signer / socket)

HARD OFFLINE BOUNDARY (REQ-093/094, AC-017): the whole lane runs against a RECORDING facade fake in
Mode-A (``dry_run``). There is NO live call, NO network, NO wallet/signer/submit/cancel, NO Mode-B
arm. :func:`test_whole_lane_no_direct_wire_no_live_call` bans ``socket.socket`` for the duration of a
whole-lane run as executable evidence the lane opens no socket, and asserts the recording facade sees
ONLY typed :class:`NeutralIntent` requests (never a wire primitive) and returns ONLY the clean Mode-A
abstention (never a ``SUBMITTED`` the adapter could treat as live).

REUSE, not reinvention: the A/B ablation harness (``run_cadence`` + ``decide`` fold + the six-metric
venue-referenced report + the pinned-label run receipt), the E5-T4 OFFLINE ``_RecordingFakeFacade`` +
the pinned adapter request config, the R4-B rank-denylist ground truth, and the R4-A sealed-JSON
enumeration + git-``HEAD`` SHA loop are all imported from the existing suites — this file wires them
into ONE whole-lane flow and adds no bespoke fake.

WHY A WARM SEED STATE: the pure ``core`` fails CLOSED to HOLD/NO_QUOTE until its rolling references
are warm (a live session that has been running), so a fold from a cold ``StrategyState`` over a short
canned tape never actually RESTS a quote — the adapter/wire-boundary tiers would go unexercised. The
fold here is seeded with a warm state (mirroring ``test_mm_strategy_quote_math``'s ``_warm_state``) so
the lane genuinely emits an actionable ``QUOTE_TWO_SIDED`` and the adapter builds a WIREABLE request.
The observation STREAM still comes end-to-end from the typed recorder events through the assembler.
"""

from __future__ import annotations

import hashlib
import json
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import pytest

# Reuse the A/B ablation harness: the assembler+core replay spine, the six-metric venue-referenced
# report, the pinned-label run receipt, and the ReplayResult shape the report consumes.
from tests.mm_strategy_ablation_harness import (
    PROMOTED_EVIDENCE_CLASSES,
    SIX_METRIC_KEYS,
    MarkoutReferenceError,
    ReplayResult,
    arm_configs,
    load_base_config_overrides,
    load_tape,
    matched_opportunity_report,
    mint_run_receipt,
)

# Reuse the R4-A whole-lane sealed-JSON enumeration + repo root + git-HEAD SHA gate.
from tests.test_dust_execution_integration import _REPO_ROOT, _enumerate_sealed_json

# Reuse the E5-T4 OFFLINE recording facade + the pinned Mode-A adapter request config + a clean
# (non-freezing) Mode-A ABSTAINED boundary result. NEVER a real facade: no network/wallet/submit.
from tests.test_mm_strategy_adapter import _pinned_config, _RecordingFakeFacade, _result

# Reuse the R4-B rank-denylist ground truth + a complete valid directional row (assertion: the three
# rank surfaces reject any dumped R4-B diagnostic field).
from tests.test_mm_strategy_rank_sealed import EXPECTED_R4B_FIELDS, VALID_DIR
from veridex.dust_execution.facade import MMExecutionToolRequest
from veridex.dust_execution.manifest import StrategyExperimentManifest
from veridex.dust_execution.runner import _build_resting_order
from veridex.leaderboard import _rank_key as clv_key
from veridex.live_recorder.contracts import LiveRecorderSessionMeta
from veridex.live_recorder.recorder import LiveRecorder
from veridex.live_recorder.replay import (
    iter_change_series,
    read_session,
    replay_reproduces,
)
from veridex.maker.leaderboard import maker_rank_key
from veridex.mm_strategy.assembler import run_cadence
from veridex.mm_strategy.config import StrategyConfig
from veridex.mm_strategy.contracts import (
    NeutralIntent,
    StrategyDecision,
    StrategyObservation,
    StrategyState,
    reject_mixed_phase_plan,
)
from veridex.mm_strategy.core import decide
from veridex.mm_strategy.execution_adapter import (
    PlanExecutionResult,
    build_r4a_request,
    execute_plan,
)
from veridex.scoring import _rank_key as dir_key

_ENDED_TS = 10_000


def _warm_seed_state() -> StrategyState:
    """A mid-session WARM :class:`StrategyState` — smoother seeded + both rolling references past
    ``ref_min_samples`` — so a healthy in-window frame reaches the row-H quote logic and the lane
    genuinely RESTS a quote (mirrors ``test_mm_strategy_quote_math._warm_state``). Purely offline,
    deterministic, no clock: it represents a live session that has already been running."""
    return StrategyState(
        last_observation_sequence=1,
        last_book_source_epoch=1,
        last_as_of_ts=1,
        last_market_status_epoch=1,
        last_market_status_recv_ts=1,
        guard_watermark=None,
        smoother_mid=0.50,
        smoother_mid_ts=1,
        spread_ref_samples=tuple(0.02 for _ in range(25)),
        depth_ref_samples=tuple(100.0 for _ in range(25)),
    )


def _session_meta(config: StrategyConfig, fixture_id: int) -> LiveRecorderSessionMeta:
    """A deterministic recorder session meta bound to the arm's ``config_hash`` (offline, no clock)."""
    return LiveRecorderSessionMeta(
        session_ts=1_700_000_000,
        endpoints={"venue": "wss://example.invalid"},
        tool_version="e8t1-whole-lane",
        config_hash=config.config_hash(),
        source_provenance={"venue": "poly"},
        fixture_ids=(fixture_id,),
    )


def _r4a_boundary_manifest(token_id: str) -> StrategyExperimentManifest:
    """A minimal R4-A manifest whose ``universe`` admits ``token_id`` — the pure-constructor input the
    REAL :func:`_build_resting_order` needs to prove the adapter's request is WIREABLE. OFFLINE: no
    venue, no signer, no wire."""
    return StrategyExperimentManifest(
        strategy_id="dust-maker-v0",
        strategy_config_hash="cfg" * 4,
        evidence_class="EXPERIMENTAL_DUST",
        market="0xcondition",
        universe=(token_id,),
        mode="dry_run",
        max_orders=3,
        max_notional=5.0,
        max_session_loss=2.0,
        max_daily_loss=4.0,
        session_window=(1_700_000_000_000, 1_700_000_600_000),
        required_inputs=("fair_value", "venue_book"),
        permitted_intent_kinds=("make_quote", "cancel_replace", "cancel_all", "no_quote"),
        market_fee_snapshot_hash="fee" * 4,
        operator_authorization="op-ref-1",
        forbidden_claims=("PROVEN_EDGE", "CALIBRATED"),
    )


@dataclass(frozen=True)
class WholeLaneRun:
    """The captured artifacts of ONE offline whole-lane run (recorder→assembler→core→adapter→facade)."""

    observations: tuple[StrategyObservation, ...]
    decisions: tuple[StrategyDecision, ...]
    requests: tuple[MMExecutionToolRequest, ...]
    plan_results: tuple[PlanExecutionResult, ...]
    facade: _RecordingFakeFacade
    final_state: StrategyState
    change_series: tuple[object, ...]
    byte_reproduces: bool


def _drive_whole_lane(
    tape_health: str, config: StrategyConfig, session_dir: Path
) -> WholeLaneRun:
    """Drive ONE offline whole-lane flow end-to-end and capture every tier's artifact.

    recorder+assembler: :func:`run_cadence` folds the typed tape events into the minted observation
    stream under ``config.guard_enabled`` (guard-off emits ``guard_fv=None`` WITHOUT reading the FV
    cache). replay.py: the session is sealed and re-read, and ``replay_reproduces`` confirms it
    re-hashes byte-identically. core: the ONE pure :func:`decide` is folded from a WARM seed state
    (clock = the observation's explicit ``as_of_ts``). adapter: for EACH decision, every leg's
    singular R4-A request is built with the reviewed ``observation`` + the stamped ``decision`` BOUND
    (CRITICAL-1), and :func:`execute_plan` fires the plan at the OFFLINE recording facade IN ORDER.
    The facade is scripted with a clean Mode-A ABSTAINED result — nothing SUBMITs, nothing freezes.
    """
    tape = load_tape(tape_health)
    recorder = LiveRecorder(session_dir, _session_meta(config, tape.identity.fixture_id))
    run = run_cadence(recorder, tape.events, guard_enabled=config.guard_enabled)
    recorder.finalize(ended_ts=_ENDED_TS)
    recorder.close()

    # replay.py tier: re-read the sealed tape and prove byte-deterministic reproduction.
    _meta, events, gaps = read_session(session_dir)
    change_series = tuple(iter_change_series(events, gaps))
    byte_reproduces = replay_reproduces(session_dir)

    # core tier: fold the ONE pure decide over the minted stream from a warm seed.
    state = _warm_seed_state()
    decisions: list[StrategyDecision] = []
    for observation in run.observations:
        decision, state = decide(observation, state, config)
        decisions.append(decision)

    # adapter tier: build the bound singular request per leg, then execute the single-phase plan at
    # the OFFLINE recording facade. The pinned config is Mode-A (dry_run); the facade abstains.
    adapter_config = _pinned_config()
    facade = _RecordingFakeFacade(results=[_result()])
    requests: list[MMExecutionToolRequest] = []
    plan_results: list[PlanExecutionResult] = []
    for observation, decision in zip(run.observations, tuple(decisions), strict=True):
        # Single-phase invariant (defense in depth): a mixed cancel+placement plan fails closed here.
        reject_mixed_phase_plan(decision.intent_plan, context="e8-t1 whole-lane")
        for leg in decision.intent_plan:
            requests.append(
                build_r4a_request(
                    leg, adapter_config, observation=observation, decision=decision
                )
            )
        plan_results.append(execute_plan(decision.intent_plan, facade))

    return WholeLaneRun(
        observations=run.observations,
        decisions=tuple(decisions),
        requests=tuple(requests),
        plan_results=tuple(plan_results),
        facade=facade,
        final_state=state,
        change_series=change_series,
        byte_reproduces=byte_reproduces,
    )


def _canonical_digest(items: list[object]) -> str:
    """A canonical byte digest of a pydantic-model sequence — the concrete 'byte-identical' artifact."""
    canonical = json.dumps(
        [item.model_dump() for item in items], sort_keys=True, default=str  # type: ignore[attr-defined]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _as_replay_result(run: WholeLaneRun) -> ReplayResult:
    """Adapt a captured whole-lane run into the :class:`ReplayResult` the six-metric report consumes.

    ``matched_opportunity_report`` reads only ``observations`` + ``decisions`` (candidate fills,
    capital-at-risk, abstention count), so the report is scored over the SAME whole-lane decisions the
    adapter tier just fired — never a synthetic side stream.
    """
    return ReplayResult(
        observations=run.observations,
        observation_hashes=tuple(o.observation_hash() for o in run.observations),
        decisions=run.decisions,
        decisions_digest=_canonical_digest(list(run.decisions)),
        final_state=run.final_state,
        state_hash=run.final_state.state_hash(),
        change_series=run.change_series,
        byte_reproduces=run.byte_reproduces,
    )


# =====================================================================================
# RED #1 — the whole lane replays deterministically into byte-identical, WIREABLE, honest intents.
# =====================================================================================


def test_whole_lane_offline_replay_produces_reproducible_intents(tmp_path: Path) -> None:
    """WHOLE LANE (deterministic + wireable): replaying the offline lane twice on the SAME tape yields
    byte-identical decisions AND byte-identical R4-A intents; the guard-off baseline arm is byte-
    identical across FV-health variants (the guard-off projection makes it FV-independent); and the
    adapter's normal output is a WIREABLE R4-A request BOUND to the reviewed observation+decision
    (derived token, BUY/SELL, GTC, size R4-A-owned), single-phase, honestly labeled.
    """
    arms = arm_configs(load_base_config_overrides())

    # (1) DETERMINISM — the same tape replayed twice folds to byte-identical decisions AND intents.
    run_a = _drive_whole_lane("healthy", arms.baseline, tmp_path / "det_a")
    run_b = _drive_whole_lane("healthy", arms.baseline, tmp_path / "det_b")
    assert run_a.byte_reproduces and run_b.byte_reproduces, "each sealed tape must reproduce byte-identically"
    assert run_a.decisions == run_b.decisions
    assert _canonical_digest(list(run_a.decisions)) == _canonical_digest(list(run_b.decisions))
    assert run_a.requests == run_b.requests
    assert _canonical_digest(list(run_a.requests)) == _canonical_digest(list(run_b.requests))

    # Non-vacuous: the lane genuinely RESTED a quote — at least one QUOTE decision + one built intent
    # (a cold/empty lane would make the reproducibility + wireability claims vacuous).
    resting_requests = [r for r in run_a.requests if r.intent_params.side is not None]
    assert any(d.kind.startswith("QUOTE") for d in run_a.decisions), "the lane must actually quote"
    assert resting_requests, "the whole lane must build at least one order-resting R4-A request"

    # (2) BASELINE-ARM IDENTITY — the guard-off projection emits guard_fv=None WITHOUT reading the FV
    # cache, so arm A's decision stream is byte-identical across FV-health variants. Suppressing the
    # guard-off projection (arm A -> guard-on) makes it FV-dependent and this identity fails (mutation a).
    baseline_digests = {
        health: _canonical_digest(
            list(_drive_whole_lane(health, arms.baseline, tmp_path / f"id_{health}").decisions)
        )
        for health in ("healthy", "stale", "absent")
    }
    assert len(set(baseline_digests.values())) == 1, (
        "the guard-off baseline arm must be byte-identical across FV-health variants "
        f"(FV-independent by the guard-off projection); got {baseline_digests}"
    )

    # (3) WIREABLE at the REAL R4-A boundary — the DERIVED token, a {BUY,SELL} side, the config-pinned
    # GTC maker TIF, NO adapter-set size, and the pinned EXPERIMENTAL_DUST label. Each resting request
    # constructs a VALID post-only R4-A resting order via the REAL _build_resting_order, with the wire
    # SIZE R4-A-owned (never adapter-set) — proof the bound observation+decision are wireable.
    wire_size = 4.0  # R4-A's resolve_dust_size output — the SOLE size authority (never the adapter)
    for request in resting_requests:
        derived_token = run_a.observations[0].stream_identity().token_id
        assert request.intent_params.token_id == derived_token, "target token DERIVED from the reviewed obs"
        assert request.intent_params.side in {"BUY", "SELL"}
        assert request.intent_params.tif == "GTC"
        assert request.intent_params.size is None, "the adapter sets NO size — R4-A owns sizing (mutation b)"
        assert request.evidence_class == "EXPERIMENTAL_DUST"

        order = _build_resting_order(
            token_id=derived_token,
            manifest=_r4a_boundary_manifest(derived_token),
            intent_params=request.intent_params,
            wire_size=wire_size,
            tick_size=0.01,
        )
        assert order is not None, "the adapter's normal output must be WIREABLE at the real R4-A boundary"
        assert order.side == request.intent_params.side
        assert order.tif == "GTC"
        assert order.post_only is True
        assert order.token_id == derived_token
        assert order.size == wire_size  # R4-A-owned size, not adapter-set

    # (4) SINGLE-PHASE — every plan is cancel XOR place (no mixed-phase plan reached the adapter).
    for decision in run_a.decisions:
        reject_mixed_phase_plan(decision.intent_plan, context="e8-t1 single-phase")
        kinds = {leg.kind for leg in decision.intent_plan}
        assert not ({"place_quote", "replace_quote"} & kinds and "cancel_all_orders" in kinds), (
            "a plan must never mix a fresh write with a cancel phase (single-phase, REQ-090)"
        )

    # (5) LABELS PINNED — the whole-lane run receipt pins EXPERIMENTAL_DUST / NOT_PROVEN_EDGE; an
    # untrusted PROMOTED relabel under Gate-B OPEN has ZERO effect (fails closed, stays dust).
    receipt = mint_run_receipt(
        _as_replay_result(run_a),
        arms.baseline,
        gate_b_status="OPEN",
        gate_b_evidence_revision="rev-e8t1",
        requested_evidence_class="PROMOTED",
    )
    assert receipt.labels()["evidence_class"] == "EXPERIMENTAL_DUST"
    assert receipt.labels()["edge_label"] == "NOT_PROVEN_EDGE"
    assert receipt.evidence_class not in PROMOTED_EVIDENCE_CLASSES

    # (6) SIX-METRIC REPORT — the whole-lane decisions score into EXACTLY the six mandatory metrics,
    # reported together, against the VENUE reference (never the FV the guard consumes). An FV reference
    # fails closed — scoring the FV-driven guard against the same FV is circular self-validation.
    guarded_run = _drive_whole_lane("healthy", arms.guarded, tmp_path / "six_guarded")
    report = matched_opportunity_report(_as_replay_result(run_a), _as_replay_result(guarded_run))
    assert set(report.metrics().keys()) == SIX_METRIC_KEYS, "the six metrics travel ALWAYS together"
    assert report.markout_reference == "venue", "every markout is VENUE-referenced, never FV"
    with pytest.raises(MarkoutReferenceError):
        matched_opportunity_report(
            _as_replay_result(run_a), _as_replay_result(guarded_run), reference="fv"
        )


# =====================================================================================
# RED #2 — the whole lane touches NO wire and issues NO live call (OFFLINE, Mode-A/replay only).
# =====================================================================================


def test_whole_lane_no_direct_wire_no_live_call(tmp_path: Path) -> None:
    """WHOLE LANE (no direct wire / no live call, AC-017): the R4-B lane has NO direct wire authority.
    With ``socket.socket`` BANNED for the duration of the run (executable no-network evidence), the
    lane drives end-to-end and the RECORDING facade sees ONLY typed high-level requests (never a
    submit/cancel/sign wire primitive) and returns ONLY the clean Mode-A abstention — no order is ever
    treated as live, the book is never treated flat, and no leg is assumed filled or withdrawn.
    """
    arms = arm_configs(load_base_config_overrides())

    # No network: ban socket construction for the whole run. The offline lane (recorder file I/O,
    # assembler, pure core, adapter, recording fake) opens no socket — a live call would raise here.
    with mock.patch.object(socket, "socket", side_effect=AssertionError("R4-B opened a socket — NO NETWORK")):
        run = _drive_whole_lane("healthy", arms.baseline, tmp_path / "no_wire")

    # Mode-A/replay only — the pinned adapter config is dry_run (never an armed Mode-B money surface).
    assert _pinned_config().mode == "dry_run"

    # The recording facade saw ONLY typed NeutralIntent requests — never a wire primitive. And it was
    # actually driven (an actionable leg reached it), so this is not a vacuous no-call assertion.
    assert run.facade.call_count > 0, "the whole lane must actually drive the facade (non-vacuous)"
    assert all(isinstance(call, NeutralIntent) for call in run.facade.calls), (
        "the facade must see ONLY typed high-level NeutralIntent requests — never a wire primitive"
    )
    # The facade has no submit/cancel/sign surface at all — its ONLY entry point is __call__(leg).
    assert not any(
        hasattr(run.facade, primitive)
        for primitive in ("submit_order", "cancel_order", "cancel_all_orders", "sign", "submit")
    ), "the OFFLINE recording facade exposes NO wire primitive"

    # Mode-A never submits: every boundary result is a clean ABSTAINED — nothing is possibly-unresolved,
    # nothing is assumed filled/withdrawn, and the book is NEVER treated flat (REQ-093/094).
    for plan_result in run.plan_results:
        assert plan_result.awaiting_reconciliation is False, "a clean Mode-A abstention reaches no live order"
        assert plan_result.frozen is False
        assert plan_result.book_treated_flat is False
        assert plan_result.replacement_triggered is False
        for outcome in plan_result.outcomes:
            assert outcome.possibly_unresolved is False
            assert outcome.assumed_filled is False
            assert outcome.assumed_withdrawn is False
            if outcome.attempted:
                assert outcome.result is not None
                assert outcome.result.execution_status != "SUBMITTED", "Mode A places NO order on any wire"


# =====================================================================================
# RED #3 — the whole lane leaves the sealed JSONs byte-identical; the rank surfaces reject R4-B rows.
# =====================================================================================


def test_whole_lane_sealed_byte_identical(tmp_path: Path) -> None:
    """WHOLE LANE (sealed byte-identity + rank rejection, AC-003/033 / SEC-004): running the offline
    R4-B lane leaves the enumerated E0-T1 sealed-JSON set BYTE-IDENTICAL (SHA-256 vs committed
    ``HEAD``) — the R4-B lane is a distinct strategy that never touches the sealed maker/directional
    artifacts — and the three rank surfaces REJECT any dumped R4-B diagnostic row (a diagnostic field
    can never enter the leaderboard).
    """
    sealed_files = _enumerate_sealed_json()
    # Anti-inert: the enumeration must carry EVERY known E0-T1 sealed output, so a shrunk set cannot
    # let the SHA loop pass vacuously (reused verbatim from the R4-A whole-lane gate).
    required_sealed = {
        "scripts/txline_live/cp1/maker-arena-result.json",
        "contracts/fixtures/leaderboard.json",
        "contracts/fixtures/maker_arena_result.json",
    }
    missing = required_sealed - set(sealed_files)
    assert not missing, (
        f"enumerated sealed set is missing required sealed outputs {sorted(missing)}; got {sealed_files}"
    )

    # Drive the WHOLE lane (both arms) — the real thing, not a stub — before the byte-identity check.
    arms = arm_configs(load_base_config_overrides())
    run_a = _drive_whole_lane("healthy", arms.baseline, tmp_path / "sealed_a")
    run_b = _drive_whole_lane("healthy", arms.guarded, tmp_path / "sealed_b")
    assert run_a.byte_reproduces and run_b.byte_reproduces, "both arms genuinely replayed (non-vacuous)"

    # (a) EACH enumerated sealed file on disk is byte-identical to its committed HEAD content (SHA-256).
    for rel_path in sealed_files:
        on_disk = (_REPO_ROOT / rel_path).read_bytes()
        committed = subprocess.run(
            ["git", "show", f"HEAD:{rel_path}"],
            cwd=_REPO_ROOT,
            capture_output=True,
            check=True,
        ).stdout
        assert hashlib.sha256(on_disk).hexdigest() == hashlib.sha256(committed).hexdigest(), (
            f"sealed file {rel_path} must be byte-identical to its committed HEAD content after the R4-B lane"
        )

    # (b) The three rank surfaces reject a dumped R4-B diagnostic row — EVERY R4-B telemetry field is
    # denylisted on the directional, cross-run CLV, and maker toxicity keys, INCLUDING the raw
    # sorted(..., key=...) bypass path. A whole-lane diagnostic can never rank.
    for field in sorted(EXPECTED_R4B_FIELDS):
        for keyfn, base in ((dir_key, VALID_DIR), (clv_key, VALID_DIR), (maker_rank_key, {})):
            with pytest.raises(AssertionError):
                keyfn({**base, field: 1.0})
            with pytest.raises(AssertionError):
                sorted([{**base, field: 1.0}], key=keyfn)
