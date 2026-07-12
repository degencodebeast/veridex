"""E1-T3 tests for the agent-callable MM tool boundary (§4.3, AC-020, §6 group 10).

Trust boundaries proven here:

* ``MMExecutionToolRequest`` is frozen + ``extra="forbid"``; a REQUIRED pinned hash that is
  missing is rejected at construction (fail closed). The sanctioned admission constructor
  :meth:`MMExecutionToolRequest.build` cross-checks the strategy-declared
  ``manifest_hash`` / ``policy_hash`` / ``strategy_config_hash`` against the admitted pins and
  RAISES on any mismatch — a mismatch is a hard failure, never a soft flag (§4.3, group 12).
* ``MMExecutionToolResult`` is frozen + ``extra="forbid"`` and is STRUCTURALLY incapable of
  carrying a raw venue/signer/client handle: every field annotation bottoms out in a
  JSON-primitive leaf (``str``/``int``/``float``/``bool``/``None``) or a pinned ``Literal`` —
  never a rich object type. This is AC-020 / §6 group 10 (the result returns only a typed
  admission + an opaque ``lifecycle_receipt_ref``, never a live client/wallet/key).
* The honest labels reuse the pinned literals from ``contracts.py`` (``DUST_LIVE`` /
  ``UNCALIBRATED`` / ``NOT_PROVEN_EDGE`` / ``EXPERIMENTAL_DUST``); the result carries no
  profitability/edge claim field.
"""

from __future__ import annotations

import dataclasses
import typing

import pytest
from pydantic import BaseModel, ValidationError

from veridex.dust_execution import facade  # module handle: proposer looked up dynamically (RED-clean)
from veridex.dust_execution.clobv2_gate import Clobv2GateResult
from veridex.dust_execution.contracts import DustRunLabelEvent, OperatorInterlockEvent
from veridex.dust_execution.facade import (
    MMExecutionToolRequest,
    MMExecutionToolResult,
    MMIntentParams,
    OperatorInterlock,
)
from veridex.dust_execution.manifest import StrategyExperimentManifest
from veridex.dust_execution.privy_control_plane import PrivyPreflightResult, ProvisioningResult
from veridex.dust_execution.runner import (
    BookSide,
    DustQuote,
    ModeBArming,
    run_dust_execution,
)
from veridex.dust_execution.signer import LocalFakeWalletControlPlane
from veridex.dust_execution.wallet_binding import (
    ALLOWED_SIGN_METHOD,
    CHAIN_ID_POLYGON,
    CLOB_AUTH_PRIMARY_TYPE,
    ORDER_PRIMARY_TYPE,
    AuthorizationQuorum,
    ExecutionWalletBinding,
    PolicyRule,
    PrivyWalletPolicy,
)
from veridex.policy.envelope import PolicyEnvelope
from veridex.runtime import agent as runtime_agent
from veridex.runtime.runtime_events import RuntimeEvent, RuntimeEventType
from veridex.runtime.schemas import AgentAction, SportsActionType
from veridex.venues.sx_bet import FakeVenueAdapter

# JSON-primitive leaf types a boundary-safe result field may bottom out in.
_ALLOWED_LEAF_TYPES = frozenset({str, int, float, bool, type(None)})


def _assert_only_safe_leaves(annotation: object) -> None:
    """Recursively assert an annotation contains ONLY primitive/Literal leaves.

    A raw venue/signer/client handle would surface as a rich object type (a bare class not in
    ``_ALLOWED_LEAF_TYPES`` and not a pydantic ``BaseModel`` primitive carrier); this walk fails
    on it, making the no-raw-handle guarantee STRUCTURAL rather than by-inspection.
    """
    origin = typing.get_origin(annotation)
    if origin is None:
        if isinstance(annotation, type):
            # A nested BaseModel would (recursively) also have to be handle-free; the result
            # contract intentionally uses NO nested model, so any bare class must be a leaf.
            assert not (
                isinstance(annotation, type) and issubclass(annotation, BaseModel)
            ), f"result field nests a model ({annotation!r}); keep result fields flat"
            assert annotation in _ALLOWED_LEAF_TYPES, (
                f"unsafe result field leaf type {annotation!r} — a raw handle could hide here"
            )
        return
    if origin is typing.Literal:
        for arg in typing.get_args(annotation):
            assert isinstance(arg, (str, int, bool)), f"non-primitive Literal member {arg!r}"
        return
    for arg in typing.get_args(annotation):
        if arg is Ellipsis:
            continue
        _assert_only_safe_leaves(arg)


