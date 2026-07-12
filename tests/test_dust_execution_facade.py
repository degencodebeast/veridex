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

import typing

import pytest
from pydantic import BaseModel, ValidationError

from veridex.dust_execution import facade  # module handle: proposer looked up dynamically (RED-clean)
from veridex.dust_execution.facade import (
    MMExecutionToolRequest,
    MMExecutionToolResult,
    MMIntentParams,
)
from veridex.dust_execution.manifest import StrategyExperimentManifest
from veridex.dust_execution.runner import BookSide, DustQuote
from veridex.dust_execution.signer import LocalFakeWalletControlPlane
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
    for name, field in MMExecutionToolResult.model_fields.items():
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
        "permitted_intent_kinds": ("make",),
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
