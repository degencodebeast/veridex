"""E5-T4 — pending/ambiguous freeze + startup truth (the R4-B→R4-A adapter tier).

OFFLINE ONLY (REQ-093/094/097, AC-017/021, RED-15/16). The R4-A execution facade is an
INJECTABLE seam; every test here drives a RECORDING FAKE facade that counts calls and returns a
scripted :class:`MMExecutionToolResult`. No real facade call, no network, no wallet/signer/order/
submit/cancel, no Mode-B arm. The "exactly one facade call" assertions count calls on the FAKE.

The freeze safety boundary (REQ-093): the moment a fresh-write leg returns an UNCERTAIN outcome —
a plain ``SUBMITTED`` (possibly-unresolved ACK), a pending-reconciliation freeze
(``attempt_pending_reconciliation``), or a withheld ``mode_b_not_armed`` — every remaining
fresh-write leg is FROZEN and NO further fresh-write facade call is issued. ``SUBMITTED`` is never
assumed filled or withdrawn; the book is not treated flat until a reconciled projection confirms.
Resumption comes ONLY from a new observation/projection decision, never a within-plan retry.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import get_args

import pytest

from veridex.dust_execution.facade import (
    IntentKind,
    MMExecutionToolRequest,
    MMExecutionToolResult,
    MMIntentParams,
)
from veridex.dust_execution.manifest import StrategyExperimentManifest
from veridex.dust_execution.runner import _build_resting_order
from veridex.mm_strategy.config import StrategyConfig
from veridex.mm_strategy.contracts import (
    InventoryProjection,
    NeutralIntent,
    NeutralIntentKind,
    StrategyDecision,
    StrategyObservation,
)
from veridex.mm_strategy.core import projection_startup_gate
from veridex.mm_strategy.execution_adapter import (
    NEUTRAL_TO_R4A,
    PlanExecutionResult,
    R4ARequestConfig,
    build_r4a_request,
    execute_plan,
    freezes_fresh_writes,
    is_possibly_unresolved,
)
from veridex.policy.envelope import PolicyEnvelope

# --- builders ---------------------------------------------------------------------------------


def _fresh_write(client_order_id: str, *, leg_role: str = "bid", price: float = 0.49) -> NeutralIntent:
    """A fresh-write (``place_quote``) leg — the kind subject to the freeze boundary."""
    return NeutralIntent(
        kind="place_quote", leg_role=leg_role, price=price, client_order_id=client_order_id
    )


def _cancel_leg() -> NeutralIntent:
    """A risk-reducing cancel leg (never a fresh write)."""
    return NeutralIntent(kind="cancel_all_orders", leg_role=None, price=None)


def _result(
    *,
    admission: str = "APPROVED",
    execution_status: str = "ABSTAINED",
    reason_codes: tuple[str, ...] = (),
    execution_reason_codes: tuple[str, ...] = (),
) -> MMExecutionToolResult:
    """A typed boundary result with the honest pinned labels; only the disposition fields vary."""
    return MMExecutionToolResult(
        admission=admission,  # type: ignore[arg-type]
        reason_codes=reason_codes,
        execution_status=execution_status,  # type: ignore[arg-type]
        execution_reason_codes=execution_reason_codes,
        lifecycle_receipt_ref="dust-lifecycle:test:deadbeefdeadbeef",
        run_label="DUST_LIVE",
        calibration_label="UNCALIBRATED",
        edge_label="NOT_PROVEN_EDGE",
        evidence_class="EXPERIMENTAL_DUST",
        policy_hash="policy-hash",
    )


@dataclass
class _RecordingFakeFacade:
    """OFFLINE recording fake — counts calls and returns a scripted result per call.

    NEVER a real facade: no network, no wallet/signer, no submit/cancel. The seam consumes the typed
    :class:`MMExecutionToolRequest` the adapter built and pin-cross-checked (Gate #4 C-CRITICAL-1) —
    never a raw ``NeutralIntent`` — and records it, so ``calls`` is executable evidence the facade sees
    exactly the bound request whose result controls freezing. The scripted ``results`` are replayed in
    order (the last is reused if the plan somehow issues more calls than scripted, which would itself be
    a freeze-boundary violation the count assertion catches).
    """

    results: list[MMExecutionToolResult]
    calls: list[MMExecutionToolRequest] = field(default_factory=list)

    def __call__(self, request: MMExecutionToolRequest) -> MMExecutionToolResult:
        result = self.results[min(len(self.calls), len(self.results) - 1)]
        self.calls.append(request)
        return result

    @property
    def call_count(self) -> int:
        return len(self.calls)


def _run_reviewed_plan(
    *legs: NeutralIntent,
    facade: _RecordingFakeFacade,
    config: R4ARequestConfig | None = None,
    strategy_config: StrategyConfig | None = None,
) -> PlanExecutionResult:
    """Build the reviewed (observation, decision) pair for ``legs`` and drive the UNIFIED
    build→execute path (Gate #4 C-CRITICAL-1): ``execute_plan`` builds each actionable leg's bound
    typed request (the declared ``config`` cross-checked against the admitted pins DERIVED from the
    TRUSTED ``_pinned_manifest`` / ``_pinned_envelope``) and hands THAT request to ``facade``. This is
    the ONE composed path the freeze boundary now wraps — the facade never sees a raw ``NeutralIntent``."""
    observation, decision = _reviewed_pair(*legs)
    return execute_plan(
        decision,
        facade,
        observation=observation,
        config=config if config is not None else _pinned_config(),
        manifest=_pinned_manifest(),
        envelope=_pinned_envelope(),
        strategy_config=(
            strategy_config if strategy_config is not None else _pinned_strategy_config()
        ),
    )


# --- freeze on pending / uncertain ACK -------------------------------------------------------


def test_pending_leg_freezes_fresh_writes() -> None:
    """AC-017/RED-15: a leg returning ``attempt_pending_reconciliation`` freezes the remaining
    fresh-write legs — no further facade call is issued."""
    plan = (_fresh_write("A"), _fresh_write("B"), _fresh_write("C"))
    pending = _result(
        admission="DENIED",
        reason_codes=("attempt_pending_reconciliation",),
        execution_status="NOT_ARMED",
        execution_reason_codes=("mode_b_not_armed",),
    )
    facade = _RecordingFakeFacade(results=[pending])

    result = _run_reviewed_plan(*plan, facade=facade)

    assert facade.call_count == 1  # only leg A was attempted
    assert result.frozen is True
    assert result.outcomes[0].attempted is True
    assert result.outcomes[1].frozen is True  # B never attempted
    assert result.outcomes[2].frozen is True  # C never attempted
    assert result.awaiting_reconciliation is True


def test_submitted_is_possibly_unresolved_not_filled() -> None:
    """REQ-093: a ``SUBMITTED`` leg is possibly-unresolved — NEVER assumed filled or withdrawn."""
    submitted = _result(admission="APPROVED", execution_status="SUBMITTED")
    assert is_possibly_unresolved(submitted) is True

    plan = (_fresh_write("A"),)
    facade = _RecordingFakeFacade(results=[submitted])
    result = _run_reviewed_plan(*plan, facade=facade)

    outcome = result.outcomes[0]
    assert outcome.possibly_unresolved is True
    assert outcome.assumed_filled is False  # never assumed filled
    assert outcome.assumed_withdrawn is False  # never assumed withdrawn
    assert result.book_treated_flat is False  # book not flat until a reconciled projection confirms


def test_first_leg_submitted_freezes_second_fresh_write_exactly_one_facade_call() -> None:
    """Codex's exact object: a TWO-leg placement plan whose leg 1 ACKs ``SUBMITTED`` must issue
    EXACTLY ONE facade call total, freezing the second fresh-write leg until a NEW decision."""
    plan = (_fresh_write("A"), _fresh_write("B"))
    leg1 = MMExecutionToolResult(
        admission="APPROVED",
        execution_status="SUBMITTED",
        execution_reason_codes=(),
        reason_codes=(),
        lifecycle_receipt_ref="dust-lifecycle:test:deadbeefdeadbeef",
        run_label="DUST_LIVE",
        calibration_label="UNCALIBRATED",
        edge_label="NOT_PROVEN_EDGE",
        evidence_class="EXPERIMENTAL_DUST",
        policy_hash="policy-hash",
    )
    facade = _RecordingFakeFacade(results=[leg1])

    result = _run_reviewed_plan(*plan, facade=facade)

    assert facade.call_count == 1  # EXACTLY ONE facade call across the plan
    assert result.outcomes[0].attempted is True
    assert result.outcomes[1].frozen is True  # second fresh write frozen
    assert result.can_resume_within_plan is False  # only a new reconciled-projection decision resumes


def test_cancel_ack_alone_no_replacement() -> None:
    """AC-021/RED-16: a cancel ACK alone does NOT trigger a replacement — no fresh write is placed
    until a reconciled projection permits it."""
    plan = (_cancel_leg(),)
    cancel_ack = _result(admission="APPROVED", execution_status="SUBMITTED")  # cancel reached the wire
    facade = _RecordingFakeFacade(results=[cancel_ack])

    result = _run_reviewed_plan(*plan, facade=facade)

    assert facade.call_count == 1  # only the cancel — no replacement fresh write
    assert result.replacement_triggered is False
    assert all(outcome.leg.kind != "place_quote" for outcome in result.outcomes)
    assert result.book_treated_flat is False  # a cancel ACK is not assumed to have flattened the book


def test_missing_projection_no_quote() -> None:
    """REQ-097: startup truth comes from the INJECTED projection (never a venue query); a missing or
    stale projection is data-degraded → ``NO_QUOTE(projection_stale)``; a fresh one may proceed."""
    missing = projection_startup_gate(None)
    assert missing is not None
    assert missing.kind == "NO_QUOTE"
    assert missing.reason_codes == ("projection_stale",)

    stale = InventoryProjection(net_position=0.0, resting=(), projection_as_of_ts=1, fresh=False)
    stale_decision = projection_startup_gate(stale)
    assert stale_decision is not None
    assert stale_decision.kind == "NO_QUOTE"
    assert stale_decision.reason_codes == ("projection_stale",)

    fresh = InventoryProjection(net_position=0.0, resting=(), projection_as_of_ts=1, fresh=True)
    assert projection_startup_gate(fresh) is None  # present + fresh → startup may proceed


def test_reconcile_path_reached_not_bypassed() -> None:
    """The smm negative fixture: a possibly-unresolved outcome REACHES the reconcile path
    (``awaiting_reconciliation``) instead of early-returning as done/flat."""
    plan = (_fresh_write("A"), _fresh_write("B"))
    submitted = _result(admission="APPROVED", execution_status="SUBMITTED")
    facade = _RecordingFakeFacade(results=[submitted])

    result = _run_reviewed_plan(*plan, facade=facade)

    assert result.awaiting_reconciliation is True  # reconcile path REACHED, not bypassed
    assert result.book_treated_flat is False
    assert result.frozen is True
    assert freezes_fresh_writes(submitted) is True


def test_execute_plan_fails_closed_on_mixed_plan_before_first_call() -> None:
    """Gate #3 IMPORTANT-1 / RED-48 (+ Gate #4 C-CRITICAL-1): the single-phase invariant is enforced
    at the SOURCE. ``execute_plan`` now walks a reviewed ``StrategyDecision`` (the unified build→execute
    path), and a mixed cancel+placement plan is UNCONSTRUCTABLE at ``StrategyDecision`` — so the
    bare-tuple bypass surface no longer exists: there is no way to hand ``execute_plan`` a mixed plan.
    ``execute_plan`` re-asserts the invariant on ``decision.intent_plan`` as byte-safe defense in depth.
    A mixed plan must NEVER place a fresh write ahead of a reconciled projection confirming the cancel."""
    # Primary defense: a mixed plan (either ordering) is UNCONSTRUCTABLE at ``StrategyDecision``, so it
    # can never even be built into the input ``execute_plan`` now requires (no fresh write, no cancel).
    obs = _reviewed_observation()
    with pytest.raises(ValueError):
        StrategyDecision(
            kind="QUOTE_TWO_SIDED",
            intent_plan=(_cancel_leg(), _fresh_write("A")),  # cancel-phase + placement-phase
            observation_hash=obs.observation_hash(),
        )
    with pytest.raises(ValueError):
        StrategyDecision(
            kind="QUOTE_TWO_SIDED",
            intent_plan=(_fresh_write("A"), _cancel_leg()),  # reverse ordering, equally rejected
            observation_hash=obs.observation_hash(),
        )

    # Valid CONTROLS still execute normally through the composed path: a cancel-only plan (cancel THEN
    # abstain) and a placement-only plan each drive the facade for their single actionable phase, and
    # the facade sees the bound TYPED request (never a raw intent). A clean (non-freezing) ``ABSTAINED``
    # result keeps the control focused on the phase invariant.
    cancel_only_facade = _RecordingFakeFacade(results=[_result()])
    cancel_result = _run_reviewed_plan(
        _cancel_leg(),
        NeutralIntent(kind="abstain", leg_role=None, price=None),
        facade=cancel_only_facade,
    )
    assert cancel_only_facade.call_count == 1  # only the cancel is actionable; abstain is skipped
    assert cancel_result.frozen is False
    assert isinstance(cancel_only_facade.calls[0], MMExecutionToolRequest)

    placement_facade = _RecordingFakeFacade(results=[_result()])
    placement_result = _run_reviewed_plan(
        _fresh_write("A"), _fresh_write("B"), facade=placement_facade
    )
    assert placement_facade.call_count == 2  # both placement legs attempted (clean, non-freezing)
    assert placement_result.frozen is False
    assert all(isinstance(c, MMExecutionToolRequest) for c in placement_facade.calls)


# --- Gate#3 MINOR-1: skipped_non_actionable vs frozen_by_prior_outcome ------------------------


def test_clean_cancel_abstain_abstain_not_frozen() -> None:
    """Gate#3 MINOR-1: in a clean ``(cancel_all_orders, abstain)`` plan the trailing ``abstain`` leg
    was NEVER going to be attempted — it is ``skipped_non_actionable``, NOT frozen. Labeling a
    non-actionable skip as a freeze corrupts per-leg audit semantics: ``plan.frozen`` is False yet the
    abstain leg would have reported ``frozen == True`` under the old ``frozen == not attempted``."""
    plan = (_cancel_leg(), NeutralIntent(kind="abstain", leg_role=None, price=None))
    facade = _RecordingFakeFacade(results=[_result()])  # clean ABSTAINED cancel — non-freezing

    result = _run_reviewed_plan(*plan, facade=facade)

    assert facade.call_count == 1  # only the cancel is actionable; the abstain is skipped
    assert result.frozen is False  # plan not frozen — the cancel returned a clean result
    cancel_outcome, abstain_outcome = result.outcomes
    assert cancel_outcome.attempted is True
    # The abstain leg: skipped because non-actionable, NOT frozen by a prior uncertain outcome.
    assert abstain_outcome.attempted is False
    assert abstain_outcome.frozen is False  # honest label — a skip is not a freeze
    assert abstain_outcome.skipped_non_actionable is True
    assert abstain_outcome.frozen_by_prior_outcome is False


def test_uncertain_first_leg_freezes_subsequent() -> None:
    """Gate#3 MINOR-1 (preserves the E5-T4 freeze semantics): an uncertain first leg FREEZES every
    subsequent actionable leg — those are ``frozen_by_prior_outcome`` (``frozen == True``), a genuine
    freeze, not a non-actionable skip. This is the freeze half the MINOR-1 relabeling must not weaken."""
    plan = (_fresh_write("A"), _fresh_write("B"), _fresh_write("C"))
    submitted = _result(admission="APPROVED", execution_status="SUBMITTED")  # uncertain ACK

    result = _run_reviewed_plan(*plan, facade=_RecordingFakeFacade(results=[submitted]))

    assert result.frozen is True
    attempted_leg, *frozen_legs = result.outcomes
    assert attempted_leg.attempted is True
    for frozen_leg in frozen_legs:  # B and C — actionable legs stopped by the prior freeze
        assert frozen_leg.attempted is False
        assert frozen_leg.frozen is True  # frozen by the prior uncertain outcome
        assert frozen_leg.frozen_by_prior_outcome is True
        assert frozen_leg.skipped_non_actionable is False


# --- E5-T5: neutral→R4-A mapping + singular request + no size/take -----------------------------


def _pinned_config() -> R4ARequestConfig:
    """The ONE pinned request config — every hash/id/mode/sizing input is pinned session config,
    NEVER agent-supplied (REQ-058). ``wallet_equity``/``fixed_fraction`` are the pinned sizing
    inputs the adapter threads to R4-A's proposer; the adapter itself never sizes.

    The DECLARED hashes are DERIVED from the SAME reviewed ``_pinned_manifest`` / ``_pinned_envelope``
    the adapter cross-checks against (Gate #4 C-CRITICAL-1): this is the HONEST pinned strategy declaring
    the pins it genuinely operates under, so declared == the manifest/envelope-DERIVED admitted and the
    build succeeds. An attacker's unreviewed declaration (a free-form hash) mismatches the real manifest
    derivation and fails closed."""
    manifest = _pinned_manifest()
    envelope = _pinned_envelope()
    return R4ARequestConfig(
        strategy_id="mm-dust",
        strategy_config_hash=manifest.strategy_config_hash,
        policy_hash=envelope.policy_hash(),
        session_id="sess-1",
        manifest_hash=manifest.manifest_hash(),
        mode="dry_run",
        wallet_equity_at_decision=1000.0,
        fixed_fraction=0.001,
        tif="GTC",
    )


def _pinned_strategy_config(*, tif: str = "GTC") -> StrategyConfig:
    """The pinned, hash-bound ``StrategyConfig`` whose ``tif`` is the SINGLE time-in-force authority
    (Gate #4 F-MINOR-2 / REQ-056). ``execute_plan`` cross-checks the DECLARED ``R4ARequestConfig.tif``
    against THIS pinned tif — the wire tif must be the hash-bound maker tif, never a divergent
    request-config value. Defaults to ``GTC`` so it matches ``_pinned_config()``'s pinned tif."""
    return StrategyConfig(guard_enabled=False, tif=tif)  # type: ignore[arg-type]


# The reviewed token identity — in production this is the decision's
# ``observation.stream_identity().token_id`` (Gate #2 MAJOR-2). The adapter DERIVES the singular
# request's target token from the reviewed observation's stream identity (Gate#3 C-4 / CRITICAL-1
# residual), so the request targets EXACTLY the token the decision reviewed — never a caller string.
_R4A_TOKEN = "tok-reviewed"

# A SECOND manifest-admitted token — the substitution adversary's target (Gate#3 CRITICAL-1
# residual). A price decided from ``_R4A_TOKEN``'s book must NEVER be routable onto this token.
_OTHER_ADMITTED_TOKEN = "tok-other-admitted"


def _reviewed_observation(*, token_id: str = _R4A_TOKEN) -> StrategyObservation:
    """A healthy, guard-off reviewed observation on ``token_id`` — the TYPED reviewed input the
    adapter DERIVES the target token from (``stream_identity().token_id``), never a caller string."""
    return StrategyObservation(
        fixture_id=42,
        market_ref="TEAM-A/YES",
        side="YES",
        token_id=token_id,
        venue_market_ref="0xmarket",
        tick_size=0.01,
        observation_sequence=10,
        book_source_epoch=1,
        bid=0.49,
        ask=0.51,
        bid_size=100.0,
        ask_size=120.0,
        book_status="ok",
        status_reason=None,
        book_recv_ts=1_000,
        level_count_in_band=5,
        tick_regime_changed=False,
        phase=1,
        suspended=False,
        match_state_recv_ts=990,
        guard_fv=None,
        market_status="ACTIVE",
        market_status_recv_ts=995,
        market_status_epoch=3,
        order_stream_ok=True,
        projection_fresh=True,
        inventory=InventoryProjection(
            net_position=0.0, resting=(), projection_as_of_ts=1_000, fresh=True
        ),
        as_of_ts=1_000,
    )


def _reviewed_pair(
    *legs: NeutralIntent, token_id: str = _R4A_TOKEN
) -> tuple[StrategyObservation, StrategyDecision]:
    """The reviewed (observation, decision) pair the singular request is DERIVED-from and BOUND-to
    (Gate#3 CRITICAL-1 residual). The decision is STAMPED with the observation's own
    ``observation_hash`` and carries exactly ``legs`` in its single-phase plan, on ``token_id`` — so
    ``build_r4a_request`` derives the target token from the reviewed stream identity and refuses any
    leg/observation not bound to this decision. No caller may pass a bare token string."""
    observation = _reviewed_observation(token_id=token_id)
    decision = StrategyDecision(
        kind="QUOTE_TWO_SIDED",
        intent_plan=legs,
        observation_hash=observation.observation_hash(),
    )
    return observation, decision


def test_mapping_total_exact_image_take_excluded() -> None:
    """§6.3(5): ``NEUTRAL_TO_R4A`` is TOTAL over the neutral domain, its image is EXACTLY the
    non-aggressive 4-set, and the aggressive ``take`` kind is UNREACHABLE (never in the image)."""
    neutral_members = set(get_args(NeutralIntentKind))
    intent_members = set(get_args(IntentKind))

    # TOTAL: every NeutralIntentKind member is a key (no neutral kind is left unmapped).
    assert set(NEUTRAL_TO_R4A.keys()) == neutral_members

    # EXACT image: the closed non-aggressive 4-set — no more, no less.
    assert set(NEUTRAL_TO_R4A.values()) == {"make_quote", "cancel_replace", "cancel_all", "no_quote"}

    # ``take`` (the aggressive kind) is NEVER in the image — it is unrepresentable by design.
    assert "take" not in set(NEUTRAL_TO_R4A.values())
    assert "take" in intent_members  # (sanity: ``take`` IS a valid R4-A kind — just never emitted)

    # Every image value is a real R4-A ``IntentKind`` (no minted synonym).
    assert set(NEUTRAL_TO_R4A.values()) <= intent_members

    # The exact pinned pairing.
    assert dict(NEUTRAL_TO_R4A) == {
        "place_quote": "make_quote",
        "replace_quote": "cancel_replace",
        "cancel_all_orders": "cancel_all",
        "abstain": "no_quote",
    }


def test_adapter_pins_evidence_class_not_caller_param() -> None:
    """The adapter PINS ``evidence_class="EXPERIMENTAL_DUST"`` — it is a constant in the adapter,
    never a caller/agent argument (not on the build function, not on the pinned config)."""
    config = _pinned_config()
    leg = _fresh_write("A")
    obs, decision = _reviewed_pair(leg)
    request = build_r4a_request(
        leg, config, observation=obs, decision=decision, manifest=_pinned_manifest(), envelope=_pinned_envelope()
    )

    assert request.evidence_class == "EXPERIMENTAL_DUST"

    # NOT a caller/agent parameter of the adapter's request builder.
    builder_params = inspect.signature(build_r4a_request).parameters
    assert "evidence_class" not in builder_params

    # NOT a field of the pinned config either — an agent cannot smuggle it in via config.
    assert "evidence_class" not in set(R4ARequestConfig.__dataclass_fields__)


def test_adapter_sets_no_size() -> None:
    """RED-22 / REQ-058: the adapter sets NO agent size on the built request — sizing is deferred
    to R4-A's ``resolve_dust_size`` (the sole wire-size authority)."""
    config = _pinned_config()

    # A fresh-write (make_quote) leg is the sizeable case — the adapter still sets no size.
    make_leg = _fresh_write("A")
    make_obs, make_decision = _reviewed_pair(make_leg)
    make_request = build_r4a_request(
        make_leg, config, observation=make_obs, decision=make_decision, manifest=_pinned_manifest(), envelope=_pinned_envelope()
    )
    assert make_request.intent_params.size is None

    # Holds for every neutral kind: no built request ever carries an adapter-set size.
    for leg in (
        _fresh_write("A"),
        NeutralIntent(
            kind="replace_quote",
            leg_role="bid",
            price=0.5,
            client_order_id="B",
            replaces_client_order_id="B-old",
        ),
        _cancel_leg(),
        NeutralIntent(kind="abstain", leg_role=None, price=None),
    ):
        obs, decision = _reviewed_pair(leg)
        assert (
            build_r4a_request(
                leg, config, observation=obs, decision=decision, manifest=_pinned_manifest(), envelope=_pinned_envelope()
            ).intent_params.size
            is None
        )


def test_valid_replacement_lineage_reaches_r4a_request() -> None:
    """Codex Gate#3 IMPORTANT-2 / REQ-091 / AC-021: a VALID `replace_quote` (naming the exact prior
    order) reaches the singular R4-A request with its lineage preserved UNCHANGED — the adapter maps
    the kind to `cancel_replace` and forwards `replaces_client_order_id` verbatim, never dropping or
    rewriting the old-order reference the decision committed to."""
    config = _pinned_config()
    leg = NeutralIntent(
        kind="replace_quote",
        leg_role="bid",
        price=0.5,
        client_order_id="new-order",
        replaces_client_order_id="old-order-1",
    )

    obs, decision = _reviewed_pair(leg)
    request = build_r4a_request(
        leg, config, observation=obs, decision=decision, manifest=_pinned_manifest(), envelope=_pinned_envelope()
    )

    # The lineage the decision named survives the neutral→R4-A translation byte-for-byte.
    assert request.intent_params.replaces_client_order_id == "old-order-1"
    # ... and the surrounding translation is the wireable maker replacement (kind + reviewed token).
    assert request.intent_params.token_id == _R4A_TOKEN
    assert request.intent_params.side == "BUY"


def test_reason_confidence_cannot_move_decision() -> None:
    """AC-024 / RED-21: untrusted FV metadata (reason / confidence / proof status) has ZERO effect
    on the mapping or the built request — it is never an input to, nor forwarded by, the adapter."""
    config = _pinned_config()
    leg = _fresh_write("A")
    obs, decision = _reviewed_pair(leg)

    # The adapter never forwards untrusted agent metadata: the request carries no reason/confidence.
    request = build_r4a_request(
        leg, config, observation=obs, decision=decision, manifest=_pinned_manifest(), envelope=_pinned_envelope()
    )
    assert request.reason is None
    assert request.confidence is None

    # The builder's ONLY inputs are the TRUSTED leg, the pinned config, and the reviewed
    # (observation, decision) pair — no untrusted-metadata CHANNEL exists. The target token is
    # DERIVED from ``observation.stream_identity().token_id`` and BOUND to the stamped decision
    # (Gate#3 C-4 / CRITICAL-1 residual), never a caller-supplied bare string.
    builder_params = set(inspect.signature(build_r4a_request).parameters)
    assert builder_params == {"leg", "config", "observation", "decision", "manifest", "envelope"}
    assert "token_id" not in builder_params  # no bare caller channel for the target token
    assert not (builder_params & {"reason", "confidence", "proof_status", "fv_message_id"})

    # Two decisions that DIFFER only in untrusted FV metadata — bound to the SAME reviewed
    # observation and carrying the SAME leg — build the IDENTICAL request (FV metadata has zero
    # effect on the mapping, the derived token, or the binding).
    hot = StrategyDecision(
        kind="QUOTE_TWO_SIDED",
        intent_plan=(leg,),
        observation_hash=obs.observation_hash(),
        fv_message_id="msg-hot",
        fv_proof_status="proven",
    )
    cold = StrategyDecision(
        kind="QUOTE_TWO_SIDED",
        intent_plan=(leg,),
        observation_hash=obs.observation_hash(),
        fv_message_id="msg-cold",
        fv_proof_status="absent",
    )
    assert build_r4a_request(
        hot.intent_plan[0], config, observation=obs, decision=hot, manifest=_pinned_manifest(), envelope=_pinned_envelope()
    ) == build_r4a_request(
        cold.intent_plan[0], config, observation=obs, decision=cold, manifest=_pinned_manifest(), envelope=_pinned_envelope()
    )

    # The intent kind is a pure function of the TRUSTED leg.kind — nothing else moves it.
    assert request.intent_kind == NEUTRAL_TO_R4A[leg.kind]


# --- Gate #3 CRITICAL-1: wireability at the REAL R4-A boundary ----------------------------------


def _boundary_manifest() -> StrategyExperimentManifest:
    """A minimal R4-A manifest for the REAL ``_build_resting_order`` constructor (it reads only
    ``strategy_id`` from the manifest, as the client-order-id fallback). OFFLINE: a pure constructor
    input — no venue, no signer, no wire, no submit/cancel."""
    return StrategyExperimentManifest(
        strategy_id="dust-maker-v0",
        strategy_config_hash="cfg" * 4,
        evidence_class="EXPERIMENTAL_DUST",
        market="0xcondition",
        universe=(_R4A_TOKEN,),
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


def _pinned_manifest() -> StrategyExperimentManifest:
    """The REVIEWED session manifest — the TRUSTED admitted authority the declared ``R4ARequestConfig``
    is cross-checked against (Gate #4 C-CRITICAL-1 residual; mirrors R4-A
    ``runner._authorization_block_reason``, runner.py:1149). The admitted pins the adapter DERIVES —
    ``manifest.manifest_hash()`` (a sha256 over every field) and ``manifest.strategy_config_hash`` —
    come from THIS object, supplied by the session/composed layer, NEVER a free-form value the request
    caller can set. Reuses ``_boundary_manifest`` (universe == (``_R4A_TOKEN``,)) so the SAME reviewed
    manifest anchors the wireability proofs and the admitted-pin authority."""
    return _boundary_manifest()


def _pinned_envelope() -> PolicyEnvelope:
    """The REVIEWED policy envelope — its ``policy_hash()`` is the admitted policy pin (runner.py:1150),
    the SAME trusted authority R4-A's runner sources the admitted policy hash from. OFFLINE: a pure
    pydantic value; no venue, signer, or wire."""
    return PolicyEnvelope(
        max_stake=5.0,
        max_orders_per_run=3,
        max_orders_per_session=10,
        max_orders_per_day=20,
        venue_allowlist=["poly"],
        market_allowlist=["0xcondition"],
        min_edge_bps=10,
        max_slippage_bps=50,
        max_price=0.99,
        max_quote_age_s=5,
        cooldown_s=1,
        human_approval_threshold=100.0,
    )


def test_normal_bid_and_ask_build_valid_r4a_resting_order() -> None:
    """Gate #3 CRITICAL-1 (RED at the REAL boundary): a normal ``place_quote(bid, 0.49)`` and
    ``place_quote(ask, 0.51)`` — the adapter's NORMAL output for a core-produced quote — must EACH
    build ``intent_params`` that construct a VALID post-only R4-A resting order via the REAL
    ``_build_resting_order`` (bid→BUY, ask→SELL; the reviewed ``token_id``; the config-pinned GTC
    maker TIF), with the resting SIZE R4-A-owned (``wire_size``), never adapter-set.

    Before the fix ``_intent_params`` forwards ``side='bid'`` / ``token_id=None`` / ``tif=None``, so
    ``_build_resting_order`` returns ``None`` — the adapter's normal output is UNWIREABLE for every
    core quote (Codex CRITICAL-1)."""
    config = _pinned_config()
    manifest = _boundary_manifest()
    wire_size = 4.0  # R4-A's resolve_dust_size output — the SOLE size authority (never the adapter)
    tick_size = 0.01

    bid_leg = _fresh_write("bid-1", leg_role="bid", price=0.49)
    ask_leg = _fresh_write("ask-1", leg_role="ask", price=0.51)
    bid_obs, bid_decision = _reviewed_pair(bid_leg)
    ask_obs, ask_decision = _reviewed_pair(ask_leg)

    bid_params = build_r4a_request(
        bid_leg, config, observation=bid_obs, decision=bid_decision, manifest=_pinned_manifest(), envelope=_pinned_envelope()
    ).intent_params
    ask_params = build_r4a_request(
        ask_leg, config, observation=ask_obs, decision=ask_decision, manifest=_pinned_manifest(), envelope=_pinned_envelope()
    ).intent_params

    # The adapter set NO size — sizing stays R4-A-owned (REQ-058/RED-22).
    assert bid_params.size is None
    assert ask_params.size is None

    bid_order = _build_resting_order(
        token_id=_R4A_TOKEN,
        manifest=manifest,
        intent_params=bid_params,
        wire_size=wire_size,
        tick_size=tick_size,
    )
    ask_order = _build_resting_order(
        token_id=_R4A_TOKEN,
        manifest=manifest,
        intent_params=ask_params,
        wire_size=wire_size,
        tick_size=tick_size,
    )

    # WIREABLE post-only maker orders (each was None — unwireable — before the fix).
    assert bid_order is not None
    assert bid_order.side == "BUY"
    assert bid_order.tif == "GTC"
    assert bid_order.post_only is True
    assert bid_order.native_price == 0.49
    assert bid_order.token_id == _R4A_TOKEN
    assert bid_order.size == wire_size  # R4-A-owned size, not adapter-set

    assert ask_order is not None
    assert ask_order.side == "SELL"
    assert ask_order.tif == "GTC"
    assert ask_order.post_only is True
    assert ask_order.native_price == 0.51
    assert ask_order.token_id == _R4A_TOKEN
    assert ask_order.size == wire_size


def test_token_substitution_two_admitted_tokens_fails_closed() -> None:
    """Gate #3 CRITICAL-1 (RESIDUAL): with TWO manifest-admitted tokens, a decision reviewed from
    observation A (``tok-reviewed``) can NEVER be routed onto a DIFFERENT admitted token B
    (``tok-other-admitted``). The singular request's target token is DERIVED from the reviewed
    observation's ``stream_identity().token_id`` and BOUND to the stamped decision
    (``decision.observation_hash == observation.observation_hash()``) — a caller has NO bare-string
    channel to substitute token B.

    Before the fix, ``build_r4a_request(reviewed_bid_leg, config, token_id="tok-other-admitted")``
    copied the bare caller string into ``MMIntentParams`` and built a VALID, wireable R4-A
    ``RestingOrder`` on token B — a price decided from token A's book targeting token B (Codex
    CRITICAL-1 residual). After the fix that channel does not exist: substitution FAILS CLOSED (no
    request built, no resting order, no facade call), and the only buildable request derives token A."""
    config = _pinned_config()
    leg = _fresh_write("bid-1", leg_role="bid", price=0.49)

    # The decision was reviewed from observation A (token "tok-reviewed").
    obs_a, decision = _reviewed_pair(leg, token_id=_R4A_TOKEN)

    # Adversary lever #1 — pass a DIFFERENT admitted observation (token B) to try to steer the
    # derived token to "tok-other-admitted". That observation is NOT the one the decision reviewed,
    # so its hash != decision.observation_hash → FAIL CLOSED (no request built, no facade call).
    obs_b = _reviewed_observation(token_id=_OTHER_ADMITTED_TOKEN)
    assert obs_b.observation_hash() != decision.observation_hash
    with pytest.raises(ValueError):
        build_r4a_request(
            leg, config, observation=obs_b, decision=decision, manifest=_pinned_manifest(), envelope=_pinned_envelope()
        )

    # Adversary lever #2 — a leg the reviewed decision never committed to also fails closed.
    foreign_leg = _fresh_write("foreign", leg_role="ask", price=0.51)
    with pytest.raises(ValueError):
        build_r4a_request(
            foreign_leg, config, observation=obs_a, decision=decision, manifest=_pinned_manifest(), envelope=_pinned_envelope()
        )

    # The ONLY buildable request derives the reviewed token A — token B is unrepresentable here.
    request = build_r4a_request(
        leg, config, observation=obs_a, decision=decision, manifest=_pinned_manifest(), envelope=_pinned_envelope()
    )
    assert request.intent_params.token_id == _R4A_TOKEN
    assert request.intent_params.token_id != _OTHER_ADMITTED_TOKEN

    # ... and it is a VALID wireable token-A resting order at the REAL R4-A boundary (bid→BUY/GTC),
    # so the fix is corrected-not-weakened: the normal output stays wireable, only substitution is
    # barred.
    manifest = _boundary_manifest()  # universe == (_R4A_TOKEN,)
    order = _build_resting_order(
        token_id=_R4A_TOKEN,
        manifest=manifest,
        intent_params=request.intent_params,
        wire_size=4.0,
        tick_size=0.01,
    )
    assert order is not None
    assert order.token_id == _R4A_TOKEN
    assert order.side == "BUY"
    assert order.tif == "GTC"


def test_wrong_or_missing_token_side_tif_fails_closed() -> None:
    """Gate #3 CRITICAL-1: a request with a wrong/missing token, a non-{BUY,SELL} side, or a
    non-{GTC,GTD} TIF must FAIL CLOSED at the REAL R4-A boundary — ``_build_resting_order`` returns
    ``None`` and the singular target token matches no universe token. The adapter never emits a
    wireable resting order from a malformed leg."""
    manifest = _boundary_manifest()
    wire_size = 4.0
    tick_size = 0.01

    def _rest(params: MMIntentParams) -> object:
        return _build_resting_order(
            token_id=_R4A_TOKEN,
            manifest=manifest,
            intent_params=params,
            wire_size=wire_size,
            tick_size=tick_size,
        )

    # Codex's exact pre-fix control: the neutral role forwarded literally + no token + no TIF.
    prefix_control = MMIntentParams(token_id=None, side="bid", price=0.49, tif=None)
    assert _rest(prefix_control) is None  # unwireable — R4-A rejects the maker order

    # Each wire dimension is independently load-bearing at the real boundary:
    assert _rest(MMIntentParams(token_id=_R4A_TOKEN, side="bid", price=0.49, tif="GTC")) is None  # role literal
    assert _rest(MMIntentParams(token_id=_R4A_TOKEN, side="BUY", price=0.49, tif=None)) is None  # missing TIF
    assert _rest(MMIntentParams(token_id=_R4A_TOKEN, side="BUY", price=0.49, tif="FOK")) is None  # taker TIF

    # A missing target token: the runner's C-4 target (``intent_params.token_id``) matches NO
    # universe token, so every token abstains ``intent_token_mismatch`` (fail closed, zero wire).
    assert prefix_control.token_id is None
    assert prefix_control.token_id not in manifest.universe


# =====================================================================================
# Gate #4 C-CRITICAL-1 — unify build↔execute: the facade consumes the bound TYPED request, the
# admitted pin is INDEPENDENTLY sourced (no self-select), and the freeze wraps the request-level call.
# =====================================================================================


@dataclass
class _StrictRequestFacade:
    """Codex control 1: a facade that REQUIRES the typed :class:`MMExecutionToolRequest` — as the real
    R4-A proposer ``propose_mm_execution`` does — and TypeErrors on a raw ``NeutralIntent``. Before the
    unification ``execute_plan`` called ``facade(leg)`` with the raw neutral intent, so this facade
    raised; after, it receives the exact bound typed request. Records the requests it consumed."""

    result: MMExecutionToolResult
    calls: list[MMExecutionToolRequest] = field(default_factory=list)

    def __call__(self, request: object) -> MMExecutionToolResult:
        if not isinstance(request, MMExecutionToolRequest):
            raise TypeError(
                f"facade requires MMExecutionToolRequest, got {type(request).__name__}"
            )
        self.calls.append(request)
        return self.result


def test_facade_receives_typed_request_not_raw_intent() -> None:
    """Gate #4 C-CRITICAL-1 (control 1): the unified build→execute path hands the R4-A facade the EXACT
    bound typed ``MMExecutionToolRequest`` for each leg — NEVER a raw ``NeutralIntent``. A facade that
    (like the real proposer) requires a typed request no longer TypeErrors, and each request it receives
    is byte-identical to ``build_r4a_request`` for the same reviewed (observation, decision) — so the
    token/side/TIF/hashes the facade consumes ARE the bound reviewed values.

    Before the fix ``execute_plan`` called ``facade(leg)`` with the raw neutral intent, so a
    request-requiring facade raised ``TypeError`` and no whole-lane path proved the facade consumed the
    bound request whose result controls freezing (Codex CRITICAL-1)."""
    config = _pinned_config()
    manifest = _pinned_manifest()
    envelope = _pinned_envelope()
    bid = _fresh_write("bid-1", leg_role="bid", price=0.49)
    ask = _fresh_write("ask-1", leg_role="ask", price=0.51)
    observation, decision = _reviewed_pair(bid, ask)

    facade = _StrictRequestFacade(result=_result())  # clean ABSTAINED — non-freezing, both legs fire
    result = execute_plan(
        decision,
        facade,
        observation=observation,
        config=config,
        manifest=manifest,
        envelope=envelope,
        strategy_config=_pinned_strategy_config(),
    )

    # Both actionable legs reached the facade as TYPED requests (no TypeError = not a raw intent).
    assert len(facade.calls) == 2
    assert all(isinstance(r, MMExecutionToolRequest) for r in facade.calls)

    # Each received request is byte-identical to the standalone build for the SAME reviewed pair —
    # the facade consumes exactly the bound, pin-cross-checked request, not a detached side artifact.
    assert facade.calls[0] == build_r4a_request(
        bid, config, observation=observation, decision=decision, manifest=manifest, envelope=envelope
    )
    assert facade.calls[1] == build_r4a_request(
        ask, config, observation=observation, decision=decision, manifest=manifest, envelope=envelope
    )

    # The bound wire fields the facade actually consumes are exactly the reviewed values.
    assert facade.calls[0].intent_params.token_id == _R4A_TOKEN
    assert facade.calls[0].intent_params.side == "BUY"
    assert facade.calls[0].intent_params.tif == "GTC"
    assert facade.calls[1].intent_params.side == "SELL"
    assert result.frozen is False


def test_request_tif_must_equal_pinned_strategy_tif() -> None:
    """Gate #4 F-MINOR-2 / REQ-056: the TIF that reaches the wire has ONE authority — the pinned,
    hash-bound ``StrategyConfig.tif`` (covered by ``strategy_config_hash``). ``execute_plan``
    cross-checks the DECLARED ``R4ARequestConfig.tif`` against that pinned tif and FAILS CLOSED on a
    disagreement — no request built, no facade call — so the applied tif can never silently diverge
    from the tif the identity hash commits to (mirrors the declared-vs-admitted pin cross-check).

    Before the fix ``StrategyConfig.tif`` was hash-bound but read NOWHERE while the adapter applied its
    OWN unhashed ``R4ARequestConfig.tif`` — so changing the hashed knob moved the identity hash but
    changed no wire behavior, and the tif that DID reach the wire was uncommitted (F-MINOR-2)."""
    pinned = _pinned_strategy_config(tif="GTC")  # the SINGLE hash-bound tif authority
    leg = _fresh_write("A", leg_role="bid", price=0.49)
    observation, decision = _reviewed_pair(leg)

    # A request config whose tif DISAGREES with the pinned, hash-bound strategy tif (GTD ≠ GTC).
    drifted = R4ARequestConfig(
        strategy_id="mm-dust",
        strategy_config_hash="cfg-hash",
        policy_hash="policy-hash",
        session_id="sess-1",
        manifest_hash="manifest-hash",
        mode="dry_run",
        wallet_equity_at_decision=1000.0,
        fixed_fraction=0.001,
        tif="GTD",
    )
    facade = _RecordingFakeFacade(results=[_result()])
    with pytest.raises(ValueError):
        execute_plan(
            decision,
            facade,
            observation=observation,
            config=drifted,
            manifest=_pinned_manifest(), envelope=_pinned_envelope(),
            strategy_config=pinned,
        )
    # Fail closed BEFORE any facade call — no order, no wire tif divergence.
    assert facade.call_count == 0

    # Corrected-not-weakened: when the declared request tif EQUALS the pinned tif, the honest path
    # builds and the built request emits EXACTLY the pinned, hash-bound tif on the wire.
    ok_facade = _RecordingFakeFacade(results=[_result()])
    execute_plan(
        decision,
        ok_facade,
        observation=observation,
        config=_pinned_config(),  # tif == "GTC" == pinned.tif
        manifest=_pinned_manifest(), envelope=_pinned_envelope(),
        strategy_config=pinned,
    )
    assert ok_facade.call_count == 1
    assert ok_facade.calls[0].intent_params.tif == pinned.tif == "GTC"


def test_freeze_preserved_around_request_level_call() -> None:
    """Gate #4 C-CRITICAL-1 (control 3): the first-uncertain FREEZE is preserved AROUND the unified
    request-level call. A three-leg placement plan whose first leg ACKs ``SUBMITTED`` issues EXACTLY ONE
    facade call (a TYPED request), freezes the two remaining fresh writes (no further request built, no
    further facade call), reaches the reconcile path, and never treats the book flat or resumes in-plan."""
    plan = (_fresh_write("A"), _fresh_write("B"), _fresh_write("C"))
    submitted = _result(admission="APPROVED", execution_status="SUBMITTED")  # uncertain first-leg ACK
    facade = _RecordingFakeFacade(results=[submitted])

    result = _run_reviewed_plan(*plan, facade=facade)

    assert facade.call_count == 1  # EXACTLY ONE request-level facade call across the plan
    assert isinstance(facade.calls[0], MMExecutionToolRequest)  # and it consumed a TYPED request
    assert result.frozen is True
    assert result.awaiting_reconciliation is True  # reconcile path REACHED, not bypassed
    assert result.outcomes[0].attempted is True
    assert result.outcomes[1].frozen is True  # second fresh write frozen (never built/called)
    assert result.outcomes[2].frozen is True  # third fresh write frozen
    assert result.can_resume_within_plan is False
    assert result.book_treated_flat is False


# =====================================================================================
# Gate #4 C-CRITICAL-1 RESIDUAL — the admitted-pin authority is SOURCED from the trusted
# manifest/envelope (a sha256 derivation), NEVER a caller-forgeable AdmittedPins parameter.
# =====================================================================================


def test_unreviewed_config_cannot_reach_facade_via_forged_admission() -> None:
    """Gate #4 C-CRITICAL-1 RESIDUAL (the money boundary): an unreviewed ``live_guarded``
    ``R4ARequestConfig`` (attacker manifest/policy/config hashes) cross-checked against the REAL
    reviewed ``manifest``/``envelope`` FAILS CLOSED — the recording facade sees ZERO calls and no
    unfrozen result escapes.

    The admitted authority is now DERIVED from the TRUSTED manifest/envelope
    (``manifest.manifest_hash()`` / ``envelope.policy_hash()`` / ``manifest.strategy_config_hash``),
    not a caller-supplied ``AdmittedPins`` at the same call level as the declared config. So a single
    caller can no longer supply MATCHING values for BOTH the declared config AND the admitted side to
    self-approve (object separation was not authority separation). This mirrors R4-A
    ``runner._authorization_block_reason``: the SESSION supplies the manifest/envelope (the independent
    admitted authority), the AGENT supplies only the declared request.

    Codex reproduced the pre-fix hole EXACTLY: ``facade_calls=1 mode=live_guarded
    manifest=unreviewed-manifest policy=unreviewed-policy config_hash=unreviewed-cfg frozen=False``.
    After the fix there is no admitted parameter, and the unreviewed declared config mismatches the
    manifest-DERIVED admitted → fail closed. Forging the admitted side now requires forging the
    reviewed manifest itself (a sha256 preimage) — the R4-A/review trust boundary, out of adapter scope.
    """
    manifest = _pinned_manifest()  # the REAL reviewed authority — NOT caller-forgeable free strings
    envelope = _pinned_envelope()
    leg = _fresh_write("A", leg_role="bid", price=0.49)
    observation, decision = _reviewed_pair(leg)

    # Codex's exact adversarial object: unreviewed hashes under the real-money live_guarded mode.
    evil = R4ARequestConfig(
        strategy_id="mm-dust",
        strategy_config_hash="unreviewed-cfg",
        policy_hash="unreviewed-policy",
        session_id="sess-1",
        manifest_hash="unreviewed-manifest",
        mode="live_guarded",
        wallet_equity_at_decision=1000.0,
        fixed_fraction=0.001,
        tif="GTC",
    )

    # Direct build: the declared pins are cross-checked against the manifest-DERIVED admitted side and
    # FAIL CLOSED — the config cannot self-select its own admitted pin (there is no admitted parameter).
    with pytest.raises(ValueError):
        build_r4a_request(
            leg,
            evil,
            observation=observation,
            decision=decision,
            manifest=manifest,
            envelope=envelope,
        )

    # Composed path (the capstone control Codex demanded): no request is built, so the recording facade
    # sees ZERO calls and no unfrozen result escapes (fail closed BEFORE any facade call).
    facade = _RecordingFakeFacade(results=[_result()])
    with pytest.raises(ValueError):
        execute_plan(
            decision,
            facade,
            observation=observation,
            config=evil,
            manifest=manifest,
            envelope=envelope,
            strategy_config=_pinned_strategy_config(),
        )
    assert facade.call_count == 0  # the unreviewed config reached ZERO facade calls


def test_admitted_pins_parameter_is_gone() -> None:
    """The forgeable ``AdmittedPins`` channel is REMOVED: neither ``execute_plan`` nor
    ``build_r4a_request`` accepts an ``admitted`` parameter (a caller can no longer supply the admitted
    side at all); both now REQUIRE the TRUSTED ``manifest``/``envelope`` the admitted pins are DERIVED
    from. The dataclass itself is gone from the adapter's public surface — there is no caller-constructible
    admitted authority anywhere."""
    import veridex.mm_strategy.execution_adapter as adapter_module

    exec_params = set(inspect.signature(execute_plan).parameters)
    build_params = set(inspect.signature(build_r4a_request).parameters)

    assert "admitted" not in exec_params, "the forgeable admitted channel must be gone from execute_plan"
    assert "admitted" not in build_params, "the forgeable admitted channel must be gone from build_r4a_request"
    assert {"manifest", "envelope"} <= exec_params, "execute_plan must require the trusted manifest/envelope"
    assert {"manifest", "envelope"} <= build_params, "build_r4a_request must require the trusted manifest/envelope"

    assert not hasattr(adapter_module, "AdmittedPins"), (
        "the forgeable AdmittedPins dataclass must be removed — admitted pins are DERIVED from the "
        "trusted manifest/envelope, never a caller-constructible parameter"
    )


def test_reviewed_config_matching_manifest_reaches_facade() -> None:
    """Corrected-not-weakened (the HAPPY path): a declared ``R4ARequestConfig`` whose hashes MATCH the
    reviewed ``manifest``/``envelope`` (the pinned strategy operating under its own reviewed authority)
    builds and reaches the facade EXACTLY once with the bound request. The fix bars a MISMATCH, never
    the honest run — and the request the facade consumes carries the manifest-DERIVED admitted pins."""
    manifest = _pinned_manifest()
    envelope = _pinned_envelope()
    leg = _fresh_write("A", leg_role="bid", price=0.49)
    observation, decision = _reviewed_pair(leg)

    facade = _RecordingFakeFacade(results=[_result()])  # clean ABSTAINED — non-freezing
    result = execute_plan(
        decision,
        facade,
        observation=observation,
        config=_pinned_config(),  # its hashes are DERIVED from the SAME reviewed manifest/envelope
        manifest=manifest,
        envelope=envelope,
        strategy_config=_pinned_strategy_config(),
    )

    assert facade.call_count == 1  # the legitimate declaration reaches the facade
    request = facade.calls[0]
    # The declared config matched the manifest-DERIVED admitted authority (declared == admitted).
    assert request.manifest_hash == manifest.manifest_hash()
    assert request.policy_hash == envelope.policy_hash()
    assert request.strategy_config_hash == manifest.strategy_config_hash
    assert result.frozen is False