def _params() -> MMIntentParams:
    return MMIntentParams(
        token_id="0xtokenYES",
        side="BUY",
        price=0.42,
        size=1.0,
        tif="FAK",
        client_order_id="coid-1",
    )


def _request_fields() -> dict[str, object]:
    return {
        "intent_kind": "take",
        "intent_params": _params(),
        "strategy_id": "dust-maker-v0",
        "strategy_config_hash": "cfg" * 4,
        "policy_hash": "pol-hash",
        "session_id": "sess-1",
        "manifest_hash": "MANIFEST_GOOD",
        "evidence_class": "EXPERIMENTAL_DUST",
        "mode": "dry_run",
    }


def test_facade_result_never_carries_raw_handles_and_hash_mismatch_denies() -> None:
    # (A) The sanctioned admission constructor builds when the declared pins MATCH the admitted
    # pins, and the untrusted reason/confidence are accepted but distinct from the pins.
    fields = _request_fields()
    ok = MMExecutionToolRequest.build(
        admitted_manifest_hash="MANIFEST_GOOD",
        admitted_policy_hash="pol-hash",
        admitted_strategy_config_hash="cfg" * 4,
        reason="agent thinks this is a good quote",  # untrusted metadata
        confidence=0.99,  # untrusted metadata
        **fields,
    )
    assert ok.manifest_hash == "MANIFEST_GOOD"
    assert ok.intent_kind == "take"

    # (B) A MISMATCHED manifest_hash fails closed at build/validation — a hard raise, not a flag.
    with pytest.raises(ValueError):
        MMExecutionToolRequest.build(
            admitted_manifest_hash="MANIFEST_OTHER",  # != declared "MANIFEST_GOOD"
            admitted_policy_hash="pol-hash",
            admitted_strategy_config_hash="cfg" * 4,
            **fields,
        )
    # A mismatched policy_hash and a mismatched strategy_config_hash also fail closed.
    with pytest.raises(ValueError):
        MMExecutionToolRequest.build(
            admitted_manifest_hash="MANIFEST_GOOD",
            admitted_policy_hash="WRONG_POLICY",
            admitted_strategy_config_hash="cfg" * 4,
            **fields,
        )
    with pytest.raises(ValueError):
        MMExecutionToolRequest.build(
            admitted_manifest_hash="MANIFEST_GOOD",
            admitted_policy_hash="pol-hash",
            admitted_strategy_config_hash="WRONG_CFG",
            **fields,
        )

    # (C) A MISSING required pinned hash is rejected at construction (extra="forbid" + required).
    missing = {k: v for k, v in fields.items() if k != "manifest_hash"}
    with pytest.raises(ValidationError):
        MMExecutionToolRequest(**missing)

    # (D) The request is frozen + extra="forbid": an unmodelled/leaked field is rejected.
    with pytest.raises(ValidationError):
        MMExecutionToolRequest(venue_client="leaked-handle", **fields)

    # (E) STRUCTURAL no-raw-handle guarantee: every MMExecutionToolResult field bottoms out in a
    # JSON-primitive/Literal leaf — no field of venue/signer/client type can exist (AC-020).
    result = MMExecutionToolResult(
        admission="APPROVED",
        reason_codes=("admitted",),
        lifecycle_receipt_ref="receipt:0xabc",
        run_label="DUST_LIVE",
        calibration_label="UNCALIBRATED",
        edge_label="NOT_PROVEN_EDGE",
        evidence_class="EXPERIMENTAL_DUST",
        policy_hash="pol-hash",
    )
    assert result.lifecycle_receipt_ref == "receipt:0xabc"
    assert result.admission == "APPROVED"
    for field in MMExecutionToolResult.model_fields.values():
        _assert_only_safe_leaves(field.annotation)

    # (F) The result is frozen + extra="forbid": a raw handle field cannot be smuggled in.
    with pytest.raises(ValidationError):
        MMExecutionToolResult(
            admission="APPROVED",
            reason_codes=(),
            lifecycle_receipt_ref="receipt:0xabc",
            run_label="DUST_LIVE",
            calibration_label="UNCALIBRATED",
            edge_label="NOT_PROVEN_EDGE",
            evidence_class="EXPERIMENTAL_DUST",
            policy_hash="pol-hash",
            venue_client="leaked-handle",  # extra="forbid" rejects a raw handle field
        )


