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


# ===============================================================================================
# E3-T8 — L1 ClobAuth cred lifecycle + keyless L2 HMAC transport (live commitment wiring) +
# operator Privy preflight + pUSD provisioning gate + POLY_* scrubbing (REQ-018a/d/f/f2/g).
# ===============================================================================================

import base64  # noqa: E402
import copy  # noqa: E402
import json  # noqa: E402

from veridex.dust_execution import l2_transport as l2t  # noqa: E402
from veridex.dust_execution.l2_transport import (  # noqa: E402
    POLY_L2_HEADER_NAMES,
    InMemoryPreSubmitStore,
    KeylessL2Transport,
    build_l2_headers,
    reconcile_ack_lost,
    scrub_headers_for_output,
    scrub_l2_output,
)
from veridex.dust_execution.privy_control_plane import (  # noqa: E402
    CLOB_AUTH_PRIMARY_TYPE,
    L2ApiCredentials,
    build_clob_auth_typed_data,
    mint_l2_credentials,
    operator_privy_preflight,
    operator_pusd_provisioning_preflight,
)
from veridex.dust_execution.signing_compiler import order_hash_from_typed_data  # noqa: E402

# --- UNIQUE fake secrets (Codex-M3 store-scan) — never collide with a real value ------------------
_SECRET_OWNER = "UNIQUE-OWNER-UUID-Zzq9Kx7-SECRET-DO-NOT-PERSIST"
_SECRET_API_SECRET = base64.urlsafe_b64encode(b"UNIQUE-HMAC-SECRET-Xy7v-DO-NOT-PERSIST").decode()
_SECRET_PASSPHRASE = "UNIQUE-PASSPHRASE-Q42w-SECRET-DO-NOT-PERSIST"


def _compile_owner(owner: str, *, maker_amount: str = "1000000", taker_amount: str = "2000000",
                   token_id: str = "123456789"):
    compiler = PolymarketV2SigningCompiler()
    intent = AdmittedPostRoundingIntent(
        side="BUY", maker_amount=maker_amount, taker_amount=taker_amount,
        native_price=0.5, size=1.0, tif="GTC",
    )
    market = OrderMarket(token_id=token_id, neg_risk=False)
    binding = SignerBinding(
        salt="1", maker=_WALLET_ADDRESS, owner=owner, timestamp="1700000000", signer=_WALLET_ADDRESS,
    )
    return compiler.compile(intent, market=market, binding=binding)


COMPILED_L2 = _compile_owner(_SECRET_OWNER)

_CREDS = L2ApiCredentials(
    api_key=_SECRET_OWNER, api_secret=_SECRET_API_SECRET, api_passphrase=_SECRET_PASSPHRASE,
    derivation_ref="wallet-ref-1:0", derivation_nonce=0,
)


class L2FakePrivy(PolicyFakePrivy):
    """A recording-fake Privy that signs BOTH the V2 Order and the L1 ClobAuth domains."""

    def __init__(self, *, events: list[str] | None = None, priv: int = _ENCLAVE_PRIV) -> None:
        super().__init__(priv=priv)
        self._events = events

    def sign_typed_data(self, *, wallet_ref: str, typed_data: dict[str, Any], auth: PrivyAuthContext):
        pt = typed_data.get("primaryType")
        self.sign_calls.append({"wallet_ref": wallet_ref, "auth": auth, "primary_type": pt})
        if pt == "Order":
            if self._events is not None:
                self._events.append("sign")
            digest_hex = order_hash_from_typed_data(typed_data)
        elif pt == "ClobAuth":
            from veridex.dust_execution.privy_control_plane import clob_auth_hash_from_typed_data
            digest_hex = clob_auth_hash_from_typed_data(typed_data)
        else:  # pragma: no cover - the control plane must never route a disallowed domain here
            raise AssertionError(f"fake asked to sign disallowed primaryType {pt!r}")
        sig = _sign(self._priv, bytes.fromhex(digest_hex[2:]))
        return {"method": ALLOWED_SIGN_METHOD, "data": {"signature": "0x" + sig.hex(), "encoding": "hex"}}


class _LoggingStore(InMemoryPreSubmitStore):
    """An append-only store that logs a 'persist' event so persist-before-sign is provable."""

    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self._events = events

    def append_presubmit(self, record) -> None:
        self._events.append("persist")
        super().append_presubmit(record)


class _RecordingHttp:
    """A recording-fake async HTTP transport (never a live venue)."""

    def __init__(self) -> None:
        self.received: dict[str, Any] | None = None

    async def post(self, *, path: str, headers: dict[str, str], body: bytes) -> dict[str, Any]:
        self.received = {"path": path, "headers": dict(headers), "body": body}
        return {
            "success": True, "orderID": "0xVENUEACK", "status": "live",
            "makingAmount": "", "takingAmount": "", "transactionsHashes": [], "tradeIDs": [],
            "errorMsg": "",
        }


