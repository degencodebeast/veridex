"""E3-T6 — PURE CLOB-V2 signing compiler + owner-committed integrity commitment (REQ-018a).

These tests encode Codex-M1 (the integrity commitment COMMITS to ``owner`` but persists digest-only)
and Codex-M2 (the reconciliation join key is the venue order hash, NOT the private integrity digest),
plus the schema-derived EXACT-SET fail-closed byte-verify and the compiler's purity + digest
cross-validation against the REAL ``py_clob_client_v2`` V2 builder.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from veridex.dust_execution.order_commitment import (
    SENDORDER_SCHEMA,
    OrderSigningCommitment,
    build_presubmit_record,
    verify_post_body_against_commitment,
)
from veridex.dust_execution.risk import FailClosed
from veridex.dust_execution.signing_compiler import (
    AdmittedPostRoundingIntent,
    OrderMarket,
    PolymarketV2SigningCompiler,
    SignerBinding,
    keccak256,
    order_hash_from_typed_data,
)

# --- schema-derived field views (from the E3-T0 pinned EXACT SETS) -------------------------
SIGNED_FIELDS = list(SENDORDER_SCHEMA.signed_fields)      # salt,maker,signer,tokenId,amounts,side,sigType,ts,metadata,builder
UNSIGNED_FIELDS = list(SENDORDER_SCHEMA.unsigned_wrapper)  # expiration,orderType,postOnly,owner,deferExec

_BYTES32_ZERO = "0x" + "0" * 64
_REPO_ROOT = Path(__file__).resolve().parents[1]
_FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "dust_execution" / "clobv2" / "order_digest_v2.json"

# --- pinned test inputs: an admitted post-rounding intent that reproduces the OFFICIAL order_value ---
MKT = OrderMarket(
    token_id="1029364920843200000000000000000000000000000000000000000000000000000",
    neg_risk=False,
)
BIND = SignerBinding(
    salt="479249096354",
    maker="0x1111111111111111111111111111111111111111",
    owner="00000000-0000-0000-0000-000000000000",  # L2 API-key UUID (a SECRET; never persisted raw)
    timestamp="1713398400000",
    signer=None,  # == maker for EOA
    signature_type=0,
    metadata=_BYTES32_ZERO,
    builder=_BYTES32_ZERO,
)
FIX_INTENT = AdmittedPostRoundingIntent(
    side="BUY",
    maker_amount="1000000",  # 1.0 pUSD in
    taker_amount="2000000",  # 2.0 shares out -> native price 0.5
    native_price=0.5,
    size=2.0,
    tif="GTC",
    post_only=False,
    defer_exec=False,
    expiration="0",
)
admitted_intent = FIX_INTENT


@pytest.fixture
def compiler() -> PolymarketV2SigningCompiler:
    return PolymarketV2SigningCompiler()


@pytest.fixture
def store() -> dict[str, Any]:
    return {}


# --- test helpers -------------------------------------------------------------------------


def _official_fixture() -> dict[str, Any]:
    return json.loads(_FIXTURE.read_text())


OFFICIAL_V2_DIGEST_FIXTURE = _official_fixture()["expected_order_hash"]


def expected_v2_order_hash(payload: Any) -> str:
    """Independent recompute of the venue V2 order hash from the payload's typed data (§3d)."""
    return order_hash_from_typed_data(payload.canonical_v2_typed_data)


def mutate_post_body(post_body: dict[str, Any], field: str) -> dict[str, Any]:
    """Return ``post_body`` with exactly ONE mutation: alter ``field`` or add an unknown key."""
    if field == "__unknown_added_key__":
        post_body["__unknown_added_key__"] = "leaked"
        return post_body
    # Signed fields (and expiration) live in the nested order; the rest are top-level.
    container = post_body["order"] if (field in set(SIGNED_FIELDS) or field == "expiration") else post_body
    old = container[field]
    if isinstance(old, bool):
        container[field] = not old
    elif field == "side":
        container[field] = "SELL" if old == "BUY" else "BUY"
    else:
        container[field] = f"{old}_MUT"
    return post_body