# ---------------------------------------------------------------------------
# E7-T1 — the injectable MM facade proposer (R4-B intent -> R4-A execute).
#
# The proposer is NOT a tool on the tools=[] agent: it drives R4-A admission/
# execution/reconciliation THROUGH the runner and emits its lifecycle ONLY into
# the OPS RuntimeEvent sink, returning a typed MMExecutionToolResult + an opaque
# lifecycle receipt REF (never a raw venue/signer/client handle). REQ-003,
# AC-019/020/026.
# ---------------------------------------------------------------------------

_MM_TOKEN = "0xtokenYES"
_MM_NOW_S = 1_700_000_000


def _mm_manifest(**kw: object) -> StrategyExperimentManifest:
    base: dict[str, object] = {
        "strategy_id": "dust-maker-v0",
        "strategy_config_hash": "cfg" * 4,
        "evidence_class": "EXPERIMENTAL_DUST",
        "market": "0xcondition",
        "universe": (_MM_TOKEN,),
        "mode": "dry_run",
        "max_orders": 3,
        "max_notional": 5.0,
        "max_session_loss": 2.0,
        "max_daily_loss": 4.0,
        "session_window": (1_700_000_000_000, 1_700_000_600_000),
        "required_inputs": ("fair_value", "venue_book"),
        "permitted_intent_kinds": ("make_quote", "take", "cancel_replace", "cancel_all", "no_quote"),
        "market_fee_snapshot_hash": "fee" * 4,
        "operator_authorization": "op-ref-1",
        "forbidden_claims": ("PROVEN_EDGE", "CALIBRATED"),
    }
    base.update(kw)
    return StrategyExperimentManifest(**base)  # type: ignore[arg-type]


def _mm_env(**kw: object) -> PolicyEnvelope:
    base: dict[str, object] = {
        "max_stake": 100.0,
        "max_orders_per_run": 5,
        "max_orders_per_session": 20,
        "max_orders_per_day": 50,
        "venue_allowlist": ["sx_bet"],
        "market_allowlist": ["0xcondition"],
        "min_edge_bps": 50,
        "max_slippage_bps": 100,
        "max_price": 3.0,
        "max_quote_age_s": 10,
        "cooldown_s": 0,
        "human_approval_threshold": 1000.0,
        "kill_switch": False,
    }
    base.update(kw)
    return PolicyEnvelope(**base)  # type: ignore[arg-type]


def _mm_fresh_quote() -> DustQuote:
    return DustQuote(
        token_id=_MM_TOKEN,
        quote_ts_s=_MM_NOW_S,
        event_suspended=False,
        no_quote=False,
        bid=BookSide(price=0.49, size=10.0),
        ask=BookSide(price=0.51, size=10.0),
    )


class _MMScriptedSource:
    """A recording-free injected quote source returning one scripted, gate-passing quote."""

    def __init__(self, quote: DustQuote) -> None:
        self._quote = quote
        self.reads: list[str] = []

    async def read_quote(self, token_id: str) -> DustQuote:
        self.reads.append(token_id)
        return self._quote


def _mm_clock() -> int:
    return _MM_NOW_S


async def _mm_noop_sleep(_seconds: float) -> None:  # injected sleep seam — never a real wall-clock wait
    return None


def _admitted_request(
    manifest: StrategyExperimentManifest,
    envelope: PolicyEnvelope,
    *,
    evidence_class: str = "EXPERIMENTAL_DUST",
) -> MMExecutionToolRequest:
    """A sanctioned, hash-matched agent intent (the runner fails closed on any pin mismatch)."""
    return MMExecutionToolRequest.build(
        intent_kind="make_quote",
        intent_params=MMIntentParams(
            token_id=_MM_TOKEN, side="BUY", price=0.49, size=1.0, tif="GTC", client_order_id="coid-1"
        ),
        strategy_id=manifest.strategy_id,
        strategy_config_hash=manifest.strategy_config_hash,
        policy_hash=envelope.policy_hash(),
        session_id="sess-mm-1",
        manifest_hash=manifest.manifest_hash(),
        evidence_class=evidence_class,  # type: ignore[arg-type]
        mode="dry_run",
        admitted_manifest_hash=manifest.manifest_hash(),
        admitted_policy_hash=envelope.policy_hash(),
        admitted_strategy_config_hash=manifest.strategy_config_hash,
    )


