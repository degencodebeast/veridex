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

from veridex.dust_execution.clobv2_gate import Clobv2GateResult
from veridex.dust_execution.contracts import (
    DustExecutionSessionMeta,
    DustRunLabelEvent,
    ExecutionMode,
    OrderAckEvent,
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
    ShutdownDecision,
    ShutdownPolicy,
    StaleVenueBook,
    SubmitDecision,
    run_dust_execution,
)
from veridex.dust_execution.signer import LocalFakeWalletControlPlane, SignedArtifact
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
        "permitted_intent_kinds": ("make",),
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


def _arming(
    binding: ExecutionWalletBinding | None = None,
    *,
    mode_a_passed: bool = True,
    clobv2: Clobv2GateResult | None = None,
    preflight: PrivyPreflightResult | None = None,
    provisioning: ProvisioningResult | None = None,
    live_policy: PrivyWalletPolicy | None = None,
    live_quorum: AuthorizationQuorum | None = None,
) -> ModeBArming:
    """A fully-passing Mode-B arming bundle, with per-precondition overrides for the failure tests."""
    b = binding if binding is not None else _binding()
    return ModeBArming(
        mode_a_passed=mode_a_passed,
        clobv2_gate=clobv2 if clobv2 is not None else _clobv2_ok(),
        privy_preflight=preflight if preflight is not None else _preflight_ok(),
        provisioning=provisioning if provisioning is not None else _provisioning_ok(),
        binding=b,
        live_policy=live_policy if live_policy is not None else _policy(),
        live_quorum=live_quorum if live_quorum is not None else _quorum(),
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
    shutdown_policy: ShutdownPolicy = "leave_flat",
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
    """A typed agent request declaring the admitted pins; ``confidence``/``size`` are untrusted."""
    return MMExecutionToolRequest(
        intent_kind="make_quote",
        intent_params=MMIntentParams(token_id=_TOKEN, side="BUY", price=0.51, size=requested_size),
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


# --- E6-T6: shutdown cancel-all or explicit leave-flat decision (SAF-006, AC-009) -------------
#
# THE safety property: at shutdown the outcome must be one of exactly two EXPLICIT states —
# cancel-all fired (wire cancel + block) OR an explicit recorded leave-flat/leave-open decision. A
# silent abandon (the run ends with resting orders and NEITHER a fired cancel-all NOR a recorded
# decision) is the failure these tests expose. Mutation: make shutdown a silent no-op (neither cancel
# nor record) -> ``result.shutdown_decision`` would not exist / the cancel-all WIRE would not fire ->
# these tests fail.


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
    assert result.shutdown_decision.cancel_all_fired is True


async def test_shutdown_leave_flat_policy_records_explicit_decision_no_wire() -> None:
    """SAF-006: a leave-flat shutdown policy records an EXPLICIT decision and fires NO cancel-all
    wire — a recorded choice, never a silent omission.
    """
    adapter = RecordingFakeAdapter(fill=True)
    safety, session = _make_safety()

    result = await _run_guarded(
        adapter=adapter, safety=safety, session=session, shutdown_policy="leave_flat"
    )

    assert adapter.cancel_all_calls == 0, "a leave-flat shutdown policy must fire NO cancel-all wire"
    assert session.submit_blocked is False, "leave-flat must never engage the emergency-stop block"
    assert isinstance(result.shutdown_decision, ShutdownDecision)
    assert result.shutdown_decision.policy == "leave_flat"
    assert result.shutdown_decision.cancel_all_fired is False


async def test_shutdown_default_policy_is_explicit_leave_flat_never_omitted() -> None:
    """POSITIVE CONTROL: the pinned default shutdown policy still yields an EXPLICIT, non-``None``
    decision — proving the "never silent" property holds even when the caller supplies nothing.
    """
    adapter = RecordingFakeAdapter(fill=True)

    result = await _run_guarded(adapter=adapter)

    assert isinstance(result.shutdown_decision, ShutdownDecision)
    assert result.shutdown_decision.policy == "leave_flat"
    assert result.shutdown_decision.cancel_all_fired is False
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


async def test_shutdown_cancel_all_is_idempotent_after_prior_safety_trigger() -> None:
    """A cancel-all shutdown after an EARLIER safety trigger already swept is idempotent: the wire is
    NOT re-fired (the session is already blocked), yet the shutdown outcome is still explicitly
    recorded — never silently skipped just because the session was already blocked.
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

    assert adapter.cancel_all_calls == 1, "the idempotent primitive must NOT re-fire the wire at shutdown"
    assert session.last_cancel_all_ack is not None
    assert session.last_cancel_all_ack.trigger_cause == "breaker", "the ORIGINAL trigger cause is preserved"
    assert isinstance(result.shutdown_decision, ShutdownDecision)
    assert result.shutdown_decision.policy == "cancel_all"
    assert result.shutdown_decision.cancel_all_fired is True
