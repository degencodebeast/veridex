"""E6-T1 — ``run_dust_execution`` skeleton + submit-gate tests (SAF-007, AC-010/017, §6 group 6).

Trust boundaries proven here (the SAFETY CORE of the dust-execution runner):

* **Mode A places NO orders.** In ``dry_run`` mode a fully clean, gate-passing quote never reaches
  the injected recording-fake adapter's ``submit_order`` wire.
* **Submit gates refuse to submit (abstain, no order on the wire)** when ANY of: the quote age
  exceeds ``envelope.max_quote_age_s``; the injected source is gapped / disconnected / mid-resync
  (raises :class:`~veridex.dust_execution.runner.StaleVenueBook`); the market is event-suspended;
  a no-quote / boundary state; a negative-liquidity book. A **missing book side is ABSTAINED,
  never imputed / fabricated**.
* **No secret leaks** into the decision telemetry — every field is a JSON-primitive / closed-vocab
  reason, never a raw signer artifact, order, or venue handle.

Everything is INJECTED (adapter, signer, source, clocks, envelope, manifest, mode) — the lane's
async discipline: no wall-clock, no real sleep, Mode B stays UNARMED and offline (the adapter is
the established :class:`~veridex.venues.sx_bet.FakeVenueAdapter` recording-fake; the signer is the
Mode-A :class:`~veridex.dust_execution.signer.LocalFakeWalletControlPlane`).

The positive control (``test_mode_b_clear_quote_submits``) proves the wire actually fires when every
gate is clear, so the mutation check (delete the staleness gate → a stale quote submits → the stale
test fails) is meaningful and not vacuously green.
"""

from __future__ import annotations

from typing import cast

from veridex.dust_execution.clobv2_gate import Clobv2GateResult
from veridex.dust_execution.contracts import (
    DustExecutionSessionMeta,
    DustRunLabelEvent,
    ExecutionMode,
    OrderAckEvent,
    OrderCancelEvent,
    OrderStatusEvent,
    OrderSubmitAttempt,
    OrderSubmitIntent,
    RealFillReconciliation,
    SessionRiskSnapshot,
)
from veridex.dust_execution.emergency import DustSafetySession, SafetyController
from veridex.dust_execution.facade import MMExecutionToolRequest, MMIntentParams
from veridex.dust_execution.manifest import (
    StrategyAuthorizationDecision,
    StrategyExperimentManifest,
)
from veridex.dust_execution.noncrossing import LegKind, OwnOrderLeg
from veridex.dust_execution.privy_control_plane import (
    PrivyPreflightResult,
    ProvisioningResult,
)
from veridex.dust_execution.risk import RealizedFillRecord, RiskAccumulator
from veridex.dust_execution.runner import (
    ABSTAIN_REASONS,
    BookSide,
    DustExecutionResult,
    DustQuote,
    ModeBArming,
    OperatorInterlockProof,
    SessionOutcome,
    ShutdownDecision,
    ShutdownPolicy,
    StaleVenueBook,
    SubmitDecision,
    run_dust_execution,
)
from veridex.dust_execution.signer import (
    LocalFakeWalletControlPlane,
    SignedArtifact,
    SignerMode,
    SigningPayload,
)
from veridex.dust_execution.sizing import resolve_dust_size
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
from veridex.policy.circuit_breaker import CircuitBreaker, CircuitState
from veridex.policy.envelope import PolicyEnvelope
from veridex.runtime.evidence import compute_evidence_hash
from veridex.venues.base import Order, SubmitAck
from veridex.venues.sx_bet import FakeVenueAdapter

# The E1-T2 canonical lifecycle-stream ordering (session meta precedes this, unnumbered):
# risk snapshot -> intent -> attempt -> ack/reject -> status -> fill/reconciliation -> labels.
_EXPECTED_EVENT_TYPES: tuple[str, ...] = (
    "SessionRiskSnapshot",
    "OrderSubmitIntent",
    "OrderSubmitAttempt",
    "OrderAckEvent",
    "OrderStatusEvent",
    "RealFillReconciliation",
    "DustRunLabelEvent",
)

# --- Fixtures --------------------------------------------------------------------------------

_NOW_S = 1_700_000_000  # frozen source clock, integer SECONDS (matches max_quote_age_s units)
_TOKEN = "0xtokenYES"