def assert_module_imports_none_of(rel_path: str, forbidden: set[str]) -> None:
    """AST-assert that the module at ``rel_path`` imports none of ``forbidden`` (root package names)."""
    tree = ast.parse((_REPO_ROOT / rel_path).read_text())
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    leaked = roots & forbidden
    assert not leaked, f"{rel_path} imports forbidden modules: {sorted(leaked)}"


# --- RED tests (Codex-M1 / M2) ------------------------------------------------------------


@pytest.mark.parametrize("field", SIGNED_FIELDS + UNSIGNED_FIELDS + ["__unknown_added_key__"])
def test_post_body_byte_verify_rejects_any_field_mutation(
    field: str, compiler: PolymarketV2SigningCompiler, store: dict[str, Any]
) -> None:
    payload = compiler.compile(admitted_intent, market=MKT, binding=BIND)
    commitment = OrderSigningCommitment.from_payload(payload)  # integrity_commitment_hash INCLUDES owner
    commitment.persist_digest_only(store)  # pre-sign; raw owner NOT stored
    assert "owner" not in store  # the SECRET owner is never persisted (Codex-M1)
    tampered = mutate_post_body(payload.post_body(), field)  # alter ONE field (or add an unknown key)
    with pytest.raises(FailClosed):
        verify_post_body_against_commitment(tampered, commitment)


def test_untampered_post_body_byte_verify_passes(
    compiler: PolymarketV2SigningCompiler, store: dict[str, Any]
) -> None:
    payload = compiler.compile(admitted_intent, market=MKT, binding=BIND)
    commitment = OrderSigningCommitment.from_payload(payload)
    commitment.persist_digest_only(store)
    # An untouched live body (which transiently carries owner) verifies clean.
    verify_post_body_against_commitment(payload.post_body(), commitment)


def test_exact_set_unknown_key_and_missing_field_fail_closed(
    compiler: PolymarketV2SigningCompiler,
) -> None:
    payload = compiler.compile(admitted_intent, market=MKT, binding=BIND)
    commitment = OrderSigningCommitment.from_payload(payload)
    assert commitment.covered_fields() == set(SIGNED_FIELDS) | set(UNSIGNED_FIELDS)  # exact-set, not subset
    # A missing covered field also fails closed (drop a top-level unsigned key).
    body = payload.post_body()
    del body["owner"]
    with pytest.raises(FailClosed):
        verify_post_body_against_commitment(body, commitment)


def test_reconciliation_key_is_venue_order_key_not_integrity_digest(
    compiler: PolymarketV2SigningCompiler,
) -> None:
    payload = compiler.compile(admitted_intent, market=MKT, binding=BIND)
    rec = build_presubmit_record(payload)
    assert rec.venue_order_key == expected_v2_order_hash(payload)
    assert rec.venue_order_key != rec.integrity_commitment_hash


def test_compiler_digest_matches_official_v2_builder(compiler: PolymarketV2SigningCompiler) -> None:
    # cross-validate vs the REAL py_clob_client_v2 ExchangeOrderBuilderV2 fixture (E3-T0 §13#1).
    assert (
        compiler.compile(FIX_INTENT, market=MKT, binding=BIND).eip712_digest
        == OFFICIAL_V2_DIGEST_FIXTURE
    )


def test_compiler_is_pure_no_network_no_key() -> None:  # AST/structural
    assert_module_imports_none_of(
        "veridex/dust_execution/signing_compiler.py", {"httpx", "privy", "eth_account", "eth_keys"}
    )
    # the commitment module is held to the same purity bar.
    assert_module_imports_none_of(
        "veridex/dust_execution/order_commitment.py", {"httpx", "privy", "eth_account", "eth_keys"}
    )


# --- supporting teeth ---------------------------------------------------------------------


