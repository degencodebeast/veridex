"""E3-T5 — CLOB-V2 write-contract compatibility gate (REQ-017, AC-036, §6 group 17).

The gate is the pre-Mode-B blocker. It has TWO structurally distinct signals that BOTH must pass
before Mode B (live_guarded real money) can arm:

* ``fixtures_match`` (MACHINE, OFFLINE): the signature/payload fixtures validate against the
  E3-T0-pinned CURRENT official V2 schemas — the §1b/§2b EXACT SET, §1a domain ``version="2"`` +
  V2 verifyingContract, the §4 ``DELETE /order`` response shape, and the §5 paginated ``get_orders``
  shape. A fixture built against a STALE V1-ish schema (carrying removed ``taker``/``nonce``/
  ``feeRateBps`` or missing the V2 ``timestamp``/``metadata``/``builder``, or signed against the V1
  domain/verifyingContract) → the gate FAILS CLOSED.

* ``operator_smoke`` (``ok``): a REAL-venue production compatibility check that is OPERATOR-RUN and
  OUT of CI. It starts ``None`` (operator-pending) and is NEVER auto-``True`` — and CRITICALLY it is
  NEVER inferred from the offline fake fixtures passing. Only an operator who actually ran the real
  check may set it.

Mode-B admission requires BOTH: the machine fixture-match AND the operator-confirmed smoke. Until an
operator runs the real smoke, ``operator_smoke_ok`` stays ``None`` and Mode B is DENIED.

MONEY-NETWORK BOUNDARY: this whole test suite is OFFLINE. It validates fixtures + drives the gate's
decision logic; it NEVER calls a real venue, never arms Mode B, and never sets the operator smoke to
a truthy value on its own.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from veridex.dust_execution.clobv2_gate import (
    Clobv2GateResult,
    evaluate_clobv2_gate,
    operator_production_smoke,
    validate_cancel_response,
    validate_get_orders_page,
    validate_sendorder_fixture,
    validate_signed_order,
)

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "dust_execution" / "clobv2"


def _load(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURE_DIR / f"{name}.json").read_text())


# ---------------------------------------------------------------------------
# Machine fixture validation against the E3-T0 pinned EXACT-SET V2 schemas
# ---------------------------------------------------------------------------


def test_valid_v2_sendorder_fixtures_validate() -> None:
    """The GTC-EOA and GTD-postOnly fixtures match the §2 SendOrder EXACT SET."""
    ok_gtc, reasons_gtc = validate_sendorder_fixture(_load("sendorder_gtc_eoa"))
    assert ok_gtc is True, reasons_gtc
    ok_gtd, reasons_gtd = validate_sendorder_fixture(_load("sendorder_gtd_postonly"))
    assert ok_gtd is True, reasons_gtd


def test_valid_v2_signed_order_validates() -> None:
    """The §1 signed struct + §1a domain (version=2, V2 verifyingContract) validate."""
    ok, reasons = validate_signed_order(_load("order_signed_v2"))
    assert ok is True, reasons


def test_stale_v1_wire_fixture_fails_closed() -> None:
    """A stale V1-shaped SendOrder (taker/nonce/feeRateBps present, timestamp/builder missing) is
    rejected against the V2 exact-set — unknown/removed keys and missing V2 keys both fail closed."""
    ok, reasons = validate_sendorder_fixture(_load("reject_stale_v1_wire"))
    assert ok is False
    joined = " ".join(reasons).lower()
    assert "taker" in joined or "nonce" in joined or "feeratebps" in joined
    assert "timestamp" in joined or "builder" in joined


def test_sigtype3_fixture_fails_closed() -> None:
    """signatureType=3 (POLY_1271 deposit wallet) is out of scope for R4-A EOA-only → fail closed."""
    ok, reasons = validate_sendorder_fixture(_load("reject_sigtype3"))
    assert ok is False
    assert any("signaturetype" in r.lower() for r in reasons)


def test_v1_domain_signed_order_fails_closed() -> None:
    """An order signed against the V1 exchange domain (version "1") / V1 verifyingContract is
    rejected — it would produce a different order hash and is unsupported on prod."""
    ok, reasons = validate_signed_order(_load("reject_v1_domain"))
    assert ok is False
    joined = " ".join(reasons).lower()
    assert "version" in joined or "verifyingcontract" in joined


def test_cancel_and_get_orders_shapes_verified() -> None:
    """§4 DELETE /order response + §5 paginated get_orders shapes validate."""
    ok_cancel, r_cancel = validate_cancel_response(_load("cancel_response"))
    assert ok_cancel is True, r_cancel
    ok_orders, r_orders = validate_get_orders_page(_load("get_orders_page"))
    assert ok_orders is True, r_orders


# ---------------------------------------------------------------------------
# Operator production smoke — ok=None until an operator runs it (never auto-True)
# ---------------------------------------------------------------------------


def test_operator_production_smoke_is_pending_by_default() -> None:
    """The real-venue compatibility smoke is OPERATOR-RUN / OUT of CI: ok=None until an operator
    runs it. It is NEVER auto-True."""
    smoke = operator_production_smoke()
    assert smoke.ok is None
    assert "operator" in smoke.detail.lower()


def test_operator_production_smoke_only_true_when_operator_ran_and_confirmed() -> None:
    """Only an operator who actually ran the real check may set ok=True."""
    pending = operator_production_smoke(operator_ran=False)
    assert pending.ok is None
    passed = operator_production_smoke(operator_ran=True, operator_result=True)
    assert passed.ok is True
    failed = operator_production_smoke(operator_ran=True, operator_result=False)
    assert failed.ok is False


# ---------------------------------------------------------------------------
# THE load-bearing gate test (REQ-017): stale fixtures → fail closed → Mode B denied;
# operator smoke ok=None (operator-pending), NEVER inferred from the fake tests passing.
# ---------------------------------------------------------------------------


def test_mode_b_blocked_until_clobv2_gate_and_fixtures_match_official() -> None:
    # --- (1) STALE fixtures deliberately mismatched to a stale schema → gate FAILS CLOSED ---
    stale = evaluate_clobv2_gate(
        client_version="2",
        sendorder_fixtures={"stale": _load("reject_stale_v1_wire")},
        signed_fixtures={"v2": _load("order_signed_v2")},
        cancel_response=_load("cancel_response"),
        get_orders_page=_load("get_orders_page"),
        operator_smoke_ok=None,
    )
    assert isinstance(stale, Clobv2GateResult)
    assert stale.fixtures_match is False  # stale schema rejected
    assert stale.machine_ok is False
    # Mode B admission DENIED while the machine fixture-match fails.
    assert stale.mode_b_admitted is False
    # And the operator smoke was never auto-set — it is operator-pending.
    assert stale.operator_smoke_ok is None

    # --- (2) VALID fixtures → machine match passes, BUT operator smoke still pending → Mode B DENIED ---
    machine_only = evaluate_clobv2_gate(
        client_version="2",
        sendorder_fixtures={
            "gtc": _load("sendorder_gtc_eoa"),
            "gtd": _load("sendorder_gtd_postonly"),
        },
        signed_fixtures={"v2": _load("order_signed_v2")},
        cancel_response=_load("cancel_response"),
        get_orders_page=_load("get_orders_page"),
        operator_smoke_ok=None,  # operator has NOT run the real smoke
    )
    assert machine_only.fixtures_match is True
    assert machine_only.machine_ok is True
    # CRITICAL: passing offline fake fixtures does NOT infer operator-smoke success.
    assert machine_only.operator_smoke_ok is None  # NOT auto-True from the fakes passing
    # Mode B still DENIED — the operator-confirmed smoke is missing.
    assert machine_only.mode_b_admitted is False

    # --- (3) The vendored V1 client (CLOB_VERSION="1") is NOT production-ready ---
    v1_client = evaluate_clobv2_gate(
        client_version="1",
        sendorder_fixtures={"gtc": _load("sendorder_gtc_eoa")},
        signed_fixtures={"v2": _load("order_signed_v2")},
        cancel_response=_load("cancel_response"),
        get_orders_page=_load("get_orders_page"),
        operator_smoke_ok=True,  # even with an operator smoke, an unsupported client blocks
    )
    assert v1_client.supported_client is False
    assert v1_client.machine_ok is False
    assert v1_client.mode_b_admitted is False

    # --- (4) BOTH machine match AND operator-confirmed smoke → Mode B ADMITTED ---
    admitted = evaluate_clobv2_gate(
        client_version="2",
        sendorder_fixtures={
            "gtc": _load("sendorder_gtc_eoa"),
            "gtd": _load("sendorder_gtd_postonly"),
        },
        signed_fixtures={"v2": _load("order_signed_v2")},
        cancel_response=_load("cancel_response"),
        get_orders_page=_load("get_orders_page"),
        operator_smoke_ok=True,  # operator RAN the real smoke and it passed
    )
    assert admitted.machine_ok is True
    assert admitted.operator_smoke_ok is True
    assert admitted.mode_b_admitted is True


def test_gate_does_not_infer_operator_smoke_from_fake_fixtures() -> None:
    """MUTATION anchor: the operator-smoke tri-state carried on the result MUST equal exactly what
    the operator supplied — NEVER inferred from the (offline, fake) machine checks passing. If the
    gate were mutated to set ``operator_smoke_ok = machine_ok`` (infer success from fakes), Mode B
    would arm on fakes and this assertion would fail."""
    result = evaluate_clobv2_gate(
        client_version="2",
        sendorder_fixtures={"gtc": _load("sendorder_gtc_eoa")},
        signed_fixtures={"v2": _load("order_signed_v2")},
        cancel_response=_load("cancel_response"),
        get_orders_page=_load("get_orders_page"),
        operator_smoke_ok=None,  # operator did NOT run it
    )
    # Machine checks all pass...
    assert result.machine_ok is True
    # ...yet the operator smoke stays exactly None (not inferred True from the passing fakes).
    assert result.operator_smoke_ok is None
    # ...and Mode B stays DENIED (fake tests passing can never arm Mode B).
    assert result.mode_b_admitted is False


# ---------------------------------------------------------------------------
# Preflight wiring: Mode B cannot arm while the gate is failing/pending
# ---------------------------------------------------------------------------


async def _run_preflight_with_gate(gate: Clobv2GateResult | None):
    from veridex.venues.polymarket_preflight import run_preflight
    from veridex.venues.polymarket_resolver import ResolvedMarket

    class _Balances:
        async def get_balance_allowance(self, asset_type: str, token_id: str | None = None,
                                        signature_type: int = -1, **kwargs: Any) -> dict[str, Any]:
            if asset_type == "COLLATERAL":
                return {"balance": 100.0, "allowance": 100.0}
            return {"balance": 5.0, "allowance": 5.0}

    class _Quote:
        async def quote_market(self, market_ref: str, for_size: float | None = None):
            from veridex.venues.base import Quote
            return Quote(market_ref="ref", price=1.90, native_price=0.526, size=50.0,
                         for_size=for_size or 0.0, ts=0)

    class _Egress:
        async def reachable(self) -> bool:
            return True

    from veridex.policy.envelope import PolicyEnvelope

    envelope = PolicyEnvelope(
        max_stake=1000.0, max_orders_per_run=100, max_orders_per_session=100,
        max_orders_per_day=100, venue_allowlist=["polymarket"], market_allowlist=["ref"],
        min_edge_bps=0, max_slippage_bps=500, max_price=100.0, max_quote_age_s=60,
        cooldown_s=0, human_approval_threshold=1_000_000.0, kill_switch=False,
    )
    return await run_preflight(
        market_ref="ref", order_size=10.0, required_usdc=10.0,
        resolved=ResolvedMarket(condition_id="0xcond", token_id_yes="111", token_id_no="222", tick_size=0.01),
        quote_adapter=_Quote(), balances=_Balances(), egress=_Egress(), envelope=envelope,
        actual_sig_type=2, expected_sig_type=2, max_slippage_bps=500, reference_price=1.90,
        dry_run=True, neg_risk_approved=True, fak_smoke_passed=True, clobv2_gate=gate,
    )


async def test_preflight_mode_b_blocked_when_gate_absent() -> None:
    """No CLOB-V2 gate supplied → the clobv2 check is operator-pending (ok=None) and mode_b_ready is
    fail-closed False, even though the taker live_ready is True."""
    report = await _run_preflight_with_gate(None)
    checks = {c.name: c for c in report.checks}
    assert checks["clobv2_write_contract"].ok is None
    assert report.live_ready is True  # taker-path readiness unaffected (additive)
    assert report.mode_b_ready is False  # Mode B fail-closed until the gate passes


async def test_preflight_mode_b_blocked_when_gate_machine_fails() -> None:
    """A failing gate (stale fixtures) → clobv2 check ok=False (hard fail) → report.ok False,
    mode_b_ready False."""
    gate = evaluate_clobv2_gate(
        client_version="2",
        sendorder_fixtures={"stale": _load("reject_stale_v1_wire")},
        signed_fixtures={"v2": _load("order_signed_v2")},
        cancel_response=_load("cancel_response"),
        get_orders_page=_load("get_orders_page"),
        operator_smoke_ok=None,
    )
    report = await _run_preflight_with_gate(gate)
    checks = {c.name: c for c in report.checks}
    assert checks["clobv2_write_contract"].ok is False
    assert report.ok is False
    assert report.mode_b_ready is False


async def test_preflight_mode_b_blocked_when_smoke_pending_even_if_fixtures_match() -> None:
    """Machine fixtures match but operator smoke pending (ok=None) → clobv2 check ok=None,
    report.ok unaffected, but mode_b_ready still fail-closed False."""
    gate = evaluate_clobv2_gate(
        client_version="2",
        sendorder_fixtures={"gtc": _load("sendorder_gtc_eoa")},
        signed_fixtures={"v2": _load("order_signed_v2")},
        cancel_response=_load("cancel_response"),
        get_orders_page=_load("get_orders_page"),
        operator_smoke_ok=None,
    )
    report = await _run_preflight_with_gate(gate)
    checks = {c.name: c for c in report.checks}
    assert checks["clobv2_write_contract"].ok is None
    assert report.ok is True  # ok=None does not fail the report
    assert report.mode_b_ready is False  # blocked: operator smoke pending


async def test_preflight_mode_b_ready_only_when_gate_fully_admits() -> None:
    """Machine match AND operator-confirmed smoke → clobv2 check ok=True → mode_b_ready True."""
    gate = evaluate_clobv2_gate(
        client_version="2",
        sendorder_fixtures={"gtc": _load("sendorder_gtc_eoa"), "gtd": _load("sendorder_gtd_postonly")},
        signed_fixtures={"v2": _load("order_signed_v2")},
        cancel_response=_load("cancel_response"),
        get_orders_page=_load("get_orders_page"),
        operator_smoke_ok=True,
    )
    report = await _run_preflight_with_gate(gate)
    checks = {c.name: c for c in report.checks}
    assert checks["clobv2_write_contract"].ok is True
    assert report.mode_b_ready is True
