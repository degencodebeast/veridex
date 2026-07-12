"""E3-T7 (REQ-018a/b/b2/c/e, AC-042/043/046/047) — Privy typed-data control plane.

MONEY-NETWORK BOUNDARY. Every Privy interaction here is a RECORDING-FAKE: NO live Privy, NO real
credentials, NO network, NO local signing key in the money path. The fake "enclave" signs with a
pure-stdlib secp256k1 signer that stands in for Privy's REMOTE enclave — it is test scaffolding, not a
Mode-B module, and the control plane itself never holds a key.

Proven here:

* typed-data-ONLY default-deny: ``sign_raw_hash`` (secp256k1_sign) and ``send_transaction``
  (eth_sendTransaction) fail closed at custody; only ``eth_signTypedData_v4`` signs;
* recover-and-require: the recovered signer MUST equal both ``binding.wallet_address`` and the compiled
  order's ``signer``;
* policy CONTENT hash + quorum ownership pinned in the binding, verified at arming;
* the explicit ``execution_wallet_binding_hash`` manifest field (no reroute);
* auth MECHANICS: signed request-expiry replay guard, quorum signature SET, idempotency on mutations;
* the FIVE no-local-key controls (AST denylist over the whole Mode-B set, factory-shape, runtime
  constructor-poison, object-graph, outage-freeze-never-local).
"""

from __future__ import annotations

import ast
import contextlib
import importlib
import inspect
import time
from typing import Any

import pytest

import veridex.dust_execution
from tests.test_no_r3_r4_code import _walk_modnames
from veridex.dust_execution.privy_control_plane import (
    TYPED_DATA_ONLY,
    PrivyAuthContext,
    PrivyEvmWalletControlPlane,
    arm_mode_b,
    build_mode_b,
    execute_with,
    pin_manifest,
)
from veridex.dust_execution.risk import FailClosed
from veridex.dust_execution.signing_compiler import (
    AdmittedPostRoundingIntent,
    OrderMarket,
    PolymarketV2SigningCompiler,
    SignerBinding,
    keccak256,
)
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

# ---------------------------------------------------------------------------
# Pure-stdlib secp256k1 SIGNER — stands in for Privy's remote enclave (test scaffolding only).
# Mirrors the recover side in privy_control_plane; the round-trip is validated by the tests below.
# ---------------------------------------------------------------------------

_P = 2**256 - 2**32 - 977
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
_GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8
_G = (_GX, _GY)


def _inv(a: int, m: int) -> int:
    return pow(a % m, m - 2, m)


def _add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and (y1 + y2) % _P == 0:
        return None
    lam = (
        (3 * x1 * x1) * _inv(2 * y1, _P) % _P
        if p1 == p2
        else (y2 - y1) * _inv((x2 - x1) % _P, _P) % _P
    )
    x3 = (lam * lam - x1 - x2) % _P
    y3 = (lam * (x1 - x3) - y1) % _P
    return (x3, y3)


def _mul(k: int, p):
    r = None
    k %= _N
    while k:
        if k & 1:
            r = _add(r, p)
        p = _add(p, p)
        k >>= 1
    return r


def _address_of(priv: int) -> str:
    x, y = _mul(priv, _G)
    return "0x" + keccak256(x.to_bytes(32, "big") + y.to_bytes(32, "big"))[-20:].hex()


def _sign(priv: int, digest: bytes) -> bytes:
    z = int.from_bytes(digest, "big")
    import hashlib

    k = (int.from_bytes(hashlib.sha256(priv.to_bytes(32, "big") + digest).digest(), "big") % (_N - 1)) + 1
    while True:
        big_r = _mul(k, _G)
        r = big_r[0] % _N
        if r == 0:
            k += 1
            continue
        s = (_inv(k, _N) * (z + r * priv)) % _N
        if s == 0:
            k += 1
            continue
        rec_id = (big_r[1] & 1) | (((big_r[0] >= _N) & 1) << 1)
        if s > _N // 2:
            s = _N - s
            rec_id ^= 1
        return r.to_bytes(32, "big") + s.to_bytes(32, "big") + bytes([27 + rec_id])


