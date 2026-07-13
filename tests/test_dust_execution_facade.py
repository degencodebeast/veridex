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

import asyncio
import contextlib
import dataclasses
import threading
import typing
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ValidationError

# Gate#3 C-1 fix: an ARMED Mode-B run now structurally requires a non-FAKE_LOCAL signer + an
# injected keyless write port + authorization context. REUSE the offline enclave-backed fixtures
# already built for this in ``tests/test_dust_execution_runner.py`` (which itself reuses the E3-T8
# fakes) rather than re-deriving them a third time.
from tests.test_dust_execution_privy_signer import _WALLET_ADDRESS
from tests.test_dust_execution_runner import (
    _ORDER_AUTH,
    RecordingFakeAdapter,
    _default_write_port,
    _mode_b_signer,
    _NoOpInterlockStore,
)
from veridex.dust_execution import facade  # module handle: proposer looked up dynamically (RED-clean)
from veridex.dust_execution.clobv2_gate import Clobv2GateResult
from veridex.dust_execution.contracts import (
    DustRunLabelEvent,
    OperatorInterlockEvent,
    OrderAckEvent,
    OrderSubmitAttempt,
    PreSubmitRecord,
    RealFillReconciliation,
)
from veridex.dust_execution.facade import (
    MMExecutionToolRequest,
    MMExecutionToolResult,
    MMIntentParams,
    OperatorInterlock,
)
from veridex.dust_execution.manifest import StrategyExperimentManifest
from veridex.dust_execution.operator_interlock_store import (
    InMemoryOperatorInterlockStore,
    OperatorInterlockStore,
)
from veridex.dust_execution.privy_control_plane import PrivyPreflightResult, ProvisioningResult
from veridex.dust_execution.risk import RealizedFillRecord, RiskAccumulator
from veridex.dust_execution.runner import (
    BookSide,
    DustQuote,
    ModeBArming,
    run_dust_execution,
)
from veridex.dust_execution.session_state import (
    DurableSessionState,
    DurableSessionStateProvider,
    InMemoryDurableSessionStateProvider,
    ReconciliationState,
    ReservationOutcome,
    ScopedReservationOutcome,
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
        execution_status="SUBMITTED",
        execution_reason_codes=(),
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
            execution_status="SUBMITTED",
            execution_reason_codes=(),
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

# A DECIMAL-integer-string CLOB token id (Gate#3 C-1 fix): the real V2 signing compiler parses
# ``tokenId`` via ``int(...)`` (an ERC1155 token id), so an armed Mode-B submit that actually
# compiles a real order needs a numerically-valid id — never a human-readable "0x..." placeholder.
_MM_TOKEN = "111111111111111111111111111111"
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


def _mm_now_dt() -> datetime:
    """The fixed offline UTC clock the provider buckets its durable state by (mirrors ``_mm_clock``)."""
    return datetime.fromtimestamp(_MM_NOW_S, tz=UTC)


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
        # Gate#3 C-1 fix: the pure-stdlib secp256k1 "enclave" address the offline recording-fake
        # Privy client signs for (see ``tests/test_dust_execution_privy_signer.py``) — so a genuine
        # armed Mode-B submit's recover-and-require check passes cleanly.
        wallet_address=_WALLET_ADDRESS,
        chain_id=CHAIN_ID_POLYGON,
        venue="polymarket",
        privy_policy_content_hash=policy.content_hash(),
        authorization_quorum_ref=quorum.quorum_ref,
        authorization_quorum_content_hash=quorum.content_hash(),
        quorum_threshold=quorum.threshold,
    )


def _mm_arming(
    binding: ExecutionWalletBinding, *, write_port: object | None = None
) -> ModeBArming:
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
        # Gate#3 C-1 fix: the narrow injected keyless write port + signed authorization context — an
        # armed Mode-B run has no real money-moving surface without both (REUSES the offline E3-T8
        # stack via ``tests.test_dust_execution_runner``'s fixtures).
        write_port=write_port if write_port is not None else _default_write_port(binding),  # type: ignore[arg-type]
        order_auth=_ORDER_AUTH,
    )


def _mode_b_mm_manifest(binding: ExecutionWalletBinding) -> StrategyExperimentManifest:
    return _mm_manifest(mode="live_guarded", execution_wallet_binding_hash=binding.binding_hash())