class _FakeClobAuthClient:
    """A recording-fake L1 CLOB-V2 auth client returning deterministic ApiCreds."""

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def create_or_derive_api_key(self, *, l1_headers: dict[str, str]) -> dict[str, str]:
        self.calls.append(l1_headers)
        return {
            "api_key": _SECRET_OWNER, "api_secret": _SECRET_API_SECRET,
            "api_passphrase": _SECRET_PASSPHRASE,
        }


def _transport(*, events: list[str], http: _RecordingHttp, store: _LoggingStore) -> KeylessL2Transport:
    return KeylessL2Transport(
        control_plane=PrivyEvmWalletControlPlane(client=L2FakePrivy(events=events), binding=BIND),
        creds=_CREDS, http=http, store=store, now_s=lambda: 1_700_000_123,
    )


def _store_scan_text(store) -> str:
    """Everything in the append-only store, stringified — for the UNIQUE-secret store-scan."""
    return repr(store.list_presubmit())


async def test_live_submit_path_persists_compound_presubmit_presign_and_byte_verifies_wire_body(
    monkeypatch,
):
    events: list[str] = []
    http = _RecordingHttp()
    store = _LoggingStore(events)
    transport = _transport(events=events, http=http, store=store)

    # Spy the byte-verify AT ITS l2_transport call site (proves the commitment control is WIRED here,
    # not in a helper elsewhere — Fable-MAJOR-1). The spy records the exact post_body it verified.
    captured: dict[str, Any] = {"called": False}
    real_verify = l2t.verify_post_body_against_commitment

    def _spy(post_body, commitment):
        captured["called"] = True
        captured["post_body"] = copy.deepcopy(post_body)
        return real_verify(post_body, commitment)

    monkeypatch.setattr(l2t, "verify_post_body_against_commitment", _spy)

    result = await transport.submit_live_order(COMPILED_L2, binding=BIND, auth=CTX)

    rows = store.list_presubmit()
    assert len(rows) == 1

    # (c) NO raw owner / L2 creds / signature in the persisted row (UNIQUE-secret store-scan, Codex-M3).
    #     Checked FIRST so a raw-wrapper persist (leaking owner) is caught here specifically.
    scan = _store_scan_text(store)
    eth_signature = json.loads(result.wire_bytes)["order"]["signature"]
    for secret in (_SECRET_OWNER, _SECRET_API_SECRET, _SECRET_PASSPHRASE, eth_signature):
        assert secret not in scan

    # (a) the COMPOUND PreSubmitRecord (non-null venue_order_key == official V2 order hash, DISTINCT
    #     from the private integrity digest) is in the append-only store BEFORE the sign call.
    rec = rows[0]
    assert rec.venue_order_key == COMPILED_L2.venue_order_key == COMPILED_L2.eip712_digest
    assert rec.venue_order_key != rec.integrity_commitment_hash  # NOT a bare digest
    assert rec.integrity_commitment_hash == COMPILED_L2.integrity_commitment_hash
    assert events.index("persist") < events.index("sign")  # persist-BEFORE-sign

    # (b) the byte-verify ran (WIRED here) and the EXACT bytes handed to the HMAC transport equal the
    #     byte-verified bytes (no re-serialization between verify and send).
    assert captured["called"] is True
    from veridex.runtime.evidence import serialize_payload
    assert serialize_payload(captured["post_body"]).encode("utf-8") == http.received["body"]
    assert result.wire_bytes == http.received["body"]
    # the five POLY_* L2 headers are present on the wire.
    for name in POLY_L2_HEADER_NAMES:
        assert name in http.received["headers"]

    # (d) restart-join: a reconciler that knows ONLY venue_order_key resolves the ACK-lost fill to
    #     RESOLVED (fill history is keyed by the official V2 id, not Veridex's digest — Codex round-6).
    fill_key = COMPILED_L2.venue_order_key

    async def _fill_reader(key: str) -> dict[str, Any]:
        if key == fill_key:
            return {"trades": [{"taker_order_id": key, "size": 3.0, "status": "CONFIRMED"}]}
        return {"trades": []}

    reconciled = await reconcile_ack_lost(store, _fill_reader)
    assert len(reconciled) == 1
    assert reconciled[0].reconciled_state == "RESOLVED"
    assert reconciled[0].reconciled_fill_size == 3.0


