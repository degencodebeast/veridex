"""E8-T1 (AC-016, §6 final gate): the WHOLE-lane dust-execution integration test + the full
R4-A regression + the SEALED byte-identity gate.

This is the whole-lane end-to-end proof for the R4-A dust-execution lane. It drives ONE Mode-A
(``dry_run``) session through the SANCTIONED entry — :func:`veridex.dust_execution.facade.
propose_mm_execution` (facade.py) → :func:`veridex.dust_execution.runner.run_dust_execution`
(runner.py, ``mode="dry_run"``) — with the CANONICAL offline recording fakes (never a live
venue / Privy / provisioning / order surface), exercising the whole lane in one flow:

    manifest pinned -> authorized -> mechanically sized (E2-T6) -> intents admitted through the
    non-crossing + risk + reconcile gates -> the lifecycle stream sealed -> a simulated safety
    trigger (breaker / realized-loss-cap / kill-switch) fires the ONE idempotent cancel-all on the
    wire -> shutdown leaves the session FLAT.

It asserts the cross-cutting invariants (each mapped to its acceptance criterion in the test
docstrings):

1. Mode A NEVER submits — every decision is ``dry_run_not_submitted`` / ``venue_order_id=None``
   and NO order reaches any submit surface (the generic adapter OR the keyless write port).
2. The ONE cancel-all — a simulated breaker-open / realized-loss-cap breach / kill-switch EACH
   fires EXACTLY ONE ``cancel_all_orders`` on the wire (idempotent single sweep), sets the
   submit-block, and records the honest trigger cause.
3. Shutdown FLAT — the session ends with no resting own exposure.
4. Lifecycle stream sealed — the emitted stream is well-formed, ordered, gap-free.
5. Three rank surfaces reject the full R4-A field set (denylist/rank guards hold).
6. R4-A import isolation bars hold.
7. SEALED byte-identity (the core deliverable) — the enumerated E0-T1 sealed-JSON set is
   byte-identical after the whole-lane session, proven BOTH via
   :func:`~veridex.maker.result.assert_score_run_untouched` around a ``score_run`` capture AND via
   a SHA-256 of the on-disk sealed file vs its committed ``HEAD`` content.

REUSE, not reinvention: the fakes / manifest+envelope builders / recording write port / rank-guard
+ import-bar patterns / ``assert_score_run_untouched`` are imported from the existing suites
(``test_dust_execution_facade`` / ``test_dust_execution_runner`` / ``test_dust_execution_sec_isolation``
/ ``test_no_r3_r4_code``) — this file wires them into ONE whole-lane flow and adds no bespoke fake.

TRUSTED-INPUT BOUNDARY (executable note, not a production edit): the facade is the SOLE sanctioned
entry. ``run_dust_execution`` is exercised DIRECTLY below ONLY as the runner half of the same
sanctioned lane (runner.py, the exact delegate the facade calls) to inspect decision/lifecycle
detail the typed facade result encapsulates — never as a Mode-B entry. Every direct-runner call
here is ``mode="dry_run"`` with no arming money surface; the Mode-A dual-surface submit checks are
executable evidence that even a PRESENT keyless write port is never touched in dry-run.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

import veridex.maker as maker_pkg

# --- Reuse the canonical Mode-A facade + runner fixtures (no bespoke fakes) -------------------
from tests.test_dust_execution_facade import (
    _admitted_request,
    _mm_clock,
    _mm_env,
    _mm_fresh_quote,
    _mm_manifest,
    _mm_noop_sleep,
    _MMScriptedSource,
)
from tests.test_dust_execution_runner import (
    _EXPECTED_EVENT_TYPES,
    _FIXED_FRACTION,
    _INTERLOCK_STORE,
    _WALLET_EQUITY,
    RecordingFakeAdapter,
    _arming,
    _binding,
    _clock,
    _default_write_port,
    _env,
    _fresh_quote,
    _manifest,
    _noop_sleep,
    _RecordingWritePort,
    _ScriptedSource,
)

# Reuse the R4-A rank-guard ground truth + the three rank-key surfaces (assertion #5).
from tests.test_dust_execution_sec_isolation import EXPECTED_R4A_FIELDS, VALID_DIR

# Reuse the static import-bar detector (assertion #6).
from tests.test_no_r3_r4_code import _imports_module, _walk_modnames
from veridex.dust_execution import facade
from veridex.dust_execution.contracts import OrderAckEvent
from veridex.dust_execution.emergency import DustSafetySession, SafetyController
from veridex.dust_execution.facade import MMExecutionToolResult
from veridex.dust_execution.risk import RealizedFillRecord, RiskAccumulator
from veridex.dust_execution.runner import DustExecutionResult, run_dust_execution
from veridex.dust_execution.signer import LocalFakeWalletControlPlane
from veridex.leaderboard import _rank_key as clv_key
from veridex.maker.leaderboard import maker_rank_key
from veridex.maker.result import assert_score_run_untouched
from veridex.policy.circuit_breaker import CircuitBreaker, CircuitState
from veridex.rank_guards import R3_R4_RANK_DENYLIST, R4A_EXECUTION_DENYLIST_FIELDS
from veridex.runtime.evidence import compute_evidence_hash
from veridex.runtime.orchestrator import RunResult
from veridex.runtime.runtime_events import RuntimeEvent, RuntimeEventType
from veridex.scoring import _rank_key as dir_key
from veridex.scoring import score_run

_REPO_ROOT = Path(__file__).resolve().parents[1]
_NOW_S = 1_700_000_000  # the frozen offline clock the runner fixtures use (integer seconds)
#: The Mode-A safety/ledger join key the runner runs a self-driven dry-run under.
_MODE_A_SESSION_ID = "dust-maker-v0:dry_run"


# =====================================================================================
# Shared whole-lane drivers (facade boundary + the runner half of the same sanctioned lane).
# =====================================================================================


async def _drive_whole_lane_facade(
    *, kill_switch: bool = False
) -> tuple[MMExecutionToolResult, RecordingFakeAdapter, list[RuntimeEvent]]:
    """Drive ONE Mode-A (``dry_run``) session through the SANCTIONED facade entry.

    Returns ``(tool_result, adapter, ops_sink)``. A ``kill_switch`` envelope simulates the safety
    trigger that fires the ONE cancel-all on the wire through the facade path.
    """
    manifest = _mm_manifest()
    envelope = _mm_env(kill_switch=kill_switch)
    request = _admitted_request(manifest, envelope)
    adapter = RecordingFakeAdapter(fill=True, open_orders=[])
    sink: list[RuntimeEvent] = []

    result = await facade.propose_mm_execution(
        request,
        adapter=adapter,
        signer=LocalFakeWalletControlPlane(),
        sources=_MMScriptedSource(_mm_fresh_quote()),
        now_fn=_mm_clock,
        sleep_fn=_mm_noop_sleep,
        envelope=envelope,
        manifest=manifest,
        wallet_equity_at_decision=100.0,
        fixed_fraction=0.01,
        event_sink=sink.append,
    )
    return result, adapter, sink


async def _drive_whole_lane_runner_detail() -> (
    tuple[DustExecutionResult, RecordingFakeAdapter, _RecordingWritePort]
):
    """Drive the runner half of the SAME sanctioned lane (runner.py, ``mode="dry_run"``).

    Mirrors the exact delegation the facade performs (facade.py calls ``run_dust_execution`` with
    ``request.mode``); used ONLY to inspect the decision/lifecycle detail the typed facade result
    encapsulates. Injects a PRESENT keyless write port through a Mode-A arming bundle so the
    dual-surface no-submit check (assertion #1) is executable — a present write port that Mode A
    never touches (it is not vacuously absent).
    """
    binding = _binding()
    write_port = _default_write_port(binding)
    adapter = RecordingFakeAdapter(fill=True, open_orders=[])
    result = await run_dust_execution(
        adapter=adapter,
        signer=LocalFakeWalletControlPlane(),
        sources=_ScriptedSource(quote=_fresh_quote()),
        now_fn=_clock,
        sleep_fn=_noop_sleep,
        envelope=_env(),
        manifest=_manifest(mode="dry_run"),
        mode="dry_run",
        wallet_equity_at_decision=_WALLET_EQUITY,
        fixed_fraction=_FIXED_FRACTION,
        # A PRESENT Mode-B arming bundle (with a recording write port) — ignored by dry-run, so its
        # write port MUST stay untouched. Makes the "flip the dry-run guard" mutation catchable.
        arming=_arming(binding, write_port=write_port),
        operator_interlock_store=_INTERLOCK_STORE,
    )
    return result, adapter, write_port


# =====================================================================================
# Assertions #1, #3, #4 — the whole-lane Mode-A dry-run session (no submit / flat / sealed stream).
# =====================================================================================


async def test_whole_lane_mode_a_dry_run_session_never_submits_and_seals_the_stream() -> None:
    """WHOLE LANE (AC-017/003/009): a clean Mode-A session driven end-to-end through the sanctioned
    facade entry admits the pinned EXPERIMENTAL_DUST manifest, mechanically sizes, admits the intent,
    seals the lifecycle stream, and ends FLAT — placing NO order on any wire.

    Covers assertion #1 (Mode A never submits), #3 (shutdown flat), #4 (lifecycle sealed).
    """
    # (A) The facade boundary — the sanctioned whole-lane entry.
    tool_result, adapter, sink = await _drive_whole_lane_facade()

    # #1: Mode A never submits — no order reached the generic adapter's submit wire.
    assert adapter.submit_calls == 0, "Mode A must place NO order on the generic adapter wire"
    # A clean dry-run admits the manifest (APPROVED) and honestly reports the dry-run abstention.
    assert tool_result.admission == "APPROVED"
    assert tool_result.execution_status == "ABSTAINED"
    assert "mode_a_no_orders" in tool_result.execution_reason_codes
    # Honest labels — never a proven-edge / calibrated claim.
    assert tool_result.run_label == "DUST_LIVE"
    assert tool_result.calibration_label == "UNCALIBRATED"
    assert tool_result.edge_label == "NOT_PROVEN_EDGE"
    assert tool_result.evidence_class == "EXPERIMENTAL_DUST"
    # An opaque lifecycle receipt REF into the evidence stream — never a raw handle.
    assert isinstance(tool_result.lifecycle_receipt_ref, str)
    assert tool_result.lifecycle_receipt_ref.startswith("dust-lifecycle:")

    # #4 (boundary view): the OPS lifecycle telemetry is well-formed + ordered — RUN_STARTED first,
    # RUN_COMPLETED last, every event on the OPS channel (never a tool/evidence path).
    assert sink, "the facade must emit its lifecycle into the injected OPS sink"
    assert all(isinstance(e, RuntimeEvent) and e.channel == "OPS" for e in sink)
    assert sink[0].type == RuntimeEventType.RUN_STARTED
    assert sink[-1].type == RuntimeEventType.RUN_COMPLETED
    completed = sink[-1]
    # #3 (boundary view): the session ends FLAT — zero submitted, a bounded SUCCESS safety outcome.
    assert completed.payload.get("submitted_count") == 0, "a Mode-A session ends with NO submits (flat)"
    assert completed.payload.get("session_status") == "SUCCESS"

    # (B) The runner half of the SAME sanctioned lane — the decision/lifecycle detail the typed
    # facade result encapsulates (runner.py delegate, mode="dry_run").
    result, runner_adapter, write_port = await _drive_whole_lane_runner_detail()

    # #1 (dual submit surface): NO order reaches the generic adapter OR the keyless write port —
    # even with a PRESENT write port in the (ignored) arming bundle.
    assert runner_adapter.submit_calls == 0, "Mode A must not reach the generic adapter submit surface"
    assert write_port.submit_calls == 0, "Mode A must not reach the keyless write-port submit surface"
    assert result.submitted_count == 0

    # #1 (per decision): every decision is a dry-run abstention with NO fabricated venue order id.
    for decision in result.decisions:
        assert decision.submitted is False
        assert decision.abstain_reason == "mode_a_no_orders"
        assert decision.venue_order_id is None
    # The ack event honestly records the dry-run-not-submitted status + a None venue order id.
    ack = next(e for e in result.events if isinstance(e, OrderAckEvent))
    assert ack.ack_status == "dry_run_not_submitted"
    assert ack.venue_order_id is None

    # #4 (sealed stream): the lifecycle event stream is well-formed, correctly ordered, and gap-free.
    assert result.session_meta.mode == "dry_run"
    types = tuple(type(e).__name__ for e in result.events)
    assert types == _EXPECTED_EVENT_TYPES, f"lifecycle stream shape drifted: {types}"
    seqs = [e.sequence_no for e in result.events]
    assert seqs == list(range(1, len(seqs) + 1)), "sequence_no must be append-only, unique, gap-free"
    # The shared canonical evidence-hash helper independently rejects a malformed/duplicate stream.
    compute_evidence_hash([e.model_dump() for e in result.events])

    # #3 (runner view): a clean dry-run leaves no resting exposure — the SUCCESS terminal outcome and
    # a leave_open shutdown that fired NO wire (there was nothing to sweep — no order was ever placed).
    assert result.session_outcome.status == "SUCCESS"
    assert result.session_outcome.promoted is False
    assert result.shutdown_decision.cancel_all_fired is False


# =====================================================================================
# Assertion #2 — the ONE cancel-all: breaker / loss-cap / kill-switch EACH fires EXACTLY ONE
# idempotent sweep, sets the submit-block, and records the honest trigger cause.
# =====================================================================================


async def _run_mode_a_with_trigger(
    *,
    breaker: CircuitBreaker | None = None,
    realized_fills: tuple[RealizedFillRecord, ...] = (),
    envelope_kwargs: dict[str, object] | None = None,
) -> tuple[DustExecutionResult, RecordingFakeAdapter, DustSafetySession]:
    """Drive a Mode-A (``dry_run``) session that surfaces ONE safety trigger to the runner.

    Reuses the E2-T3 :class:`SafetyController` / :class:`DustSafetySession` primitives; the recording
    fake's ``cancel_all_calls`` increments ONLY when the sweep coroutine is actually awaited.
    """
    adapter = RecordingFakeAdapter(fill=True, open_orders=[])
    safety = SafetyController(clock_ms=lambda: _NOW_S * 1000)
    session = DustSafetySession(session_id=_MODE_A_SESSION_ID)
    risk = RiskAccumulator(_MODE_A_SESSION_ID)
    envelope = _env(**(envelope_kwargs or {}))
    result = await run_dust_execution(
        adapter=adapter,
        signer=LocalFakeWalletControlPlane(),
        sources=_ScriptedSource(quote=_fresh_quote()),
        now_fn=_clock,
        sleep_fn=_noop_sleep,
        envelope=envelope,
        manifest=_manifest(mode="dry_run"),
        mode="dry_run",
        wallet_equity_at_decision=_WALLET_EQUITY,
        fixed_fraction=_FIXED_FRACTION,
        safety=safety,
        session=session,
        risk=risk,
        breaker=breaker,
        realized_fills=realized_fills,
    )
    return result, adapter, session


async def test_each_safety_trigger_fires_exactly_one_cancel_all_with_honest_cause() -> None:
    """SAF-002/003/010 (assertion #2): each runner-reachable safety trigger fires EXACTLY ONE
    idempotent ``cancel_all_orders`` on the wire, sets the submit-block, and records the honest
    trigger cause — even in Mode A (the sweep is a safety mechanism, never gated on mode). No order
    ever reaches the submit wire, and the recorded cancel-all ack carries NO venue order id."""
    # (a) BREAKER-OPEN.
    result, adapter, session = await _run_mode_a_with_trigger(
        breaker=CircuitBreaker(state=CircuitState.OPEN, opened_at=0.0, consecutive_failures=5)
    )
    assert adapter.cancel_all_calls == 1, "breaker-open must fire the recording-fake cancel-all WIRE once"
    assert session.submit_blocked is True
    assert adapter.submit_calls == 0
    assert result.decisions[0].abstain_reason == "safety_blocked"
    assert session.last_cancel_all_ack is not None
    assert session.last_cancel_all_ack.trigger_cause == "breaker"
    assert "venue_order_id" not in session.last_cancel_all_ack.model_dump()

    # (b) REALIZED-LOSS-CAP BREACH — driven by a REAL fill through the RiskAccumulator.
    loss_fill = RealizedFillRecord(
        realized_pnl=-2.5, fee=0.0, session_id=_MODE_A_SESSION_ID, fill_ts_ms=_NOW_S * 1000
    )
    result, adapter, session = await _run_mode_a_with_trigger(
        realized_fills=(loss_fill,),
        envelope_kwargs={"max_session_loss": 2.0, "max_daily_loss": 4.0},
    )
    assert adapter.cancel_all_calls == 1, "a realized-loss breach must fire the cancel-all WIRE once"
    assert session.submit_blocked is True
    assert adapter.submit_calls == 0
    assert result.decisions[0].abstain_reason == "safety_blocked"
    assert session.last_cancel_all_ack is not None
    assert session.last_cancel_all_ack.trigger_cause == "loss_breach"
    assert "venue_order_id" not in session.last_cancel_all_ack.model_dump()

    # (c) KILL-SWITCH ENGAGE.
    result, adapter, session = await _run_mode_a_with_trigger(envelope_kwargs={"kill_switch": True})
    assert adapter.cancel_all_calls == 1, "kill-switch engage must fire the cancel-all WIRE once"
    assert session.submit_blocked is True
    assert adapter.submit_calls == 0
    assert result.decisions[0].abstain_reason == "safety_blocked"
    assert session.last_cancel_all_ack is not None
    assert session.last_cancel_all_ack.trigger_cause == "kill_switch"
    assert "venue_order_id" not in session.last_cancel_all_ack.model_dump()


async def test_facade_kill_switch_fires_the_one_cancel_all_on_the_wire_and_ends_flat() -> None:
    """Assertion #2 (through the SANCTIONED facade entry): a simulated kill-switch in the hash-pinned
    envelope fires EXACTLY ONE ``cancel_all_orders`` on the wire, denies the run for the honest
    ``kill_switch_engaged`` cause, blocks submits, and ends FLAT (zero submitted, the sweep cleared
    any exposure)."""
    tool_result, adapter, sink = await _drive_whole_lane_facade(kill_switch=True)

    assert adapter.cancel_all_calls == 1, "the facade path must fire EXACTLY ONE cancel-all on the wire"
    assert adapter.submit_calls == 0, "a swept Mode-A session must place NO order on the wire"
    # Honest denial cause surfaced on the typed result (admission + execution disposition).
    assert tool_result.admission == "DENIED"
    assert "kill_switch_engaged" in tool_result.reason_codes
    assert tool_result.execution_status == "DENIED"
    assert "safety_blocked" in tool_result.execution_reason_codes
    # Ends FLAT — zero submitted, and the safety-derived terminal reports the halted outcome.
    completed = sink[-1]
    assert completed.payload.get("submitted_count") == 0
    assert completed.payload.get("session_status") == "FAILED"


# =====================================================================================
# Assertion #5 — the three rank surfaces still reject the full R4-A execution field set.
# Mirrors tests/test_dust_execution_sec_isolation.py (reuses its independent EXPECTED_R4A_FIELDS
# ground truth + VALID_DIR row + the three canonical rank-key surfaces).
# =====================================================================================


@pytest.mark.parametrize("field", sorted(EXPECTED_R4A_FIELDS))
def test_all_three_rank_surfaces_still_reject_every_r4a_field(field: str) -> None:
    # The guard (AssertionError, not KeyError) fires FIRST on every surface — including the raw
    # sorted(..., key=...) bypass — for every realized-execution OUTCOME field.
    for keyfn, base in ((dir_key, VALID_DIR), (clv_key, VALID_DIR), (maker_rank_key, {})):
        with pytest.raises(AssertionError):
            keyfn({**base, field: 1.0})
        with pytest.raises(AssertionError):
            sorted([{**base, field: 1.0}], key=keyfn)


def test_r4a_field_set_still_matches_ground_truth_and_is_denylisted() -> None:
    # The production denylist EXACTLY equals the independent ground-truth literal, and the canonical
    # rank denylist enforces it — so a forgotten R4-A field can never rank across the three surfaces.
    assert R4A_EXECUTION_DENYLIST_FIELDS == EXPECTED_R4A_FIELDS
    assert EXPECTED_R4A_FIELDS <= R3_R4_RANK_DENYLIST


# =====================================================================================
# Assertion #6 — the R4-A import isolation bars still hold (mirrors test_no_r3_r4_code.py's
# import-bar assertions): no ranked lane imports the dust-execution or the recorder lane.
# =====================================================================================


def test_r4a_import_isolation_bars_hold() -> None:
    # The three directional rank surfaces (recursive veridex.maker.* + scoring + leaderboard) must
    # import NEITHER the R4-A real-fill dust-execution lane NOR the R3 counterfactual recorder lane.
    ranked = _walk_modnames(maker_pkg, "veridex.maker.") + ["veridex.scoring", "veridex.leaderboard"]
    assert ranked, "the recursive ranked-lane scan must find modules (anti-inert)"

    dust_offenders = [m for m in ranked if _imports_module(m, "veridex.dust_execution")]
    assert not dust_offenders, f"ranked lanes must not import veridex.dust_execution: {dust_offenders}"

    recorder_offenders = [m for m in ranked if _imports_module(m, "veridex.live_recorder")]
    assert not recorder_offenders, f"ranked lanes must not import veridex.live_recorder: {recorder_offenders}"


# =====================================================================================
# Assertion #7 — the SEALED byte-identity gate (the core deliverable, AC-016 / SEC-004).
#
# The enumerated E0-T1 sealed-JSON set is COMPUTED (not a hardcoded wildcard) so the assertion
# tracks the real committed set, then proven byte-identical after the whole-lane session BOTH via
# assert_score_run_untouched around a score_run capture AND via a SHA-256 of the on-disk file vs
# its committed HEAD content. Mirrors tests/test_dust_execution_sec_isolation.py:498-517 and
# tests/test_live_recorder_sec005.py:238.
# =====================================================================================


def _enumerate_sealed_json() -> list[str]:
    """Compute the enumerated E0-T1 sealed-JSON set from the REAL committed tree.

    Runs the task's canonical enumeration — ``git ls-files`` over the two sealed roots filtered to
    the maker/arena/leaderboard/clv/score family — so the byte-identity assertion tracks the actual
    committed set rather than a hardcoded wildcard.

    NOTE: the roots are passed as DIRECTORY pathspecs (``git ls-files`` expands them recursively),
    NOT ``**/*.json`` globs. A ``dir/**/*.json`` pathspec requires an intermediate directory segment
    and so DROPS direct children — e.g. ``contracts/fixtures/leaderboard.json`` — silently shrinking
    the sealed set to a subset (the sealed directional leaderboard + maker fixtures AC-016/SEC-004
    name would go unverified). Enumerate the directories and filter by suffix + family instead.
    """
    listed = subprocess.run(
        ["git", "ls-files", "scripts/txline_live/", "contracts/fixtures/"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    keywords = ("maker", "arena", "leaderboard", "clv", "score")
    return sorted(
        p for p in listed if p.lower().endswith(".json") and any(k in p.lower() for k in keywords)
    )


def _representative_directional_run() -> RunResult:
    """A representative directional run the byte-identity capture scores (mirrors sec005/sec_isolation)."""

    def _row(agent_id: str, tick_seq: int, clv_bps: int, confidence: float | None = None) -> dict:
        params: dict = {"market_key": "OU_2_5", "side": "over"}
        if confidence is not None:
            params["confidence"] = confidence
        return {
            "agent_id": agent_id,
            "tick_seq": tick_seq,
            "clv_bps": clv_bps,
            "valid": True,
            "reason": "",
            "raw_prescore": {"raw_action": {"type": "FLAG_VALUE", "params": params}},
        }

    rows = [
        _row("agent-alpha", 0, 15, confidence=0.7),
        _row("agent-alpha", 1, -5, confidence=0.4),
        _row("agent-beta", 0, 8, confidence=0.55),
        _row("agent-beta", 1, 3),
    ]
    return RunResult(
        run_id="run-e8t1",
        source_mode="replay",
        agent_ids=["agent-alpha", "agent-beta"],
        run_events=[],
        score_rows=rows,
        evidence_hash="",
        proof_mode_map={"agent-alpha": "reproducible", "agent-beta": "reproducible"},
    )


async def test_sealed_json_byte_identical_after_whole_lane_session() -> None:
    """AC-016 / SEC-004 (assertion #7, the core deliverable): running the WHOLE R4-A dust-execution
    lane leaves the enumerated E0-T1 sealed-JSON set BYTE-IDENTICAL.

    (a) The directional leaderboard ``score_run`` result is byte-identical across the whole-lane
        session (``assert_score_run_untouched``).
    (b) EACH enumerated sealed file on disk is SHA-256-identical to its committed ``HEAD`` content.
    The enumerated list is COMPUTED from the real committed tree (never a hardcoded wildcard).
    """
    sealed_files = _enumerate_sealed_json()
    # Anti-inert: the enumeration must include EVERY known E0-T1 sealed output — the sealed maker
    # AND directional-leaderboard fixtures AC-016 ("sealed maker and directional outputs") /
    # SEC-004 name — so a future glob/relocation regression cannot silently shrink the set.
    required_sealed = {
        "scripts/txline_live/cp1/maker-arena-result.json",
        "contracts/fixtures/leaderboard.json",
        "contracts/fixtures/maker_arena_result.json",
    }
    missing = required_sealed - set(sealed_files)
    assert not missing, (
        f"enumerated sealed set is missing required sealed outputs {sorted(missing)}; got {sealed_files}"
    )

    run = _representative_directional_run()
    before = score_run(run)

    # Drive the WHOLE lane end-to-end (the clean dry-run session AND a safety-swept session) — the
    # real thing, not a stub — so the byte-identity proof spans an actual whole-lane run.
    await _drive_whole_lane_facade()
    await _drive_whole_lane_facade(kill_switch=True)
    await _drive_whole_lane_runner_detail()

    after = score_run(run)
    # (a) The sealed directional leaderboard result is byte-identical across the whole-lane session.
    assert_score_run_untouched(before, after)  # raises iff before != after

    # (b) EACH enumerated sealed file is byte-identical to its committed HEAD content (SHA-256).
    for rel_path in sealed_files:
        on_disk = (_REPO_ROOT / rel_path).read_bytes()
        committed = subprocess.run(
            ["git", "show", f"HEAD:{rel_path}"],
            cwd=_REPO_ROOT,
            capture_output=True,
            check=True,
        ).stdout
        assert hashlib.sha256(on_disk).hexdigest() == hashlib.sha256(committed).hexdigest(), (
            f"sealed file {rel_path} must be byte-identical to its committed HEAD content"
        )