async def _drive_facade(
    manifest: StrategyExperimentManifest,
    envelope: PolicyEnvelope,
    request: MMExecutionToolRequest,
    *,
    adapter: FakeVenueAdapter,
    signer: LocalFakeWalletControlPlane,
    sink: list[RuntimeEvent] | None,
) -> MMExecutionToolResult:
    return await facade.propose_mm_execution(
        request,
        adapter=adapter,
        signer=signer,
        sources=_MMScriptedSource(_mm_fresh_quote()),
        now_fn=_mm_clock,
        sleep_fn=_mm_noop_sleep,
        envelope=envelope,
        manifest=manifest,
        wallet_equity_at_decision=100.0,
        fixed_fraction=0.01,
        event_sink=(sink.append if sink is not None else None),
    )


async def test_facade_proposer_runs_r4a_and_returns_typed_result_via_ops_sink() -> None:
    """R4-B intent -> R4-A execute: the injectable proposer drives the runner offline with a pinned
    EXPERIMENTAL_DUST manifest and returns a typed MMExecutionToolResult + an opaque lifecycle
    receipt REF, emitting its lifecycle ONLY into the injected OPS RuntimeEvent sink (AC-019/026)."""
    manifest = _mm_manifest()
    envelope = _mm_env()
    request = _admitted_request(manifest, envelope)
    adapter = FakeVenueAdapter(fill=True)
    signer = LocalFakeWalletControlPlane()
    sink: list[RuntimeEvent] = []

    result = await _drive_facade(manifest, envelope, request, adapter=adapter, signer=signer, sink=sink)

    # (1) Typed result out — an admitted EXPERIMENTAL_DUST intent returns APPROVED + honest labels.
    assert isinstance(result, MMExecutionToolResult)
    assert result.admission == "APPROVED"
    assert result.run_label == "DUST_LIVE"
    assert result.calibration_label == "UNCALIBRATED"
    assert result.edge_label == "NOT_PROVEN_EDGE"
    assert result.evidence_class == "EXPERIMENTAL_DUST"
    assert result.policy_hash == envelope.policy_hash()

    # (2) A lifecycle receipt REF (an opaque string into the evidence stream) — NEVER a live object.
    assert isinstance(result.lifecycle_receipt_ref, str)
    assert result.lifecycle_receipt_ref.startswith("dust-lifecycle:")

    # (3) Mode A dry-run stays offline: no order reached the injected recording-fake wire.
    assert adapter.submit_calls == 0

    # (4) Lifecycle emitted ONLY into the OPS RuntimeEvent sink (never onto a tool/evidence path).
    assert sink, "the facade must emit its lifecycle into the injected OPS RuntimeEvent sink"
    assert all(isinstance(e, RuntimeEvent) and e.channel == "OPS" for e in sink)
    emitted_types = {e.type for e in sink}
    assert RuntimeEventType.RUN_STARTED in emitted_types
    assert RuntimeEventType.RUN_COMPLETED in emitted_types
    # The receipt ref rides the OPS telemetry as a REF (closed-vocab payload), never a raw handle.
    assert any(e.payload.get("lifecycle_receipt_ref") == result.lifecycle_receipt_ref for e in sink)


async def test_facade_result_exposes_no_raw_venue_or_signer_handle() -> None:
    """No-raw-handle leak (AC-020): the returned MMExecutionToolResult carries typed data + a
    receipt REF only — the injected adapter/signer/source objects are NOT reachable from it, and
    every field value bottoms out in a JSON-primitive leaf."""
    manifest = _mm_manifest()
    envelope = _mm_env()
    request = _admitted_request(manifest, envelope)
    adapter = FakeVenueAdapter(fill=True)
    signer = LocalFakeWalletControlPlane()

    result = await _drive_facade(manifest, envelope, request, adapter=adapter, signer=signer, sink=None)

    # The proposer returns the typed boundary result — NOT the raw runner result, adapter, or signer.
    assert type(result) is MMExecutionToolResult
    reachable = set(map(id, vars(result).values()))
    assert id(adapter) not in reachable, "a raw venue handle leaked onto the result"
    assert id(signer) not in reachable, "a raw signer handle leaked onto the result"
    # Behavioral leaf check on the ACTUAL returned instance: no rich handle object hides in a field.
    for value in vars(result).values():
        if isinstance(value, tuple):
            assert all(isinstance(v, (str, int, float, bool, type(None))) for v in value)
        else:
            assert isinstance(value, (str, int, float, bool, type(None)))