async def test_mode_b_blocked_until_all_three_gates():
    from veridex.dust_execution.clobv2_gate import evaluate_from_fixture_dir
    from veridex.policy.envelope import PolicyEnvelope
    from veridex.venues.polymarket_preflight import run_preflight
    from veridex.venues.polymarket_resolver import ResolvedMarket

    class _Balances:
        async def get_balance_allowance(self, asset_type, token_id=None, signature_type=-1, **kw):
            return {"balance": 100.0, "allowance": 100.0} if asset_type == "COLLATERAL" else {"balance": 5.0, "allowance": 5.0}

    class _Quote:
        async def quote_market(self, market_ref, for_size=None):
            from veridex.venues.base import Quote
            return Quote(market_ref="ref", price=1.90, native_price=0.526, size=50.0, for_size=for_size or 0.0, ts=0)

    class _Egress:
        async def reachable(self):
            return True

    envelope = PolicyEnvelope(
        max_stake=1000.0, max_orders_per_run=100, max_orders_per_session=100, max_orders_per_day=100,
        venue_allowlist=["polymarket"], market_allowlist=["ref"], min_edge_bps=0, max_slippage_bps=500,
        max_price=100.0, max_quote_age_s=60, cooldown_s=0, human_approval_threshold=1_000_000.0,
        kill_switch=False,
    )
    gate = evaluate_from_fixture_dir(operator_smoke_ok=True)  # CLOB-V2 gate FULLY admits

    async def _run(*, privy_preflight_ok, provisioning_ok):
        return await run_preflight(
            market_ref="ref", order_size=10.0, required_usdc=10.0,
            resolved=ResolvedMarket(condition_id="0xcond", token_id_yes="111", token_id_no="222", tick_size=0.01),
            quote_adapter=_Quote(), balances=_Balances(), egress=_Egress(), envelope=envelope,
            actual_sig_type=2, expected_sig_type=2, max_slippage_bps=500, reference_price=1.90,
            dry_run=True, neg_risk_approved=True, fak_smoke_passed=True, clobv2_gate=gate,
            privy_preflight_ok=privy_preflight_ok, provisioning_ok=provisioning_ok,
        )

    # CLOB-V2 admits but the two operator gates are pending → Mode B NOT armable.
    r = await _run(privy_preflight_ok=None, provisioning_ok=None)
    assert r.mode_b_ready is True  # clobv2 gate alone
    assert r.mode_b_armable is False
    # Privy preflight passes but provisioning pending → still blocked.
    assert (await _run(privy_preflight_ok=True, provisioning_ok=None)).mode_b_armable is False
    # Provisioning passes but Privy preflight pending → still blocked.
    assert (await _run(privy_preflight_ok=None, provisioning_ok=True)).mode_b_armable is False
    # A failed gate → blocked.
    assert (await _run(privy_preflight_ok=False, provisioning_ok=True)).mode_b_armable is False
    # ALL THREE pass → armable.
    r_ok = await _run(privy_preflight_ok=True, provisioning_ok=True)
    assert r_ok.mode_b_armable is True
    names = {c.name for c in r_ok.checks}
    assert {"privy_preflight", "pusd_provisioning"} <= names


def test_preflight_fixture_non_executable_by_signed_fields():
    cp = PrivyEvmWalletControlPlane(client=L2FakePrivy(), binding=BIND)
    # Codex-M4: allowed-domain but signed-field-INVALID — bad token + ZERO amounts (NOT an expired
    # wrapper, since V2 expiration is unsigned). Signs fine (recovery verified), never executable.
    invalid = _compile_owner(_SECRET_OWNER, maker_amount="0", taker_amount="0", token_id="0")
    assert invalid.canonical_v2_typed_data["message"]["makerAmount"] == "0"

    # Operator-pending by default: ok=None, nothing signed/submitted.
    pending = operator_privy_preflight(
        cp, binding=BIND, invalid_order_payload=invalid, order_auth=CTX, clob_auth=CTX,
        operator_ran=False,
    )
    assert pending.ok is None
    assert pending.submitted is False and pending.persisted is False

    # Operator-run: exercises BOTH the ClobAuth and Order rules, recovery verified, never submitted.
    ran = operator_privy_preflight(
        cp, binding=BIND, invalid_order_payload=invalid, order_auth=CTX, clob_auth=CTX,
        operator_ran=True,
    )
    assert ran.ok is True
    assert set(ran.exercised_rules) == {CLOB_AUTH_PRIMARY_TYPE, "Order"}
    assert ran.recovery_verified is True
    assert ran.submitted is False and ran.persisted is False


