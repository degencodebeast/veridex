"""R4-A CLOB-V2 write-contract compatibility gate (E3-T5, REQ-017, AC-036, §6 group 17).

The pre-Mode-B blocker. Before Mode B (live_guarded real money) can arm, the CLOB-V2 write contract
must be proven compatible along TWO structurally distinct signals that BOTH must pass:

1. ``fixtures_match`` — a MACHINE, OFFLINE decision: the signature/payload fixtures validate against
   the E3-T0-pinned CURRENT official V2 schemas (``docs/maker/r4a-clobv2-wire-contract.md``):

   * §1a exchange EIP-712 domain — ``name="Polymarket CTF Exchange"``, ``version="2"`` (V1 was "1"),
     ``chainId=137``, ``verifyingContract`` one of the two V2 addresses (the V1 addresses are
     REJECTED-value).
   * §1b signed ``Order`` struct — the EXACT SET
     ``{salt, maker, signer, tokenId, makerAmount, takerAmount, side, signatureType, timestamp,
     metadata, builder}``; V1's ``{taker, expiration, nonce, feeRateBps}`` are REMOVED from the
     signed hash and MUST be absent.
   * §2a ``SendOrder`` top-level EXACT SET — required ``{order, owner}``; optional
     ``{orderType, deferExec, postOnly}``.
   * §2b ``order`` wire body EXACT SET — required
     ``{salt, maker, signer, tokenId, makerAmount, takerAmount, side, expiration, timestamp, builder,
     signature, signatureType}``; optional ``metadata``; REMOVED ``{taker, nonce, feeRateBps}`` MUST
     be absent. R4-A emits ONLY ``signatureType=0`` (EOA); ``1/2/3`` are rejected-value.
   * §4 ``DELETE /order`` response — ``{canceled: list, not_canceled: map}``.
   * §5 paginated ``get_orders`` — a page envelope (``data``/``orders`` list + ``count`` +
     ``next_cursor``) whose entries match the OpenOrder EXACT SET.

   ANY unknown/removed key, missing V2 key, or wrong domain version/contract → the gate FAILS CLOSED.
   (This is the whole point: a fixture built against a stale V1-ish schema — like the mixed
   ``endpoints.md`` autodoc still shipping ``nonce``/``feeRateBps``/``makerToken`` — must be rejected,
   not silently accepted.)

2. ``operator_smoke_ok`` — a REAL-venue production compatibility check that is OPERATOR-RUN and OUT
   of CI (:func:`operator_production_smoke`). It starts ``None`` (operator-pending) and is NEVER
   auto-``True``. CRITICALLY it is NEVER inferred from the offline fake fixtures passing — the gate
   copies the operator-supplied tri-state through verbatim; it does not compute it from
   ``machine_ok``. Only an operator who actually ran the real check may set it.

Mode-B admission (:attr:`Clobv2GateResult.mode_b_admitted`) requires BOTH the machine fixture-match
AND the operator-confirmed smoke. Until an operator runs the real smoke, ``operator_smoke_ok`` stays
``None`` and Mode B is DENIED.

MONEY-NETWORK BOUNDARY: this module performs NO network I/O, holds NO credentials, and NEVER signs.
The real production smoke is executed by an operator outside CI; here it is only ever the
operator-pending stub. The vendored V1 client (``CLOB_VERSION="1"``) is NOT production-ready until
this gate passes; Mode B cannot arm until it passes.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# E3-T0 pinned constants (verbatim from docs/maker/r4a-clobv2-wire-contract.md)
# ---------------------------------------------------------------------------

#: The supported production CLOB version. The vendored client pins ``CLOB_VERSION="1"`` (V1), which
#: is NOT production-ready for R4-A; only "2" passes the client/version decision.
SUPPORTED_CLOB_VERSION: str = "2"

# §1a exchange EIP-712 domain (V2).
_EXCHANGE_DOMAIN_NAME: str = "Polymarket CTF Exchange"
_EXCHANGE_DOMAIN_VERSION_V2: str = "2"
_CHAIN_ID: int = 137

# §1a verifyingContract — the two V2 exchange addresses (compared case-insensitively). The V1
# addresses are rejected-value: signing against them fails closed.
_V2_VERIFYING_CONTRACTS: frozenset[str] = frozenset(
    a.lower()
    for a in (
        "0xE111180000d2663C0091e4f400237545B87B996B",  # standard exchange (V2)
        "0xe2222d279d744050d28e00520010520000310F59",  # neg-risk exchange (V2)
    )
)

# §1b signed Order struct — EXACT SET (fields inside the signed hash) + the V1 fields REMOVED in V2.
_SIGNED_ORDER_FIELDS: frozenset[str] = frozenset(
    {
        "salt",
        "maker",
        "signer",
        "tokenId",
        "makerAmount",
        "takerAmount",
        "side",
        "signatureType",
        "timestamp",
        "metadata",
        "builder",
    }
)
_SIGNED_ORDER_REMOVED: frozenset[str] = frozenset({"taker", "expiration", "nonce", "feeRateBps"})

# §2a SendOrder top-level.
_SENDORDER_REQUIRED: frozenset[str] = frozenset({"order", "owner"})
_SENDORDER_OPTIONAL: frozenset[str] = frozenset({"orderType", "deferExec", "postOnly"})

# §2b order wire body — required + optional + REMOVED.
_ORDER_WIRE_REQUIRED: frozenset[str] = frozenset(
    {
        "salt",
        "maker",
        "signer",
        "tokenId",
        "makerAmount",
        "takerAmount",
        "side",
        "expiration",
        "timestamp",
        "builder",
        "signature",
        "signatureType",
    }
)
_ORDER_WIRE_OPTIONAL: frozenset[str] = frozenset({"metadata"})
_ORDER_WIRE_REMOVED: frozenset[str] = frozenset({"taker", "nonce", "feeRateBps"})

# R4-A emits ONLY EOA (0). 1/2/3 are rejected-value fixtures (§1c).
_SUPPORTED_SIGNATURE_TYPE: int = 0

# §2a / §6 order-type enum.
_ORDER_TYPES: frozenset[str] = frozenset({"GTC", "GTD", "FOK", "FAK"})
_RESTING_ORDER_TYPES: frozenset[str] = frozenset({"GTC", "GTD"})

# §5 OpenOrder EXACT SET.
_OPEN_ORDER_FIELDS: frozenset[str] = frozenset(
    {
        "id",
        "status",
        "market",
        "asset_id",
        "side",
        "original_size",
        "size_matched",
        "price",
        "outcome",
        "order_type",
        "maker_address",
        "owner",
        "expiration",
        "associate_trades",
        "created_at",
    }
)

#: Canonical fixture directory (E3-T0 §9).
DEFAULT_FIXTURE_DIR: Path = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "dust_execution"
    / "clobv2"
)


# ---------------------------------------------------------------------------
# Machine validators (dict in, (ok, reasons) out) — pure, offline, fail-closed
# ---------------------------------------------------------------------------


def _exact_set_reasons(
    obj: Mapping[str, Any],
    *,
    label: str,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
    removed: frozenset[str] = frozenset(),
) -> list[str]:
    """Fail-closed EXACT-SET check: report missing-required, removed, and unknown keys.

    Metadata/comment keys prefixed with ``_`` (e.g. a fixture ``_comment``) are ignored so a fixture
    may carry an explanatory note without tripping the unknown-key guard.
    """
    reasons: list[str] = []
    keys = {k for k in obj if not k.startswith("_")}
    for missing in sorted(required - keys):
        reasons.append(f"{label}: missing required V2 key {missing!r}")
    for gone in sorted(removed & keys):
        reasons.append(f"{label}: V1 field {gone!r} was REMOVED in V2 and must be absent")
    allowed = required | optional
    for unknown in sorted(keys - allowed - removed):
        reasons.append(f"{label}: unknown key {unknown!r} (not in the V2 exact set)")
    return reasons


def validate_signed_order(fixture: Mapping[str, Any]) -> tuple[bool, tuple[str, ...]]:
    """Validate a §1 signed-order fixture (``{domain, order_value}``) against the V2 exact-set.

    Checks the §1a domain (name, ``version="2"``, ``chainId=137``, a V2 ``verifyingContract``) and the
    §1b signed struct EXACT SET. Returns ``(True, ())`` only when nothing fails closed.
    """
    reasons: list[str] = []

    domain = fixture.get("domain")
    if not isinstance(domain, Mapping):
        reasons.append("signed_order: missing 'domain'")
    else:
        if domain.get("name") != _EXCHANGE_DOMAIN_NAME:
            reasons.append(f"domain: name {domain.get('name')!r} != {_EXCHANGE_DOMAIN_NAME!r}")
        if str(domain.get("version")) != _EXCHANGE_DOMAIN_VERSION_V2:
            reasons.append(
                f"domain: version {domain.get('version')!r} != V2 {_EXCHANGE_DOMAIN_VERSION_V2!r} "
                "(a V1-signed order yields a different order hash)"
            )
        if int(domain.get("chainId", -1)) != _CHAIN_ID:
            reasons.append(f"domain: chainId {domain.get('chainId')!r} != {_CHAIN_ID}")
        contract = str(domain.get("verifyingContract", "")).lower()
        if contract not in _V2_VERIFYING_CONTRACTS:
            reasons.append(
                f"domain: verifyingContract {domain.get('verifyingContract')!r} is not a V2 exchange "
                "address (V1 addresses are rejected-value)"
            )

    order_value = fixture.get("order_value")
    if not isinstance(order_value, Mapping):
        reasons.append("signed_order: missing 'order_value'")
    else:
        reasons.extend(
            _exact_set_reasons(
                order_value,
                label="signed_struct",
                required=_SIGNED_ORDER_FIELDS,
                removed=_SIGNED_ORDER_REMOVED,
            )
        )
        sig_type = order_value.get("signatureType")
        if sig_type is not None and int(sig_type) != _SUPPORTED_SIGNATURE_TYPE:
            reasons.append(
                f"signed_struct: signatureType {sig_type!r} is not the R4-A EOA type "
                f"({_SUPPORTED_SIGNATURE_TYPE})"
            )

    return (not reasons, tuple(reasons))


def validate_sendorder_fixture(fixture: Mapping[str, Any]) -> tuple[bool, tuple[str, ...]]:
    """Validate a §2 ``SendOrder`` fixture against the pinned V2 exact-set (fail-closed).

    Enforces the §2a top-level EXACT SET, the §2b order wire-body EXACT SET (removed V1 fields absent,
    no unknown keys), ``signatureType=0`` (EOA only), a known ``orderType``, and the §6 post-only rule
    (post-only is valid ONLY with GTC/GTD). Returns ``(True, ())`` only when nothing fails closed.
    """
    reasons: list[str] = _exact_set_reasons(
        fixture,
        label="sendorder",
        required=_SENDORDER_REQUIRED,
        optional=_SENDORDER_OPTIONAL,
    )

    order_type = fixture.get("orderType", "GTC")
    if order_type not in _ORDER_TYPES:
        reasons.append(f"sendorder: orderType {order_type!r} not in {sorted(_ORDER_TYPES)}")

    post_only = bool(fixture.get("postOnly", False))
    if post_only and order_type not in _RESTING_ORDER_TYPES:
        # §6: postOnly with FOK/FAK → INVALID_POST_ONLY_ORDER_TYPE.
        reasons.append(
            f"sendorder: postOnly is valid only with GTC/GTD, not orderType {order_type!r}"
        )

    order = fixture.get("order")
    if not isinstance(order, Mapping):
        reasons.append("sendorder: missing 'order' object")
    else:
        reasons.extend(
            _exact_set_reasons(
                order,
                label="order",
                required=_ORDER_WIRE_REQUIRED,
                optional=_ORDER_WIRE_OPTIONAL,
                removed=_ORDER_WIRE_REMOVED,
            )
        )
        sig_type = order.get("signatureType")
        if sig_type is not None and int(sig_type) != _SUPPORTED_SIGNATURE_TYPE:
            reasons.append(
                f"order: signatureType {sig_type!r} is not the R4-A EOA type "
                f"({_SUPPORTED_SIGNATURE_TYPE}); POLY_1271 (3) is out of scope"
            )
        side = order.get("side")
        if side not in ("BUY", "SELL"):
            reasons.append(f"order: wire side {side!r} must be the string 'BUY' or 'SELL'")

    return (not reasons, tuple(reasons))


def validate_cancel_response(response: Mapping[str, Any]) -> tuple[bool, tuple[str, ...]]:
    """Validate the §4 ``DELETE /order`` response shape: ``{canceled: list, not_canceled: map}``."""
    reasons: list[str] = []
    if not isinstance(response.get("canceled"), list):
        reasons.append("cancel: 'canceled' must be a list of order ids")
    not_canceled = response.get("not_canceled")
    if not isinstance(not_canceled, Mapping):
        reasons.append("cancel: 'not_canceled' must be a map of orderId -> failure reason")
    return (not reasons, tuple(reasons))


def validate_get_orders_page(page: Mapping[str, Any]) -> tuple[bool, tuple[str, ...]]:
    """Validate a §5 paginated ``get_orders`` page: a ``data``/``orders`` list + ``count`` +
    ``next_cursor``, whose entries match the OpenOrder EXACT SET (fail-closed on any drift).
    """
    reasons: list[str] = []
    orders = page.get("data")
    if orders is None:
        orders = page.get("orders")
    if not isinstance(orders, list):
        reasons.append("get_orders: page must carry a 'data' (or 'orders') list")
        orders = []
    if "count" not in page:
        reasons.append("get_orders: page missing 'count'")
    if "next_cursor" not in page:
        reasons.append("get_orders: page missing 'next_cursor'")
    for idx, order in enumerate(orders):
        if not isinstance(order, Mapping):
            reasons.append(f"get_orders[{idx}]: entry is not an object")
            continue
        reasons.extend(
            _exact_set_reasons(
                order,
                label=f"open_order[{idx}]",
                required=_OPEN_ORDER_FIELDS,
            )
        )
    return (not reasons, tuple(reasons))


# ---------------------------------------------------------------------------
# Operator-gated production smoke — ok=None until an operator runs it (OUT of CI)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProductionSmokeResult:
    """The operator-gated real-venue V2 compatibility smoke result.

    Attributes:
        ok: Tri-state — ``None`` when the operator has NOT run the real check (operator-pending, the
            default), ``True``/``False`` only when an operator actually ran it. NEVER auto-``True``
            and NEVER inferred from the offline fake fixtures.
        detail: Human-readable status.
    """

    ok: bool | None
    detail: str


def operator_production_smoke(
    *, operator_ran: bool = False, operator_result: bool | None = None
) -> ProductionSmokeResult:
    """Return the operator-gated production smoke result (NEVER auto-run in CI, NEVER touches a venue).

    The REAL compatibility check against a live venue is OPERATOR-RUN and OUT of CI. This function is
    only the operator-pending stub / result carrier: until an operator explicitly ran it
    (``operator_ran=True``) and supplied the outcome, the result is ``ok=None`` (pending). It performs
    NO network I/O and infers nothing from the fake tests.

    Args:
        operator_ran: Whether an operator actually executed the real-venue smoke (out of CI).
        operator_result: The operator-observed outcome; consulted ONLY when ``operator_ran`` is True.

    Returns:
        ``ok=None`` while pending; otherwise the operator-supplied ``True``/``False``.
    """
    if not operator_ran:
        return ProductionSmokeResult(
            ok=None,
            detail=(
                "operator-pending: the real-venue CLOB-V2 compatibility smoke is OPERATOR-RUN and "
                "OUT of CI — it has not been run, so ok=None (never auto-True, never inferred from "
                "the offline fixtures)"
            ),
        )
    return ProductionSmokeResult(
        ok=operator_result,
        detail=f"operator-confirmed CLOB-V2 production smoke={operator_result}",
    )


# ---------------------------------------------------------------------------
# Gate result + orchestration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Clobv2GateResult:
    """The CLOB-V2 write-contract compatibility verdict.

    Attributes:
        supported_client: The client/version decision — ``True`` only for the supported V2 client
            (``CLOB_VERSION="2"``); the vendored V1 client fails this.
        client_version: The CLOB version the decision was made against.
        fixtures_match: MACHINE, OFFLINE — every supplied signature/payload fixture validated against
            the pinned V2 exact-set schemas (and the client is supported). ``False`` fails closed.
        cancel_verified: The §4 ``DELETE /order`` response shape validated.
        get_orders_verified: The §5 paginated ``get_orders`` shape validated.
        operator_smoke_ok: The OPERATOR-supplied production-smoke tri-state, copied through VERBATIM.
            ``None`` = operator-pending (default); never inferred from ``machine_ok`` / the fakes.
        reasons: Ordered fail-closed reasons for every machine check that failed (empty when clean).
    """

    supported_client: bool
    client_version: str
    fixtures_match: bool
    cancel_verified: bool
    get_orders_verified: bool
    operator_smoke_ok: bool | None
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def machine_ok(self) -> bool:
        """Every OFFLINE machine signal passed (supported client + fixtures + cancel + get_orders).

        This is the machine half ONLY — it deliberately does NOT consult ``operator_smoke_ok`` so the
        operator-smoke can never be inferred from the machine checks.
        """
        return (
            self.supported_client
            and self.fixtures_match
            and self.cancel_verified
            and self.get_orders_verified
        )

    @property
    def mode_b_admitted(self) -> bool:
        """Mode B may arm ONLY when BOTH the machine fixture-match AND the operator smoke pass.

        Fail-closed: the operator smoke must be EXPLICITLY ``True`` (an operator ran the real check).
        While it is ``None`` (operator-pending) or ``False``, Mode B is DENIED regardless of how many
        offline fake tests pass.
        """
        return self.machine_ok and self.operator_smoke_ok is True


def evaluate_clobv2_gate(
    *,
    client_version: str,
    sendorder_fixtures: Mapping[str, Mapping[str, Any]],
    signed_fixtures: Mapping[str, Mapping[str, Any]] | None = None,
    cancel_response: Mapping[str, Any] | None = None,
    get_orders_page: Mapping[str, Any] | None = None,
    operator_smoke_ok: bool | None = None,
) -> Clobv2GateResult:
    """Evaluate the CLOB-V2 write-contract gate over injected fixtures + the operator smoke tri-state.

    Purely offline: it validates the supplied fixtures against the E3-T0 pinned V2 exact-set schemas
    and copies the operator-supplied ``operator_smoke_ok`` through VERBATIM. It NEVER touches a venue
    and NEVER infers the operator smoke from the machine checks passing.

    Args:
        client_version: The CLOB version to gate on; only :data:`SUPPORTED_CLOB_VERSION` passes.
        sendorder_fixtures: Named §2 SendOrder wire fixtures; ALL must validate.
        signed_fixtures: Named §1 ``{domain, order_value}`` signed fixtures; ALL must validate.
        cancel_response: A §4 ``DELETE /order`` response to shape-verify (``None`` fails that check).
        get_orders_page: A §5 paginated ``get_orders`` page to shape-verify (``None`` fails it).
        operator_smoke_ok: The OPERATOR-supplied production-smoke tri-state (``None`` = pending). It
            is copied through untouched — the machine result never sets it.

    Returns:
        A :class:`Clobv2GateResult` whose ``mode_b_admitted`` is ``True`` only when BOTH the machine
        fixture-match AND an operator-confirmed smoke (``operator_smoke_ok is True``) hold.
    """
    reasons: list[str] = []

    supported_client = client_version == SUPPORTED_CLOB_VERSION
    if not supported_client:
        reasons.append(
            f"client: CLOB version {client_version!r} is not the supported V2 client "
            f"({SUPPORTED_CLOB_VERSION!r}); the vendored V1 client is not production-ready"
        )

    signed_fixtures = signed_fixtures or {}
    fixtures_ok = True
    for name, fixture in sendorder_fixtures.items():
        ok, fx_reasons = validate_sendorder_fixture(fixture)
        if not ok:
            fixtures_ok = False
            reasons.extend(f"sendorder[{name}] {r}" for r in fx_reasons)
    for name, fixture in signed_fixtures.items():
        ok, fx_reasons = validate_signed_order(fixture)
        if not ok:
            fixtures_ok = False
            reasons.extend(f"signed[{name}] {r}" for r in fx_reasons)
    # The machine fixture-match requires a supported client AND clean fixtures — a stale fixture or an
    # unsupported client both fail it closed.
    fixtures_match = supported_client and fixtures_ok

    if cancel_response is None:
        cancel_verified = False
        reasons.append("cancel: no DELETE /order response supplied to verify")
    else:
        cancel_verified, c_reasons = validate_cancel_response(cancel_response)
        reasons.extend(c_reasons)

    if get_orders_page is None:
        get_orders_verified = False
        reasons.append("get_orders: no paginated page supplied to verify")
    else:
        get_orders_verified, g_reasons = validate_get_orders_page(get_orders_page)
        reasons.extend(g_reasons)

    # NOTE (anti-fake invariant): operator_smoke_ok is passed straight through. The gate MUST NOT
    # derive it from the machine checks (e.g. `operator_smoke_ok or machine_ok`) — doing so would let
    # the offline fake fixtures arm Mode B. It stays exactly what the operator supplied.
    return Clobv2GateResult(
        supported_client=supported_client,
        client_version=client_version,
        fixtures_match=fixtures_match,
        cancel_verified=cancel_verified,
        get_orders_verified=get_orders_verified,
        operator_smoke_ok=operator_smoke_ok,
        reasons=tuple(reasons),
    )


# ---------------------------------------------------------------------------
# Fixture loading convenience (offline)
# ---------------------------------------------------------------------------


def load_fixture(name: str, *, fixture_dir: Path | None = None) -> dict[str, Any]:
    """Load a named JSON fixture from the canonical CLOB-V2 fixture directory (E3-T0 §9)."""
    base = fixture_dir or DEFAULT_FIXTURE_DIR
    return json.loads((base / f"{name}.json").read_text())


def evaluate_from_fixture_dir(
    *,
    client_version: str = SUPPORTED_CLOB_VERSION,
    sendorder_names: Sequence[str] = ("sendorder_gtc_eoa", "sendorder_gtd_postonly"),
    signed_names: Sequence[str] = ("order_signed_v2",),
    cancel_name: str = "cancel_response",
    get_orders_name: str = "get_orders_page",
    operator_smoke_ok: bool | None = None,
    fixture_dir: Path | None = None,
) -> Clobv2GateResult:
    """Convenience: run :func:`evaluate_clobv2_gate` over the canonical on-disk fixtures.

    Defaults to the current-official V2 fixtures + an operator-pending smoke, so a plain call returns
    ``machine_ok=True`` but ``mode_b_admitted=False`` (operator-pending) — the honest default.
    """
    return evaluate_clobv2_gate(
        client_version=client_version,
        sendorder_fixtures={n: load_fixture(n, fixture_dir=fixture_dir) for n in sendorder_names},
        signed_fixtures={n: load_fixture(n, fixture_dir=fixture_dir) for n in signed_names},
        cancel_response=load_fixture(cancel_name, fixture_dir=fixture_dir),
        get_orders_page=load_fixture(get_orders_name, fixture_dir=fixture_dir),
        operator_smoke_ok=operator_smoke_ok,
    )