async def test_facade_ships_with_pinned_experimental_dust_manifest_alone() -> None:
    """R4-A ships safety-complete WITHOUT R4-B (AC-019): the proposer functions with a pinned
    EXPERIMENTAL_DUST manifest alone — it requires NO real/promoted strategy or alpha to run."""
    manifest = _mm_manifest(evidence_class="EXPERIMENTAL_DUST")
    envelope = _mm_env()
    request = _admitted_request(manifest, envelope, evidence_class="EXPERIMENTAL_DUST")

    result = await _drive_facade(
        manifest,
        envelope,
        request,
        adapter=FakeVenueAdapter(fill=True),
        signer=LocalFakeWalletControlPlane(),
        sink=None,
    )

    # An EXPERIMENTAL_DUST manifest admits (APPROVED) with the honest not-proven-edge labels — no
    # promoted/validated strategy is ever required or implied.
    assert result.admission == "APPROVED"
    assert result.evidence_class == "EXPERIMENTAL_DUST"
    assert result.edge_label == "NOT_PROVEN_EDGE"


async def test_to_tool_result_evidence_class_fallback_pins_canonical_not_request() -> None:
    """NIT-1: the ``evidence_class`` fallback PINS the canonical default, never the agent request.

    The full flow always emits a terminal :class:`DustRunLabelEvent`, so the ``label is None``
    fallback in ``_to_tool_result`` is unreachable in practice — but it must still PIN the honest
    default like its sibling label fallbacks (run/calibration/edge), never let the agent-supplied
    ``evidence_class`` influence the honest evidence class even defensively. Drives a genuine Mode-A
    result, STRIPS its terminal label so the fallback fires, and calls ``_to_tool_result`` with a
    request whose ``evidence_class`` is a NON-canonical ``"PROMOTED"``. The fallback MUST return
    ``"EXPERIMENTAL_DUST"``, never echo the request's ``"PROMOTED"``.
    """
    manifest = _mm_manifest()
    envelope = _mm_env()
    result = await run_dust_execution(
        adapter=FakeVenueAdapter(fill=True),
        signer=LocalFakeWalletControlPlane(),
        sources=_MMScriptedSource(_mm_fresh_quote()),
        now_fn=_mm_clock,
        sleep_fn=_mm_noop_sleep,
        envelope=envelope,
        manifest=manifest,
        mode="dry_run",
        wallet_equity_at_decision=100.0,
        fixed_fraction=0.01,
    )
    # Strip the terminal label so ``_terminal_label`` returns None → exercise the fallback branch.
    stripped = dataclasses.replace(
        result,
        events=tuple(e for e in result.events if not isinstance(e, DustRunLabelEvent)),
    )
    assert facade._terminal_label(stripped) is None, "label must be stripped to reach the fallback"

    # An agent request that tries to smuggle a NON-canonical evidence class through the fallback.
    request = _admitted_request(manifest, envelope, evidence_class="PROMOTED")
    tool_result = facade._to_tool_result(stripped, request)

    assert tool_result.evidence_class == "EXPERIMENTAL_DUST", (
        "the evidence_class fallback must PIN the canonical default, not echo the agent request"
    )


# ---------------------------------------------------------------------------
# E7-T3 — the human operator precondition interlock that gates Mode-B arming.
#
# Mode B (real money) cannot ARM unless ALL FIVE human operator preconditions are
# POSITIVELY satisfied AND recorded via OperatorInterlockEvent (REQ-005/006, AC-002):
#   (a) an isolated FUNDED wallet;
#   (b) operator jurisdiction / legal comfort — the OPERATOR asserts this; the model
#       makes NO jurisdiction/legal conclusion (it only RECORDS the operator's assertion);
#   (c) a declared max capital-at-risk (a positive magnitude);
#   (d) kill-switch readiness;
#   (e) explicit operator authorization of the FIRST order.
# A MISSING precondition is an explicit no-go: the facade WITHHOLDS the Mode-B arming
# bundle from the runner (feeds arming=None into the EXISTING E6-T4 arming gate), so
# Mode B stays UNARMED — the SAME fail-closed outcome, never a parallel arming path.
# ---------------------------------------------------------------------------