def test_provisioning_preflight_operator_gated():
    # Pending by default (ok=None), never auto-True.
    assert operator_pusd_provisioning_preflight(
        pusd_balance=100.0, session_need=10.0, approvals_present=True, default_deny_restored=True,
        live_policy_content_hash="h", pinned_policy_content_hash="h", operator_ran=False,
    ).ok is None
    # Operator-run, all conditions met → True.
    assert operator_pusd_provisioning_preflight(
        pusd_balance=100.0, session_need=10.0, approvals_present=True, default_deny_restored=True,
        live_policy_content_hash="h", pinned_policy_content_hash="h", operator_ran=True,
    ).ok is True
    # Under-funded / weakened policy → fail closed.
    assert operator_pusd_provisioning_preflight(
        pusd_balance=1.0, session_need=10.0, approvals_present=True, default_deny_restored=True,
        live_policy_content_hash="h", pinned_policy_content_hash="h", operator_ran=True,
    ).ok is False
    assert operator_pusd_provisioning_preflight(
        pusd_balance=100.0, session_need=10.0, approvals_present=True, default_deny_restored=True,
        live_policy_content_hash="live", pinned_policy_content_hash="pinned", operator_ran=True,
    ).ok is False


def test_non_order_domain_denied_by_default_deny():
    cp = PrivyEvmWalletControlPlane(client=L2FakePrivy(), binding=BIND)
    # A non-order, non-ClobAuth EIP-712 domain (e.g. a Permit) is denied at custody by default-deny.
    permit = build_clob_auth_typed_data(address=_WALLET_ADDRESS, timestamp="1700000000")
    permit = copy.deepcopy(permit)
    permit["primaryType"] = "Permit"
    with pytest.raises(FailClosed):
        cp.sign_clob_auth(permit, binding=BIND, auth=CTX)
    # And the order path refuses a non-Order primaryType too.
    import dataclasses
    bogus = copy.deepcopy(COMPILED_L2.canonical_v2_typed_data)
    bogus["primaryType"] = "Permit"
    bad = dataclasses.replace(COMPILED_L2, canonical_v2_typed_data=bogus)
    with pytest.raises(FailClosed):
        cp.sign_typed_data(bad, binding=BIND, auth=CTX)


def test_poly_and_l2_headers_scrubbed_from_all_output():
    headers = build_l2_headers(
        _CREDS, address=_WALLET_ADDRESS, timestamp="1700000000", method="POST",
        request_path="/order", body='{"order":{}}',
    )
    # An error/log line that accidentally embeds the headers must be scrubbed of every L2 secret.
    leaky = (
        f"submit failed headers={headers} owner={_SECRET_OWNER} secret={_SECRET_API_SECRET} "
        f"pass={_SECRET_PASSPHRASE}"
    )
    scrubbed = scrub_l2_output(leaky, *_CREDS.secret_values(), headers["POLY_SIGNATURE"])
    for secret in (_SECRET_OWNER, _SECRET_API_SECRET, _SECRET_PASSPHRASE, headers["POLY_SIGNATURE"]):
        assert secret not in scrubbed
    assert "[REDACTED]" in scrubbed

    # scrub_headers_for_output redacts the secret-bearing POLY_* header values.
    safe = scrub_headers_for_output(headers, _CREDS)
    assert safe["POLY_API_KEY"] == "[REDACTED]"
    assert safe["POLY_PASSPHRASE"] == "[REDACTED]"
    assert safe["POLY_SIGNATURE"] == "[REDACTED]"

    # The creds repr/str never leaks a secret.
    assert _SECRET_OWNER not in repr(_CREDS)
    assert _SECRET_API_SECRET not in str(_CREDS)


def test_l2_transport_needs_no_private_key():
    # The L2 transport module imports NO local-key crypto surface (extends the no-local-key sweep).
    assert_no_imports("veridex.dust_execution.l2_transport", _LOCAL_KEY_BANNED)
    # The transport constructor takes NO key/signer parameter — only a control plane + HMAC creds.
    assert set(inspect.signature(KeylessL2Transport.__init__).parameters) <= {
        "self", "control_plane", "creds", "http", "store", "now_s",
    }
    # The HMAC path works with only the base64url secret (an HMAC secret, not a signing key).
    sig = l2t.l2_hmac_signature(
        api_secret=_SECRET_API_SECRET, timestamp="1", method="POST", request_path="/order", body="{}",
    )
    assert isinstance(sig, str) and sig


async def test_mint_l2_credentials_signs_clobauth_and_holds_no_secret_in_reference():
    cp = PrivyEvmWalletControlPlane(client=L2FakePrivy(), binding=BIND)
    client = _FakeClobAuthClient()
    creds = mint_l2_credentials(
        cp, binding=BIND, auth=CTX, clob_auth_client=client, timestamp="1700000000", nonce=0,
    )
    # The L1 ClobAuth was signed (POLY_SIGNATURE header present) to derive the creds.
    assert len(client.calls) == 1
    assert "POLY_SIGNATURE" in client.calls[0]
    # Only a NON-secret re-derivation reference is persistable — never the raw creds.
    ref = creds.persistable_reference()
    assert set(ref) == {"derivation_ref", "derivation_nonce"}
    for secret in (_SECRET_OWNER, _SECRET_API_SECRET, _SECRET_PASSPHRASE):
        assert secret not in repr(ref)