# The enclave's private key + its EVM address (the ONE wallet address the recovered signer must equal).
_ENCLAVE_PRIV = 0xA11CE
_WALLET_ADDRESS = _address_of(_ENCLAVE_PRIV)
# A different key/address to prove custody mismatch fails closed.
_OTHER_PRIV = 0xB0B
_OTHER_ADDRESS = _address_of(_OTHER_PRIV)


# ---------------------------------------------------------------------------
# Compiled payload + bindings + auth fixtures
# ---------------------------------------------------------------------------


def _compile(signer_address: str):
    compiler = PolymarketV2SigningCompiler()
    intent = AdmittedPostRoundingIntent(
        side="BUY",
        maker_amount="1000000",
        taker_amount="2000000",
        native_price=0.5,
        size=1.0,
        tif="GTC",
    )
    market = OrderMarket(token_id="123456789", neg_risk=False)
    binding = SignerBinding(
        salt="1",
        maker=signer_address,
        owner="l2-api-key-uuid-SECRET",
        timestamp="1700000000",
        signer=signer_address,
    )
    return compiler.compile(intent, market=market, binding=binding)


COMPILED = _compile(_WALLET_ADDRESS)

_CORRECT_QUORUM = AuthorizationQuorum(
    quorum_ref="quorum-1", authorization_key_refs=("authkey-A", "authkey-B"), threshold=2
)

BIND = ExecutionWalletBinding(
    provider="privy",
    wallet_ref="wallet-ref-1",
    wallet_address=_WALLET_ADDRESS,
    chain_id=CHAIN_ID_POLYGON,
    venue="polymarket",
    privy_policy_content_hash=TYPED_DATA_ONLY.content_hash(),
    authorization_quorum_ref="quorum-1",
    authorization_quorum_content_hash=_CORRECT_QUORUM.content_hash(),
    quorum_threshold=2,
)

# A binding whose bound address is WRONG (recover-and-require must reject it).
BIND_WRONG_ADDR = ExecutionWalletBinding(
    provider="privy",
    wallet_ref="wallet-ref-1",
    wallet_address=_OTHER_ADDRESS,
    chain_id=CHAIN_ID_POLYGON,
    venue="polymarket",
    privy_policy_content_hash=TYPED_DATA_ONLY.content_hash(),
    authorization_quorum_ref="quorum-1",
    authorization_quorum_content_hash=_CORRECT_QUORUM.content_hash(),
    quorum_threshold=2,
)

BIND_A = BIND
BIND_B = ExecutionWalletBinding(
    provider="privy",
    wallet_ref="wallet-ref-2",  # different wallet → different binding hash
    wallet_address=_OTHER_ADDRESS,
    chain_id=CHAIN_ID_POLYGON,
    venue="polymarket",
    privy_policy_content_hash=TYPED_DATA_ONLY.content_hash(),
    authorization_quorum_ref="quorum-1",
    authorization_quorum_content_hash=_CORRECT_QUORUM.content_hash(),
    quorum_threshold=2,
)


def _future_ms() -> int:
    return int(time.time() * 1000) + 10_000_000


def _past_ms() -> int:
    return int(time.time() * 1000) - 10_000_000


CTX = PrivyAuthContext(
    request_expiry_ms=_future_ms(),
    quorum_signatures=("sig-A", "sig-B"),
    quorum_threshold=2,
    idempotency_key="idem-1",
)
EXPIRED_EXPIRY = PrivyAuthContext(
    request_expiry_ms=_past_ms(),
    quorum_signatures=("sig-A", "sig-B"),
    quorum_threshold=2,
    idempotency_key="idem-1",
)
BELOW_QUORUM = PrivyAuthContext(
    request_expiry_ms=_future_ms(),
    quorum_signatures=("sig-A",),  # one signer, threshold 2 → below quorum
    quorum_threshold=2,
    idempotency_key="idem-1",
)