#: The five human operator preconditions, in the fixed recording order (test-local expectation so
#: collection does not depend on the not-yet-wired ``facade.OPERATOR_PRECONDITIONS`` symbol).
_EXPECTED_PRECONDITIONS: tuple[str, ...] = (
    "isolated_funded_wallet",
    "operator_jurisdiction_comfort",
    "declared_max_capital_at_risk",
    "kill_switch_ready",
    "first_order_authorized",
)

#: For EACH precondition, the override that leaves ONLY that one unsatisfied (others satisfied).
_MISSING_OVERRIDE: dict[str, dict[str, object]] = {
    "isolated_funded_wallet": {"isolated_funded_wallet": False},
    "operator_jurisdiction_comfort": {"operator_jurisdiction_comfort": False},
    "declared_max_capital_at_risk": {"declared_max_capital_at_risk": None},
    "kill_switch_ready": {"kill_switch_ready": False},
    "first_order_authorized": {"first_order_authorized": False},
}


def _full_interlock(**overrides: object) -> OperatorInterlock:
    """A fully-satisfied operator interlock, with per-precondition overrides for the no-go tests."""
    base: dict[str, object] = {
        "isolated_funded_wallet": True,
        "operator_jurisdiction_comfort": True,
        "declared_max_capital_at_risk": 5.0,
        "kill_switch_ready": True,
        "first_order_authorized": True,
        "operator_authorization_ref": "op-interlock-ref-1",
    }
    base.update(overrides)
    return OperatorInterlock(**base)  # type: ignore[arg-type]


# --- Mode-B arming fixtures (mirror the E6-T4 runner arming bundle) --------------------------


def _mm_policy() -> PrivyWalletPolicy:
    return PrivyWalletPolicy(
        rules=(
            PolicyRule(ALLOWED_SIGN_METHOD, ORDER_PRIMARY_TYPE, "ALLOW"),
            PolicyRule(ALLOWED_SIGN_METHOD, CLOB_AUTH_PRIMARY_TYPE, "ALLOW"),
        ),
        default_action="DENY",
        owner_type="quorum",
    )


def _mm_quorum() -> AuthorizationQuorum:
    return AuthorizationQuorum(quorum_ref="q-mode-b", authorization_key_refs=("k1", "k2"), threshold=2)


def _mm_binding() -> ExecutionWalletBinding:
    policy = _mm_policy()
    quorum = _mm_quorum()
    return ExecutionWalletBinding(
        provider="privy",
        wallet_ref="wallet-mode-b",
        wallet_address="0xExecWalletModeB",
        chain_id=CHAIN_ID_POLYGON,
        venue="polymarket",
        privy_policy_content_hash=policy.content_hash(),
        authorization_quorum_ref=quorum.quorum_ref,
        authorization_quorum_content_hash=quorum.content_hash(),
        quorum_threshold=quorum.threshold,
    )


def _mm_arming(binding: ExecutionWalletBinding) -> ModeBArming:
    return ModeBArming(
        mode_a_passed=True,
        clobv2_gate=Clobv2GateResult(
            supported_client=True,
            client_version="2",
            fixtures_match=True,
            cancel_verified=True,
            get_orders_verified=True,
            operator_smoke_ok=True,
        ),
        privy_preflight=PrivyPreflightResult(
            ok=True,
            detail="operator-confirmed",
            exercised_rules=(CLOB_AUTH_PRIMARY_TYPE, ORDER_PRIMARY_TYPE),
            recovery_verified=True,
        ),
        provisioning=ProvisioningResult(ok=True, detail="operator-confirmed"),
        binding=binding,
        live_policy=_mm_policy(),
        live_quorum=_mm_quorum(),
    )


def _mode_b_mm_manifest(binding: ExecutionWalletBinding) -> StrategyExperimentManifest:
    return _mm_manifest(mode="live_guarded", execution_wallet_binding_hash=binding.binding_hash())