def _manifest(**kw: object) -> StrategyExperimentManifest:
    base: dict[str, object] = {
        "strategy_id": "dust-maker-v0",
        "strategy_config_hash": "cfg" * 4,
        "evidence_class": "EXPERIMENTAL_DUST",
        "market": "0xcondition",
        "universe": (_TOKEN,),
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


def _env(**kw: object) -> PolicyEnvelope:
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


def _fresh_quote(**kw: object) -> DustQuote:
    """A fully clean, gate-passing quote captured exactly at ``_NOW_S`` (age 0)."""
    base: dict[str, object] = {
        "token_id": _TOKEN,
        "quote_ts_s": _NOW_S,
        "event_suspended": False,
        "no_quote": False,
        "bid": BookSide(price=0.49, size=10.0),
        "ask": BookSide(price=0.51, size=10.0),
    }
    base.update(kw)
    return DustQuote(**base)  # type: ignore[arg-type]


class _ScriptedSource:
    """A recording-free injected quote source: returns a scripted quote OR raises on read.

    ``raises`` models the gapped / disconnected / mid-resync source that refuses to serve a stale
    book (mirrors the live-recorder concept, defined IN-LANE per SEC-003).
    """

    def __init__(self, *, quote: DustQuote | None = None, raises: BaseException | None = None) -> None:
        self._quote = quote
        self._raises = raises
        self.reads: list[str] = []

    async def read_quote(self, token_id: str) -> DustQuote:
        self.reads.append(token_id)
        if self._raises is not None:
            raise self._raises
        assert self._quote is not None
        return self._quote


def _clock() -> int:
    return _NOW_S


async def _noop_sleep(_seconds: float) -> None:  # injected sleep seam — never a real wall-clock wait
    return None


# --- E6-T4 pinned sizing inputs + Mode-B arming fixtures -------------------------------------
#
# The pinned (never agent-supplied) sizing state: fixed_fraction * wallet_equity, clamped by the
# manifest/policy caps. With these defaults resolve_dust_size == min(1.0, 5.0, 100.0) == 1.0, so the
# real wire size matches the E6-T1 placeholder and the AC-003 equivalence tests are unaffected.
_WALLET_EQUITY = 100.0
_FIXED_FRACTION = 0.01


def _policy() -> PrivyWalletPolicy:
    """The pinned default-deny, typed-data-only wallet policy the arming gate verifies against."""
    return PrivyWalletPolicy(
        rules=(
            PolicyRule(ALLOWED_SIGN_METHOD, ORDER_PRIMARY_TYPE, "ALLOW"),
            PolicyRule(ALLOWED_SIGN_METHOD, CLOB_AUTH_PRIMARY_TYPE, "ALLOW"),
        ),
        default_action="DENY",
        owner_type="quorum",
    )


def _quorum() -> AuthorizationQuorum:
    return AuthorizationQuorum(quorum_ref="q-mode-b", authorization_key_refs=("k1", "k2"), threshold=2)


def _binding(*, wallet_address: str = "0xExecWalletModeB") -> ExecutionWalletBinding:
    """A valid, deterministic custody binding (equal-by-value across calls → identical hash)."""
    policy = _policy()
    quorum = _quorum()
    return ExecutionWalletBinding(
        provider="privy",
        wallet_ref="wallet-mode-b",
        wallet_address=wallet_address,
        chain_id=CHAIN_ID_POLYGON,
        venue="polymarket",
        privy_policy_content_hash=policy.content_hash(),
        authorization_quorum_ref=quorum.quorum_ref,
        authorization_quorum_content_hash=quorum.content_hash(),
        quorum_threshold=quorum.threshold,
    )


def _clobv2_ok() -> Clobv2GateResult:
    return Clobv2GateResult(
        supported_client=True,
        client_version="2",
        fixtures_match=True,
        cancel_verified=True,
        get_orders_verified=True,
        operator_smoke_ok=True,
    )


def _preflight_ok() -> PrivyPreflightResult:
    return PrivyPreflightResult(
        ok=True,
        detail="operator-confirmed",
        exercised_rules=(CLOB_AUTH_PRIMARY_TYPE, ORDER_PRIMARY_TYPE),
        recovery_verified=True,
    )


def _provisioning_ok() -> ProvisioningResult:
    return ProvisioningResult(ok=True, detail="operator-confirmed")


def _mode_b_manifest(binding: ExecutionWalletBinding | None = None, **kw: object) -> StrategyExperimentManifest:
    """A Mode-B manifest whose explicit ``execution_wallet_binding_hash`` pins the binding."""
    b = binding if binding is not None else _binding()
    fields: dict[str, object] = {"mode": "live_guarded", "execution_wallet_binding_hash": b.binding_hash()}
    fields.update(kw)
    return _manifest(**fields)


#: A RECORDED-satisfied human-operator interlock proof (Gate#3 MAJOR-1). Stands in for the facade's
#: minted proof so the runner-level E6-T4 arming positive controls still ARM offline.
_RECORDED_INTERLOCK_PROOF = OperatorInterlockProof(satisfied=True, recording_receipt="operator-interlock:test:recorded")


def _arming(
    binding: ExecutionWalletBinding | None = None,
    *,
    mode_a_passed: bool = True,
    clobv2: Clobv2GateResult | None = None,
    preflight: PrivyPreflightResult | None = None,
    provisioning: ProvisioningResult | None = None,
    live_policy: PrivyWalletPolicy | None = None,
    live_quorum: AuthorizationQuorum | None = None,
    operator_interlock: OperatorInterlockProof | None = _RECORDED_INTERLOCK_PROOF,
) -> ModeBArming:
    """A fully-passing Mode-B arming bundle, with per-precondition overrides for the failure tests.

    Includes the RECORDED-satisfied human-operator interlock proof by default (Gate#3 MAJOR-1) so the
    E6-T4 technical-arming positive controls still ARM; pass ``operator_interlock=None`` to exercise
    the facade-bypass case (a technical-only bundle must stay UNARMED)."""
    b = binding if binding is not None else _binding()
    return ModeBArming(
        mode_a_passed=mode_a_passed,
        clobv2_gate=clobv2 if clobv2 is not None else _clobv2_ok(),
        privy_preflight=preflight if preflight is not None else _preflight_ok(),
        provisioning=provisioning if provisioning is not None else _provisioning_ok(),
        binding=b,
        live_policy=live_policy if live_policy is not None else _policy(),
        live_quorum=live_quorum if live_quorum is not None else _quorum(),
        operator_interlock=operator_interlock,
    )


def _technical_only_arming(binding: ExecutionWalletBinding) -> ModeBArming:
    """A Mode-B arming bundle satisfying ONLY the six E6-T4 TECHNICAL conditions — carrying NO
    operator-interlock proof (Gate#3 MAJOR-1, REQ-005).

    A DIRECT runner call with this bundle must stay UNARMED: the six technical conditions alone must
    never arm real money without a RECORDED human-precondition proof, which only the facade can mint.
    """
    return ModeBArming(
        mode_a_passed=True,
        clobv2_gate=_clobv2_ok(),
        privy_preflight=_preflight_ok(),
        provisioning=_provisioning_ok(),
        binding=binding,
        live_policy=_policy(),
        live_quorum=_quorum(),
    )


#: Sentinel so ``_run_guarded`` can distinguish "arming not passed" (default → valid) from an
#: explicit ``arming=None`` (the "binding absent" fail-closed case).
_ARMING_DEFAULT: object = object()


async def _run(*, adapter: FakeVenueAdapter, source: _ScriptedSource, mode: ExecutionMode) -> DustExecutionResult:
    binding = _binding()
    manifest = _mode_b_manifest(binding) if mode == "live_guarded" else _manifest(mode=mode)
    return await run_dust_execution(
        adapter=adapter,
        signer=LocalFakeWalletControlPlane(),
        sources=source,
        now_fn=_clock,
        sleep_fn=_noop_sleep,
        envelope=_env(),
        manifest=manifest,
        mode=mode,
        wallet_equity_at_decision=_WALLET_EQUITY,
        fixed_fraction=_FIXED_FRACTION,
        arming=_arming(binding) if mode == "live_guarded" else None,
    )


# --- Mode A: places NO orders ----------------------------------------------------------------


async def test_mode_a_places_no_orders() -> None:
    """A fully clean quote in Mode A (``dry_run``) never reaches the submit wire (AC-017)."""
    adapter = FakeVenueAdapter(fill=True)
    source = _ScriptedSource(quote=_fresh_quote())

    result = await _run(adapter=adapter, source=source, mode="dry_run")

    assert adapter.submit_calls == 0, "Mode A must place NO orders"
    (decision,) = result.decisions
    assert decision.submitted is False
    assert decision.abstain_reason == "mode_a_no_orders"
    assert decision.venue_order_id is None
    assert result.submitted_count == 0


# --- Positive control: Mode B with a clean quote DOES submit ---------------------------------


async def test_mode_b_clear_quote_submits() -> None:
    """POSITIVE CONTROL: in Mode B a fully clean quote fires the submit wire exactly once.

    This is what makes the staleness MUTATION meaningful: if a gate is deleted, the gated quote
    would follow THIS same path onto the wire.
    """
    adapter = FakeVenueAdapter(fill=True)
    source = _ScriptedSource(quote=_fresh_quote())

    result = await _run(adapter=adapter, source=source, mode="live_guarded")

    assert adapter.submit_calls == 1, "a clean Mode B quote must reach the submit wire"
    (decision,) = result.decisions
    assert decision.submitted is True
    assert decision.abstain_reason is None
    assert decision.venue_order_id is not None
    assert result.submitted_count == 1


# --- Submit gates: each gated quote is ABSTAINED, never on the wire --------------------------


async def test_stale_by_age_quote_not_submitted() -> None:
    """MUTATION TARGET: a quote older than ``max_quote_age_s`` never reaches the wire."""
    adapter = FakeVenueAdapter(fill=True)
    # age = max_quote_age_s + 1 second → strictly stale.
    stale = _fresh_quote(quote_ts_s=_NOW_S - (_env().max_quote_age_s + 1))
    source = _ScriptedSource(quote=stale)

    result = await _run(adapter=adapter, source=source, mode="live_guarded")

    assert adapter.submit_calls == 0, "a stale-by-age quote must NOT reach the submit wire"
    (decision,) = result.decisions
    assert decision.submitted is False
    assert decision.abstain_reason == "stale_quote_age"


async def test_stale_source_not_submitted() -> None:
    """A gapped / disconnected / mid-resync source raises StaleVenueBook → abstain, no wire."""
    adapter = FakeVenueAdapter(fill=True)
    source = _ScriptedSource(raises=StaleVenueBook("venue book disconnected / mid-resync"))

    result = await _run(adapter=adapter, source=source, mode="live_guarded")

    assert adapter.submit_calls == 0, "a StaleVenueBook source must NOT reach the submit wire"
    (decision,) = result.decisions
    assert decision.submitted is False
    assert decision.abstain_reason == "stale_source"


async def test_event_suspended_not_submitted() -> None:
    """An event-suspended market never reaches the submit wire."""
    adapter = FakeVenueAdapter(fill=True)
    source = _ScriptedSource(quote=_fresh_quote(event_suspended=True))

    result = await _run(adapter=adapter, source=source, mode="live_guarded")

    assert adapter.submit_calls == 0, "an event-suspended market must NOT reach the submit wire"
    (decision,) = result.decisions
    assert decision.submitted is False
    assert decision.abstain_reason == "event_suspended"


async def test_no_quote_boundary_not_submitted() -> None:
    """An explicit no-quote / boundary state never reaches the submit wire."""
    adapter = FakeVenueAdapter(fill=True)
    source = _ScriptedSource(quote=_fresh_quote(no_quote=True, bid=None, ask=None))

    result = await _run(adapter=adapter, source=source, mode="live_guarded")

    assert adapter.submit_calls == 0, "a no-quote / boundary state must NOT reach the submit wire"
    (decision,) = result.decisions
    assert decision.submitted is False
    assert decision.abstain_reason == "no_quote"


async def test_negative_liquidity_not_submitted() -> None:
    """A negative-liquidity book never reaches the submit wire."""
    adapter = FakeVenueAdapter(fill=True)
    source = _ScriptedSource(quote=_fresh_quote(bid=BookSide(price=0.49, size=-1.0)))

    result = await _run(adapter=adapter, source=source, mode="live_guarded")

    assert adapter.submit_calls == 0, "a negative-liquidity book must NOT reach the submit wire"
    (decision,) = result.decisions
    assert decision.submitted is False
    assert decision.abstain_reason == "negative_liquidity"


async def test_missing_book_side_abstained_never_imputed() -> None:
    """A missing book side is ABSTAINED and NEVER fabricated/imputed onto the wire."""
    adapter = FakeVenueAdapter(fill=True)
    source = _ScriptedSource(quote=_fresh_quote(ask=None))  # bid present, ask MISSING

    result = await _run(adapter=adapter, source=source, mode="live_guarded")

    assert adapter.submit_calls == 0, "a missing book side must NOT be imputed onto the submit wire"
    (decision,) = result.decisions
    assert decision.submitted is False
    assert decision.abstain_reason == "missing_book_side"
    assert decision.venue_order_id is None


# --- No secret / raw-handle leak in the decision telemetry ----------------------------------


async def test_no_raw_handle_or_secret_in_result_telemetry() -> None:
    """Decision telemetry carries only JSON-primitives + closed-vocab reasons — no raw handles."""
    adapter = FakeVenueAdapter(fill=True)
    source = _ScriptedSource(quote=_fresh_quote())

    result = await _run(adapter=adapter, source=source, mode="live_guarded")

    (decision,) = result.decisions
    assert isinstance(decision, SubmitDecision)
    # Every field is a JSON-primitive; no Order / SignedArtifact / adapter handle leaks across.
    for value in vars(decision).values():
        assert value is None or isinstance(value, (str, bool))
        assert not isinstance(value, (Order, SignedArtifact, FakeVenueAdapter))
    # Abstain reasons are drawn from the single closed vocabulary (boolean/id-only telemetry).
    for d in result.decisions:
        assert d.abstain_reason is None or d.abstain_reason in ABSTAIN_REASONS
    assert "mode_a_no_orders" in ABSTAIN_REASONS and "stale_quote_age" in ABSTAIN_REASONS


# --- E6-T2: full lifecycle-event stream, identical contract shape in Mode A and Mode B --------


async def test_mode_a_and_mode_b_emit_identical_lifecycle_contract_shape() -> None:
    """AC-003: the SAME pinned clean quote yields IDENTICAL event TYPES + ORDERING in both modes.

    The stream is: session meta (unnumbered) -> risk snapshot -> intent -> attempt -> ack ->
    status -> fill/reconciliation -> labels. Only the recorded DATA differs (whether a real order
    moved) -- never the shape.
    """
    quote = _fresh_quote()

    result_a = await _run(
        adapter=FakeVenueAdapter(fill=True), source=_ScriptedSource(quote=quote), mode="dry_run"
    )
    result_b = await _run(
        adapter=FakeVenueAdapter(fill=True), source=_ScriptedSource(quote=quote), mode="live_guarded"
    )

    assert isinstance(result_a.session_meta, DustExecutionSessionMeta)
    assert isinstance(result_b.session_meta, DustExecutionSessionMeta)
    assert result_a.session_meta.mode == "dry_run"
    assert result_b.session_meta.mode == "live_guarded"

    types_a = tuple(type(e).__name__ for e in result_a.events)
    types_b = tuple(type(e).__name__ for e in result_b.events)
    assert types_a == _EXPECTED_EVENT_TYPES, f"Mode A stream shape drifted: {types_a}"
    assert types_b == _EXPECTED_EVENT_TYPES, f"Mode B stream shape drifted: {types_b}"
    assert types_a == types_b, "Mode A and Mode B must emit the IDENTICAL event-type stream (AC-003)"

    # The ONLY difference is whether a real order moved -- not the shape of the contracts.
    ack_a = next(e for e in result_a.events if isinstance(e, OrderAckEvent))
    ack_b = next(e for e in result_b.events if isinstance(e, OrderAckEvent))
    assert ack_a.venue_order_id is None, "Mode A must never fabricate a venue_order_id"
    assert ack_b.venue_order_id is not None, "Mode B's clean-gate ack must carry a real venue_order_id"
    assert ack_a.ack_status != ack_b.ack_status

    status_a = next(e for e in result_a.events if isinstance(e, OrderStatusEvent))
    status_b = next(e for e in result_b.events if isinstance(e, OrderStatusEvent))
    assert status_a.status == status_b.status  # same honest provisional status label, both modes

    assert quote.ask is not None
    intent_a = next(e for e in result_a.events if isinstance(e, OrderSubmitIntent))
    intent_b = next(e for e in result_b.events if isinstance(e, OrderSubmitIntent))
    assert intent_a.token_id == intent_b.token_id == _TOKEN
    assert intent_a.price == intent_b.price == quote.ask.price

    attempt_a = next(e for e in result_a.events if isinstance(e, OrderSubmitAttempt))
    attempt_b = next(e for e in result_b.events if isinstance(e, OrderSubmitAttempt))
    assert attempt_a.presubmit_record.integrity_commitment_hash
    assert attempt_b.presubmit_record.integrity_commitment_hash

    recon_a = next(e for e in result_a.events if isinstance(e, RealFillReconciliation))
    recon_b = next(e for e in result_b.events if isinstance(e, RealFillReconciliation))
    assert recon_a.reconciled_state == recon_b.reconciled_state

    labels_a = next(e for e in result_a.events if isinstance(e, DustRunLabelEvent))
    labels_b = next(e for e in result_b.events if isinstance(e, DustRunLabelEvent))
    assert labels_a.run_label == labels_b.run_label == "DUST_LIVE"
    assert labels_a.calibration_label == labels_b.calibration_label == "UNCALIBRATED"
    assert labels_a.edge_label == labels_b.edge_label == "NOT_PROVEN_EDGE"


async def test_sequence_no_unique_append_only_monotonic() -> None:
    """``sequence_no`` is append-only, unique, and gap-free across the whole event stream."""
    result = await _run(
        adapter=FakeVenueAdapter(fill=True), source=_ScriptedSource(quote=_fresh_quote()), mode="live_guarded"
    )

    seqs = [e.sequence_no for e in result.events]
    assert len(seqs) >= len(_EXPECTED_EVENT_TYPES)
    assert seqs == list(range(1, len(seqs) + 1)), "sequence_no must be append-only, unique, and gap-free"

    # The shared canonical evidence-hash helper independently rejects a duplicate sequence_no.
    compute_evidence_hash([e.model_dump() for e in result.events])


# --- E6-T3: runner delegates breaker/loss/kill to SafetyController + non-crossing + reconcile ---
#
# Anti-inert discipline (Codex-M3 / Fable-m2): the RED assertion is on the WIRE — the recording-fake
# adapter's ``cancel_all_orders`` was ACTUALLY awaited, subsequent submits are BLOCKED — NOT that the
# SafetyController is internally correct. A controller that is standalone-correct but that the runner
# never CALLS must make ``test_runner_delegates_breaker_loss_kill_to_safety_controller`` RED.


class RecordingFakeAdapter(FakeVenueAdapter):
    """The established :class:`FakeVenueAdapter` extended to RECORD the cancel-all WIRE call.

    Inherits the sealed four-method :class:`~veridex.venues.base.VenueAdapter` behaviour (submit /
    status / cancel / quote) unchanged and ADDS the two seams E6-T3 wires:

    * ``cancel_all_orders`` — the E2-T3 :class:`~veridex.dust_execution.emergency.CancelAllAdapter`
      sweep wire. ``cancel_all_calls`` increments ONLY when the coroutine is actually awaited, so a
      mere submit-block flag flip inside the controller can never move it (that is the load-bearing
      recording-fake rule: prove the venue sweep FIRED, not that a boolean was set).
    * ``get_fill_history`` — the E4 :class:`~veridex.venues.base.VenueReconciliationReads` surface
      the tri-state reconcile queries by ``venue_order_key``. When ``fill_history_matches`` it echoes
      a matching own trade so the reconcile resolves to ``RESOLVED``; otherwise it stays empty (the
      fail-closed AMBIGUOUS default), so a run that never submits can never fabricate a fill.
    * ``get_orders`` — the E3-T2 :class:`~veridex.venues.base.VenueReconciliationReads` open-order read
      surface the E6-T5 STARTUP SWEEP queries. Returns the injected ``open_orders`` (empty by default →
      no pre-existing exposure) and records ``get_orders_calls`` so a test can prove the runner ACTUALLY
      queried the isolated wallet's resting orders on arm (not merely that a sweep exists in the code).
    """

    def __init__(
        self,
        *,
        fill: bool = True,
        fill_history_matches: bool = False,
        open_orders: list[dict[str, object]] | None = None,
    ) -> None:
        super().__init__(fill=fill)
        self.cancel_all_calls = 0
        self._fill_history_matches = fill_history_matches
        #: The pre-existing OPEN orders ``get_orders`` reports for the isolated wallet (E6-T5 sweep).
        self._open_orders: list[dict[str, object]] = list(open_orders) if open_orders else []
        #: How many times the runner queried the open-order read on arm (the sweep-QUERIED proof).
        self.get_orders_calls = 0
        #: Every ``Order`` that actually reached the submit wire — the wire-size proof (E6-T4) reads
        #: the size off the ORDER the runner built, never the value the agent requested.
        self.submitted_orders: list[Order] = []

    async def submit_order(self, order: Order) -> SubmitAck:
        self.submitted_orders.append(order)
        return await super().submit_order(order)

    async def cancel_all_orders(self) -> int:
        self.cancel_all_calls += 1
        return 3

    async def get_orders(self, **kwargs: object) -> list[dict[str, object]]:
        self.get_orders_calls += 1
        return list(self._open_orders)

    async def get_fill_history(self, **kwargs: object) -> list[dict[str, object]]:
        key = kwargs.get("venue_order_key")
        if not self._fill_history_matches or not isinstance(key, str):
            return []
        # A matched own trade keyed on the OFFICIAL venue_order_key (never Veridex's private digest).
        return [{"taker_order_id": key, "size": 1.0}]


_SESSION_ID = "dust-maker-v0:live_guarded"


def _make_safety() -> tuple[SafetyController, DustSafetySession]:
    return SafetyController(clock_ms=lambda: _NOW_S * 1000), DustSafetySession(session_id=_SESSION_ID)


async def _run_guarded(
    *,
    adapter: FakeVenueAdapter,
    safety: SafetyController | None = None,
    session: DustSafetySession | None = None,
    risk: RiskAccumulator | None = None,
    breaker: CircuitBreaker | None = None,
    realized_fills: tuple[RealizedFillRecord, ...] = (),
    own_legs: tuple[OwnOrderLeg, ...] = (),
    envelope: PolicyEnvelope | None = None,
    source: _ScriptedSource | None = None,
    arming: object = _ARMING_DEFAULT,
    manifest: StrategyExperimentManifest | None = None,
    request: MMExecutionToolRequest | None = None,
    wallet_equity_at_decision: float = _WALLET_EQUITY,
    fixed_fraction: float = _FIXED_FRACTION,
    shutdown_policy: ShutdownPolicy = "leave_open",
) -> DustExecutionResult:
    binding = _binding()
    effective_manifest = manifest if manifest is not None else _mode_b_manifest(binding)
    effective_arming = _arming(binding) if arming is _ARMING_DEFAULT else arming
    return await run_dust_execution(
        adapter=adapter,
        signer=LocalFakeWalletControlPlane(),
        sources=source if source is not None else _ScriptedSource(quote=_fresh_quote()),
        now_fn=_clock,
        sleep_fn=_noop_sleep,
        envelope=envelope if envelope is not None else _env(),
        manifest=effective_manifest,
        mode="live_guarded",
        wallet_equity_at_decision=wallet_equity_at_decision,
        fixed_fraction=fixed_fraction,
        arming=effective_arming,  # type: ignore[arg-type]
        request=request,
        safety=safety,
        session=session,
        risk=risk,
        breaker=breaker,
        realized_fills=realized_fills,
        own_legs=own_legs,
        shutdown_policy=shutdown_policy,
    )


async def test_runner_delegates_breaker_loss_kill_to_safety_controller() -> None:
    """LOAD-BEARING anti-inert: each runner-reachable trigger reaches the SafetyController WIRE.

    Three sub-cases — (a) breaker-open, (b) realized-loss-cap breach via a REAL fill, (c) kill-switch
    engage. For EACH: the runner delegates to the E2-T3 :class:`SafetyController`, the recording-fake
    ``cancel_all_orders`` WIRE is ACTUALLY fired, subsequent submits are BLOCKED (no order reaches the
    submit wire), and the ack carries the honest trigger CAUSE, never an order id.
    """
    # (a) BREAKER-OPEN — an OPEN circuit breaker surfaced to the runner.
    adapter = RecordingFakeAdapter(fill=True)
    safety, session = _make_safety()
    breaker = CircuitBreaker(state=CircuitState.OPEN, opened_at=0.0, consecutive_failures=5)

    result = await _run_guarded(adapter=adapter, safety=safety, session=session, breaker=breaker)

    assert adapter.cancel_all_calls == 1, "breaker-open must fire the recording-fake cancel-all WIRE"
    assert session.submit_blocked is True
    assert safety.check_can_submit(session) is False
    assert adapter.submit_calls == 0, "a swept session must place NO further orders on the wire"
    (decision,) = result.decisions
    assert decision.submitted is False and decision.abstain_reason == "safety_blocked"
    assert session.last_cancel_all_ack is not None
    assert session.last_cancel_all_ack.trigger_cause == "breaker"
    assert "venue_order_id" not in session.last_cancel_all_ack.model_dump()

    # (b) REALIZED-LOSS-CAP BREACH — driven by a REAL fill through the RiskAccumulator.
    adapter = RecordingFakeAdapter(fill=True)
    safety, session = _make_safety()
    risk = RiskAccumulator(_SESSION_ID)
    loss_fill = RealizedFillRecord(
        realized_pnl=-2.5, fee=0.0, session_id=_SESSION_ID, fill_ts_ms=_NOW_S * 1000
    )
    env = _env(max_session_loss=2.0, max_daily_loss=4.0)

    result = await _run_guarded(
        adapter=adapter,
        safety=safety,
        session=session,
        risk=risk,
        realized_fills=(loss_fill,),
        envelope=env,
    )

    assert adapter.cancel_all_calls == 1, "a realized-loss breach must fire the cancel-all WIRE"
    assert session.submit_blocked is True
    assert adapter.submit_calls == 0
    (decision,) = result.decisions
    assert decision.submitted is False and decision.abstain_reason == "safety_blocked"
    assert session.last_cancel_all_ack is not None
    assert session.last_cancel_all_ack.trigger_cause == "loss_breach"
    assert "venue_order_id" not in session.last_cancel_all_ack.model_dump()

    # (c) KILL-SWITCH ENGAGE — envelope.kill_switch surfaced to the runner.
    adapter = RecordingFakeAdapter(fill=True)
    safety, session = _make_safety()
    env = _env(kill_switch=True)

    result = await _run_guarded(adapter=adapter, safety=safety, session=session, envelope=env)

    assert adapter.cancel_all_calls == 1, "kill-switch engage must fire the cancel-all WIRE"
    assert session.submit_blocked is True
    assert adapter.submit_calls == 0
    (decision,) = result.decisions
    assert decision.submitted is False and decision.abstain_reason == "safety_blocked"
    assert session.last_cancel_all_ack is not None
    assert session.last_cancel_all_ack.trigger_cause == "kill_switch"
    assert "venue_order_id" not in session.last_cancel_all_ack.model_dump()


async def test_crossing_order_refused_in_submit_path() -> None:
    """MUTATION TARGET (non-crossing): a proposed order that self-crosses an own leg NEVER submits.

    An own resting SELL (ask) at 0.50 on the SAME token, with the proposed BUY at the quote's ask
    (0.51), self-crosses (``highest_own_bid 0.51 >= lowest_own_ask 0.50``). The runner MUST route the
    proposed order through :func:`~veridex.dust_execution.noncrossing.check_non_crossing` BEFORE the
    submit wire and REFUSE it. Bypassing that call lets the crossing order reach ``submit_order``.
    """
    adapter = RecordingFakeAdapter(fill=True)
    safety, session = _make_safety()
    own = (OwnOrderLeg(token_id=_TOKEN, side="SELL", price=0.50, kind=LegKind.OPEN),)

    result = await _run_guarded(adapter=adapter, safety=safety, session=session, own_legs=own)

    assert adapter.submit_calls == 0, "a self-crossing proposed order must NOT reach the submit wire"
    (decision,) = result.decisions
    assert decision.submitted is False
    assert decision.abstain_reason == "self_cross"


async def test_non_crossing_clear_order_still_submits() -> None:
    """POSITIVE CONTROL for the non-crossing gate: a NON-crossing own leg still lets the order submit.

    Makes the crossing MUTATION meaningful: an own SELL at 0.80 (well above the proposed BUY 0.51) does
    NOT cross, so the clean order still reaches the wire exactly once.
    """
    adapter = RecordingFakeAdapter(fill=True)
    safety, session = _make_safety()
    own = (OwnOrderLeg(token_id=_TOKEN, side="SELL", price=0.80, kind=LegKind.OPEN),)

    result = await _run_guarded(adapter=adapter, safety=safety, session=session, own_legs=own)

    assert adapter.submit_calls == 1
    (decision,) = result.decisions
    assert decision.submitted is True and decision.abstain_reason is None


async def test_runner_wires_real_reconcile_resolved_status() -> None:
    """The E6-T2 PROVISIONAL status/reconcile seam is CLOSED: it reflects recording-fake venue truth.

    Mode B submits, then the runner routes the presubmit through the E4 tri-state reconcile
    (:func:`~veridex.dust_execution.reconcile.assess_uncertain_submit`) keyed on the ``venue_order_key``.
    The recording-fake echoes a matching own fill, so the status resolves to ``filled`` and the
    reconciliation to ``RESOLVED`` with the matched size — never the hardcoded ``unresolved`` /
    ``AMBIGUOUS`` placeholders.
    """
    adapter = RecordingFakeAdapter(fill=True, fill_history_matches=True)
    safety, session = _make_safety()

    result = await _run_guarded(adapter=adapter, safety=safety, session=session)

    assert adapter.submit_calls == 1
    status = next(e for e in result.events if isinstance(e, OrderStatusEvent))
    recon = next(e for e in result.events if isinstance(e, RealFillReconciliation))
    assert status.status == "filled", "reconcile against venue truth must resolve the honest status"
    assert status.filled_size == 1.0
    assert recon.reconciled_state == "RESOLVED"
    assert recon.reconciled_fill_size == 1.0


async def test_runner_risk_snapshot_threads_real_realized_loss() -> None:
    """The E6-T2 PROVISIONAL risk seam is CLOSED: the snapshot carries the RiskAccumulator's real loss.

    A REAL fill (fee-inclusive loss 1.25) that does NOT breach the (disabled) caps is folded through
    the accumulator; the ``SessionRiskSnapshot`` reports the real ``realized_loss_session/daily`` (1.25)
    instead of the hardcoded 0.0 placeholder, and the run still proceeds (no sweep).
    """
    adapter = RecordingFakeAdapter(fill=True)
    safety, session = _make_safety()
    risk = RiskAccumulator(_SESSION_ID)
    fill = RealizedFillRecord(
        realized_pnl=-1.0, fee=0.25, session_id=_SESSION_ID, fill_ts_ms=_NOW_S * 1000
    )

    result = await _run_guarded(
        adapter=adapter, safety=safety, session=session, risk=risk, realized_fills=(fill,)
    )

    snap = next(e for e in result.events if isinstance(e, SessionRiskSnapshot))
    assert snap.realized_loss_session == 1.25
    assert snap.realized_loss_daily == 1.25
    assert snap.breaker_open is False
    assert snap.kill_switch_engaged is False
    assert adapter.cancel_all_calls == 0, "a non-breaching fill must NOT fire the cancel-all wire"
    assert adapter.submit_calls == 1, "a non-breaching fill leaves the submit path open"


# --- E6-T4: mechanical size bound to the wire (Codex-M4 / Fable-m3) --------------------------
#
# THE load-bearing proof: the size that reaches the submit wire is ``resolve_dust_size(...)`` and
# NOTHING else. Two different agent ``confidence`` / requested ``size`` values on the SAME pinned
# state MUST produce the SAME wire size. Mutation: point the runner's submit at the agent-requested
# size instead of ``resolve_dust_size`` → this test fails.


def _mm_request(
    *,
    manifest: StrategyExperimentManifest,
    envelope: PolicyEnvelope,
    confidence: float | None,
    requested_size: float | None,
    manifest_hash: str | None = None,
) -> MMExecutionToolRequest:
    """A typed agent request declaring the admitted pins; ``confidence``/``size`` are untrusted.

    Uses the ``take`` (taker FOK) intent so the mechanical-size proof exercises the DISPATCHED taker
    submit wire (``adapter.submit_order``); ``make_quote`` now dispatches to a distinct resting-maker
    wire (see the per-intent dispatch tests), so it would no longer reach ``submit_order`` here.
    """
    return MMExecutionToolRequest(
        intent_kind="take",
        intent_params=MMIntentParams(
            token_id=_TOKEN, side="BUY", price=0.51, size=requested_size, tif="FOK"
        ),
        strategy_id=manifest.strategy_id,
        strategy_config_hash=manifest.strategy_config_hash,
        policy_hash=envelope.policy_hash(),
        session_id="agent-session",
        manifest_hash=manifest_hash if manifest_hash is not None else manifest.manifest_hash(),
        evidence_class=manifest.evidence_class,
        mode="live_guarded",
        reason="agent rationale (untrusted)",
        confidence=confidence,
    )


async def test_runner_wire_size_is_mechanical_regardless_of_agent_input() -> None:
    """The wire size == ``resolve_dust_size(...)`` for BOTH agent inputs — never the requested size.

    Same pinned state (wallet_equity, fixed_fraction, manifest); two DIFFERENT agent
    ``confidence``/requested-``size`` values. The size that reaches ``RecordingFakeAdapter`` is
    IDENTICAL in both and equals ``resolve_dust_size(...)``. A confidence/size term can never RAISE
    or move the executable size (GUD-001).
    """
    binding = _binding()
    manifest = _mode_b_manifest(binding)
    env = _env()
    # A clamping case so the proof is non-trivial: 0.5 * 100 == 50, clamped by max_notional 5.0.
    fixed_fraction, wallet_equity = 0.5, 100.0
    expected = resolve_dust_size(
        fixed_fraction=fixed_fraction,
        wallet_equity_at_decision=wallet_equity,
        max_notional=manifest.max_notional,
        max_per_order=env.max_stake,
    )
    assert expected == 5.0  # min(50.0, 5.0, 100.0)

    adapter_hi = RecordingFakeAdapter(fill=True)
    await _run_guarded(
        adapter=adapter_hi,
        manifest=manifest,
        envelope=env,
        arming=_arming(binding),
        request=_mm_request(manifest=manifest, envelope=env, confidence=0.99, requested_size=999.0),
        wallet_equity_at_decision=wallet_equity,
        fixed_fraction=fixed_fraction,
    )
    adapter_lo = RecordingFakeAdapter(fill=True)
    await _run_guarded(
        adapter=adapter_lo,
        manifest=manifest,
        envelope=env,
        arming=_arming(binding),
        request=_mm_request(manifest=manifest, envelope=env, confidence=0.01, requested_size=0.001),
        wallet_equity_at_decision=wallet_equity,
        fixed_fraction=fixed_fraction,
    )

    assert adapter_hi.submitted_orders and adapter_lo.submitted_orders
    size_hi = adapter_hi.submitted_orders[-1].size
    size_lo = adapter_lo.submitted_orders[-1].size
    assert size_hi == size_lo == expected, "the wire size must be resolve_dust_size(...), identical"
    assert size_hi not in (999.0, 0.001), "the agent-requested size must NEVER reach the wire"


class _RecordingSigner:
    """The Mode-A signer, wrapped to CAPTURE each :class:`SigningPayload` it signs.

    Delegates the actual (offline, deterministic) signing to :class:`LocalFakeWalletControlPlane`, but
    records every payload so a test can assert the SIGNED order carries the runner's injected
    ``tick_size`` — the single-source proof for the non-crossing gate ↔ signed-payload tick.
    """

    mode: SignerMode = "FAKE_LOCAL"

    def __init__(self) -> None:
        self._inner = LocalFakeWalletControlPlane()
        self.signed_payloads: list[SigningPayload] = []

    async def sign_order(self, payload: SigningPayload) -> SignedArtifact:
        self.signed_payloads.append(payload)
        return await self._inner.sign_order(payload)


async def test_signed_payload_tick_is_single_sourced_from_runner_tick_size() -> None:
    """MINOR-1: the SIGNED payload's ``tick_size`` is the runner's ``tick_size`` param, ONE source.

    The non-crossing gate and the signed order payload must read the tick from a SINGLE source that
    cannot drift. Drives the Mode-B submit path with a NON-default ``tick_size=0.005``; the recording
    signer captures the exact payload the runner signs. Its ``tick_size`` MUST be ``"0.005"`` (the
    injected tick, losslessly stringified) — NOT a hardcoded ``"0.01"`` literal.

    MUTATION: re-hardcode ``SigningPayload(tick_size="0.01")`` → the signed tick diverges from the
    injected non-crossing tick and this test fails; a single change to the runner's tick source now
    moves BOTH the non-crossing gate AND the signed payload.
    """
    binding = _binding()
    adapter = RecordingFakeAdapter(fill=True)
    signer = _RecordingSigner()

    result = await run_dust_execution(
        adapter=adapter,
        signer=signer,
        sources=_ScriptedSource(quote=_fresh_quote()),
        now_fn=_clock,
        sleep_fn=_noop_sleep,
        envelope=_env(),
        manifest=_mode_b_manifest(binding),
        mode="live_guarded",
        wallet_equity_at_decision=_WALLET_EQUITY,
        fixed_fraction=_FIXED_FRACTION,
        arming=_arming(binding),
        tick_size=0.005,  # NON-default: exposes a hardcoded "0.01" in the signed payload
    )

    (decision,) = result.decisions
    assert decision.submitted is True, "positive control: the clean Mode-B order must sign+submit"
    assert len(signer.signed_payloads) == 1, "the runner must sign exactly one order for one token"
    assert signer.signed_payloads[-1].tick_size == "0.005", (
        "the SIGNED payload tick must be the runner's injected tick (single source), not '0.01'"
    )


# --- E6-T4: Mode A -> Mode B hard gate + fail-closed arming ----------------------------------


async def test_mode_b_hard_gate_blocks_until_mode_a_passes() -> None:
    """HARD GATE: Mode B cannot arm until Mode A (dry-run) has passed, even if all else is valid.

    Mutation: allow Mode B to arm without Mode A passing → this test fails (it would submit).
    """
    binding = _binding()
    adapter = RecordingFakeAdapter(fill=True)

    result = await _run_guarded(
        adapter=adapter,
        manifest=_mode_b_manifest(binding),
        arming=_arming(binding, mode_a_passed=False),  # every OTHER precondition is valid
    )

    assert adapter.submit_calls == 0, "Mode B must stay blocked until Mode A passes"
    (decision,) = result.decisions
    assert decision.submitted is False
    assert decision.abstain_reason == "mode_b_not_armed"


async def test_mode_b_blocked_when_any_arming_precondition_fails() -> None:
    """Mode B stays BLOCKED if ANY arming precondition fails, the binding is absent, or the policy
    content hash mismatches. Each case is driven OFFLINE via a passing/failing fixture — no live call.
    """
    binding = _binding()
    manifest = _mode_b_manifest(binding)

    # A policy whose CONTENT differs from the pinned binding (arm_mode_b content-hash mismatch).
    weakened_policy = PrivyWalletPolicy(
        rules=(
            PolicyRule(ALLOWED_SIGN_METHOD, ORDER_PRIMARY_TYPE, "ALLOW"),
            PolicyRule(ALLOWED_SIGN_METHOD, CLOB_AUTH_PRIMARY_TYPE, "ALLOW"),
        ),
        default_action="ALLOW",  # weakened default → different content_hash than the pinned binding
        owner_type="quorum",
    )
    # A binding pinned to a DIFFERENT wallet → its hash ≠ the manifest's pinned field.
    rerouted_binding = _binding(wallet_address="0xAttackerWallet")

    cases: dict[str, object | None] = {
        "binding_absent": None,
        "mode_a_not_passed": _arming(binding, mode_a_passed=False),
        "clobv2_gate_pending": _arming(
            binding,
            clobv2=Clobv2GateResult(
                supported_client=True,
                client_version="2",
                fixtures_match=True,
                cancel_verified=True,
                get_orders_verified=True,
                operator_smoke_ok=None,  # operator smoke not run → mode_b_admitted is False
            ),
        ),
        "privy_preflight_pending": _arming(
            binding, preflight=PrivyPreflightResult(ok=None, detail="operator-pending")
        ),
        "provisioning_pending": _arming(
            binding, provisioning=ProvisioningResult(ok=None, detail="operator-pending")
        ),
        "policy_content_hash_mismatch": _arming(binding, live_policy=weakened_policy),
        "binding_hash_mismatch": _arming(rerouted_binding),
    }

    for name, arming in cases.items():
        adapter = RecordingFakeAdapter(fill=True)
        result = await _run_guarded(adapter=adapter, manifest=manifest, arming=arming)
        assert adapter.submit_calls == 0, f"{name}: Mode B must stay blocked (no order on the wire)"
        (decision,) = result.decisions
        assert decision.submitted is False, name
        assert decision.abstain_reason == "mode_b_not_armed", name


async def test_mode_b_arms_and_submits_when_all_preconditions_pass() -> None:
    """POSITIVE CONTROL: with Mode A passed AND every arming precondition valid, Mode B submits once.

    Makes the hard-gate / blocked-on-precondition MUTATIONS meaningful (not vacuously green).
    """
    adapter = RecordingFakeAdapter(fill=True)

    result = await _run_guarded(adapter=adapter)  # default: valid arming + pinned Mode-B manifest

    assert adapter.submit_calls == 1, "a fully-armed Mode B must reach the submit wire"
    (decision,) = result.decisions
    assert decision.submitted is True and decision.abstain_reason is None


async def test_direct_runner_call_with_technical_only_bundle_stays_unarmed() -> None:
    """Gate#3 MAJOR-1 (REQ-005): the operator-interlock is UNBYPASSABLE via the public runner.

    A DIRECT ``run_dust_execution`` with a bundle satisfying only the SIX technical arming
    conditions — but carrying NO recorded operator-interlock proof — must NOT arm/submit. Only the
    facade can mint that proof (after evaluating AND durably recording the five human preconditions);
    the runner fails closed on its absence, so the interlock cannot be side-stepped by calling the
    runner directly.

    RED before the fix: the runner accepts the technical-only bundle and submits real money
    (``submit_calls == 1``). GREEN after: no bound proof → ``operator_interlock_unproven`` → no wire.
    """
    adapter = RecordingFakeAdapter(fill=True)

    result = await _run_guarded(adapter=adapter, arming=_technical_only_arming(_binding()))

    assert adapter.submit_calls == 0, (
        "a technical-only bundle (no recorded interlock proof) must NOT arm/submit — REQ-005"
    )
    (decision,) = result.decisions
    assert decision.submitted is False
    assert decision.abstain_reason == "operator_interlock_unproven"


# --- E6-T4: manifest authorization (missing/mismatched fails closed; admit-but-cap) ----------


async def test_mismatched_manifest_hash_on_request_fails_closed() -> None:
    """A request whose DECLARED manifest hash does not match the admitted pin fails closed."""
    binding = _binding()
    manifest = _mode_b_manifest(binding)
    env = _env()
    bad_request = _mm_request(
        manifest=manifest, envelope=env, confidence=0.5, requested_size=1.0, manifest_hash="00" * 32
    )
    adapter = RecordingFakeAdapter(fill=True)

    result = await _run_guarded(
        adapter=adapter, manifest=manifest, envelope=env, arming=_arming(binding), request=bad_request
    )

    assert adapter.submit_calls == 0, "a mismatched declared manifest hash must NOT reach the wire"
    (decision,) = result.decisions
    assert decision.submitted is False
    assert decision.abstain_reason == "manifest_hash_mismatch"


async def test_experimental_dust_admits_without_profitability_but_trips_loss_cap() -> None:
    """An EXPERIMENTAL_DUST manifest admits WITHOUT a profitability flag yet still trips the loss cap.

    (a) fresh session → ``ALLOW`` (no profitability flag required) → submits.
    (b) accumulated loss reaching ``max_session_loss`` → admission ``DENY`` → no order on the wire.
    """
    binding = _binding()
    manifest = _mode_b_manifest(binding)  # evidence_class == EXPERIMENTAL_DUST, max_session_loss 2.0
    assert manifest.evidence_class == "EXPERIMENTAL_DUST"

    # (a) admitted at the strictest caps, no profitability flag anywhere.
    adapter_ok = RecordingFakeAdapter(fill=True)
    result_ok = await _run_guarded(adapter=adapter_ok, manifest=manifest)
    assert result_ok.admission.verdict == "ALLOW"
    assert result_ok.admission.reason_codes == ()
    assert adapter_ok.submit_calls == 1

    # (b) the loss cap is still enforced — a session at the cap is DENIED admission.
    adapter_capped = RecordingFakeAdapter(fill=True)
    breached_risk = RiskAccumulator.seeded(
        session_id="dust-maker-v0:live_guarded", net_session=-2.0, net_day=-2.0, current_day=None
    )
    result_capped = await _run_guarded(adapter=adapter_capped, manifest=manifest, risk=breached_risk)
    assert result_capped.admission.verdict == "DENY"
    assert "session_loss_cap" in result_capped.admission.reason_codes
    assert adapter_capped.submit_calls == 0
    (decision,) = result_capped.decisions
    assert decision.submitted is False
    assert decision.abstain_reason == "admission_denied"


async def test_identical_request_and_hashes_yield_identical_admission_across_modes() -> None:
    """Identical request + hashes → IDENTICAL admission verdict in dry-run and live-guarded (AC-021)."""
    binding = _binding()
    manifest = _mode_b_manifest(binding)
    env = _env()
    request = _mm_request(manifest=manifest, envelope=env, confidence=0.7, requested_size=2.0)

    async def _admit(mode: ExecutionMode, arming: ModeBArming | None) -> StrategyAuthorizationDecision:
        # A shared session identity so the admission is a pure function of manifest+policy+session
        # (mode-independent); the session_id is otherwise mode-tagged by the runner.
        result = await run_dust_execution(
            adapter=RecordingFakeAdapter(fill=True),
            signer=LocalFakeWalletControlPlane(),
            sources=_ScriptedSource(quote=_fresh_quote()),
            now_fn=_clock,
            sleep_fn=_noop_sleep,
            envelope=env,
            manifest=manifest,
            mode=mode,
            wallet_equity_at_decision=_WALLET_EQUITY,
            fixed_fraction=_FIXED_FRACTION,
            arming=arming,
            request=request,
            session=DustSafetySession(session_id="admission-parity"),
        )
        return result.admission

    admission_dry = await _admit("dry_run", None)
    admission_live = await _admit("live_guarded", _arming(binding))

    assert admission_dry == admission_live, "admission must be identical across runner modes (AC-021)"
    assert admission_dry.verdict == "ALLOW"


# --- E6-T5: startup open-order sweep before arming (SAF-005) ---------------------------------
#
# THE safety property: on arm, the runner MUST query ``get_orders`` for the isolated wallet and
# reconcile/cancel any pre-existing open orders BEFORE it submits anything — it cannot blindly submit
# into pre-existing exposure. The load-bearing anti-inert proof is on the WIRE: ``get_orders`` was
# ACTUALLY queried AND the recording-fake ``cancel_all_orders`` sweep WIRE fired, and NO order reached
# the submit wire atop the pre-existing orders. Mutation: skip the startup sweep → the clean quote
# submits atop the pre-existing open orders → this test fails.

_PREEXISTING_OPEN_ORDERS: list[dict[str, object]] = [
    {"order_id": "0xpre1", "asset_id": _TOKEN, "size": 5.0},
    {"order_id": "0xpre2", "asset_id": _TOKEN, "size": 3.0},
]


async def test_startup_sweep_cancels_preexisting_orders_before_any_submit() -> None:
    """SAF-005: on arm, pre-existing open orders are swept BEFORE any submit — never submitted atop.

    A fully-armed Mode B run whose isolated wallet already carries resting orders (``get_orders``
    reports two) MUST query ``get_orders``, fire the cancel-all WIRE to sweep them, BLOCK submits, and
    place NO order atop the pre-existing exposure. Mutation: skip the startup sweep → the clean quote
    submits atop the pre-existing orders → this test fails (``submit_calls == 1``, no cancel wire).
    """
    adapter = RecordingFakeAdapter(fill=True, open_orders=_PREEXISTING_OPEN_ORDERS)
    safety, session = _make_safety()

    result = await _run_guarded(adapter=adapter, safety=safety, session=session)

    assert adapter.get_orders_calls >= 1, "the runner must query get_orders for pre-existing exposure on arm"
    assert adapter.cancel_all_calls == 1, "pre-existing open orders must be swept via the cancel-all WIRE"
    assert session.submit_blocked is True, "a startup sweep of pre-existing orders must block submits (fail-closed)"
    assert adapter.submit_calls == 0, "no order may be submitted atop pre-existing exposure (SAF-005)"
    (decision,) = result.decisions
    assert decision.submitted is False
    assert decision.abstain_reason == "safety_blocked"
    assert session.last_cancel_all_ack is not None
    assert session.last_cancel_all_ack.trigger_cause == "manual"
    assert "venue_order_id" not in session.last_cancel_all_ack.model_dump()


async def test_startup_sweep_with_no_preexisting_orders_still_submits() -> None:
    """POSITIVE CONTROL: an armed Mode B with an EMPTY open-order book queries get_orders, sweeps
    nothing, and submits once — so the startup-sweep MUTATION is not vacuously green.
    """
    adapter = RecordingFakeAdapter(fill=True, open_orders=[])
    safety, session = _make_safety()

    result = await _run_guarded(adapter=adapter, safety=safety, session=session)

    assert adapter.get_orders_calls >= 1, "the runner must query get_orders on arm even when the book is empty"
    assert adapter.cancel_all_calls == 0, "an empty open-order book must fire NO cancel sweep"
    assert adapter.submit_calls == 1, "a clean startup (no pre-existing orders) must still reach the submit wire"
    (decision,) = result.decisions
    assert decision.submitted is True and decision.abstain_reason is None


async def test_startup_sweep_contract_exercised_in_mode_a_no_wire_touched() -> None:
    """Mode A (dry-run) still EXERCISES the sweep read (queries get_orders) but touches NO wire.

    Dry-run places no orders, so there is no submit to protect from pre-existing exposure; the runner
    queries ``get_orders`` to exercise the SAF-005 contract but fires NO cancel wire and submits
    nothing (the AC-017 ``submit_calls == 0`` invariant is preserved).
    """
    adapter = RecordingFakeAdapter(fill=True, open_orders=_PREEXISTING_OPEN_ORDERS)
    source = _ScriptedSource(quote=_fresh_quote())

    result = await run_dust_execution(
        adapter=adapter,
        signer=LocalFakeWalletControlPlane(),
        sources=source,
        now_fn=_clock,
        sleep_fn=_noop_sleep,
        envelope=_env(),
        manifest=_manifest(mode="dry_run"),
        mode="dry_run",
        wallet_equity_at_decision=_WALLET_EQUITY,
        fixed_fraction=_FIXED_FRACTION,
        arming=None,
    )

    assert adapter.get_orders_calls >= 1, "Mode A must still exercise the get_orders sweep read (contract wired)"
    assert adapter.cancel_all_calls == 0, "Mode A (dry-run) must fire NO cancel wire"
    assert adapter.submit_calls == 0, "Mode A places no orders (AC-017)"
    (decision,) = result.decisions
    assert decision.submitted is False and decision.abstain_reason == "mode_a_no_orders"


# --- Gate#3 MAJOR-2: an UNKNOWN startup open-order read must BLOCK submit (never fail-open to 0) ---
#
# THE safety property (SAF-005): in ARMED Mode B the startup open-order truth must be OBTAINED and
# TRUSTWORTHY before any submit. A SUCCESSFUL read returning zero orders permits submit (the E6-T5
# positive control above); a SUCCESSFUL read returning >=1 order sweeps + blocks (E6-T5, preserved);
# but an ABSENT / RAISING / MALFORMED / INCOMPLETE-PAGINATED read is UNKNOWN exposure -- it is NOT
# proof of zero open orders, so it must fail CLOSED: conservatively fire the cancel-all WIRE and
# BLOCK submits so nothing lands atop possibly-existing exposure. The bug this closes degraded a
# FAILED read to "0 open orders" and PERMITTED submit (fail-OPEN). Mutation: revert the fix so an
# unknown read degrades to 0-and-permit -> these four tests fail (submit_calls == 1, no cancel wire).


class _NoGetOrdersAdapter(FakeVenueAdapter):
    """A misconfigured ARMED adapter that CANNOT report open orders (no ``get_orders``) but CAN sweep.

    ``getattr(adapter, "get_orders", None)`` is ``None`` here -- the startup open-order truth is
    UNKNOWN. It still exposes the E2-T3 ``cancel_all_orders`` sweep wire so the conservative
    fail-closed block-via-sweep can actually fire (records ``cancel_all_calls``).
    """

    def __init__(self, *, fill: bool = True) -> None:
        super().__init__(fill=fill)
        self.cancel_all_calls = 0

    async def cancel_all_orders(self) -> int:
        self.cancel_all_calls += 1
        return 3


class _RaisingGetOrdersAdapter(RecordingFakeAdapter):
    """The Codex repro: ``get_orders`` RAISES (venue open-order read timed out / unavailable)."""

    async def get_orders(self, **kwargs: object) -> list[dict[str, object]]:
        self.get_orders_calls += 1
        raise RuntimeError("startup read unavailable")


class _MalformedGetOrdersAdapter(RecordingFakeAdapter):
    """``get_orders`` returns a MALFORMED / INCOMPLETE-PAGINATED shape (not the flattened bare list)."""

    def __init__(self, *, response: object, fill: bool = True) -> None:
        super().__init__(fill=fill)
        self._response = response

    async def get_orders(self, **kwargs: object) -> list[dict[str, object]]:
        self.get_orders_calls += 1
        # A recording fake deliberately VIOLATING the ``list[dict]`` read contract at runtime (a
        # misbehaving venue): the cast keeps the static override signature honest.
        return cast("list[dict[str, object]]", self._response)


async def test_startup_sweep_absent_read_surface_blocks_submit() -> None:
    """Gate#3 MAJOR-2: an armed adapter with NO ``get_orders`` surface has UNKNOWN exposure -> BLOCK.

    An absent read surface is NOT proof of zero open orders. Fail-closed: the runner conservatively
    fires the cancel-all WIRE and blocks submits so no order lands atop possibly-existing exposure.
    Today the absent read degrades to zero and the clean quote submits (fail-open) -> this fails RED.
    """
    adapter = _NoGetOrdersAdapter(fill=True)
    safety, session = _make_safety()

    result = await _run_guarded(adapter=adapter, safety=safety, session=session)

    assert adapter.cancel_all_calls == 1, "an UNKNOWN (absent) open-order read must fire the cancel-all WIRE"
    assert session.submit_blocked is True, "an UNKNOWN open-order read must block submits (fail-closed)"
    assert adapter.submit_calls == 0, "no order may be submitted atop UNKNOWN exposure (SAF-005 / MAJOR-2)"
    (decision,) = result.decisions
    assert decision.submitted is False and decision.abstain_reason == "safety_blocked"


async def test_startup_sweep_raising_read_blocks_submit() -> None:
    """Gate#3 MAJOR-2 (the Codex repro): a ``get_orders`` that RAISES is UNKNOWN exposure -> BLOCK.

    A venue open-order read that times out / raises is NOT proof of no exposure. Today the runner
    degrades the raised read to zero and submits one order (fail-open); it must instead fail closed.
    """
    adapter = _RaisingGetOrdersAdapter(fill=True)
    safety, session = _make_safety()

    result = await _run_guarded(adapter=adapter, safety=safety, session=session)

    assert adapter.get_orders_calls >= 1, "the runner must ATTEMPT the open-order read on arm"
    assert adapter.cancel_all_calls == 1, "a RAISING open-order read must fire the cancel-all WIRE"
    assert session.submit_blocked is True
    assert adapter.submit_calls == 0, "a raised startup read must NOT fail-open to a submit"
    (decision,) = result.decisions
    assert decision.submitted is False and decision.abstain_reason == "safety_blocked"


async def test_startup_sweep_malformed_read_blocks_submit() -> None:
    """Gate#3 MAJOR-2: a ``get_orders`` returning a MALFORMED (non-list ``None``) shape -> BLOCK.

    The read contract is a flattened bare list of open-order records; a ``None`` / non-list response
    is not trustworthy proof of zero exposure. Today ``len(None or [])`` degrades it to zero and the
    clean quote submits (fail-open) -> this fails RED; the fix must fail closed (never a submit).
    """
    adapter = _MalformedGetOrdersAdapter(response=None, fill=True)
    safety, session = _make_safety()

    result = await _run_guarded(adapter=adapter, safety=safety, session=session)

    assert adapter.get_orders_calls >= 1
    assert adapter.cancel_all_calls == 1, "a MALFORMED open-order read must fire the cancel-all WIRE"
    assert session.submit_blocked is True
    assert adapter.submit_calls == 0, "a malformed startup read must NOT fail-open to a submit"
    (decision,) = result.decisions
    assert decision.submitted is False and decision.abstain_reason == "safety_blocked"


async def test_startup_sweep_incomplete_pagination_blocks_submit() -> None:
    """Gate#3 MAJOR-2: a ``get_orders`` returning a PARTIAL page (pagination not completed) -> BLOCK.

    The reconciliation read surface (``VenueReconciliationReads.get_orders``) returns the FLATTENED
    bare list of open orders -- the surface itself has NO pagination; the adapter is responsible for
    iterating the §5 cursor pages (``next_cursor``: ``MA==`` first, ``LTE=`` terminal) and flattening
    them. A response STILL shaped as a partial page ENVELOPE (a non-list dict carrying a NON-terminal
    ``next_cursor``) means pagination LEAKED / did not complete: more open orders exist that were not
    fetched, so the open-order truth is INCOMPLETE / UNKNOWN. The fix routes it through the SAME
    non-list UNKNOWN guard as the malformed case and blocks submit. (This truncated shape is
    truthy, so it happens to block under today's ``len``-counts-keys code too; the load-bearing
    fail-OPEN proof lives in the absent / raises / malformed(None) tests, which permit submit today.)
    """
    partial_page = {
        "data": [{"order_id": "0xpre1", "asset_id": _TOKEN}],
        "count": 1,
        "next_cursor": "MTA=",  # NON-terminal (terminal is "LTE=") -> more pages not fetched
    }
    adapter = _MalformedGetOrdersAdapter(response=partial_page, fill=True)
    safety, session = _make_safety()

    result = await _run_guarded(adapter=adapter, safety=safety, session=session)

    assert adapter.get_orders_calls >= 1
    assert adapter.cancel_all_calls == 1, "an INCOMPLETE-paginated open-order read must fire the cancel-all WIRE"
    assert session.submit_blocked is True
    assert adapter.submit_calls == 0, "an incomplete-paginated startup read must NOT fail-open to a submit"
    (decision,) = result.decisions
    assert decision.submitted is False and decision.abstain_reason == "safety_blocked"


async def test_startup_sweep_unknown_read_in_mode_a_records_no_wire_no_crash() -> None:
    """Gate#3 MAJOR-2: Mode A (dry-run) with a RAISING open-order read records the unavailable read
    WITHOUT any money I/O and WITHOUT crashing.

    Dry-run places no orders, so there is nothing to protect: the read is exercised (the SAF-005
    contract is wired) but NO cancel wire and NO submit wire is touched (AC-017).
    """
    adapter = _RaisingGetOrdersAdapter(fill=True)
    source = _ScriptedSource(quote=_fresh_quote())

    result = await run_dust_execution(
        adapter=adapter,
        signer=LocalFakeWalletControlPlane(),
        sources=source,
        now_fn=_clock,
        sleep_fn=_noop_sleep,
        envelope=_env(),
        manifest=_manifest(mode="dry_run"),
        mode="dry_run",
        wallet_equity_at_decision=_WALLET_EQUITY,
        fixed_fraction=_FIXED_FRACTION,
        arming=None,
    )

    assert adapter.get_orders_calls >= 1, "Mode A must still EXERCISE the open-order read (contract wired)"
    assert adapter.cancel_all_calls == 0, "Mode A (dry-run) must fire NO cancel wire even on an unknown read"
    assert adapter.submit_calls == 0, "Mode A places no orders (AC-017)"
    (decision,) = result.decisions
    assert decision.submitted is False and decision.abstain_reason == "mode_a_no_orders"


# --- E6-T6: shutdown cancel-all or explicit leave-open decision (SAF-006, AC-009) -------------
#
# THE safety property: at shutdown the outcome must be one of exactly two EXPLICIT states —
# cancel-all fired (wire cancel + block) OR an explicit recorded leave-open decision. A
# silent abandon (the run ends with resting orders and NEITHER a fired cancel-all NOR a recorded
# decision) is the failure these tests expose. Mutation: make shutdown a silent no-op (neither cancel
# nor record) -> ``result.shutdown_decision`` would not exist / the cancel-all WIRE would not fire ->
# these tests fail.
#
# Gate#3 MINOR-1 (honesty): the no-cancel branch is named ``"leave_open"`` (never ``"leave_flat"`` —
# that branch leaves resting orders OPEN, not flat), and ``cancel_all_fired`` must be ``True`` ONLY
# when THIS shutdown call actually fired a FRESH wire sweep — never when an earlier safety trigger
# already satisfied the cancel-all outcome. Mutation: hardcode ``cancel_all_fired=True`` regardless of
# whether THIS call fired the wire -> the prior-sweep idempotent test below fails.


async def test_shutdown_cancel_all_policy_fires_cancel_all_wire() -> None:
    """SAF-006/AC-009: a cancel-all shutdown policy fires the E2-T3 cancel-all WIRE under the honest
    ``"shutdown"`` cause (never an order id) and the outcome is explicitly recorded on the result.
    """
    adapter = RecordingFakeAdapter(fill=True)
    safety, session = _make_safety()

    result = await _run_guarded(
        adapter=adapter, safety=safety, session=session, shutdown_policy="cancel_all"
    )

    assert adapter.cancel_all_calls == 1, "a cancel-all shutdown policy must fire the recording-fake cancel-all WIRE"
    assert session.submit_blocked is True
    assert session.last_cancel_all_ack is not None
    assert session.last_cancel_all_ack.trigger_cause == "shutdown"
    assert "venue_order_id" not in session.last_cancel_all_ack.model_dump()
    assert isinstance(result.shutdown_decision, ShutdownDecision)
    assert result.shutdown_decision.policy == "cancel_all"
    assert result.shutdown_decision.cancel_all_fired is True, (
        "CONTRAST case (Gate#3 MINOR-1): with NO prior safety sweep, a cancel_all shutdown must "
        "claim it fired the wire, since it did"
    )
    assert result.shutdown_decision.already_satisfied_by_prior_sweep is False


async def test_shutdown_leave_open_policy_records_explicit_decision_no_wire() -> None:
    """SAF-006 (Gate#3 MINOR-1 honest label): a leave-open shutdown policy records an EXPLICIT
    decision and fires NO cancel-all wire — a recorded choice, never a silent omission. Named
    ``"leave_open"`` (never ``"leave_flat"``) because the branch leaves resting orders OPEN.
    """
    adapter = RecordingFakeAdapter(fill=True)
    safety, session = _make_safety()

    result = await _run_guarded(
        adapter=adapter, safety=safety, session=session, shutdown_policy="leave_open"
    )

    assert adapter.cancel_all_calls == 0, "a leave-open shutdown policy must fire NO cancel-all wire"
    assert session.submit_blocked is False, "leave-open must never engage the emergency-stop block"
    assert isinstance(result.shutdown_decision, ShutdownDecision)
    assert result.shutdown_decision.policy == "leave_open"
    assert result.shutdown_decision.cancel_all_fired is False
    assert result.shutdown_decision.already_satisfied_by_prior_sweep is False


async def test_shutdown_default_policy_is_explicit_leave_open_never_omitted() -> None:
    """POSITIVE CONTROL: the pinned default shutdown policy still yields an EXPLICIT, non-``None``
    decision — proving the "never silent" property holds even when the caller supplies nothing.
    """
    adapter = RecordingFakeAdapter(fill=True)

    result = await _run_guarded(adapter=adapter)

    assert isinstance(result.shutdown_decision, ShutdownDecision)
    assert result.shutdown_decision.policy == "leave_open"
    assert result.shutdown_decision.cancel_all_fired is False
    assert result.shutdown_decision.already_satisfied_by_prior_sweep is False
    assert adapter.cancel_all_calls == 0, "the pinned default must not fire the cancel-all wire"


async def test_shutdown_mode_a_cancel_all_policy_records_decision_but_touches_no_wire() -> None:
    """Mode A (dry-run) records the SAME explicit shutdown-decision contract but NEVER touches the
    cancel-all wire, even under a ``"cancel_all"`` policy — dry-run places no orders, so there is
    nothing to sweep (AC-017), mirroring the E6-T5 startup-sweep Mode A discipline (AC-003).
    """
    adapter = RecordingFakeAdapter(fill=True)
    source = _ScriptedSource(quote=_fresh_quote())

    result = await run_dust_execution(
        adapter=adapter,
        signer=LocalFakeWalletControlPlane(),
        sources=source,
        now_fn=_clock,
        sleep_fn=_noop_sleep,
        envelope=_env(),
        manifest=_manifest(mode="dry_run"),
        mode="dry_run",
        wallet_equity_at_decision=_WALLET_EQUITY,
        fixed_fraction=_FIXED_FRACTION,
        arming=None,
        shutdown_policy="cancel_all",
    )

    assert adapter.cancel_all_calls == 0, "Mode A must fire NO cancel wire even under a cancel-all shutdown policy"
    assert adapter.submit_calls == 0, "Mode A places no orders (AC-017)"
    assert isinstance(result.shutdown_decision, ShutdownDecision)
    assert result.shutdown_decision.policy == "cancel_all"
    assert result.shutdown_decision.cancel_all_fired is False
    assert result.shutdown_decision.already_satisfied_by_prior_sweep is False, (
        "Mode A never attempts a sweep at all (AC-017) — this is a no-attempt, not a "
        "prior-sweep-already-satisfied outcome"
    )


async def test_shutdown_cancel_all_is_idempotent_after_prior_safety_trigger() -> None:
    """Gate#3 MINOR-1 (honesty RED): a cancel-all shutdown AFTER an EARLIER safety trigger already
    fired+blocked is a wire NO-OP for THIS call — the recording-fake ``cancel_all_calls`` must NOT
    increment on the shutdown call, and the returned :class:`ShutdownDecision` must NOT claim THIS
    call fired the wire. Before the MINOR-1 fix, ``cancel_all_fired`` was hardcoded ``True`` whenever
    ``shutdown_policy == "cancel_all"`` in Mode B, over-claiming a wire fire that a prior breaker sweep
    had already performed. The honest telemetry instead reports
    ``cancel_all_fired=False`` + ``already_satisfied_by_prior_sweep=True``: the cancel-all OUTCOME
    already holds, but this shutdown call touched no wire. The shutdown decision is still explicitly
    recorded — never silently skipped just because the session was already blocked (SAF-006).
    """
    adapter = RecordingFakeAdapter(fill=True)
    safety, session = _make_safety()
    breaker = CircuitBreaker(state=CircuitState.OPEN, opened_at=0.0, consecutive_failures=5)

    result = await _run_guarded(
        adapter=adapter,
        safety=safety,
        session=session,
        breaker=breaker,
        shutdown_policy="cancel_all",
    )

    assert adapter.cancel_all_calls == 1, (
        "the idempotent primitive must NOT re-fire the wire at shutdown — the ONE call was the prior "
        "breaker sweep, not the shutdown call"
    )
    assert session.last_cancel_all_ack is not None
    assert session.last_cancel_all_ack.trigger_cause == "breaker", "the ORIGINAL trigger cause is preserved"
    assert isinstance(result.shutdown_decision, ShutdownDecision)
    assert result.shutdown_decision.policy == "cancel_all"
    assert result.shutdown_decision.cancel_all_fired is False, (
        "THE HONESTY PROPERTY (Gate#3 MINOR-1): this shutdown call fired NO wire — the prior breaker "
        "sweep already satisfied the cancel-all outcome — so it must not claim cancel_all_fired=True"
    )
    assert result.shutdown_decision.already_satisfied_by_prior_sweep is True, (
        "telemetry must distinguish 'already satisfied by a prior sweep' from 'this call fired the wire'"
    )


# --- E6-T7: losing-but-bounded session is a lifecycle SUCCESS, not promoted (REQ-014, AC-030) --
#
# THE HONESTY PROPERTY: R4-A proves SAFETY, not alpha. A dust session that stays within its loss
# caps and reconciles CLEANLY against venue truth is a lifecycle SUCCESS even when realized_pnl is
# NEGATIVE — a losing dust PnL is the EXPECTED outcome of a strategy-neutral safety proof and must
# NEVER flip the status to FAILED. The SAME session is simultaneously NEVER marked as promoted
# strategy evidence (no alpha was proven — the two are distinct: operationally-successful AND
# not-promoted). CONTRAST: a real SAFETY failure (a realized-loss-cap breach) is NOT a success — the
# status derives from the SAFETY outcome, never from PnL sign alone. Mutation: flip the status logic
# to fail on realized_pnl < 0 -> the losing-bounded-success test fails; mark a losing session
# promoted=True -> the not-promoted assertion fails.


async def test_losing_bounded_reconciled_session_is_success_not_promoted() -> None:
    """A bounded (loss caps not breached), cleanly RECONCILED session with realized_pnl < 0 is a
    lifecycle SUCCESS — a negative dust PnL never flips the status — and is NEVER promoted (REQ-014).
    """
    adapter = RecordingFakeAdapter(fill=True, fill_history_matches=True)
    safety, session = _make_safety()
    risk = RiskAccumulator(_SESSION_ID)
    losing_fill = RealizedFillRecord(
        realized_pnl=-1.0, fee=0.25, session_id=_SESSION_ID, fill_ts_ms=_NOW_S * 1000
    )

    result = await _run_guarded(
        adapter=adapter, safety=safety, session=session, risk=risk, realized_fills=(losing_fill,)
    )

    recon = next(e for e in result.events if isinstance(e, RealFillReconciliation))
    assert recon.reconciled_state == "RESOLVED", "the session must be cleanly reconciled (not frozen)"
    assert risk.realized_loss_session == 1.25, "the session realized a NEGATIVE fee-inclusive PnL"
    assert session.submit_blocked is False, "a bounded loss must NOT trip the emergency-stop sweep"
    assert isinstance(result.session_outcome, SessionOutcome)
    assert result.session_outcome.status == "SUCCESS", (
        "a bounded, reconciled session is a lifecycle SUCCESS even though it lost money (REQ-014)"
    )
    assert result.session_outcome.promoted is False, (
        "a losing dust session must NEVER be marked as promoted strategy evidence (no alpha proven)"
    )


async def test_safety_failure_cap_breach_is_not_success() -> None:
    """CONTRAST: a SAFETY failure (a real realized-loss-cap breach) is NOT a lifecycle SUCCESS — the
    terminal status derives from the SAFETY outcome, never from PnL sign alone.
    """
    adapter = RecordingFakeAdapter(fill=True, fill_history_matches=True)
    safety, session = _make_safety()
    risk = RiskAccumulator(_SESSION_ID)
    breaching_fill = RealizedFillRecord(
        realized_pnl=-2.5, fee=0.0, session_id=_SESSION_ID, fill_ts_ms=_NOW_S * 1000
    )
    env = _env(max_session_loss=2.0, max_daily_loss=4.0)

    result = await _run_guarded(
        adapter=adapter,
        safety=safety,
        session=session,
        risk=risk,
        realized_fills=(breaching_fill,),
        envelope=env,
    )

    assert adapter.cancel_all_calls == 1, "the cap breach must fire the emergency-stop sweep"
    assert session.submit_blocked is True
    assert isinstance(result.session_outcome, SessionOutcome)
    assert result.session_outcome.status == "FAILED", (
        "a realized-loss-cap breach is a SAFETY failure — NOT a lifecycle success"
    )
    assert result.session_outcome.promoted is False


# --- Gate#3 MINOR-1: a no-submit AMBIGUOUS reconciliation is NOT a safety freeze ---------------
#
# THE DIAGNOSTIC-HONESTY PROPERTY: a reconciliation is a genuine SAFETY FREEZE only when a REAL order
# was actually submitted to the wire for that decision (a submitted-but-unconfirmed fund state). A
# clean Mode A dry-run places NO order (``submit_calls == 0``), yet still emits a per-decision
# ``RealFillReconciliation`` with ``reconciled_state == "AMBIGUOUS"`` — there is no real venue fill to
# confirm. Counting that no-submit AMBIGUOUS as a freeze conflates "nothing was submitted, nothing to
# reconcile" (benign — the exact run an operator validates before arming) with "an order was submitted
# but its fill is unconfirmed" (a genuine Mode-B freeze). The correlation axis is the honest
# ``OrderAckEvent.ack_status``: ``"dry_run_not_submitted"`` marks that NO wire was touched for that
# decision; ``"accepted"``/``"not_accepted"`` mark that a real order reached the wire. A reconciliation
# freezes the session ONLY when its ``decision_id`` joins to an ack that actually submitted.
#
# NON-WEAKENING CONTRAST (the guardrail): a REAL Mode-B submit whose reconciliation is AMBIGUOUS is a
# genuine unresolved-fund-state freeze and MUST still be a lifecycle FAILED. Mutation A: revert to
# "any AMBIGUOUS == frozen" → the Mode-A-dry-run-SUCCESS test fails again. Mutation B: exclude ALL
# reconciliations from the freeze → the Mode-B-real-ambiguous-FAILED guardrail fails. Both prove the
# predicate is EXACTLY "submitted AND ambiguous".


async def test_clean_mode_a_dry_run_no_submit_is_lifecycle_success() -> None:
    """Gate#3 MINOR-1 (RED): a clean, no-submit Mode A dry-run is a lifecycle SUCCESS.

    Mode A places NO order (``submit_calls == 0``) yet still emits an ``AMBIGUOUS``
    ``RealFillReconciliation`` (there is no real venue fill to confirm). That no-submit AMBIGUOUS must
    NOT count as a safety freeze — "nothing was submitted, nothing to reconcile" is benign, not the
    submitted-but-unconfirmed fund state a freeze exists to flag. This is the exact run an operator
    inspects to VALIDATE before arming, so it must report SUCCESS, not a spurious FAILED.
    """
    adapter = FakeVenueAdapter(fill=True)
    source = _ScriptedSource(quote=_fresh_quote())

    result = await _run(adapter=adapter, source=source, mode="dry_run")

    assert adapter.submit_calls == 0, "Mode A must place NO orders (AC-017)"
    assert result.submitted_count == 0, "a clean dry-run reaches the wire zero times"
    recon = next(e for e in result.events if isinstance(e, RealFillReconciliation))
    assert recon.reconciled_state == "AMBIGUOUS", (
        "the no-submit dry-run still emits an AMBIGUOUS reconciliation (no real fill to confirm)"
    )
    ack = next(e for e in result.events if isinstance(e, OrderAckEvent))
    assert ack.ack_status == "dry_run_not_submitted", "the ack honestly records that NO wire was touched"
    assert isinstance(result.session_outcome, SessionOutcome)
    assert result.session_outcome.status == "SUCCESS", (
        "a clean, no-submit Mode A dry-run is a lifecycle SUCCESS — a no-submit AMBIGUOUS "
        "reconciliation is not a safety freeze (Gate#3 MINOR-1)"
    )
    assert result.session_outcome.promoted is False, "a dust session is never promoted strategy evidence"


async def test_mode_b_real_submit_with_ambiguous_reconciliation_is_failed() -> None:
    """GUARDRAIL (non-weakening): a REAL Mode-B submit whose reconciliation is AMBIGUOUS is FAILED.

    This is the genuine unresolved-fund-state freeze the MINOR-1 fix must PRESERVE: a real order
    reached the wire (``submit_calls == 1``, ``ack_status == "accepted"``) but the venue never
    confirmed the fill (``fill_history_matches=False`` → ``AMBIGUOUS``). That submitted-but-unconfirmed
    state MUST still be a lifecycle FAILED — the fix narrows the freeze to submitted decisions, it does
    NOT weaken real Mode-B safety. Must ALREADY pass (proving the freeze exists) and MUST still pass
    after the fix.
    """
    adapter = RecordingFakeAdapter(fill=True, fill_history_matches=False)
    safety, session = _make_safety()

    result = await _run_guarded(adapter=adapter, safety=safety, session=session)

    assert adapter.submit_calls == 1, "a real Mode-B order must reach the wire for this to be a freeze"
    assert result.submitted_count == 1
    recon = next(e for e in result.events if isinstance(e, RealFillReconciliation))
    assert recon.reconciled_state == "AMBIGUOUS", "the venue never confirmed the submitted order's fill"
    ack = next(e for e in result.events if isinstance(e, OrderAckEvent))
    assert ack.ack_status == "accepted", "a real order reached the wire for this decision"
    assert isinstance(result.session_outcome, SessionOutcome)
    assert result.session_outcome.status == "FAILED", (
        "a submitted order with an AMBIGUOUS reconciliation is a genuine unresolved-fund-state "
        "freeze — the MINOR-1 fix must NOT weaken this Mode-B safety property"
    )
    assert result.session_outcome.promoted is False


# =====================================================================================
# Gate#3 CRITICAL-1: the runner DISPATCHES on the ADMITTED typed intent (RED-first).
#
# THE FUND-TOUCHING PROPERTY: a fully-armed Mode B run must ACT ON the admitted typed intent —
# never a hardcoded BUY/FOK taker regardless of what the strategy proposed. One recording-fake
# negative/positive test per intent kind + the manifest permitted-intent gate:
#   * ``no_quote``   -> NEVER submits (explicit DON'T-TRADE): submit_calls == 0, no resting order.
#   * ``make_quote`` -> a RESTING maker (GTC/GTD post-only) honoring the ADMITTED side/price/TIF —
#                        NOT a FOK taker, NOT a hardcoded BUY.
#   * ``take``       -> a taker (FOK/FAK) honoring the ADMITTED side — NOT a hardcoded BUY.
#   * ``cancel_all`` -> the cancel-all safety WIRE fires; NO new order is submitted.
#   * ``cancel_replace`` -> the NAMED order is cancelled AND the replacement placed (honest cancel).
#   * manifest gate  -> an intent not in ``permitted_intent_kinds`` is DENIED (fail-closed, no wire).
#
# MUTATION: revert the dispatch to "always BUY/FOK taker submit" -> the ``no_quote`` test (submits)
# AND the ``make_quote`` test (a FOK taker fires, not a resting maker) both fail. That proves the
# DISPATCH (not merely the presence of new code) is under test.


class _MakerRecordingAdapter(RecordingFakeAdapter):
    """The cancel-all recording fake ALSO wired for the E3-T3 resting-maker + E3-T4 single-cancel wires.

    Records, without ever touching a live venue:

    * ``submit_resting_order`` — the E3-T3 :class:`~veridex.venues.base.RestingOrderVenue` wire the
      ``make_quote`` / ``cancel_replace`` intents rest an order on. ``resting_calls`` increments ONLY
      when the coroutine is actually awaited, and every wire kwarg set is captured so a test can prove
      the ADMITTED side (sign of ``amount``), resting ``order_type`` (GTC/GTD), and ``native_price``
      reached the wire — NOT a hardcoded BUY/FOK taker. The taker ``submit_calls`` counter is
      inherited unchanged, so a resting order can never be mistaken for a taker submit.
    * ``cancel_single_order`` — the E3-T4 :class:`~veridex.venues.base.SingleOrderCancelVenue`
      ``DELETE /order`` wire the ``cancel_replace`` intent cancels the NAMED order on. Records each
      cancelled id and returns the venue ``{"canceled": [...], "not_canceled": {...}}`` shape.
    """

    def __init__(
        self,
        *,
        fill: bool = True,
        fill_history_matches: bool = False,
        open_orders: list[dict[str, object]] | None = None,
    ) -> None:
        super().__init__(fill=fill, fill_history_matches=fill_history_matches, open_orders=open_orders)
        self.resting_calls = 0
        self.resting_wire_kwargs: list[dict[str, object]] = []
        self.cancel_single_calls = 0
        self.cancelled_ids: list[str] = []

    async def submit_resting_order(self, **kwargs: object) -> dict[str, object]:
        self.resting_calls += 1
        self.resting_wire_kwargs.append(dict(kwargs))
        return {"orderID": f"0xresting{self.resting_calls}", "success": True}

    async def cancel_single_order(self, order_id: str) -> dict[str, object]:
        self.cancel_single_calls += 1
        self.cancelled_ids.append(order_id)
        return {"canceled": [order_id], "not_canceled": {}}


def _intent_request(
    intent_kind: str,
    *,
    manifest: StrategyExperimentManifest,
    envelope: PolicyEnvelope,
    params: MMIntentParams,
    mode: ExecutionMode = "live_guarded",
) -> MMExecutionToolRequest:
    """A hash-matched agent request carrying an EXPLICIT typed intent (fail-closed on any mismatch)."""
    return MMExecutionToolRequest.build(
        intent_kind=intent_kind,  # type: ignore[arg-type]
        intent_params=params,
        strategy_id=manifest.strategy_id,
        strategy_config_hash=manifest.strategy_config_hash,
        policy_hash=envelope.policy_hash(),
        session_id="agent-session",
        manifest_hash=manifest.manifest_hash(),
        evidence_class=manifest.evidence_class,
        mode=mode,
        admitted_manifest_hash=manifest.manifest_hash(),
        admitted_policy_hash=envelope.policy_hash(),
        admitted_strategy_config_hash=manifest.strategy_config_hash,
    )


async def test_no_quote_intent_never_submits_and_abstains_honestly() -> None:
    """``no_quote`` is an explicit DON'T-TRADE: a fully-armed run submits NOTHING (Gate#3 CRITICAL-1).

    RED before the fix: the runner ignores the typed intent and hardcodes a BUY/FOK taker, so an
    armed ``no_quote`` request SUBMITS a real BUY (``submit_calls == 1``). After the dispatch fix it
    NEVER touches the submit wire, places no resting order, and abstains with an honest label.
    """
    binding = _binding()
    manifest = _mode_b_manifest(binding)
    env = _env()
    adapter = _MakerRecordingAdapter(fill=True)
    request = _intent_request("no_quote", manifest=manifest, envelope=env, params=MMIntentParams())

    result = await _run_guarded(
        adapter=adapter, manifest=manifest, envelope=env, arming=_arming(binding), request=request
    )

    assert adapter.submit_calls == 0, "an armed no_quote intent must NEVER submit a taker order"
    assert adapter.resting_calls == 0, "an armed no_quote intent must NEVER place a resting order"
    assert adapter.cancel_all_calls == 0, "no_quote is an abstain, not a cancel-all"
    (decision,) = result.decisions
    assert decision.submitted is False
    assert decision.abstain_reason == "intent_no_quote"
    assert decision.venue_order_id is None


async def test_make_quote_intent_places_resting_maker_honoring_side_not_taker() -> None:
    """``make_quote`` rests a GTC/GTD post-only maker honoring the ADMITTED side — NOT a FOK taker.

    RED before the fix: the runner hardcodes a BUY/FOK taker (``submit_calls == 1``), so the SELL
    resting maker is never placed (``resting_calls == 0``). After the fix a resting order rests on the
    E3-T3 wire with the admitted SELL side (negative signed ``amount``), the resting ``GTC`` order type,
    and the admitted native price — and the taker ``submit_order`` wire is never touched.
    """
    binding = _binding()
    manifest = _mode_b_manifest(binding)
    env = _env()
    adapter = _MakerRecordingAdapter(fill=True)
    params = MMIntentParams(token_id=_TOKEN, side="SELL", price=0.49, tif="GTC", client_order_id="coid-mk")
    request = _intent_request("make_quote", manifest=manifest, envelope=env, params=params)

    result = await _run_guarded(
        adapter=adapter, manifest=manifest, envelope=env, arming=_arming(binding), request=request
    )

    assert adapter.submit_calls == 0, "make_quote must NOT fire the FOK taker submit wire"
    assert adapter.resting_calls == 1, "make_quote must rest exactly one maker order on the E3-T3 wire"
    (wire,) = adapter.resting_wire_kwargs
    assert wire["order_type"] == "GTC", "make_quote must rest a GTC/GTD order, never a FOK taker"
    assert wire["post_only"] is True, "a maker rests post-only (add-liquidity-only), never crossing"
    assert isinstance(wire["amount"], float) and wire["amount"] < 0.0, (
        "the ADMITTED SELL side must reach the wire (negative signed amount), NOT a hardcoded BUY"
    )
    assert wire["native_price"] == 0.49, "the ADMITTED resting price must reach the wire"
    (decision,) = result.decisions
    assert decision.submitted is True and decision.abstain_reason is None
    assert decision.venue_order_id is not None


async def test_take_intent_submits_taker_honoring_side_not_hardcoded_buy() -> None:
    """``take`` fires a taker (FOK/FAK) honoring the ADMITTED side — NOT a hardcoded BUY.

    Positive control for the taker dispatch: an admitted SELL ``take`` reaches ``submit_order`` with
    ``side == "SELL"`` (never forced to BUY) and never rests a maker order.
    """
    binding = _binding()
    manifest = _mode_b_manifest(binding)
    env = _env()
    adapter = _MakerRecordingAdapter(fill=True)
    params = MMIntentParams(token_id=_TOKEN, side="SELL", tif="FOK", client_order_id="coid-tk")
    request = _intent_request("take", manifest=manifest, envelope=env, params=params)

    result = await _run_guarded(
        adapter=adapter, manifest=manifest, envelope=env, arming=_arming(binding), request=request
    )

    assert adapter.submit_calls == 1, "an admitted take intent must fire the taker submit wire once"
    assert adapter.resting_calls == 0, "a taker never rests a maker order"
    order = adapter.submitted_orders[-1]
    assert order.side == "SELL", "the ADMITTED SELL side must reach the taker wire, NOT a hardcoded BUY"
    assert order.tif == "FOK", "a take intent is a FOK/FAK taker"
    (decision,) = result.decisions
    assert decision.submitted is True and decision.abstain_reason is None


async def test_cancel_all_intent_fires_cancel_wire_and_submits_no_new_order() -> None:
    """``cancel_all`` invokes the cancel-all safety WIRE and submits NO new order (Gate#3 CRITICAL-1).

    RED before the fix: the runner ignores the intent and submits a BUY/FOK taker. After the fix the
    E2-T3 cancel-all sweep fires (recording-fake ``cancel_all_calls == 1``), submits are blocked, and
    no taker/maker order reaches the wire.
    """
    binding = _binding()
    manifest = _mode_b_manifest(binding)
    env = _env()
    adapter = _MakerRecordingAdapter(fill=True)
    safety, session = _make_safety()
    request = _intent_request("cancel_all", manifest=manifest, envelope=env, params=MMIntentParams())

    result = await _run_guarded(
        adapter=adapter,
        manifest=manifest,
        envelope=env,
        arming=_arming(binding),
        request=request,
        safety=safety,
        session=session,
    )

    assert adapter.cancel_all_calls == 1, "cancel_all must fire the E2-T3 cancel-all sweep WIRE"
    assert adapter.submit_calls == 0, "cancel_all must NOT submit a new taker order"
    assert adapter.resting_calls == 0, "cancel_all must NOT rest a new maker order"
    assert session.submit_blocked is True, "cancel_all blocks further submits (fail-closed)"
    (decision,) = result.decisions
    assert decision.submitted is False
    assert decision.abstain_reason == "intent_cancel_all"


async def test_cancel_replace_intent_cancels_named_order_then_places_replacement() -> None:
    """``cancel_replace`` cancels the NAMED order then rests its replacement (honest cancel semantics).

    RED before the fix: the runner ignores the intent and submits a BUY/FOK taker — the named order is
    never cancelled (``cancel_single_calls == 0``). After the fix the E3-T4 single-order cancel wire
    fires for the named order AND the resting replacement is placed; no FOK taker is submitted.
    """
    binding = _binding()
    manifest = _mode_b_manifest(binding)
    env = _env()
    adapter = _MakerRecordingAdapter(fill=True)
    params = MMIntentParams(
        token_id=_TOKEN,
        side="BUY",
        price=0.49,
        tif="GTC",
        client_order_id="coid-new",
        replaces_client_order_id="0xnamed-order-to-cancel",
    )
    request = _intent_request("cancel_replace", manifest=manifest, envelope=env, params=params)

    result = await _run_guarded(
        adapter=adapter, manifest=manifest, envelope=env, arming=_arming(binding), request=request
    )

    assert adapter.cancel_single_calls == 1, "cancel_replace must cancel the NAMED order via DELETE /order"
    assert adapter.cancelled_ids == ["0xnamed-order-to-cancel"], "exactly the named order is cancelled"
    assert adapter.resting_calls == 1, "cancel_replace must place the resting replacement"
    assert adapter.submit_calls == 0, "cancel_replace is not a blind FOK taker submit"
    cancel_event = next(e for e in result.events if isinstance(e, OrderCancelEvent))
    assert cancel_event.canceled is True, "the named-order cancel must be honestly recorded"
    (decision,) = result.decisions
    assert decision.submitted is True and decision.abstain_reason is None


async def test_intent_not_in_permitted_kinds_is_denied_fail_closed() -> None:
    """An intent NOT in ``manifest.permitted_intent_kinds`` is DENIED — fail-closed, no wire.

    The manifest gates which intents it admits; a ``take`` proposed against a manifest that permits
    ONLY ``make_quote`` must abstain (``submit_calls == 0``), never submit.
    """
    binding = _binding()
    manifest = _mode_b_manifest(binding, permitted_intent_kinds=("make_quote",))
    env = _env()
    adapter = _MakerRecordingAdapter(fill=True)
    params = MMIntentParams(token_id=_TOKEN, side="BUY", tif="FOK", client_order_id="coid-x")
    request = _intent_request("take", manifest=manifest, envelope=env, params=params)

    result = await _run_guarded(
        adapter=adapter, manifest=manifest, envelope=env, arming=_arming(binding), request=request
    )

    assert adapter.submit_calls == 0, "an intent the manifest does not permit must NOT reach the wire"
    assert adapter.resting_calls == 0
    (decision,) = result.decisions
    assert decision.submitted is False
    assert decision.abstain_reason == "intent_not_permitted"


# =====================================================================================
# Gate#3 C-2 (CRITICAL): non-crossing must gate the EXACT proposed typed order — its real
# token/side/native-price — NEVER a phantom hardcoded BUY-at-the-venue-ask.
#
# THE SELF-CROSS INVARIANT (E5, SAF-009): the order actually placed can be a SELL make_quote at a
# native price that crosses an OWN resting BUY. The pre-fix gate evaluated an unrelated BUY@ask, so
# the real SELL slipped past the self-cross guard and rested atop the own book. Non-crossing must see
# the order that reaches the wire.
#
# MUTATION: re-hardcode ``_non_crossing_gate`` to a BUY@ask phantom -> the crossing test below admits
# the SELL and rests it (``resting_calls == 1``), so the test FAILS. That proves the gate now reads
# the REAL proposed order, not a phantom.

_TOKEN_NO = "0xtokenNO"  # the complementary outcome token for the multi-token universe C-4 cases


async def test_non_crossing_gates_the_real_make_quote_sell_not_phantom_buy() -> None:
    """A ``make_quote`` SELL that self-crosses an OWN resting BUY is REFUSED — no order on the wire.

    An own resting ``BUY YES @ 0.50`` and an admitted ``make_quote SELL YES @ 0.49``: the REAL SELL
    (lowest_own_ask 0.49) crosses the own BUY (highest_own_bid 0.50). RED before the fix: the gate
    evaluated a phantom ``BUY @ quote.ask`` (two bids, no ask → admitted), so the crossing SELL rested
    (``resting_calls == 1``, ``submitted``). After the fix the gate sees the real SELL and abstains.
    """
    binding = _binding()
    manifest = _mode_b_manifest(binding)
    env = _env()
    adapter = _MakerRecordingAdapter(fill=True)
    own = (OwnOrderLeg(token_id=_TOKEN, side="BUY", price=0.50, kind=LegKind.OPEN),)
    params = MMIntentParams(token_id=_TOKEN, side="SELL", price=0.49, tif="GTC", client_order_id="coid-mk")
    request = _intent_request("make_quote", manifest=manifest, envelope=env, params=params)

    result = await _run_guarded(
        adapter=adapter,
        manifest=manifest,
        envelope=env,
        arming=_arming(binding),
        request=request,
        own_legs=own,
    )

    assert adapter.resting_calls == 0, "a self-crossing make_quote SELL must NOT rest an order"
    assert adapter.submit_calls == 0, "and must NOT reach the taker submit wire either"
    (decision,) = result.decisions
    assert decision.submitted is False
    assert decision.abstain_reason == "self_cross"


async def test_non_crossing_admits_a_make_quote_sell_that_does_not_cross() -> None:
    """POSITIVE CONTROL: a ``make_quote`` SELL that does NOT cross the own BUY still rests once.

    Own resting ``BUY YES @ 0.50`` and an admitted ``make_quote SELL YES @ 0.52``: highest_own_bid
    0.50 < lowest_own_ask 0.52 → no self-cross → the real SELL rests exactly once. Makes the crossing
    refusal above meaningful (the gate admits the non-crossing real order, refuses the crossing one).
    """
    binding = _binding()
    manifest = _mode_b_manifest(binding)
    env = _env()
    adapter = _MakerRecordingAdapter(fill=True)
    own = (OwnOrderLeg(token_id=_TOKEN, side="BUY", price=0.50, kind=LegKind.OPEN),)
    params = MMIntentParams(token_id=_TOKEN, side="SELL", price=0.52, tif="GTC", client_order_id="coid-mk2")
    request = _intent_request("make_quote", manifest=manifest, envelope=env, params=params)

    result = await _run_guarded(
        adapter=adapter,
        manifest=manifest,
        envelope=env,
        arming=_arming(binding),
        request=request,
        own_legs=own,
    )

    assert adapter.resting_calls == 1, "a non-crossing make_quote SELL must rest exactly one order"
    (decision,) = result.decisions
    assert decision.submitted is True and decision.abstain_reason is None


async def test_non_crossing_gates_the_real_take_buy_not_phantom() -> None:
    """A ``take`` BUY that self-crosses an OWN resting SELL is REFUSED — no order on the taker wire.

    Own resting ``SELL YES @ 0.50`` and an admitted ``take BUY`` (lifts the ask at 0.51): the REAL
    taker BUY @ 0.51 crosses the own SELL @ 0.50 (0.51 >= 0.50). CONTROLLER-added coverage for the
    TAKER branch (the C-2 fold's own suite tested only the maker branch): BOTH order-placing branches
    must feed the exact typed order into the E5 gate, never a phantom ``BUY @ quote.ask``.
    """
    binding = _binding()
    manifest = _mode_b_manifest(binding)
    env = _env()
    adapter = _MakerRecordingAdapter(fill=True)
    own = (OwnOrderLeg(token_id=_TOKEN, side="SELL", price=0.50, kind=LegKind.OPEN),)
    params = MMIntentParams(token_id=_TOKEN, side="BUY", tif="FOK", client_order_id="coid-tk")
    request = _intent_request("take", manifest=manifest, envelope=env, params=params)

    result = await _run_guarded(
        adapter=adapter,
        manifest=manifest,
        envelope=env,
        arming=_arming(binding),
        request=request,
        own_legs=own,
    )

    assert adapter.submit_calls == 0, "a self-crossing take BUY must NOT reach the taker submit wire"
    assert adapter.resting_calls == 0
    (decision,) = result.decisions
    assert decision.submitted is False
    assert decision.abstain_reason == "self_cross"


async def test_non_crossing_admits_a_take_buy_that_does_not_cross() -> None:
    """POSITIVE CONTROL: a ``take`` BUY that does NOT cross the own SELL still submits once.

    Own resting ``SELL YES @ 0.55`` and an admitted ``take BUY`` @ 0.51 (the ask): 0.51 < 0.55 → no
    self-cross → the real taker order submits exactly once. Makes the taker refusal meaningful.
    """
    binding = _binding()
    manifest = _mode_b_manifest(binding)
    env = _env()
    adapter = _MakerRecordingAdapter(fill=True)
    own = (OwnOrderLeg(token_id=_TOKEN, side="SELL", price=0.55, kind=LegKind.OPEN),)
    params = MMIntentParams(token_id=_TOKEN, side="BUY", tif="FOK", client_order_id="coid-tk2")
    request = _intent_request("take", manifest=manifest, envelope=env, params=params)

    result = await _run_guarded(
        adapter=adapter,
        manifest=manifest,
        envelope=env,
        arming=_arming(binding),
        request=request,
        own_legs=own,
    )

    assert adapter.submit_calls == 1, "a non-crossing take BUY must submit exactly one taker order"
    (decision,) = result.decisions
    assert decision.submitted is True and decision.abstain_reason is None


# =====================================================================================
# Gate#3 C-4 (CRITICAL): a SINGULAR order-placing intent targets EXACTLY its admitted
# ``intent_params.token_id`` — it must NOT fan out across the whole manifest universe.
#
# THE FUND-TOUCHING PROPERTY: one ``make_quote`` for ``0xtokenYES`` in a two-token universe must move
# funds on ``0xtokenYES`` ONLY; every other token abstains with the closed-vocabulary token-mismatch
# reason. A missing / out-of-universe ``intent_params.token_id`` fails closed (all abstain, zero wire).
#
# MUTATION: remove the token-match guard (loop applies the intent to all tokens) -> the multi-token
# test rests TWO orders (``resting_calls == 2``), so the test FAILS. That proves the guard is under test.


async def test_singular_make_quote_targets_only_its_admitted_token() -> None:
    """One ``make_quote`` for ``0xtokenYES`` in a ``[YES, NO]`` universe rests for YES ONLY.

    RED before the fix: the token loop applies the SAME intent to EVERY token, so it rests an order for
    BOTH ``0xtokenYES`` AND ``0xtokenNO`` (``resting_calls == 2``) — the request authorized one token
    but moved funds on another. After the fix ``0xtokenNO`` abstains ``intent_token_mismatch`` and
    exactly one resting order lands on ``0xtokenYES``.
    """
    binding = _binding()
    manifest = _mode_b_manifest(binding, universe=(_TOKEN, _TOKEN_NO))
    env = _env()
    adapter = _MakerRecordingAdapter(fill=True)
    params = MMIntentParams(token_id=_TOKEN, side="SELL", price=0.52, tif="GTC", client_order_id="coid-mk")
    request = _intent_request("make_quote", manifest=manifest, envelope=env, params=params)

    result = await _run_guarded(
        adapter=adapter, manifest=manifest, envelope=env, arming=_arming(binding), request=request
    )

    assert adapter.resting_calls == 1, "a singular make_quote must rest EXACTLY one order (its token)"
    assert adapter.submit_calls == 0
    by_token = {d.token_id: d for d in result.decisions}
    assert by_token[_TOKEN].submitted is True and by_token[_TOKEN].abstain_reason is None
    assert by_token[_TOKEN_NO].submitted is False, "the non-target token must NOT move funds"
    assert by_token[_TOKEN_NO].abstain_reason == "intent_token_mismatch"


async def test_singular_make_quote_token_not_in_universe_fails_closed() -> None:
    """An ``intent_params.token_id`` NOT in the manifest universe fails closed: all abstain, zero wire.

    RED before the fix: the loop ignores ``intent_params.token_id`` entirely and rests an order for
    each universe token (``resting_calls == 2``). After the fix — since NO universe token equals the
    out-of-universe target — every token abstains ``intent_token_mismatch`` and nothing reaches the wire.
    """
    binding = _binding()
    manifest = _mode_b_manifest(binding, universe=(_TOKEN, _TOKEN_NO))
    env = _env()
    adapter = _MakerRecordingAdapter(fill=True)
    params = MMIntentParams(
        token_id="0xtokenOTHER", side="SELL", price=0.52, tif="GTC", client_order_id="coid-mk"
    )
    request = _intent_request("make_quote", manifest=manifest, envelope=env, params=params)

    result = await _run_guarded(
        adapter=adapter, manifest=manifest, envelope=env, arming=_arming(binding), request=request
    )

    assert adapter.resting_calls == 0, "a token outside the universe must never move funds"
    assert adapter.submit_calls == 0
    assert all(not d.submitted for d in result.decisions)
    assert all(d.abstain_reason == "intent_token_mismatch" for d in result.decisions)