ANY_32B = b"\x11" * 32
ANY_TX = {"to": "0xdead", "value": 1}
K = "mint-idem-key-1"


# ---------------------------------------------------------------------------
# Recording-fake Privy clients (NEVER a live Privy client)
# ---------------------------------------------------------------------------


class PolicyFakePrivy:
    """A recording-fake Privy client: records calls, signs typed data via the stand-in enclave key."""

    def __init__(self, *, policy: PrivyWalletPolicy = TYPED_DATA_ONLY, priv: int = _ENCLAVE_PRIV) -> None:
        self._policy = policy
        self._priv = priv
        self.sign_calls: list[dict[str, Any]] = []
        self._wallets: dict[str, object] = {}

    @property
    def policy(self) -> PrivyWalletPolicy:
        return self._policy

    def sign_typed_data(self, *, wallet_ref: str, typed_data: dict[str, Any], auth: PrivyAuthContext):
        self.sign_calls.append({"wallet_ref": wallet_ref, "auth": auth})
        # The remote enclave signs the EIP-712 digest of the typed data (recomputed here independently).
        from veridex.dust_execution.signing_compiler import order_hash_from_typed_data

        digest_hex = order_hash_from_typed_data(typed_data)
        sig = _sign(self._priv, bytes.fromhex(digest_hex[2:]))
        return {"method": ALLOWED_SIGN_METHOD, "data": {"signature": "0x" + sig.hex(), "encoding": "hex"}}

    def mint_wallet(self, *, idempotency_key: str) -> object:
        # A distinct object each raw call; idempotency is enforced by the control plane cache.
        return object()


class OutageFakePrivy(PolicyFakePrivy):
    """A recording-fake whose enclave is DOWN: signing raises a transient/outage error."""

    def sign_typed_data(self, *, wallet_ref: str, typed_data: dict[str, Any], auth: PrivyAuthContext):
        raise ConnectionError("privy enclave unreachable (simulated outage)")


def OutageFakePrivy_cp() -> PrivyEvmWalletControlPlane:
    return PrivyEvmWalletControlPlane(client=OutageFakePrivy(), binding=BIND)


# Policy variants for the arming checks.
WEAKENED_POLICY = PrivyWalletPolicy(
    rules=(
        PolicyRule(method=ALLOWED_SIGN_METHOD, primary_type=ORDER_PRIMARY_TYPE, effect="ALLOW"),
        PolicyRule(method=ALLOWED_SIGN_METHOD, primary_type=CLOB_AUTH_PRIMARY_TYPE, effect="ALLOW"),
        PolicyRule(method="secp256k1_sign", primary_type="*", effect="ALLOW"),  # WEAKENED: raw signing allowed
    ),
    default_action="DENY",
    owner_type="quorum",
)
# Same content as TYPED_DATA_ONLY (content hash matches) but the resource is app-secret-updatable.
POLICY_NOT_QUORUM_OWNED = PrivyWalletPolicy(
    rules=TYPED_DATA_ONLY.rules,
    default_action="DENY",
    owner_type="app_secret",
)
QUORUM_CONTENT_HASH_MISMATCH = AuthorizationQuorum(
    quorum_ref="quorum-1", authorization_key_refs=("authkey-A",), threshold=1  # threshold lowered → weakened
)


def _cp() -> PrivyEvmWalletControlPlane:
    return PrivyEvmWalletControlPlane(client=PolicyFakePrivy(), binding=BIND)


# ---------------------------------------------------------------------------
# TESTS
# ---------------------------------------------------------------------------