def _mode_b_request(
    manifest: StrategyExperimentManifest,
    envelope: PolicyEnvelope,
    *,
    client_order_id: str = "coid-1",
) -> MMExecutionToolRequest:
    # A ``take`` (taker FOK) intent so the Mode-B arming positive control exercises the taker submit
    # wire (``FakeVenueAdapter.submit_order``); ``make_quote`` now dispatches to the distinct resting
    # wire and would not reach ``submit_order`` here (see the runner's per-intent dispatch tests).
    # R5-MAJOR-1: ``client_order_id`` distinguishes a genuinely DISTINCT authorized attempt (a distinct
    # coid → a DISTINCT stable ``attempt_id``) from an IDENTICAL retry (the same coid → the SAME id).
    return MMExecutionToolRequest.build(
        intent_kind="take",
        intent_params=MMIntentParams(
            token_id=_MM_TOKEN, side="BUY", size=1.0, tif="FOK", client_order_id=client_order_id
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


# Gate#3 MAJOR-2: a sentinel so a test can pass ``provider=None`` EXPLICITLY (the live fail-closed
# case) distinct from "defaulted" (a fresh in-memory provider, the migrated positive path).
_PROVIDER_UNSET: object = object()


async def _drive_mode_b(
    *,
    interlock: OperatorInterlock | None,
    arming: ModeBArming | None,
    binding: ExecutionWalletBinding,
    adapter: FakeVenueAdapter,
    interlock_store: OperatorInterlockStore | None = None,
    envelope: PolicyEnvelope | None = None,
    provider: object = _PROVIDER_UNSET,
    client_order_id: str = "coid-1",
) -> MMExecutionToolResult:
    manifest = _mode_b_mm_manifest(binding)
    envelope = envelope if envelope is not None else _mm_env()
    request = _mode_b_request(manifest, envelope, client_order_id=client_order_id)
    # Gate#3 MAJOR-2: a live run now composes ONE authoritative durable session-state source. The
    # migrated positives default to a fresh in-memory provider; a test drives the live fail-closed
    # case by passing ``provider=None`` explicitly.
    effective_provider = (
        InMemoryDurableSessionStateProvider() if provider is _PROVIDER_UNSET else provider
    )
    return await facade.propose_mm_execution(
        request,
        adapter=adapter,
        # Gate#3 C-1 fix: an ARMED Mode-B run structurally refuses the Mode-A FAKE_LOCAL signer.
        signer=_mode_b_signer(),
        sources=_MMScriptedSource(_mm_fresh_quote()),
        now_fn=_mm_clock,
        sleep_fn=_mm_noop_sleep,
        envelope=envelope,
        manifest=manifest,
        wallet_equity_at_decision=100.0,
        fixed_fraction=0.01,
        arming=arming,
        operator_interlock=interlock,
        interlock_store=interlock_store,
        provider=effective_provider,  # type: ignore[arg-type]
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
    keeps Mode B UNARMED — NO order reaches the wire, and NO durable arming receipt is issued.

    Gate#3 M-1: a no-go interlock is never durably CERTIFIED — the store records (and issues a
    receipt for) an arming attempt ONLY when the interlock is fully satisfied, so a missing
    precondition leaves the store empty (the per-precondition no-go event recording is covered by
    ``test_interlock_is_a_no_go_when_any_single_precondition_is_missing`` at the gate level)."""
    binding = _mm_binding()
    adapter = FakeVenueAdapter(fill=True)
    store = InMemoryOperatorInterlockStore()

    await _drive_mode_b(
        interlock=_full_interlock(**_MISSING_OVERRIDE[missing]),
        arming=_mm_arming(binding),  # a fully-passing E6-T4 arming bundle
        binding=binding,
        adapter=adapter,
        interlock_store=store,
    )

    assert adapter.submit_calls == 0, f"a missing '{missing}' precondition must keep Mode B UNARMED"
    assert store.rows() == (), "a no-go interlock must issue NO durable arming receipt (fail closed)"


async def test_mode_b_arms_and_records_when_all_operator_preconditions_are_satisfied() -> None:
    """POSITIVE CONTROL: a fully-satisfied interlock + a fully-passing arming bundle lets Mode B ARM
    (the order reaches the wire) AND records all five preconditions satisfied. This makes the
    per-precondition no-go MUTATION meaningful (not vacuously green)."""
    binding = _mm_binding()
    # Trustworthy zero-orders startup read so the armed run reaches the wire (an absent open-order
    # read is UNKNOWN exposure and now fails closed under the M-2 startup-sweep fix).
    adapter = RecordingFakeAdapter(fill=True, open_orders=[])
    write_port = _default_write_port(binding)
    store = InMemoryOperatorInterlockStore()

    result = await _drive_mode_b(
        interlock=_full_interlock(),
        arming=_mm_arming(binding, write_port=write_port),
        binding=binding,
        adapter=adapter,
        interlock_store=store,
    )

    assert write_port.submit_calls == 1, "a fully-satisfied interlock + armed bundle must reach the wire"
    assert adapter.submit_calls == 0, "Mode B must NEVER reach the generic adapter submit surface"
    assert result.admission == "APPROVED"
    # The store durably recorded exactly one arming attempt, over all five satisfied preconditions.
    (row,) = store.rows()
    assert tuple(event.precondition for event in row.events) == _EXPECTED_PRECONDITIONS
    assert all(event.satisfied is True for event in row.events)


async def test_mode_b_fails_closed_when_no_recording_store_even_if_interlock_satisfied() -> None:
    """Gate#3 MAJOR-1 (REQ-005): a satisfied interlock is only "satisfied" if it was durably
    RECORDED. With ``interlock_store=None`` there is NOWHERE to durably persist the five
    ``OperatorInterlockEvent``s (and no store to ISSUE a receipt), so Mode B must FAIL CLOSED — no
    arm, no submit — EVEN when all five preconditions are asserted True. REQ-005 is absolute:
    satisfied AND recorded.

    RED before the fix: the facade evaluated the armed interlock, treated CALLBACK PRESENCE as
    durability, and submitted an UNRECORDED real order (``submit_calls == 1``). GREEN after: no store
    → no store-issued receipt → withhold the arming bundle → ``submit_calls == 0``.
    """
    binding = _mm_binding()
    adapter = FakeVenueAdapter(fill=True)

    await _drive_mode_b(
        interlock=_full_interlock(),  # all five human preconditions asserted True
        arming=_mm_arming(binding),  # a fully-passing E6-T4 technical arming bundle
        binding=binding,
        adapter=adapter,
        interlock_store=None,  # nowhere to durably record / issue a receipt → REQ-005 fails closed
    )

    assert adapter.submit_calls == 0, (
        "a satisfied interlock with NO durable store must FAIL CLOSED — REQ-005 requires recorded"
    )


async def test_mode_b_fails_closed_when_store_does_not_durably_persist() -> None:
    """Gate#3 M-1: callback/store PRESENCE is NOT durability. A no-op store that RETURNS a
    receipt-shaped string but never durably persists (its ``verify`` never confirms a row) must FAIL
    CLOSED — even with all five preconditions satisfied. The SAME store is threaded into the runner,
    which cannot verify the receipt → the arming bundle is withheld → no arm, no submit.

    RED before the fix: the facade treated ``interlock_sink is not None`` (mere presence) as durable
    and armed on a no-op sink, submitting a real order (``execution_status == "SUBMITTED"``). GREEN
    after: a non-persisting store yields an unverifiable receipt → withheld.
    """
    binding = _mm_binding()
    adapter = FakeVenueAdapter(fill=True)

    result = await _drive_mode_b(
        interlock=_full_interlock(),  # all five human preconditions asserted True
        arming=_mm_arming(binding),  # a fully-passing E6-T4 technical arming bundle
        binding=binding,
        adapter=adapter,
        interlock_store=_NoOpInterlockStore(),  # present, but never durably persists → verify False
    )

    assert adapter.submit_calls == 0, "a no-op (non-persisting) store must FAIL CLOSED — M-1"
    assert result.execution_status != "SUBMITTED", "presence-as-durability must never report SUBMITTED"
    assert result.execution_status == "NOT_ARMED"


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


# ---------------------------------------------------------------------------
# Gate#3 MAJOR-3 — the typed result SEPARATES the strategy MANIFEST ADMISSION
# from the EXECUTION DISPOSITION, so a WITHHELD execution can never read as an
# executed approval.
#
# Before the fix ``_to_tool_result`` mapped ONLY the strategy-authorization
# verdict/reason codes: an interlock-withheld run returned ``admission="APPROVED"``,
# ``reason_codes=()`` while ``submit_calls == 0`` — indistinguishable from
# "approved AND executed". The result now ALSO carries a closed-vocab
# ``execution_status`` (+ ``execution_reason_codes``) DERIVED from the runner's REAL
# ``DustExecutionResult`` disposition (submits / abstains), NOT re-derived from
# admission. ``admission`` keeps its EXISTING meaning ("the strategy manifest was
# admitted"), never "an order executed". Each test asserts THE RESULT (the typed
# fields), not only ``submit_calls`` (Codex: "assert the result, not only submit_calls").
#
# RED evidence (current code, before the fix): the interlock-withheld run returns
# ``('APPROVED', ())`` with ``submit_calls == 0`` and NO ``execution_status`` field —
# the honesty gap this fold closes (a repro captured the exact tuple).
# ---------------------------------------------------------------------------


async def _drive_mode_b_kind(
    *,
    intent_kind: str,
    intent_params: MMIntentParams,
    arming: ModeBArming | None,
    interlock: OperatorInterlock | None,
    binding: ExecutionWalletBinding,
    adapter: FakeVenueAdapter,
    interlock_store: OperatorInterlockStore | None = None,
    provider: object = _PROVIDER_UNSET,
) -> MMExecutionToolResult:
    """Drive a Mode-B run for an arbitrary admitted intent kind (hashes built to MATCH the manifest)."""
    manifest = _mode_b_mm_manifest(binding)
    envelope = _mm_env()
    effective_provider = (
        InMemoryDurableSessionStateProvider() if provider is _PROVIDER_UNSET else provider
    )
    request = MMExecutionToolRequest.build(
        intent_kind=intent_kind,  # type: ignore[arg-type]
        intent_params=intent_params,
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
    return await facade.propose_mm_execution(
        request,
        adapter=adapter,
        # Gate#3 C-1 fix: an ARMED Mode-B run structurally refuses the Mode-A FAKE_LOCAL signer.
        signer=_mode_b_signer(),
        sources=_MMScriptedSource(_mm_fresh_quote()),
        now_fn=_mm_clock,
        sleep_fn=_mm_noop_sleep,
        envelope=envelope,
        manifest=manifest,
        wallet_equity_at_decision=100.0,
        fixed_fraction=0.01,
        arming=arming,
        operator_interlock=interlock,
        interlock_store=interlock_store,
        provider=effective_provider,  # type: ignore[arg-type]
    )


def _taker_params() -> MMIntentParams:
    return MMIntentParams(
        token_id=_MM_TOKEN, side="BUY", size=1.0, tif="FOK", client_order_id="coid-1"
    )


async def test_result_reports_not_armed_when_interlock_withholds_execution() -> None:
    """Interlock-withheld (``first_order_authorized=False``): the arming bundle is WITHHELD so the
    runner correctly abstains and NO order reaches the wire — yet the strategy manifest was admitted.
    The RESULT must make the withheld execution UNMISTAKABLE: ``admission`` stays ``APPROVED`` (manifest
    admitted) but ``execution_status`` is ``NOT_ARMED`` (carrying the ``mode_b_not_armed`` reason) — NOT
    a bare ``('APPROVED', ())`` that reads as approved-AND-executed (Gate#3 MAJOR-3)."""
    binding = _mm_binding()
    adapter = FakeVenueAdapter(fill=True)

    result = await _drive_mode_b_kind(
        intent_kind="take",
        intent_params=_taker_params(),
        arming=_mm_arming(binding),  # a fully-passing E6-T4 TECHNICAL arming bundle
        interlock=_full_interlock(first_order_authorized=False),  # the human interlock FAILS
        binding=binding,
        adapter=adapter,
        interlock_store=InMemoryOperatorInterlockStore(),
    )

    # NO order reached the wire — execution was WITHHELD.
    assert adapter.submit_calls == 0
    # The strategy MANIFEST was still admitted (existing meaning of ``admission``)...
    assert result.admission == "APPROVED"
    # ...but the EXECUTION DISPOSITION is honestly reported as NOT_ARMED — never SUBMITTED, and never
    # a bare APPROVED/() that a Studio/AgentRuntime consumer would read as approved-and-executed.
    assert result.execution_status == "NOT_ARMED"
    assert result.execution_status != "SUBMITTED"
    assert "mode_b_not_armed" in result.execution_reason_codes


async def test_result_reports_abstained_for_an_armed_no_quote_intent() -> None:
    """Armed ``no_quote``: fully armed (so the abstention is the INTENT's, not a want of arming) — the
    runner abstains on the explicit DON'T-TRADE intent. The result reports ``execution_status ==
    ABSTAINED`` with the ``intent_no_quote`` reason, NOT an executed approval (Gate#3 MAJOR-3)."""
    binding = _mm_binding()
    adapter = FakeVenueAdapter(fill=True)

    result = await _drive_mode_b_kind(
        intent_kind="no_quote",
        intent_params=MMIntentParams(),  # an explicit abstention carries no order params
        arming=_mm_arming(binding),
        interlock=_full_interlock(),  # fully armed — arming is NOT the reason for the abstain
        binding=binding,
        adapter=adapter,
        interlock_store=InMemoryOperatorInterlockStore(),
    )

    assert adapter.submit_calls == 0
    assert result.admission == "APPROVED"  # the manifest admitted the strategy
    assert result.execution_status == "ABSTAINED"  # abstained on the intent, NOT an executed approval
    assert "intent_no_quote" in result.execution_reason_codes


async def test_result_reports_submitted_for_a_clean_armed_take() -> None:
    """Clean armed ``take``: a fully-satisfied interlock + fully-passing arming bundle lets an order
    reach the wire. The result honestly reports BOTH ``admission == APPROVED`` (manifest admitted) AND
    ``execution_status == SUBMITTED`` (an order actually reached the wire) — the positive control that
    makes the withheld/abstained cases meaningful (Gate#3 MAJOR-3)."""
    binding = _mm_binding()
    # Trustworthy zero-orders startup read so the armed run reaches the wire (an absent open-order
    # read is UNKNOWN exposure and now fails closed under the M-2 startup-sweep fix).
    adapter = RecordingFakeAdapter(fill=True, open_orders=[])
    write_port = _default_write_port(binding)

    result = await _drive_mode_b_kind(
        intent_kind="take",
        intent_params=_taker_params(),
        arming=_mm_arming(binding, write_port=write_port),
        interlock=_full_interlock(),
        binding=binding,
        adapter=adapter,
        interlock_store=InMemoryOperatorInterlockStore(),
    )

    assert write_port.submit_calls == 1
    assert adapter.submit_calls == 0, "Mode B must NEVER reach the generic adapter submit surface"
    assert result.admission == "APPROVED"
    assert result.execution_status == "SUBMITTED"


async def test_result_reports_denied_for_a_strategy_admission_deny() -> None:
    """Strategy admission DENY (a reached loss cap): ``admission`` is ``DENIED`` and ``execution_status``
    reflects that NO execution was authorized (``DENIED``). Drives a genuine runner result whose
    admission verdict is ``DENY`` (seeded breached risk) and maps it through ``_to_tool_result`` — the
    typed result must not imply any execution (Gate#3 MAJOR-3)."""
    manifest = _mm_manifest()
    envelope = _mm_env()
    breached = RiskAccumulator.seeded(
        session_id="dust-maker-v0:dry_run", net_session=-2.0, net_day=-2.0, current_day=None
    )
    adapter = FakeVenueAdapter(fill=True)
    result = await run_dust_execution(
        adapter=adapter,
        signer=LocalFakeWalletControlPlane(),
        sources=_MMScriptedSource(_mm_fresh_quote()),
        now_fn=_mm_clock,
        sleep_fn=_mm_noop_sleep,
        envelope=envelope,
        manifest=manifest,
        mode="dry_run",
        wallet_equity_at_decision=100.0,
        fixed_fraction=0.01,
        risk=breached,
    )
    assert result.admission.verdict == "DENY", "seeded breached risk must DENY admission"
    assert adapter.submit_calls == 0

    request = _admitted_request(manifest, envelope)
    tool_result = facade._to_tool_result(result, request)

    assert tool_result.admission == "DENIED"
    assert tool_result.execution_status == "DENIED"


# ---------------------------------------------------------------------------
# Gate#3 MAJOR-2 — the agent-facing facade composes ONE authoritative durable
# session-state source (risk + session/day order counts + immutable identity)
# and FAILS CLOSED on the money path when it is absent.
#
# Before the fix ``propose_mm_execution`` drove ``run_dust_execution`` with a FRESH
# zero ``RiskAccumulator`` and zero prior order counts on EVERY call, so the runner's
# durable run/session/UTC-day order caps and realized-loss caps RESET each invocation.
# Codex repro: two REAL facade calls, same session, ``max_orders_per_session ==
# max_orders_per_day == 1`` → BOTH reached the keyless write port. The facade now takes
# an injected ``DurableSessionStateProvider`` that supplies (BEFORE arming) the immutable
# session identity, the reconstructed realized-loss accumulator, and the persisted
# session/day attempt counts, threads them into the runner, and persists the run's
# possibly-live attempts back through the SAME identity. Live mode FAILS CLOSED without a
# provider (no fresh/zero default on the money path).
#
# Each test asserts THE WRITE-PORT (money-moving surface) and the typed result, not a
# proxy. RED today: the 2nd / over-cap / over-loss / no-provider call SUBMITS.
# ---------------------------------------------------------------------------


async def test_two_facade_calls_same_session_enforce_per_session_order_cap_across_calls() -> None:
    """Codex's object: two ``propose_mm_execution`` calls, SAME session, sharing ONE in-memory
    provider, with ``max_orders_per_session == 1`` — the 1st SUBMITTED, the 2nd (a genuinely DISTINCT
    authorized attempt) DENIED by the durable per-session order cap. ``write_port.submit_calls == 1``
    TOTAL. RED today: the facade resets the count each call so BOTH submit (``submit_calls == 2``).

    R5-MAJOR-1 migration: the 2nd call is a DISTINCT attempt (a distinct ``client_order_id``), so the
    cap — not idempotency — is what denies it; an IDENTICAL retry is covered separately (freeze)."""
    binding = _mm_binding()
    # ``fill_history_matches=True`` → the 1st call's E4 reconcile RESOLVES (definite fill), so it does
    # NOT freeze the session scope — the CAP is what denies the distinct 2nd call (IDM-002).
    adapter = RecordingFakeAdapter(fill=True, fill_history_matches=True, open_orders=[])
    write_port = _default_write_port(binding)  # SHARED across both calls — counts total submits
    store = InMemoryOperatorInterlockStore()
    provider = InMemoryDurableSessionStateProvider()  # SHARED durable session state
    envelope = _mm_env(max_orders_per_session=1)

    first = await _drive_mode_b(
        interlock=_full_interlock(),
        arming=_mm_arming(binding, write_port=write_port),
        binding=binding,
        adapter=adapter,
        interlock_store=store,
        envelope=envelope,
        provider=provider,
    )
    second = await _drive_mode_b(
        interlock=_full_interlock(),
        arming=_mm_arming(binding, write_port=write_port),
        binding=binding,
        adapter=adapter,
        interlock_store=store,
        envelope=envelope,
        provider=provider,
        client_order_id="coid-2",  # a DISTINCT authorized attempt — denied by the cap, not idempotency
    )

    assert write_port.submit_calls == 1, (
        "the SECOND same-session call must be denied by the durable per-session order cap"
    )
    assert first.execution_status == "SUBMITTED"
    assert second.execution_status != "SUBMITTED"
    assert "order_cap_session" in second.execution_reason_codes


async def test_two_facade_calls_same_session_enforce_per_day_order_cap_across_calls() -> None:
    """The per-UTC-day cap holds across calls too: ``max_orders_per_day == 1`` (session cap open) →
    the 2nd same-session call (a genuinely DISTINCT authorized attempt) is denied ``order_cap_day`` and
    never reaches the write port. R5-MAJOR-1 migration: the 2nd call carries a DISTINCT
    ``client_order_id`` so the cap — not idempotency — denies it."""
    binding = _mm_binding()
    # ``fill_history_matches=True`` → the 1st call RESOLVES (definite fill), so it does not freeze the
    # session scope — the per-day CAP is what denies the distinct 2nd call (IDM-002).
    adapter = RecordingFakeAdapter(fill=True, fill_history_matches=True, open_orders=[])
    write_port = _default_write_port(binding)
    store = InMemoryOperatorInterlockStore()
    provider = InMemoryDurableSessionStateProvider()
    envelope = _mm_env(max_orders_per_day=1, max_orders_per_session=20)

    await _drive_mode_b(
        interlock=_full_interlock(),
        arming=_mm_arming(binding, write_port=write_port),
        binding=binding,
        adapter=adapter,
        interlock_store=store,
        envelope=envelope,
        provider=provider,
    )
    second = await _drive_mode_b(
        interlock=_full_interlock(),
        arming=_mm_arming(binding, write_port=write_port),
        binding=binding,
        adapter=adapter,
        interlock_store=store,
        envelope=envelope,
        provider=provider,
        client_order_id="coid-2",  # a DISTINCT authorized attempt — denied by the day cap, not idempotency
    )

    assert write_port.submit_calls == 1, "the 2nd same-session call must be denied by the durable per-day cap"
    assert second.execution_status != "SUBMITTED"
    assert "order_cap_day" in second.execution_reason_codes


async def test_restart_provider_seeded_at_cap_denies_before_any_write_port_call() -> None:
    """Restart: a provider seeded with a prior attempt count already AT the cap → the FIRST call of a
    NEW facade instance denies before any write-port call (the durable count survives a restart)."""
    binding = _mm_binding()
    adapter = RecordingFakeAdapter(fill=True, open_orders=[])
    write_port = _default_write_port(binding)
    provider = InMemoryDurableSessionStateProvider()
    # A persisted possibly-live attempt already AT the cap: reserve it and settle it RESOLVED so it is a
    # durable prior attempt that COUNTS toward the cap but does NOT freeze the scope (a resolved fill).
    provider.reserve(session_identity="sess-mm-b", now=_mm_now_dt(), attempt_id="seed-attempt-1")
    provider.settle(attempt_id="seed-attempt-1", recon_state="RESOLVED")
    envelope = _mm_env(max_orders_per_session=1)

    result = await _drive_mode_b(
        interlock=_full_interlock(),
        arming=_mm_arming(binding, write_port=write_port),
        binding=binding,
        adapter=adapter,
        interlock_store=InMemoryOperatorInterlockStore(),
        envelope=envelope,
        provider=provider,
    )

    assert write_port.submit_calls == 0, "a persisted count at the cap must deny before any write-port I/O"
    assert result.execution_status != "SUBMITTED"
    assert "order_cap_session" in result.execution_reason_codes


async def test_prior_realized_loss_at_cap_denies_next_live_arming_before_write_port() -> None:
    """SAF-002: a provider carrying a realized loss at/over the enabled session-loss cap
    (``max_session_loss == 2.0``) → the next live arming call trips the loss cap and DENIES before any
    write-port I/O. RED today: the facade reconstructs a FRESH zero loss and the call submits."""
    binding = _mm_binding()
    adapter = RecordingFakeAdapter(fill=True, open_orders=[])
    write_port = _default_write_port(binding)
    provider = InMemoryDurableSessionStateProvider()
    # The realized-fill LOSS is fed by the provider's own durable venue-reconciliation ledger (never
    # fabricated from the facade's sealed events), so it is seeded through that seam — not through the
    # attempt reserve/settle contract.
    provider.record_reconciled_fills(
        session_identity="sess-mm-b",
        fills=(
            RealizedFillRecord(
                realized_pnl=-2.5, fee=0.0, session_id="sess-mm-b", fill_ts_ms=_MM_NOW_S * 1000
            ),
        ),
    )

    result = await _drive_mode_b(
        interlock=_full_interlock(),
        arming=_mm_arming(binding, write_port=write_port),
        binding=binding,
        adapter=adapter,
        interlock_store=InMemoryOperatorInterlockStore(),
        provider=provider,
    )

    assert write_port.submit_calls == 0, (
        "a reconstructed prior loss at the cap must DENY before any write-port I/O (SAF-002)"
    )
    assert result.admission == "DENIED"


async def test_live_guarded_fails_closed_without_a_durable_session_state_provider() -> None:
    """Fail-closed: ``live_guarded`` with NO provider → NOT_ARMED/denied, ``write_port.submit_calls ==
    0`` (no fresh/zero default on the money path). RED today: the facade drives the runner with fresh
    zero state and the armed run SUBMITS."""
    binding = _mm_binding()
    adapter = RecordingFakeAdapter(fill=True, open_orders=[])
    write_port = _default_write_port(binding)

    result = await _drive_mode_b(
        interlock=_full_interlock(),
        arming=_mm_arming(binding, write_port=write_port),
        binding=binding,
        adapter=adapter,
        interlock_store=InMemoryOperatorInterlockStore(),
        provider=None,  # NO durable source on the money path → fail closed
    )

    assert write_port.submit_calls == 0, (
        "live_guarded with NO durable session-state provider must FAIL CLOSED — no fresh/zero default"
    )
    assert result.execution_status == "NOT_ARMED"


async def test_positive_control_first_call_within_caps_with_provider_submits() -> None:
    """Positive control: the FIRST call within caps + a provider present → SUBMITTED (offline; Mode B
    UNARMED recording-fakes), and the run's possibly-live attempt persists back to the provider."""
    binding = _mm_binding()
    adapter = RecordingFakeAdapter(fill=True, open_orders=[])
    write_port = _default_write_port(binding)
    provider = InMemoryDurableSessionStateProvider()

    result = await _drive_mode_b(
        interlock=_full_interlock(),
        arming=_mm_arming(binding, write_port=write_port),
        binding=binding,
        adapter=adapter,
        interlock_store=InMemoryOperatorInterlockStore(),
        provider=provider,
    )

    assert write_port.submit_calls == 1
    assert adapter.submit_calls == 0, "Mode B must NEVER reach the generic adapter submit surface"
    assert result.execution_status == "SUBMITTED"
    assert provider.attempts("sess-mm-b") == 1, "the run's possibly-live attempt must persist back"


def test_durable_session_state_provider_protocol_is_runtime_checkable() -> None:
    """The in-memory fake structurally satisfies the injected ``DurableSessionStateProvider`` Protocol
    (the SAME injected-seam idiom as the operator-interlock / pre-submit stores)."""
    assert isinstance(InMemoryDurableSessionStateProvider(), DurableSessionStateProvider)


# --- Gate#3 R4-MAJOR-2: the facade must NOT trust a provider-substituted session identity ---------
#
# ``MMExecutionToolRequest.session_id`` is the operator-assigned IMMUTABLE identity; the provider is
# supposed to merely ADOPT/echo it. The facade must REQUIRE, before recording the interlock or
# entering the runner, that the durable state's ``session_identity`` is non-empty AND equals
# ``request.session_id``, and that the supplied risk accumulator is bound to that SAME session. A
# stale / corrupt / mis-keyed provider response that swaps the identity must FAIL CLOSED — no
# interlock receipt, no runner entry, no write-port I/O — so the requested session's accumulated caps
# + realized loss can never be bypassed by binding the run to a different safety ledger.


class _EchoRiskProvider:
    """A provider whose ``session_identity`` and risk binding are chosen per-test to exercise the
    facade's identity-assertion. ``load`` echoes the CONFIGURED (possibly wrong) identity + a risk
    accumulator bound to the CONFIGURED risk session, so a single class drives all mismatch cases."""

    def __init__(self, *, identity: str, risk_session: str) -> None:
        self._identity = identity
        self._risk_session = risk_session

    def load(self, *, session_id: str, now: datetime) -> DurableSessionState:
        return DurableSessionState(
            session_identity=self._identity,
            risk=RiskAccumulator.seeded(
                session_id=self._risk_session, net_session=0.0, net_day=0.0, current_day=now
            ),
            prior_session_order_count=0,
            prior_day_order_count=0,
        )

    def reserve(
        self,
        *,
        session_identity: str,
        now: datetime,
        attempt_id: str,
        venue_order_key: str | None = None,
    ) -> ReservationOutcome:
        # Never reached in the identity-mismatch tests: the facade fails closed at the identity
        # assertion, BEFORE the before-wire reserve-or-load.
        return "RESERVED"

    def reserve_or_freeze(
        self,
        *,
        session_identity: str,
        now: datetime,
        attempt_id: str,
        max_orders_per_session: int = 0,
        max_orders_per_day: int = 0,
        venue_order_key: str | None = None,
    ) -> ScopedReservationOutcome:
        # The ATOMIC reserve path (R6-MAJOR-2 + R8-MAJOR-1: now also accepts the threaded session/day
        # caps). Reached only by the honest-echo positive control (the mismatch cases fail closed at the
        # identity assertion first): a fresh slot with nothing unresolved in scope AND spare cap →
        # RESERVED, so the honest run proceeds to the wire.
        return "RESERVED"

    def settle(
        self,
        *,
        attempt_id: str,
        recon_state: ReconciliationState,
        venue_order_key: str | None = None,
    ) -> None:
        return None

    def has_unresolved_reservation(self, session_identity: str) -> bool:
        # No reservations in the identity-assertion tests. The facade money path no longer calls this
        # separately (it uses the atomic ``reserve_or_freeze``); retained for Protocol completeness.
        return False


async def test_live_guarded_fails_closed_when_provider_identity_differs_from_request_session() -> None:
    """R4-MAJOR-2: a provider whose ``session_identity`` differs from ``request.session_id`` must FAIL
    CLOSED — no interlock recorded, ``write_port.submit_calls == 0``, NOT_ARMED/denied. RED today: the
    facade takes the substituted identity as authoritative, ARMS, and submits the possibly-live
    attempt under the WRONG session ledger (bypassing the requested session's caps + loss)."""
    binding = _mm_binding()
    adapter = RecordingFakeAdapter(fill=True, open_orders=[])
    write_port = _default_write_port(binding)
    store = InMemoryOperatorInterlockStore()

    result = await _drive_mode_b(
        interlock=_full_interlock(),
        arming=_mm_arming(binding, write_port=write_port),
        binding=binding,
        adapter=adapter,
        interlock_store=store,
        provider=_EchoRiskProvider(identity="other-session", risk_session="other-session"),
    )

    assert write_port.submit_calls == 0, (
        "a provider identity != request.session_id must FAIL CLOSED before any write-port I/O"
    )
    assert store.rows() == (), "a mismatched provider identity must record NO interlock receipt"
    assert result.execution_status == "NOT_ARMED"
    assert result.admission == "DENIED"


async def test_live_guarded_fails_closed_when_risk_accumulator_bound_to_a_different_session() -> None:
    """R4-MAJOR-2: even when the provider echoes ``request.session_id``, a supplied ``RiskAccumulator``
    bound to a DIFFERENT session must FAIL CLOSED — the run's loss caps would be enforced against the
    wrong ledger. ``write_port.submit_calls == 0``, no interlock recorded, NOT_ARMED/denied."""
    binding = _mm_binding()
    adapter = RecordingFakeAdapter(fill=True, open_orders=[])
    write_port = _default_write_port(binding)
    store = InMemoryOperatorInterlockStore()

    result = await _drive_mode_b(
        interlock=_full_interlock(),
        arming=_mm_arming(binding, write_port=write_port),
        binding=binding,
        adapter=adapter,
        interlock_store=store,
        provider=_EchoRiskProvider(identity="sess-mm-b", risk_session="other-session"),
    )

    assert write_port.submit_calls == 0, (
        "a risk accumulator bound to a different session must FAIL CLOSED before any write-port I/O"
    )
    assert store.rows() == (), "a mis-bound risk accumulator must record NO interlock receipt"
    assert result.execution_status == "NOT_ARMED"
    assert result.admission == "DENIED"


async def test_live_guarded_fails_closed_when_provider_identity_is_empty() -> None:
    """R4-MAJOR-2: an empty ``session_identity`` is not a valid adopted identity — FAIL CLOSED. A
    substituted empty/None identity can never be asserted equal to the operator-assigned
    ``request.session_id``, so the money path must not be entered."""
    binding = _mm_binding()
    adapter = RecordingFakeAdapter(fill=True, open_orders=[])
    write_port = _default_write_port(binding)
    store = InMemoryOperatorInterlockStore()

    result = await _drive_mode_b(
        interlock=_full_interlock(),
        arming=_mm_arming(binding, write_port=write_port),
        binding=binding,
        adapter=adapter,
        interlock_store=store,
        provider=_EchoRiskProvider(identity="", risk_session="sess-mm-b"),
    )

    assert write_port.submit_calls == 0, "an empty provider identity must FAIL CLOSED before write-port I/O"
    assert store.rows() == (), "an empty provider identity must record NO interlock receipt"
    assert result.execution_status == "NOT_ARMED"
    assert result.admission == "DENIED"


async def test_live_guarded_arms_when_provider_echoes_request_session_and_binds_risk_to_it() -> None:
    """Positive control: a provider that echoes ``request.session_id`` AND binds the risk accumulator
    to the SAME session arms/submits as before (offline; Mode B UNARMED recording-fakes). This proves
    the identity assertion does NOT over-block an honest, correctly-adopting provider."""
    binding = _mm_binding()
    adapter = RecordingFakeAdapter(fill=True, open_orders=[])
    write_port = _default_write_port(binding)
    store = InMemoryOperatorInterlockStore()

    result = await _drive_mode_b(
        interlock=_full_interlock(),
        arming=_mm_arming(binding, write_port=write_port),
        binding=binding,
        adapter=adapter,
        interlock_store=store,
        provider=_EchoRiskProvider(identity="sess-mm-b", risk_session="sess-mm-b"),
    )

    assert write_port.submit_calls == 1, "an honest identity-echoing provider must arm + reach the wire"
    assert adapter.submit_calls == 0, "Mode B must NEVER reach the generic adapter submit surface"
    assert result.execution_status == "SUBMITTED"
    assert len(store.rows()) == 1, "an honest armed run records exactly one interlock receipt"


# ---------------------------------------------------------------------------
# Gate#3 R4-MAJOR-1 — the possibly-live attempt is durably RESERVED BEFORE the
# wire (fail-closed on reserve failure), so the cap is CRASH-CONSISTENT.
#
# Before this fold the facade ran the complete fund-touching runner FIRST and only
# recorded ``result.submitted_count`` AFTER it returned (the vulnerable
# ``load -> run -> record_run`` order). A durable-store failure / process crash
# AFTER the wire therefore never landed the cap consumption: the possibly-live
# attempt existed at the venue but the durable count reset, so the NEXT call
# submitted a SECOND order despite ``max_orders_per_session == 1`` (Codex repro:
# ``postwire_failure 1 ... wire_calls 1`` / ``postwire_failure 2 ... wire_calls 2``).
#
# The facade now RESERVES a possibly-live attempt durably BEFORE the runner reaches
# the write port (``load -> reserve -> run -> settle``): the reservation counts toward
# the session/day caps immediately and durably. A reserve failure FAILS CLOSED (no
# runner entry, zero write-port I/O). A post-wire ``settle`` failure or a crash after
# POST leaves the reservation STANDING (it is not rolled back / not lost), so the next
# call cannot exceed the cap — the durable-cap analog of the lane's persist-BEFORE-sign
# discipline. Each test asserts the WRITE-PORT (the money-moving surface).
# ---------------------------------------------------------------------------


class _DurableStoreOutage(RuntimeError):
    """A simulated durable-store OUTAGE raised by a test provider's recording port."""


class _PostWireSettleOutageProvider(InMemoryDurableSessionStateProvider):
    """``reserve`` durably records the possibly-live attempt (inherited); the AFTER-wire recording
    (``settle``, or the legacy ``record_run``) simulates a durable-store OUTAGE by raising — Codex's
    object: the write reaches the recording port AFTER the wire has already fired."""

    def settle(
        self,
        *,
        attempt_id: str,
        recon_state: ReconciliationState,
        venue_order_key: str | None = None,
    ) -> None:
        raise _DurableStoreOutage("durable store unavailable after wire")

    def record_run(self, *, session_identity: str, attempts: int, fills: object = ()) -> None:
        raise _DurableStoreOutage("durable store unavailable after wire")


class _ReserveOutageProvider(InMemoryDurableSessionStateProvider):
    """The ATOMIC before-wire ``reserve_or_freeze`` raises a durable-store OUTAGE; ``load`` returns valid
    zero state. The facade must FAIL CLOSED — the runner is never entered on the money path (zero
    write-port I/O), NOT_ARMED/denied. (R6-MAJOR-2 migrated the reserve path onto the single atomic op, so
    the outage is injected there.)"""

    def reserve_or_freeze(
        self,
        *,
        session_identity: str,
        now: datetime,
        attempt_id: str,
        max_orders_per_session: int = 0,
        max_orders_per_day: int = 0,
        venue_order_key: str | None = None,
    ) -> ScopedReservationOutcome:
        raise _DurableStoreOutage("durable store unavailable before wire")


class _CrashBeforeSettleProvider(InMemoryDurableSessionStateProvider):
    """Models a process crash AFTER POST but BEFORE the durable count lands: ``reserve`` records the
    possibly-live attempt durably (inherited); the AFTER-wire recording (``settle`` / legacy
    ``record_run``) is a LOST WRITE (no-op), as if the process died before it landed. The reserved,
    unsettled row stays durable and counted (conservative — a possibly-live attempt stays counted)."""

    def settle(
        self,
        *,
        attempt_id: str,
        recon_state: ReconciliationState,
        venue_order_key: str | None = None,
    ) -> None:
        return None  # the settle write never lands (crash) — the row stays PENDING (possibly-live)

    def record_run(self, *, session_identity: str, attempts: int, fills: object = ()) -> None:
        return None  # the legacy after-wire write never lands (crash)


async def test_post_wire_settle_failure_does_not_reset_the_durable_reservation() -> None:
    """Codex's object: a provider whose ``reserve`` SUCCEEDS but whose AFTER-wire recording raises a
    store outage. Two same-session calls at ``max_orders_per_session == 1``: the 1st fires the wire,
    the durable BEFORE-wire reservation survives the post-wire outage, and the 2nd is DENIED by the
    per-session cap — ``write_port.submit_calls == 1`` TOTAL. RED today: after-run counting never
    lands on the outage, the cap resets, and BOTH submit (``submit_calls == 2``)."""
    binding = _mm_binding()
    adapter = RecordingFakeAdapter(fill=True, open_orders=[])
    write_port = _default_write_port(binding)  # SHARED — counts total submits across both calls
    provider = _PostWireSettleOutageProvider()
    envelope = _mm_env(max_orders_per_session=1)

    # The AFTER-wire recording raises a store outage each call; the caller tolerates it (the
    # possibly-live attempt has already fired). GREEN swallows the settle outage inside the facade —
    # the durable reservation is what carries the cap across the two calls.
    for _ in range(2):
        # RED today: the legacy after-wire record raises and the count never lands. GREEN swallows the
        # settle outage inside the facade, so this suppress simply does not trigger.
        with contextlib.suppress(_DurableStoreOutage):
            await _drive_mode_b(
                interlock=_full_interlock(),
                arming=_mm_arming(binding, write_port=write_port),
                binding=binding,
                adapter=adapter,
                interlock_store=InMemoryOperatorInterlockStore(),
                envelope=envelope,
                provider=provider,
            )

    assert write_port.submit_calls == 1, (
        "the durable BEFORE-wire reservation must survive a post-wire store outage so the 2nd "
        "same-session call is denied by the per-session cap (RED today: both submit)"
    )


async def test_reserve_failure_before_wire_fails_closed_with_zero_write_port_calls() -> None:
    """A before-wire reservation FAILURE (store outage) must FAIL CLOSED: the runner is never entered
    on the money path, ``write_port.submit_calls == 0``, NOT_ARMED/denied. RED today: with only
    after-run counting there is no before-wire reservation gate, so the armed run submits."""
    binding = _mm_binding()
    adapter = RecordingFakeAdapter(fill=True, open_orders=[])
    write_port = _default_write_port(binding)

    result = await _drive_mode_b(
        interlock=_full_interlock(),
        arming=_mm_arming(binding, write_port=write_port),
        binding=binding,
        adapter=adapter,
        interlock_store=InMemoryOperatorInterlockStore(),
        envelope=_mm_env(max_orders_per_session=1),
        provider=_ReserveOutageProvider(),
    )

    assert write_port.submit_calls == 0, (
        "a before-wire reservation failure must FAIL CLOSED — the runner is never entered, zero "
        "write-port I/O (RED today: the armed run submits with only after-run counting)"
    )
    assert result.execution_status == "NOT_ARMED"
    assert result.admission == "DENIED"


async def test_crash_after_post_freezes_a_distinct_attempt_via_session_scope() -> None:
    """R5-MAJOR-1 / IDM-002 (Codex's exact object, RED today): a crash-after-POST leaves the first
    reservation UNSETTLED (possibly-live). A second call that changes the ``client_order_id`` (a
    genuinely DISTINCT ``attempt_id``) must NOT submit AROUND the unresolved first order — the session
    freeze scope refuses EVERY new wire while a possibly-live reservation stands, INCLUDING a distinct
    id, at ``max_orders_per_session == max_orders_per_day == 2`` (SPARE capacity, so the CAP cannot mask
    it — the freeze is driven by the possibly-live reservation, not the cap). ``submit_calls == 1``
    TOTAL, and the frozen distinct attempt is an EXPLICIT pending-reconciliation / NOT_ARMED / DENIED
    terminal, never SUBMITTED. RED today: stable-id dedup alone lets the distinct id reserve+submit a
    SECOND order (``submit_calls == 2``)."""
    binding = _mm_binding()
    adapter = RecordingFakeAdapter(fill=True, open_orders=[])
    write_port = _default_write_port(binding)  # SHARED — counts total submits across the crash
    provider = _CrashBeforeSettleProvider()  # the SAME durable provider survives the simulated crash
    envelope = _mm_env(max_orders_per_session=2, max_orders_per_day=2)  # SPARE capacity — the cap masks nothing

    # Call 1: reserve lands, the wire fires, then the process "crashes" before the settle write lands.
    await _drive_mode_b(
        interlock=_full_interlock(),
        arming=_mm_arming(binding, write_port=write_port),
        binding=binding,
        adapter=adapter,
        interlock_store=InMemoryOperatorInterlockStore(),
        envelope=envelope,
        provider=provider,
    )
    # Call 2: a DISTINCT authorized attempt (a distinct client_order_id → a distinct attempt_id) that
    # tries to submit AROUND the unresolved first order on the SAME durable provider.
    second = await _drive_mode_b(
        interlock=_full_interlock(),
        arming=_mm_arming(binding, write_port=write_port),
        binding=binding,
        adapter=adapter,
        interlock_store=InMemoryOperatorInterlockStore(),
        envelope=envelope,
        provider=provider,
        client_order_id="coid-2",  # a DISTINCT attempt_id — must STILL be frozen by the session scope
    )

    assert write_port.submit_calls == 1, (
        "a distinct attempt must NOT submit around an unresolved possibly-live reservation — the session "
        "freeze scope refuses every new wire (RED today: the distinct id reserves+submits a 2nd order)"
    )
    assert provider.attempts("sess-mm-b") == 1, "the frozen distinct attempt must NOT append a second row"
    # The frozen distinct attempt is an EXPLICIT pending/denied terminal — never a success/executed one.
    assert second.execution_status != "SUBMITTED"
    assert second.execution_status == "NOT_ARMED"
    assert second.admission == "DENIED"
    assert "attempt_pending_reconciliation" in second.reason_codes


async def test_reserved_slot_released_on_abstain_does_not_consume_cap() -> None:
    """Positive control: a fully-armed ``no_quote`` abstains (no wire), so ``settle(committed=False)``
    RELEASES the reservation — it must NOT wrongly consume a cap slot (``attempts`` back to 0)."""
    binding = _mm_binding()
    adapter = FakeVenueAdapter(fill=True)
    provider = InMemoryDurableSessionStateProvider()

    result = await _drive_mode_b_kind(
        intent_kind="no_quote",
        intent_params=MMIntentParams(),  # an explicit abstention — no order reaches the wire
        arming=_mm_arming(binding),
        interlock=_full_interlock(),  # fully armed, so the abstain is the INTENT's, not want of arming
        binding=binding,
        adapter=adapter,
        interlock_store=InMemoryOperatorInterlockStore(),
        provider=provider,
    )

    assert result.execution_status == "ABSTAINED"
    assert adapter.submit_calls == 0
    assert provider.attempts("sess-mm-b") == 0, (
        "an abstain (no wire) must RELEASE the reservation so it does not wrongly consume a cap slot"
    )


async def test_distinct_authorized_attempt_within_cap_allows_second_wire() -> None:
    """R5-MAJOR-1 (over-block guard): a genuinely DISTINCT authorized attempt (a DISTINCT
    ``client_order_id`` → a DISTINCT stable ``attempt_id``) within ``max_orders_per_session == 2`` is a
    fresh reserve + a second wire — the reserve-or-load idempotency must NOT over-block a legitimate
    second attempt. Two distinct committed possibly-live attempts persist under a cap of two."""
    binding = _mm_binding()
    # ``fill_history_matches=True`` → each call's E4 reconcile RESOLVES (definite fill), so a resolved
    # first attempt does NOT freeze the session scope — a distinct 2nd attempt is legitimately allowed.
    adapter = RecordingFakeAdapter(fill=True, fill_history_matches=True, open_orders=[])
    write_port = _default_write_port(binding)  # SHARED across both calls
    provider = InMemoryDurableSessionStateProvider()
    envelope = _mm_env(max_orders_per_session=2, max_orders_per_day=2)  # room for two distinct attempts

    first = await _drive_mode_b(
        interlock=_full_interlock(),
        arming=_mm_arming(binding, write_port=write_port),
        binding=binding,
        adapter=adapter,
        interlock_store=InMemoryOperatorInterlockStore(),
        envelope=envelope,
        provider=provider,
    )
    second = await _drive_mode_b(
        interlock=_full_interlock(),
        arming=_mm_arming(binding, write_port=write_port),
        binding=binding,
        adapter=adapter,
        interlock_store=InMemoryOperatorInterlockStore(),
        envelope=envelope,
        provider=provider,
        client_order_id="coid-2",  # a DISTINCT authorized attempt → a DISTINCT stable attempt_id
    )

    assert first.execution_status == "SUBMITTED"
    assert second.execution_status == "SUBMITTED", "a DISTINCT attempt within the cap must be allowed"
    assert write_port.submit_calls == 2
    assert provider.attempts("sess-mm-b") == 2, "both distinct committed reservations persist under the cap"


# ---------------------------------------------------------------------------
# Gate#3 R5-MAJOR-1 — the reservation ``attempt_id`` is derived from a STABLE
# request fingerprint (independent of any mutable durable count/index), and the
# provider ``reserve`` is idempotent (reserve-OR-load): an IDENTICAL retry
# reconciles to the existing reservation instead of minting a new id and
# double-submitting.
#
# Before this fold the ``attempt_id`` bound the MONOTONIC durable count
# (``prior_attempt_index``): once the first reservation existed, the next ``load``
# incremented the count, so the SAME request derived a DIFFERENT id on retry — a
# crash-after-POST retry minted a SECOND reservation and submitted a SECOND order
# whenever any spare session/day capacity existed (cap >= 2). The cap-one tests
# only PASSED because the cap incidentally blocked every next call — NOT
# idempotency. The id now binds the COMPLETE admitted-order fingerprint (session +
# intent_kind + the full intent_params + manifest/policy/strategy hashes + mode),
# so an identical retry derives the SAME id and reconciles:
#   * an existing UNSETTLED (possibly-live) row -> FREEZE UNCONDITIONALLY (no new
#     wire, regardless of spare cap) with an EXPLICIT pending-reconciliation /
#     NOT_ARMED / DENIED disposition — NEVER a SUBMITTED/success "recovered" status
#     (a live-armed path must not treat the retry as resolved until the production
#     venue-truth resolver runs);
#   * an existing COMMITTED row -> idempotent replay (no new wire);
#   * a RELEASED row (settle committed=False) -> absent -> a fresh reserve + wire.
# Each test runs at ``max_orders_per_session == max_orders_per_day == 2`` so the
# cap masks nothing — the single-wire outcome is driven by the reservation, not the
# cap. Each asserts the WRITE-PORT (the money-moving surface).
# ---------------------------------------------------------------------------


async def test_identical_retry_after_crash_freezes_no_second_wire_within_spare_cap() -> None:
    """R5-MAJOR-1 (RED today): a crash-after-POST leaves the first reservation UNSETTLED
    (possibly-live). An IDENTICAL retry sharing the durable provider derives the SAME stable
    ``attempt_id``, finds the unsettled row, and FREEZES — ZERO new write-port calls and exactly ONE
    reservation — EVEN WITH spare session/day capacity (``max_orders_per_session == max_orders_per_day
    == 2``), because a possibly-live first order exists (NOT because a cap blocked). The frozen retry's
    disposition is an EXPLICIT pending-reconciliation / NOT_ARMED / DENIED terminal — NEVER
    SUBMITTED/success. RED today: the count-derived id advances, so the retry mints a SECOND reservation
    and submits a SECOND order (``submit_calls == 2``, two reservations)."""
    binding = _mm_binding()
    adapter = RecordingFakeAdapter(fill=True, open_orders=[])
    write_port = _default_write_port(binding)  # SHARED — counts total submits across the crash+retry
    provider = _CrashBeforeSettleProvider()  # reserve lands; the after-wire settle is LOST (crash)
    envelope = _mm_env(max_orders_per_session=2, max_orders_per_day=2)  # SPARE capacity — the cap masks nothing

    # Call 1: reserve lands, the wire fires, then the settle write is LOST (process crash after POST).
    await _drive_mode_b(
        interlock=_full_interlock(),
        arming=_mm_arming(binding, write_port=write_port),
        binding=binding,
        adapter=adapter,
        interlock_store=InMemoryOperatorInterlockStore(),
        envelope=envelope,
        provider=provider,
    )
    # Call 2: the IDENTICAL request (same client_order_id) on the SAME durable provider.
    retry = await _drive_mode_b(
        interlock=_full_interlock(),
        arming=_mm_arming(binding, write_port=write_port),
        binding=binding,
        adapter=adapter,
        interlock_store=InMemoryOperatorInterlockStore(),
        envelope=envelope,
        provider=provider,
    )

    # Exactly ONE wire even though the cap left room for a second — the freeze (not the cap) is why.
    assert write_port.submit_calls == 1, (
        "an identical retry atop an unsettled possibly-live reservation must FREEZE — exactly ONE wire "
        "even with spare cap (RED today: the count-derived id advances and a SECOND order submits)"
    )
    assert provider.attempts("sess-mm-b") == 1, "the frozen retry must NOT append a second reservation row"
    # The frozen retry is an EXPLICIT pending/denied terminal — NEVER a success/executed disposition, so
    # a future live-armed path can never read it as "recovered/complete" before venue-truth reconcile.
    assert retry.execution_status != "SUBMITTED"
    assert retry.execution_status == "NOT_ARMED"
    assert retry.admission == "DENIED"
    assert "attempt_pending_reconciliation" in retry.reason_codes


async def test_committed_then_identical_retry_is_idempotent_no_second_wire() -> None:
    """R5-MAJOR-1 (RED today): the first attempt SETTLES committed (the wire fired and was recorded). An
    IDENTICAL retry derives the SAME stable ``attempt_id``, finds the COMMITTED reservation, and is
    IDEMPOTENT — no new wire; it replays the first attempt's known-good outcome. ``submit_calls == 1``
    and exactly ONE reservation under a SPARE cap of two. RED today: the count-derived id advances so
    the identical retry submits a SECOND order (``submit_calls == 2``)."""
    binding = _mm_binding()
    # ``fill_history_matches=True`` → the 1st call's E4 reconcile RESOLVES (definite fill), so the
    # reservation is a COMMITTED/RESOLVED terminal — an identical retry replays it idempotently (not a
    # possibly-live freeze).
    adapter = RecordingFakeAdapter(fill=True, fill_history_matches=True, open_orders=[])
    write_port = _default_write_port(binding)  # SHARED
    provider = InMemoryDurableSessionStateProvider()
    envelope = _mm_env(max_orders_per_session=2, max_orders_per_day=2)  # SPARE capacity — the cap masks nothing

    first = await _drive_mode_b(
        interlock=_full_interlock(),
        arming=_mm_arming(binding, write_port=write_port),
        binding=binding,
        adapter=adapter,
        interlock_store=InMemoryOperatorInterlockStore(),
        envelope=envelope,
        provider=provider,
    )
    retry = await _drive_mode_b(
        interlock=_full_interlock(),
        arming=_mm_arming(binding, write_port=write_port),
        binding=binding,
        adapter=adapter,
        interlock_store=InMemoryOperatorInterlockStore(),
        envelope=envelope,
        provider=provider,
    )

    assert first.execution_status == "SUBMITTED"
    assert write_port.submit_calls == 1, (
        "an identical retry of a COMMITTED attempt must NOT re-fire the wire (RED today: it submits again)"
    )
    assert provider.attempts("sess-mm-b") == 1, "an idempotent committed retry must NOT append a second reservation"
    # Idempotent replay of the first attempt's KNOWN-GOOD (settled-committed) outcome — a resolved
    # terminal, distinct from the possibly-live FREEZE above.
    assert retry.execution_status == "SUBMITTED"
    assert retry.admission == "APPROVED"


async def test_abstain_release_then_identical_retry_is_a_fresh_reserve_not_frozen() -> None:
    """R5-MAJOR-1 (over-block guard): the first attempt ABSTAINS (no wire), so ``settle(committed=False)``
    RELEASES the reservation. An IDENTICAL retry finds NO row (released == absent) and is a FRESH
    reserve+run — NOT frozen: it re-enters the runner (``ABSTAINED`` with ``intent_no_quote``), never
    the pending/denied freeze disposition, and leaves ZERO standing reservations. Guards against a
    released id wrongly freezing a legitimate later attempt."""
    binding = _mm_binding()
    adapter = FakeVenueAdapter(fill=True)
    provider = InMemoryDurableSessionStateProvider()

    first = await _drive_mode_b_kind(
        intent_kind="no_quote",
        intent_params=MMIntentParams(),  # an explicit abstention — no order reaches the wire
        arming=_mm_arming(binding),
        interlock=_full_interlock(),  # fully armed, so the abstain is the INTENT's, not want of arming
        binding=binding,
        adapter=adapter,
        interlock_store=InMemoryOperatorInterlockStore(),
        provider=provider,
    )
    retry = await _drive_mode_b_kind(
        intent_kind="no_quote",
        intent_params=MMIntentParams(),  # the IDENTICAL request
        arming=_mm_arming(binding),
        interlock=_full_interlock(),
        binding=binding,
        adapter=adapter,
        interlock_store=InMemoryOperatorInterlockStore(),
        provider=provider,
    )

    assert first.execution_status == "ABSTAINED"
    # The released id is ABSENT → a FRESH reserve+run, NOT a freeze: the runner is re-entered.
    assert retry.execution_status == "ABSTAINED", "a released id must permit a FRESH reserve+run, not a freeze"
    assert "intent_no_quote" in retry.execution_reason_codes
    assert "attempt_pending_reconciliation" not in retry.reason_codes
    assert adapter.submit_calls == 0
    assert provider.attempts("sess-mm-b") == 0, "an abstain-released id must leave ZERO standing reservations"


# ---------------------------------------------------------------------------
# Gate#3 R5-MAJOR-1 / IDM-002 — the FREEZE predicate keys on the RECONCILIATION
# axis (the E4 tri-state), NOT on wire-fired / ``submitted``. A submitted order
# whose reconciliation is AMBIGUOUS is possibly-live and must FREEZE the session
# scope; only a RESOLVED (definite fill) reconciliation stops freezing (the slot
# stays COUNTED). The result->settle reducer selects the disposition from the EXACT
# attempted decision, MATCHED to its official ``venue_order_key`` — never "any
# RESOLVED row anywhere in the result" — and FAILS CLOSED (stays frozen) on a
# missing / mismatched / duplicate reconciliation row.
# ---------------------------------------------------------------------------


async def test_ambiguous_reconciliation_of_first_freezes_a_distinct_second() -> None:
    """R5-MAJOR-1 / IDM-002 (RED against a submitted==resolved reducer): the 1st armed take SUBMITS but
    its E4 reconcile is AMBIGUOUS (the recording-fake's fail-closed default: no matching own trade), so
    the reservation is possibly-live and FREEZES the session scope. A genuinely DISTINCT 2nd attempt
    (distinct ``client_order_id``) must NOT submit around it — ``write_port.submit_calls == 1`` TOTAL at
    ``max_orders_per_session == max_orders_per_day == 2`` (SPARE cap — the freeze is driven by the
    AMBIGUOUS-pending first, not the cap). The frozen 2nd attempt is an EXPLICIT pending / NOT_ARMED /
    DENIED terminal. Wire-fired alone must NEVER clear the freeze — only a reconciliation RESOLUTION."""
    binding = _mm_binding()
    # Default (no ``fill_history_matches``) → the 1st call's E4 reconcile is AMBIGUOUS (possibly-live).
    adapter = RecordingFakeAdapter(fill=True, open_orders=[])
    write_port = _default_write_port(binding)  # SHARED — counts total submits
    provider = InMemoryDurableSessionStateProvider()  # a NORMAL provider: settle DOES land (AMBIGUOUS)
    envelope = _mm_env(max_orders_per_session=2, max_orders_per_day=2)  # SPARE capacity — the cap masks nothing

    await _drive_mode_b(
        interlock=_full_interlock(),
        arming=_mm_arming(binding, write_port=write_port),
        binding=binding,
        adapter=adapter,
        interlock_store=InMemoryOperatorInterlockStore(),
        envelope=envelope,
        provider=provider,
    )
    second = await _drive_mode_b(
        interlock=_full_interlock(),
        arming=_mm_arming(binding, write_port=write_port),
        binding=binding,
        adapter=adapter,
        interlock_store=InMemoryOperatorInterlockStore(),
        envelope=envelope,
        provider=provider,
        client_order_id="coid-2",  # a DISTINCT attempt — must STILL be frozen by the AMBIGUOUS-pending first
    )

    assert write_port.submit_calls == 1, (
        "a submitted-but-AMBIGUOUS first order stays possibly-live and freezes the scope — a distinct "
        "2nd attempt must NOT submit (wire-fired alone must never clear the freeze)"
    )
    assert second.execution_status != "SUBMITTED"
    assert second.execution_status == "NOT_ARMED"
    assert second.admission == "DENIED"
    assert "attempt_pending_reconciliation" in second.reason_codes


async def test_resolved_reconciliation_of_first_allows_a_distinct_second_and_first_still_counts() -> None:
    """R5-MAJOR-1 / IDM-002 (positive control): the 1st armed take SUBMITS and its E4 reconcile RESOLVES
    (a definite fill via ``fill_history_matches``), so it STOPS freezing the scope while STAYING COUNTED.
    A genuinely DISTINCT 2nd attempt IS then allowed — ``write_port.submit_calls == 2`` under a cap of
    two, and BOTH reservations still count (``attempts == 2``). Proves the reconciliation-keyed freeze
    does not over-block once nothing is unresolved, and that a RESOLVED attempt keeps consuming its cap
    slot."""
    binding = _mm_binding()
    # ``fill_history_matches=True`` → each call's E4 reconcile RESOLVES (definite fill), not AMBIGUOUS.
    adapter = RecordingFakeAdapter(fill=True, fill_history_matches=True, open_orders=[])
    write_port = _default_write_port(binding)  # SHARED
    provider = InMemoryDurableSessionStateProvider()
    envelope = _mm_env(max_orders_per_session=2, max_orders_per_day=2)

    first = await _drive_mode_b(
        interlock=_full_interlock(),
        arming=_mm_arming(binding, write_port=write_port),
        binding=binding,
        adapter=adapter,
        interlock_store=InMemoryOperatorInterlockStore(),
        envelope=envelope,
        provider=provider,
    )
    second = await _drive_mode_b(
        interlock=_full_interlock(),
        arming=_mm_arming(binding, write_port=write_port),
        binding=binding,
        adapter=adapter,
        interlock_store=InMemoryOperatorInterlockStore(),
        envelope=envelope,
        provider=provider,
        client_order_id="coid-2",  # a DISTINCT authorized attempt — allowed once nothing is unresolved
    )

    assert first.execution_status == "SUBMITTED"
    assert second.execution_status == "SUBMITTED", "a distinct attempt is allowed once nothing is unresolved"
    assert write_port.submit_calls == 2
    assert provider.attempts("sess-mm-b") == 2, "a RESOLVED first attempt still counts toward the cap"


def _recon_events(
    *,
    submit_key: str,
    recon_key: str | None,
    recon_state: str = "RESOLVED",
    duplicate: bool = False,
    recon_decision_id: str = "D1",
) -> tuple[object, ...]:
    """Build a minimal submitted-decision lifecycle stream for the reducer negative test.

    A submitted ``OrderAckEvent`` + its ``OrderSubmitAttempt`` (decision ``D1``, pre-submit
    ``venue_order_key == submit_key``) and, unless ``recon_key`` is ``None`` (MISSING), a
    ``RealFillReconciliation`` for ``recon_decision_id`` keyed on ``recon_key`` (``duplicate`` emits two
    so the reducer sees a DUPLICATE PAIR). A ``recon_key != submit_key`` is a MISMATCHED key; a
    ``recon_decision_id != "D1"`` is a FOREIGN decision (R6-MAJOR-1). The reducer must join on the exact
    PAIR ``(decision_id, venue_order_key)`` and FAIL CLOSED (``AMBIGUOUS``) on any of these, never pick a
    stray RESOLVED row belonging to a different decision.
    """
    events: list[object] = [
        OrderSubmitAttempt(
            sequence_no=1,
            event_type="OrderSubmitAttempt",
            source_ts=None,
            recv_ts=0,
            decision_id="D1",
            client_order_id="coid-1",
            request_payload_ref="ref",
            attempt_ts=0,
            presubmit_record=PreSubmitRecord(
                integrity_commitment_hash="h", venue_order_key=submit_key, captured_id=None
            ),
        ),
        OrderAckEvent(
            sequence_no=2,
            event_type="OrderAckEvent",
            source_ts=None,
            recv_ts=0,
            decision_id="D1",
            client_order_id="coid-1",
            venue_order_id="V1",
            ack_status="accepted",  # a REAL (non-dry-run) submit reached the wire
        ),
    ]
    if recon_key is not None:
        for _ in range(2 if duplicate else 1):
            events.append(
                RealFillReconciliation(
                    sequence_no=3,
                    event_type="RealFillReconciliation",
                    source_ts=None,
                    recv_ts=0,
                    decision_id=recon_decision_id,
                    venue_order_key=recon_key,
                    reconciled_state=recon_state,  # type: ignore[arg-type]
                    reconciled_fill_size=1.0,
                )
            )
    return tuple(events)


class _EventsOnlyResult:
    """A minimal ``DustExecutionResult`` stand-in exposing only ``.events`` (all the reducer reads)."""

    def __init__(self, events: tuple[object, ...]) -> None:
        self.events = events


def test_reconciliation_reducer_fails_closed_on_unmatched_venue_order_key() -> None:
    """R5-MAJOR-1 / IDM-002 (negative, keyed reducer): the result->settle reducer MUST select the
    disposition from the reconciliation row MATCHED to THIS decision's official ``venue_order_key`` and
    FAIL CLOSED (``AMBIGUOUS`` — possibly-live / frozen) on a MISMATCHED, MISSING, or DUPLICATE row —
    NEVER pick a stray RESOLVED row and wrongly clear the freeze. A mutation that returns the first
    RESOLVED row regardless of key match turns each of these ``RESOLVED``, failing this test."""
    # MISMATCH: a RESOLVED reconciliation row keyed on a DIFFERENT venue_order_key must NOT resolve.
    mismatch = _EventsOnlyResult(_recon_events(submit_key="K1", recon_key="K2", recon_state="RESOLVED"))
    assert facade._reconciliation_outcome(mismatch)[0] == "AMBIGUOUS"  # type: ignore[arg-type]
    # MISSING: a submitted decision with NO reconciliation row at all must fail closed.
    missing = _EventsOnlyResult(_recon_events(submit_key="K1", recon_key=None))
    assert facade._reconciliation_outcome(missing)[0] == "AMBIGUOUS"  # type: ignore[arg-type]
    # DUPLICATE: two reconciliation rows for THIS key is ambiguous, not a clean resolution.
    duplicate = _EventsOnlyResult(
        _recon_events(submit_key="K1", recon_key="K1", recon_state="RESOLVED", duplicate=True)
    )
    assert facade._reconciliation_outcome(duplicate)[0] == "AMBIGUOUS"  # type: ignore[arg-type]
    # Control: exactly ONE row whose key MATCHES and is RESOLVED does resolve (no over-freezing).
    matched = _EventsOnlyResult(_recon_events(submit_key="K1", recon_key="K1", recon_state="RESOLVED"))
    assert facade._reconciliation_outcome(matched)[0] == "RESOLVED"  # type: ignore[arg-type]

    # State-machine consequence: a reservation SETTLED from the unmatched (mismatch) disposition stays
    # possibly-live / FROZEN, so a subsequent DISTINCT attempt in the session scope is still frozen.
    provider = InMemoryDurableSessionStateProvider()
    provider.reserve(session_identity="sess-mm-b", now=_mm_now_dt(), attempt_id="stuck-attempt")
    frozen_state, _ = facade._reconciliation_outcome(mismatch)  # type: ignore[arg-type]
    provider.settle(attempt_id="stuck-attempt", recon_state=frozen_state)
    assert provider.has_unresolved_reservation("sess-mm-b"), (
        "an unmatched reconciliation must leave the reservation possibly-live / FROZEN (never resolved)"
    )


async def test_distinct_attempt_frozen_by_an_unresolved_reservation_in_scope() -> None:
    """R5-MAJOR-1 / IDM-002: a session carrying an UNRESOLVED (AMBIGUOUS) reservation FREEZES a fresh
    DISTINCT authorized attempt — ``write_port.submit_calls == 0`` — until it resolves. Seeds an
    ambiguous possibly-live reservation, then drives a genuinely distinct attempt in that session."""
    binding = _mm_binding()
    adapter = RecordingFakeAdapter(fill=True, open_orders=[])
    write_port = _default_write_port(binding)
    provider = InMemoryDurableSessionStateProvider()
    # A possibly-live, AMBIGUOUS-reconciled reservation already occupies the session freeze scope.
    provider.reserve(session_identity="sess-mm-b", now=_mm_now_dt(), attempt_id="stuck-attempt")
    provider.settle(attempt_id="stuck-attempt", recon_state="AMBIGUOUS")

    result = await _drive_mode_b(
        interlock=_full_interlock(),
        arming=_mm_arming(binding, write_port=write_port),
        binding=binding,
        adapter=adapter,
        interlock_store=InMemoryOperatorInterlockStore(),
        envelope=_mm_env(max_orders_per_session=2, max_orders_per_day=2),
        provider=provider,
        client_order_id="coid-distinct",  # a genuinely distinct attempt — still frozen by the scope
    )

    assert write_port.submit_calls == 0, "an unresolved reservation in scope must FREEZE a distinct attempt"
    assert result.execution_status == "NOT_ARMED"
    assert result.admission == "DENIED"
    assert "attempt_pending_reconciliation" in result.reason_codes


# ---------------------------------------------------------------------------
# Gate#3 R6-MAJOR-1 — the reconciliation reducer joins on the exact PAIR
# ``(decision_id, venue_order_key)``, not on ``venue_order_key`` ALONE. A
# reconciliation row for a FOREIGN decision that merely carries THIS decision's
# key is NOT positive terminal proof and must NOT clear the freeze.
# ---------------------------------------------------------------------------


def test_reconciliation_reducer_fails_closed_on_foreign_decision_sharing_the_key() -> None:
    """R6-MAJOR-1 (RED against a key-ONLY join): the reducer must join reconciliation on the exact PAIR
    ``(decision_id, venue_order_key)``. A ``RealFillReconciliation`` belonging to a DIFFERENT decision
    (``D2``) that merely carries THIS submitted decision's key (``K1``) is NOT positive terminal proof
    for ``D1`` — the disposition FAILS CLOSED to ``AMBIGUOUS`` (possibly-live / frozen). A key-only join
    returns ``('RESOLVED', 'K1')`` and wrongly clears ``D1``'s freeze (Codex's D2/K1 repro)."""
    # FOREIGN decision: a RESOLVED row for D2 carrying D1's key K1 must NOT resolve D1.
    foreign = _EventsOnlyResult(
        _recon_events(
            submit_key="K1", recon_key="K1", recon_state="RESOLVED", recon_decision_id="D2"
        )
    )
    assert facade._reconciliation_outcome(foreign)[0] == "AMBIGUOUS"  # type: ignore[arg-type]

    # DUPLICATE PAIR: two rows with the SAME decision AND key is ambiguous, not a clean resolution.
    duplicate_pair = _EventsOnlyResult(
        _recon_events(
            submit_key="K1",
            recon_key="K1",
            recon_state="RESOLVED",
            recon_decision_id="D1",
            duplicate=True,
        )
    )
    assert facade._reconciliation_outcome(duplicate_pair)[0] == "AMBIGUOUS"  # type: ignore[arg-type]

    # Control: exactly ONE row whose (decision_id, venue_order_key) PAIR matches and is RESOLVED resolves.
    matched = _EventsOnlyResult(
        _recon_events(
            submit_key="K1", recon_key="K1", recon_state="RESOLVED", recon_decision_id="D1"
        )
    )
    assert facade._reconciliation_outcome(matched)[0] == "RESOLVED"  # type: ignore[arg-type]

    # State-machine consequence: a reservation SETTLED from the FOREIGN (AMBIGUOUS) disposition stays
    # possibly-live / FROZEN — an unmatched foreign row can never clear it.
    provider = InMemoryDurableSessionStateProvider()
    provider.reserve(session_identity="sess-mm-b", now=_mm_now_dt(), attempt_id="stuck-attempt")
    frozen_state, _ = facade._reconciliation_outcome(foreign)  # type: ignore[arg-type]
    provider.settle(attempt_id="stuck-attempt", recon_state=frozen_state)
    assert provider.has_unresolved_reservation("sess-mm-b"), (
        "a foreign-decision reconciliation must leave the reservation possibly-live / FROZEN"
    )


async def test_foreign_decision_disposition_keeps_a_distinct_attempt_frozen() -> None:
    """R6-MAJOR-1 state-machine tie-in (RED against a key-ONLY join): a reservation settled from a
    FOREIGN-decision reconciliation disposition stays possibly-live, so a genuinely DISTINCT authorized
    attempt in the same session is FROZEN — ZERO new wire — until it resolves. RED today: the key-only
    join returns ``RESOLVED`` for the foreign D2/K1 row, the reservation is settled RESOLVED (unfrozen),
    and the distinct 2nd attempt reserves + submits a SECOND order (``submit_calls == 1``)."""
    binding = _mm_binding()
    adapter = RecordingFakeAdapter(fill=True, open_orders=[])
    write_port = _default_write_port(binding)
    provider = InMemoryDurableSessionStateProvider()
    # Seed a possibly-live reservation and settle it from the FOREIGN-decision reducer disposition (D2/K1).
    foreign = _EventsOnlyResult(
        _recon_events(
            submit_key="K1", recon_key="K1", recon_state="RESOLVED", recon_decision_id="D2"
        )
    )
    provider.reserve(session_identity="sess-mm-b", now=_mm_now_dt(), attempt_id="stuck-attempt")
    frozen_state, _ = facade._reconciliation_outcome(foreign)  # type: ignore[arg-type]
    provider.settle(attempt_id="stuck-attempt", recon_state=frozen_state)

    result = await _drive_mode_b(
        interlock=_full_interlock(),
        arming=_mm_arming(binding, write_port=write_port),
        binding=binding,
        adapter=adapter,
        interlock_store=InMemoryOperatorInterlockStore(),
        envelope=_mm_env(max_orders_per_session=2, max_orders_per_day=2),  # SPARE cap — the freeze, not the cap
        provider=provider,
        client_order_id="coid-distinct",  # a genuinely distinct attempt — must STILL be frozen
    )

    assert write_port.submit_calls == 0, (
        "a foreign-decision row must NOT clear the freeze — a distinct attempt stays frozen (RED today: "
        "the key-only join resolves the foreign row and the 2nd order submits)"
    )
    assert result.execution_status == "NOT_ARMED"
    assert result.admission == "DENIED"
    assert "attempt_pending_reconciliation" in result.reason_codes


# ---------------------------------------------------------------------------
# Gate#3 R6-MAJOR-2 — the scope-freeze check and the reserve are ONE atomic
# provider op (``reserve_or_freeze``), NOT a separate ``has_unresolved_reservation``
# followed by ``reserve`` (a TOCTOU where two concurrent requests both observe an
# empty scope and both reserve). The fused op admits EXACTLY ONE fresh reservation
# and returns ``SCOPE_FROZEN`` for a different id while the scope is occupied.
# ---------------------------------------------------------------------------


class _ReserveSpyProvider(InMemoryDurableSessionStateProvider):
    """Records which reservation ops the facade invokes, to prove the reserve path uses the SINGLE
    atomic ``reserve_or_freeze`` and NOT a separate ``has_unresolved_reservation`` + ``reserve`` pair."""

    def __init__(self) -> None:
        super().__init__()
        self.has_unresolved_calls = 0
        self.reserve_calls = 0
        self.reserve_or_freeze_calls = 0

    def has_unresolved_reservation(self, session_identity: str) -> bool:
        self.has_unresolved_calls += 1
        return super().has_unresolved_reservation(session_identity)

    def reserve(
        self,
        *,
        session_identity: str,
        now: datetime,
        attempt_id: str,
        venue_order_key: str | None = None,
    ) -> ReservationOutcome:
        self.reserve_calls += 1
        return super().reserve(
            session_identity=session_identity,
            now=now,
            attempt_id=attempt_id,
            venue_order_key=venue_order_key,
        )

    def reserve_or_freeze(
        self,
        *,
        session_identity: str,
        now: datetime,
        attempt_id: str,
        max_orders_per_session: int = 0,
        max_orders_per_day: int = 0,
        venue_order_key: str | None = None,
    ) -> ScopedReservationOutcome:
        self.reserve_or_freeze_calls += 1
        return super().reserve_or_freeze(
            session_identity=session_identity,
            now=now,
            attempt_id=attempt_id,
            max_orders_per_session=max_orders_per_session,
            max_orders_per_day=max_orders_per_day,
            venue_order_key=venue_order_key,
        )


async def test_facade_reserve_path_uses_a_single_atomic_op_not_a_separate_scope_check() -> None:
    """R6-MAJOR-2 (RED against the split check-then-reserve): the facade must make the reservation
    decision via ONE atomic provider op (``reserve_or_freeze``) that FUSES the scope-freeze check and the
    reserve-or-load — NOT a separate ``has_unresolved_reservation`` followed by ``reserve`` (the TOCTOU
    where two concurrent requests both observe an empty scope and both reserve). The spy proves the
    separate scope check is GONE from the reserve path. RED today: the facade calls
    ``has_unresolved_reservation`` (count 1) then ``reserve`` (count 1) and never the atomic op."""
    binding = _mm_binding()
    adapter = RecordingFakeAdapter(fill=True, open_orders=[])
    write_port = _default_write_port(binding)
    spy = _ReserveSpyProvider()

    result = await _drive_mode_b(
        interlock=_full_interlock(),
        arming=_mm_arming(binding, write_port=write_port),
        binding=binding,
        adapter=adapter,
        interlock_store=InMemoryOperatorInterlockStore(),
        provider=spy,
    )

    assert result.execution_status == "SUBMITTED"
    assert write_port.submit_calls == 1
    assert spy.reserve_or_freeze_calls == 1, "the reserve path must use the single atomic op exactly once"
    assert spy.has_unresolved_calls == 0, (
        "the separate scope check must be GONE from the reserve path (a split re-introduces the TOCTOU)"
    )
    assert spy.reserve_calls == 0, "the split reserve must be GONE from the reserve path"


def test_atomic_reserve_or_freeze_admits_one_where_split_check_then_reserve_admits_both() -> None:
    """R6-MAJOR-2 (RED: ``reserve_or_freeze`` missing): the SPLIT check-then-reserve is the TOCTOU — two
    requests that BOTH observe an empty scope then BOTH reserve, standing up TWO possibly-live rows even
    with spare cap. The FUSED atomic op admits EXACTLY ONE (fresh ``RESERVED``) and returns
    ``SCOPE_FROZEN`` for the second — one critical section, no interleaving window."""
    now = _mm_now_dt()
    # SPLIT (the defect): both interleave their scope check (empty) BEFORE either reserves → both reserve.
    split = InMemoryDurableSessionStateProvider()
    assert split.has_unresolved_reservation("sess-mm-b") is False
    assert split.has_unresolved_reservation("sess-mm-b") is False
    assert split.reserve(session_identity="sess-mm-b", now=now, attempt_id="A") == "RESERVED"
    assert split.reserve(session_identity="sess-mm-b", now=now, attempt_id="B") == "RESERVED"
    assert split.attempts("sess-mm-b") == 2, "the split admits BOTH — two possibly-live rows (the TOCTOU)"

    # ATOMIC: the fused scope-check-and-reserve admits exactly one; the second is SCOPE_FROZEN, uninserted.
    atomic = InMemoryDurableSessionStateProvider()
    assert (
        atomic.reserve_or_freeze(session_identity="sess-mm-b", now=now, attempt_id="A") == "RESERVED"
    )
    assert (
        atomic.reserve_or_freeze(session_identity="sess-mm-b", now=now, attempt_id="B")
        == "SCOPE_FROZEN"
    )
    assert atomic.attempts("sess-mm-b") == 1, "the atomic op admits EXACTLY ONE — no interleaving window"


def test_reserve_or_freeze_discriminates_same_id_replay_from_a_different_id_scope_freeze() -> None:
    """R6-MAJOR-2 (RED: ``reserve_or_freeze`` missing): the atomic op's outcome DISCRIMINATES same-id
    idempotent replay from a different-id scope freeze — a same-id retry atop an unsettled row is
    ``PENDING_RECONCILE``; atop a resolved row is ``COMMITTED``; a DIFFERENT id while an unresolved row
    occupies the scope is ``SCOPE_FROZEN`` (uninserted); a fresh id with nothing unresolved is
    ``RESERVED``."""
    now = _mm_now_dt()
    p = InMemoryDurableSessionStateProvider()
    assert p.reserve_or_freeze(session_identity="sess-mm-b", now=now, attempt_id="A") == "RESERVED"
    # same-id retry atop the unsettled (PENDING) possibly-live row → idempotent PENDING_RECONCILE.
    assert (
        p.reserve_or_freeze(session_identity="sess-mm-b", now=now, attempt_id="A")
        == "PENDING_RECONCILE"
    )
    # a DIFFERENT id while A is unresolved → SCOPE_FROZEN (the scope is occupied); no row appended.
    assert (
        p.reserve_or_freeze(session_identity="sess-mm-b", now=now, attempt_id="B") == "SCOPE_FROZEN"
    )
    assert p.attempts("sess-mm-b") == 1, "a scope-frozen different id must NOT append a row"
    # settle A resolved → it stops freezing (stays counted); the same-id A retry now replays COMMITTED.
    p.settle(attempt_id="A", recon_state="RESOLVED")
    assert p.reserve_or_freeze(session_identity="sess-mm-b", now=now, attempt_id="A") == "COMMITTED"
    # a fresh distinct id with nothing unresolved (A is resolved, not freezing) → a fresh RESERVED.
    assert p.reserve_or_freeze(session_identity="sess-mm-b", now=now, attempt_id="C") == "RESERVED"


def test_reserve_or_freeze_is_a_real_critical_section_under_concurrent_distinct_ids() -> None:
    """R7-MAJOR-1 (RED against the UNLOCKED check-then-insert): ``reserve_or_freeze`` must be a REAL
    critical section, not merely "a synchronous method". The prior sequential regression called caller A
    to completion THEN B — proving nothing about atomicity. Here TWO threads with DISTINCT ``attempt_id``s
    and spare cap (session=day=2) are synchronized (a :class:`threading.Barrier` injected at the scope
    check, mirroring Codex's reproduction) so BOTH reach the scope check before EITHER inserts.

    The GIL switches threads at bytecode boundaries, so a single synchronous method is NOT atomic across
    threads: without a lock spanning the scope-check AND the insert, both callers observe the empty scope
    (``_has_unresolved`` False) before either row lands, and BOTH reserve — two standing possibly-live
    orders authorized toward the write port despite the session freeze.

    Contract: EXACTLY ONE thread returns ``RESERVED``, the other ``SCOPE_FROZEN``; EXACTLY ONE standing
    reservation; and — modelling the facade gate (RESERVED proceeds to the wire, SCOPE_FROZEN freezes) —
    EXACTLY ONE recording write-port submit under the concurrent schedule.

    RED today (no lock): the barrier lets both threads through the scope check together → both ``RESERVED``
    → 2 rows → 2 write-port submits. The RED is the MISSING LOCK (both reserve), not an import/typo."""
    now = _mm_now_dt()
    provider = InMemoryDurableSessionStateProvider()
    session_identity = "sess-mm-b"

    # A test-only seam: block BOTH threads at the scope check (``_has_unresolved``) with a barrier so they
    # interleave there before either inserts — the exact schedule the GIL permits under production load.
    # The barrier carries a timeout so a REAL lock (which must serialize the whole method) does NOT
    # deadlock: under the lock only one thread can hold it and reach the barrier, the barrier times out
    # and BREAKS, that thread proceeds, and the second thread — released when the lock frees — then sees
    # the standing row and freezes. Under the DEFECT (no lock) both threads reach the barrier together, it
    # releases cleanly, and both proceed to double-reserve.
    barrier = threading.Barrier(2, timeout=2.0)
    original_has_unresolved = provider._has_unresolved

    def _barriered_has_unresolved(session_id: str) -> bool:
        # Read the scope FIRST, THEN hold at the barrier: both threads must have OBSERVED the (empty)
        # scope before either inserts, so an unlocked check-then-insert double-reserves. (Blocking BEFORE
        # the read would merely let the GIL serialize the two calls — the second read would then see the
        # first's row and the defect would hide.) A real lock serializes the whole method, so only one
        # thread reaches the barrier; it times out, breaks, and that thread proceeds.
        result = original_has_unresolved(session_id)
        with contextlib.suppress(threading.BrokenBarrierError):
            barrier.wait()
        return result

    provider._has_unresolved = _barriered_has_unresolved  # type: ignore[assignment]

    outcomes: dict[str, ScopedReservationOutcome] = {}
    submitted: list[str] = []
    submit_lock = threading.Lock()

    def _worker(attempt_id: str) -> None:
        outcome = provider.reserve_or_freeze(
            session_identity=session_identity, now=now, attempt_id=attempt_id
        )
        outcomes[attempt_id] = outcome
        # Model the facade gate: ONLY a RESERVED outcome proceeds to the recording write port.
        if outcome == "RESERVED":
            with submit_lock:
                submitted.append(attempt_id)

    threads = [
        threading.Thread(target=_worker, args=("attempt-A",)),
        threading.Thread(target=_worker, args=("attempt-B",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)
        assert not thread.is_alive(), "a lock must not deadlock the concurrent reserve_or_freeze schedule"

    assert sorted(outcomes.values()) == ["RESERVED", "SCOPE_FROZEN"], (
        "exactly one concurrent caller may RESERVE; the other must be SCOPE_FROZEN "
        f"(got {sorted(outcomes.values())} — both RESERVED means the check+insert was not atomic)"
    )
    assert provider.attempts(session_identity) == 1, (
        "exactly ONE standing reservation — a second row means the scope-check+insert interleaved"
    )
    assert len(submitted) == 1, (
        "exactly ONE write-port submit under the concurrent schedule "
        f"(got {len(submitted)} — two submits means two possibly-live orders bypassed the freeze)"
    )


# ---------------------------------------------------------------------------
# Gate#3 R8-MAJOR-1 — cap admission is ATOMIC with the reservation. The R7 lock
# serializes each provider method, but the money path read the durable cap
# counts in ``load()`` and only LATER reserved as a SEPARATE critical section —
# so two CONCURRENT calls both consumed the STALE pre-reservation count (0) and
# BOTH submitted beyond ``max_orders_per_session == max_orders_per_day == 1``.
# The unresolved-scope freeze is NOT a cap substitute: a RESOLVED first order
# STOPS freezing while STAYING COUNTED. The fix binds the cap decision to the
# reservation atomically (a discriminated ``CAP_EXCEEDED_*`` outcome), so the
# second caller sees the first COUNTED slot and refuses at the cap.
#
# This drives TWO REAL facade calls through the runner sharing ONE recording
# write port (NOT a ``list.append`` write model — the R7 test's list.append is
# exactly why it missed this): a barrier so BOTH observe prior counts 0, then a
# gate holding the 2nd reservation until the 1st has settled RESOLVED (counted,
# no longer freezing). Exactly ONE SUBMITTED / one write-port call / one counted
# slot; the other DENIED ``order_cap_*``.
# ---------------------------------------------------------------------------


class _ConcurrentCapProbeProvider(InMemoryDurableSessionStateProvider):
    """Drives Codex's R8-MAJOR-1 schedule against the REAL facade money path.

    * ``load`` holds BOTH callers at a barrier AFTER reading the durable count, so both observe the
      SAME pre-reservation count (0) before EITHER reserves — the exact stale-count window.
    * ``reserve_or_freeze`` lets the FIRST arriving caller through and HOLDS the second until the first
      has ``settle``-d RESOLVED, so the second reserves atop a first order that is COUNTED but NO LONGER
      FREEZING (a RESOLVED row) — the atomic cap, not the scope freeze, must stop it. ``**kwargs``
      forwards whatever the facade threads in (the caps once the fix lands), so the probe drives the
      RED (pre-fix, no caps) and GREEN (post-fix, caps) money path unchanged.
    """

    def __init__(self) -> None:
        super().__init__()
        self._load_barrier = threading.Barrier(2, timeout=5.0)
        self._first_settled = threading.Event()
        self._arrival_lock = threading.Lock()
        self._arrivals = 0

    def load(self, *, session_id: str, now: datetime) -> DurableSessionState:
        state = super().load(session_id=session_id, now=now)
        # Both callers observe the pre-reservation count BEFORE either reserves (the stale-count window).
        with contextlib.suppress(threading.BrokenBarrierError):
            self._load_barrier.wait()
        return state

    def reserve_or_freeze(
        self, *, session_identity: str, now: datetime, attempt_id: str, **kwargs: object
    ) -> ScopedReservationOutcome:
        with self._arrival_lock:
            self._arrivals += 1
            is_second = self._arrivals == 2
        if is_second:
            # Hold the second caller until the FIRST has settled RESOLVED: it then reserves atop a
            # COUNTED-but-unfrozen (RESOLVED) first order, so ONLY an atomic cap can stop it.
            self._first_settled.wait(timeout=5.0)
        return super().reserve_or_freeze(
            session_identity=session_identity, now=now, attempt_id=attempt_id, **kwargs  # type: ignore[arg-type]
        )

    def settle(
        self, *, attempt_id: str, recon_state: ReconciliationState, venue_order_key: str | None = None
    ) -> None:
        super().settle(attempt_id=attempt_id, recon_state=recon_state, venue_order_key=venue_order_key)
        # The first caller's RESOLVED settle releases the held second caller.
        self._first_settled.set()


def test_concurrent_calls_at_cap_one_submits_the_other_is_cap_denied_atomically() -> None:
    """R8-MAJOR-1 (RED against the stale-count cap bypass): two CONCURRENT ``propose_mm_execution`` calls,
    SAME session, ``max_orders_per_session == max_orders_per_day == 1``, sharing ONE provider + ONE
    recording write port. A barrier makes BOTH observe prior counts 0; a gate holds the 2nd reservation
    until the 1st has settled RESOLVED (COUNTED but no longer freezing). The cap decision MUST be atomic
    with the reservation: EXACTLY ONE SUBMITTED / one write-port submit / one counted slot; the other
    DENIED ``order_cap_*``.

    RED today (cap decided from the stale ``load()`` count, not the atomic reservation): both read count 0
    and BOTH submit (``submit_calls == 2``, two counted rows). The RED is the stale-count cap bypass, not
    an import/typo — the barrier+gate reproduce Codex's exact schedule against the real facade."""
    binding = _mm_binding()
    write_port = _default_write_port(binding)  # SHARED money-moving surface — counts TOTAL submits
    provider = _ConcurrentCapProbeProvider()  # SHARED durable session state
    envelope = _mm_env(max_orders_per_session=1, max_orders_per_day=1)

    results: dict[str, MMExecutionToolResult] = {}
    errors: list[BaseException] = []

    def _worker(client_order_id: str) -> None:
        # ``fill_history_matches=True`` → the run's E4 reconcile RESOLVES (definite fill), so the first
        # order STOPS freezing the scope but STAYS COUNTED — the cap is the ONLY thing that can deny the
        # second. A FRESH adapter per call (the SHARED surface under test is the write port, not the
        # Mode-B-bypassed adapter).
        adapter = RecordingFakeAdapter(fill=True, fill_history_matches=True, open_orders=[])
        try:
            results[client_order_id] = asyncio.run(
                _drive_mode_b(
                    interlock=_full_interlock(),
                    arming=_mm_arming(binding, write_port=write_port),
                    binding=binding,
                    adapter=adapter,
                    interlock_store=InMemoryOperatorInterlockStore(),
                    envelope=envelope,
                    provider=provider,
                    client_order_id=client_order_id,
                )
            )
        except BaseException as exc:  # surface a worker crash to the main thread
            errors.append(exc)

    threads = [
        threading.Thread(target=_worker, args=("coid-1",)),
        threading.Thread(target=_worker, args=("coid-2",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30.0)
        assert not thread.is_alive(), "the concurrent cap schedule must not deadlock"

    assert not errors, f"a worker raised under the concurrent schedule: {errors}"
    statuses = sorted(result.execution_status for result in results.values())
    assert statuses == ["DENIED", "SUBMITTED"], (
        "exactly one concurrent call may reach the wire; the other must be cap-DENIED "
        f"(got {statuses} — two SUBMITTED means both consumed the stale pre-reservation count)"
    )
    assert write_port.submit_calls == 1, (
        "exactly ONE write-port submit under the concurrent schedule "
        f"(got {write_port.submit_calls} — two means the cap decision was not atomic with the reservation)"
    )
    assert provider.attempts("sess-mm-b") == 1, (
        "exactly ONE counted reservation slot — a second row means the cap admission interleaved"
    )
    denied = next(result for result in results.values() if result.execution_status != "SUBMITTED")
    assert (
        "order_cap_session" in denied.execution_reason_codes
        or "order_cap_day" in denied.execution_reason_codes
    ), f"the cap-denied call must carry an order_cap_* reason (got {denied.execution_reason_codes})"