def _mode_b_request(
    manifest: StrategyExperimentManifest, envelope: PolicyEnvelope
) -> MMExecutionToolRequest:
    # A ``take`` (taker FOK) intent so the Mode-B arming positive control exercises the taker submit
    # wire (``FakeVenueAdapter.submit_order``); ``make_quote`` now dispatches to the distinct resting
    # wire and would not reach ``submit_order`` here (see the runner's per-intent dispatch tests).
    return MMExecutionToolRequest.build(
        intent_kind="take",
        intent_params=MMIntentParams(
            token_id=_MM_TOKEN, side="BUY", size=1.0, tif="FOK", client_order_id="coid-1"
        ),
        strategy_id=manifest.strategy_id,
        strategy_config_hash=manifest.strategy_config_hash,
        policy_hash=envelope.policy_hash(),
        session_id="sess-mm-b",
        manifest_hash=manifest.manifest_hash(),
        evidence_class="EXPERIMENTAL_DUST",
        mode="live_guarded",
        admitted_manifest_hash=manifest.manifest_hash(),
        admitted_policy_hash=envelope.policy_hash(),
        admitted_strategy_config_hash=manifest.strategy_config_hash,
    )


async def _drive_mode_b(
    *,
    interlock: OperatorInterlock | None,
    arming: ModeBArming | None,
    binding: ExecutionWalletBinding,
    adapter: FakeVenueAdapter,
    interlock_sink: list[OperatorInterlockEvent] | None = None,
) -> MMExecutionToolResult:
    manifest = _mode_b_mm_manifest(binding)
    envelope = _mm_env()
    request = _mode_b_request(manifest, envelope)
    return await facade.propose_mm_execution(
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
        arming=arming,
        operator_interlock=interlock,
        interlock_sink=(interlock_sink.append if interlock_sink is not None else None),
    )


def test_operator_interlock_names_exactly_the_five_human_preconditions() -> None:
    """The interlock declares EXACTLY the five REQ-005/006 human preconditions, in a fixed order."""
    assert facade.OPERATOR_PRECONDITIONS == _EXPECTED_PRECONDITIONS


@pytest.mark.parametrize("missing", _EXPECTED_PRECONDITIONS)
def test_interlock_is_a_no_go_when_any_single_precondition_is_missing(missing: str) -> None:
    """For EACH of the five preconditions: with that ONE unsatisfied (others satisfied) the interlock
    is a NO-GO (``armed`` is False), it names exactly that missing precondition, and its recorded
    event marks ONLY that precondition unsatisfied — the four others record satisfied (REQ-005)."""
    interlock = _full_interlock(**_MISSING_OVERRIDE[missing])

    gate = facade.evaluate_operator_interlock(interlock, recv_ts_ms=_MM_NOW_S * 1000)

    assert gate.armed is False, f"a missing '{missing}' precondition must block Mode-B arming"
    assert gate.missing == (missing,)
    by_name = {event.precondition: event for event in gate.events}
    assert set(by_name) == set(_EXPECTED_PRECONDITIONS), "one recorded event per precondition"
    assert by_name[missing].satisfied is False
    for name in _EXPECTED_PRECONDITIONS:
        if name != missing:
            assert by_name[name].satisfied is True, name


def test_interlock_arms_and_records_all_five_when_every_precondition_is_satisfied() -> None:
    """POSITIVE CONTROL: all five satisfied → ``armed`` is True and ALL FIVE are RECORDED satisfied
    via OperatorInterlockEvent (the REQ-005 audit trail), carrying only non-secret closed-vocab data."""
    gate = facade.evaluate_operator_interlock(_full_interlock(), recv_ts_ms=_MM_NOW_S * 1000)

    assert gate.armed is True
    assert gate.missing == ()
    assert tuple(event.precondition for event in gate.events) == _EXPECTED_PRECONDITIONS
    assert all(event.satisfied is True for event in gate.events)
    for event in gate.events:
        assert isinstance(event, OperatorInterlockEvent)
        assert isinstance(event.satisfied, bool)
        assert event.precondition in _EXPECTED_PRECONDITIONS  # closed vocab, never a secret
        assert event.operator_authorization_ref == "op-interlock-ref-1"  # a non-secret REF (SEC-005)


def test_absent_interlock_is_fail_closed_no_go() -> None:
    """Fail closed: NO interlock supplied (``None``) is a no-go — Mode B cannot arm without a
    positively-satisfied interlock; every precondition records unsatisfied."""
    gate = facade.evaluate_operator_interlock(None, recv_ts_ms=_MM_NOW_S * 1000)

    assert gate.armed is False
    assert gate.missing == _EXPECTED_PRECONDITIONS
    assert all(event.satisfied is False for event in gate.events)