def test_typed_data_only_default_deny_blocks_send_tx_and_raw_hash():
    cp = PrivyEvmWalletControlPlane(client=PolicyFakePrivy(policy=TYPED_DATA_ONLY))
    with pytest.raises(FailClosed):
        cp.sign_raw_hash(ANY_32B)  # secp256k1_sign not allowed
    with pytest.raises(FailClosed):
        cp.send_transaction(ANY_TX)  # eth_sendTransaction denied at custody
    assert cp.sign_typed_data(COMPILED, binding=BIND, auth=CTX).method == "eth_signTypedData_v4"


def test_policy_content_hash_mismatch_fails_closed():
    # ref unchanged, content weakened → caught at arming.
    with pytest.raises(FailClosed):
        arm_mode_b(binding=BIND, live_policy=WEAKENED_POLICY)


def test_policy_resource_and_quorum_must_be_owned():
    # v0.6.3 Codex-m1 / Fable-m5: an app-secret-updatable policy is not real custody.
    with pytest.raises(FailClosed):
        arm_mode_b(binding=BIND, live_policy=POLICY_NOT_QUORUM_OWNED)
    with pytest.raises(FailClosed):
        arm_mode_b(binding=BIND, live_quorum=QUORUM_CONTENT_HASH_MISMATCH)


def test_binding_hash_is_explicit_manifest_field_no_reroute():
    m = pin_manifest(binding=BIND_A)
    assert m.execution_wallet_binding_hash == BIND_A.binding_hash()
    with pytest.raises(FailClosed):
        execute_with(m, live_binding=BIND_B)


def test_recovered_signer_must_equal_binding_and_order_signer():
    cp = _cp()
    with pytest.raises(FailClosed):
        cp.sign_typed_data(COMPILED, binding=BIND_WRONG_ADDR, auth=CTX)


def test_auth_mechanics_expiry_quorum_idempotency():
    cp = _cp()
    with pytest.raises(FailClosed):
        cp.sign_typed_data(COMPILED, binding=BIND, auth=EXPIRED_EXPIRY)
    with pytest.raises(FailClosed):
        cp.sign_typed_data(COMPILED, binding=BIND, auth=BELOW_QUORUM)
    assert cp.mint_wallet(idempotency_key=K) is cp.mint_wallet(idempotency_key=K)  # idempotent on MUTATIONS


# ---- The FIVE no-local-key controls -----------------------------------------------------------

_LOCAL_KEY_BANNED = {"eth_account", "eth_keys", "coincurve", "web3", "UtilsSigner", "Account"}

# The WHOLE Mode-B module set (closure over the process, not one module): the dust-execution package
# (privy_control_plane, signing_compiler, order_commitment, wallet_binding, keyless_read_client, and any
# future l2_transport / runner Mode-B path) PLUS the venue preflight that gates arming.
_MODE_B_MODULES = sorted(
    set(_walk_modnames(veridex.dust_execution, "veridex.dust_execution."))
    | {"veridex.venues.polymarket_preflight"}
)
# Sanity: the named load-bearing modules are actually in the scanned set.
for _required in (
    "veridex.dust_execution.privy_control_plane",
    "veridex.dust_execution.signing_compiler",
    "veridex.dust_execution.order_commitment",
    "veridex.dust_execution.wallet_binding",
    "veridex.dust_execution.keyless_read_client",
    "veridex.venues.polymarket_preflight",
):
    assert _required in _MODE_B_MODULES, f"Mode-B module set is missing {_required}"

MODE_B_MODULES = _MODE_B_MODULES


def _imported_names(modname: str) -> set[str]:
    """Every imported module path AND imported symbol name in ``modname`` (AST — code, not prose)."""
    mod = importlib.import_module(modname)
    tree = ast.parse(inspect.getsource(mod))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
                names.add(alias.name.split(".")[0])
                if alias.asname:
                    names.add(alias.asname)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
                names.add(node.module.split(".")[0])
            for alias in node.names:
                names.add(alias.name)
                if alias.asname:
                    names.add(alias.asname)
    return names