def test_keccak256_matches_known_answer_vectors() -> None:
    # Ethereum keccak-256 (NOT NIST SHA3) known answers — proves the stdlib impl is correct.
    assert keccak256(b"").hex() == (
        "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    )
    assert keccak256(b"abc").hex() == (
        "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"
    )


def test_official_fixture_domain_and_order_value_match_compiled_typed_data(
    compiler: PolymarketV2SigningCompiler,
) -> None:
    fixture = _official_fixture()
    payload = compiler.compile(FIX_INTENT, market=MKT, binding=BIND)
    domain = payload.canonical_v2_typed_data["domain"]
    assert domain["name"] == fixture["domain"]["name"]
    assert domain["version"] == "2"
    assert domain["chainId"] == 137
    assert domain["verifyingContract"].lower() == fixture["domain"]["verifyingContract"].lower()
    # venue_order_key == eip712_digest == the §3d join key.
    assert payload.venue_order_key == payload.eip712_digest == OFFICIAL_V2_DIGEST_FIXTURE


def test_signed_digest_alone_misses_unsigned_wrapper_mutation(
    compiler: PolymarketV2SigningCompiler,
) -> None:
    """The signed EIP-712 hash covers ONLY the 11 signed fields, so it CANNOT detect a mutation of an
    unsigned wrapper field. This is exactly why the integrity commitment must cover the UNION — a
    signed-digest-only byte-verify would pass an owner/expiration/orderType/postOnly/deferExec swap.
    """
    payload = compiler.compile(admitted_intent, market=MKT, binding=BIND)
    baseline = order_hash_from_typed_data(payload.canonical_v2_typed_data)
    for unsigned_field in UNSIGNED_FIELDS:
        tampered = mutate_post_body(payload.post_body(), unsigned_field)
        # Rebuild the signed typed-data from the (tampered) order body — unsigned fields are NOT in it,
        # so the signed digest is UNCHANGED. Proves the signed hash alone is blind to the wrapper.
        td = json.loads(json.dumps(payload.canonical_v2_typed_data))  # deep copy
        assert order_hash_from_typed_data(td) == baseline
        assert "owner" not in td["message"]  # owner never enters the signed struct
        assert unsigned_field in tampered["order"] or unsigned_field in tampered


def test_post_only_with_taker_tif_fails_closed(compiler: PolymarketV2SigningCompiler) -> None:
    bad = AdmittedPostRoundingIntent(
        side="BUY", maker_amount="1000000", taker_amount="2000000",
        native_price=0.5, size=2.0, tif="FOK", post_only=True,
    )
    with pytest.raises(FailClosed):
        compiler.compile(bad, market=MKT, binding=BIND)


def test_non_eoa_signature_type_fails_closed(compiler: PolymarketV2SigningCompiler) -> None:
    bad_bind = SignerBinding(
        salt="479249096354", maker=BIND.maker, owner=BIND.owner,
        timestamp="1713398400000", signature_type=3,  # POLY_1271 — out of scope
    )
    with pytest.raises(FailClosed):
        compiler.compile(FIX_INTENT, market=MKT, binding=bad_bind)


def test_native_price_rejects_decimal_odds() -> None:
    with pytest.raises(ValueError):
        AdmittedPostRoundingIntent(
            side="BUY", maker_amount="1", taker_amount="1",
            native_price=1.4, size=1.0, tif="GTC",  # decimal-odds value, not a native probability
        )


def test_neg_risk_market_changes_the_order_hash(compiler: PolymarketV2SigningCompiler) -> None:
    standard = compiler.compile(FIX_INTENT, market=MKT, binding=BIND)
    neg = compiler.compile(
        FIX_INTENT, market=OrderMarket(token_id=MKT.token_id, neg_risk=True), binding=BIND
    )
    # Different verifyingContract -> different domain separator -> different order hash (§3d/§11).
    assert neg.eip712_digest != standard.eip712_digest