def test_model_makes_no_legal_conclusion_only_records_operator_jurisdiction_assertion() -> None:
    """REQ-006: the model concludes NOTHING about jurisdiction/legality — the recorded
    ``operator_jurisdiction_comfort`` event mirrors ONLY the operator's supplied assertion, both when
    the operator asserts comfort (True) and when they do not (False)."""
    for operator_assertion in (True, False):
        interlock = _full_interlock(operator_jurisdiction_comfort=operator_assertion)
        gate = facade.evaluate_operator_interlock(interlock, recv_ts_ms=_MM_NOW_S * 1000)
        jurisdiction = next(
            e for e in gate.events if e.precondition == "operator_jurisdiction_comfort"
        )
        # A pure MIRROR of the operator's input — the system derives no legal conclusion.
        assert jurisdiction.satisfied is operator_assertion
        # And the assertion alone flips whether Mode B may arm (all else satisfied).
        assert gate.armed is operator_assertion


@pytest.mark.parametrize("missing", _EXPECTED_PRECONDITIONS)
async def test_mode_b_stays_unarmed_when_any_operator_precondition_is_missing(missing: str) -> None:
    """WIRING: even with a fully-passing E6-T4 arming bundle, a single missing operator precondition
    keeps Mode B UNARMED — NO order reaches the wire, and the interlock is recorded (REQ-005/AC-002)."""
    binding = _mm_binding()
    adapter = FakeVenueAdapter(fill=True)
    recorded: list[OperatorInterlockEvent] = []

    await _drive_mode_b(
        interlock=_full_interlock(**_MISSING_OVERRIDE[missing]),
        arming=_mm_arming(binding),  # a fully-passing E6-T4 arming bundle
        binding=binding,
        adapter=adapter,
        interlock_sink=recorded,
    )

    assert adapter.submit_calls == 0, f"a missing '{missing}' precondition must keep Mode B UNARMED"
    by_name = {event.precondition: event for event in recorded}
    assert by_name[missing].satisfied is False, "the no-go precondition is RECORDED unsatisfied"


async def test_mode_b_arms_and_records_when_all_operator_preconditions_are_satisfied() -> None:
    """POSITIVE CONTROL: a fully-satisfied interlock + a fully-passing arming bundle lets Mode B ARM
    (the order reaches the wire) AND records all five preconditions satisfied. This makes the
    per-precondition no-go MUTATION meaningful (not vacuously green)."""
    binding = _mm_binding()
    adapter = FakeVenueAdapter(fill=True)
    recorded: list[OperatorInterlockEvent] = []

    result = await _drive_mode_b(
        interlock=_full_interlock(),
        arming=_mm_arming(binding),
        binding=binding,
        adapter=adapter,
        interlock_sink=recorded,
    )

    assert adapter.submit_calls == 1, "a fully-satisfied interlock + armed bundle must reach the wire"
    assert result.admission == "APPROVED"
    assert tuple(event.precondition for event in recorded) == _EXPECTED_PRECONDITIONS
    assert all(event.satisfied is True for event in recorded)


def test_facade_proposer_is_not_registered_as_a_tool_on_the_tools_empty_agent() -> None:
    """tools=[] invariant (REQ-003): the facade proposer is an INJECTABLE adapter, NOT a callable
    tool on the decision agent. The agent is assembled with an EMPTY tools list and the proposer is
    NOT among them — emission goes to the OPS sink, never via a tool registration."""
    captured: dict[str, object] = {}

    def _recording_factory(*, model: object, tools: list, output_schema: type | None) -> object:
        captured["tools"] = tools

        class _FakeResponse:
            content = AgentAction(type=SportsActionType.WAIT, params={})

        class _FakeAgent:
            def run(self, _prompt: str) -> object:
                return _FakeResponse()

        return _FakeAgent()

    action = runtime_agent.emit_agent_action(
        {"tick": 1},
        model=object(),  # sentinel: non-None so the real OpenRouter model is never built (offline)
        model_id="test/model",  # non-None so config/Settings is never read
        agent_factory=_recording_factory,
    )

    assert isinstance(action, AgentAction)
    # The HARD invariant: the agent is built with an EMPTY tools list...
    assert captured["tools"] == [], "the decision agent must be assembled with tools=[]"
    # ...and the MM facade proposer is NOT registered among them (mutation: appending it flips this).
    assert facade.propose_mm_execution not in captured["tools"]