def assert_no_imports(modname: str, banned: set[str]) -> None:
    offenders = _imported_names(modname) & banned
    assert not offenders, f"{modname} imports banned local-key surface(s): {sorted(offenders)}"


def _factory_params(fn) -> set[str]:
    return set(inspect.signature(fn).parameters)


def _iter_object_graph(root: object, *, max_nodes: int = 5000):
    seen: set[int] = set()
    stack = [root]
    while stack and len(seen) < max_nodes:
        obj = stack.pop()
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        yield obj
        d = getattr(obj, "__dict__", None)
        if isinstance(d, dict):
            for value in d.values():
                if isinstance(value, (str, bytes, int, float, bool, type(None))):
                    continue
                if isinstance(value, dict):
                    stack.extend(v for v in value.values())
                elif isinstance(value, (list, tuple, set)):
                    stack.extend(value)
                else:
                    stack.append(value)


def no_local_signer_in_object_graph(cp: object) -> bool:
    """True iff no object reachable from ``cp`` is a local-signer type from a banned crypto library."""
    for obj in _iter_object_graph(cp):
        module = type(obj).__module__ or ""
        top = module.split(".")[0]
        if top in {"eth_account", "eth_keys", "coincurve", "web3"}:
            return False
        if type(obj).__name__ in {"LocalAccount", "PrivateKey", "Account", "UtilsSigner"}:
            return False
    return True


@contextlib.contextmanager
def poison_all_local_key_ctors():
    """Poison every known local-key constructor so ANY use in the money path would explode.

    The Mode-B path imports none of these, so the full compile→sign→post completes untripped.
    """
    patched: list[tuple[Any, str, Any]] = []

    def _poison(module_name: str, attrs: tuple[str, ...]) -> None:
        try:
            mod = importlib.import_module(module_name)
        except Exception:
            return
        for attr in attrs:
            target = getattr(mod, attr, None)
            if target is None:
                continue
            patched.append((mod, attr, target))

            def _boom(*_a, _attr=attr, **_k):
                raise AssertionError(f"local-key constructor {module_name}.{_attr} was invoked in the money path")

            setattr(mod, attr, _boom)

    _poison("eth_account", ("Account",))
    _poison("eth_keys", ("keys",))
    try:
        yield
    finally:
        for mod, attr, original in patched:
            setattr(mod, attr, original)


def run_full_compile_sign_post(cp: PrivyEvmWalletControlPlane) -> dict[str, Any]:
    """Exercise the full Mode-B path: compile → typed-data sign → build the POST body (no local key)."""
    compiled = _compile(_WALLET_ADDRESS)
    sig = cp.sign_typed_data(compiled, binding=BIND, auth=CTX)
    body = compiled.post_body()
    body["order"]["signature"] = sig.signature
    return body


def test_five_no_local_key_controls():
    # (1) STATIC AST denylist over the WHOLE Mode-B module set (Fable-m4) — not just one module.
    for m in MODE_B_MODULES:
        assert_no_imports(m, _LOCAL_KEY_BANNED)
    # (2) FACTORY-SHAPE: the Mode-B factory has no key/signer parameter.
    assert _factory_params(build_mode_b) <= {"binding", "privy_client", "l2_creds", "http"}
    # (3) RUNTIME constructor-poison: the full compile→sign→post completes untripped.
    with poison_all_local_key_ctors():
        run_full_compile_sign_post(_cp())
    # (4) OBJECT-GRAPH: no local signer reachable from the control plane.
    assert no_local_signer_in_object_graph(_cp())
    # (5) OUTAGE-FREEZE, NEVER local: a provider outage fails closed (never a local-key fallback).
    with pytest.raises(FailClosed):
        OutageFakePrivy_cp().sign_typed_data(COMPILED, binding=BIND, auth=CTX)
