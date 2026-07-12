"""E3-T7 — Privy EVM typed-data control plane for the Mode-B money path (REQ-018a/b/b2/c/e).

MONEY-NETWORK BOUNDARY — this is the MOST trust-critical dust-lane surface. It is a *control plane*
that STRUCTURALLY cannot sign with a local key and cannot reach a real venue directly:

* **eth_signTypedData_v4 ONLY.** :meth:`PrivyEvmWalletControlPlane.sign_typed_data` signs a compiled
  V2 order via Privy's ``eth_signTypedData_v4`` intent and NOTHING else. :meth:`sign_raw_hash`
  (``secp256k1_sign``) and :meth:`send_transaction` (``eth_sendTransaction``) fail closed *before any
  I/O* — the default-deny policy allows only the two typed-data rules, so those methods are denied at
  custody. (Adapted from the Agent-Rank Solana ``signAndSendTransaction`` pattern to EVM typed-data.)

* **Recover-and-require.** The signer address is recovered (pure-stdlib secp256k1, NO ``eth_account`` /
  ``eth_keys`` / ``coincurve``) from the returned signature over the *locally* recomputed EIP-712 digest
  and REQUIRED to equal BOTH ``binding.wallet_address`` AND the compiled order's ``signer``. Any
  mismatch → :class:`FailClosed`. (This is verification only — recovering a PUBLIC address from a
  signature is not signing and holds no private key.)

* **Owner/quorum authorization + replay guard + idempotency.** A sensitive ``/rpc`` action carries a
  SIGNED ``privy-request-expiry`` (replay guard), a quorum signature SET meeting a threshold, and an
  idempotency key on mutations — validated *before* the client call (refuse-before-I/O). (Adapted from
  the reference's P-256 authorization-signature + key-quorum ownership; here it is provider-neutral —
  the signature bytes are the operator's, never a local key.)

* **Policy content hash + quorum ownership, pinned in the binding, verified at arming.** :func:`arm_mode_b`
  requires the live policy's CONTENT hash (not just its ref) to equal the pinned hash, the policy to be
  typed-data-only default-deny, the resource to be quorum-owned (never app-secret-updatable), and the
  live quorum's content to match — else fail closed.

* **Explicit binding-hash manifest field.** :func:`pin_manifest` writes ``binding.binding_hash()`` into
  the explicit ``execution_wallet_binding_hash`` manifest field; :func:`execute_with` recomputes it from
  the live binding and compares at admission/restart — a different live binding → fail closed (no reroute).

* **Provider outage FREEZES, never local.** Any provider error from the Privy client is re-raised as
  :class:`FailClosed`; there is no local-key fallback path in the object graph at all.

NO live Privy, NO real credentials, NO network: the injected client is always a RECORDING-FAKE in tests
(Mode B stays UNARMED). This module imports ONLY stdlib + intra-lane ``veridex.dust_execution.*`` — never
a local-key crypto library — so the no-local-key guarantee holds structurally.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from veridex.dust_execution.manifest import StrategyExperimentManifest
from veridex.dust_execution.risk import FailClosed
from veridex.dust_execution.signing_compiler import (
    CompiledSigningPayload,
    _addr32,
    _u256,
    keccak256,
    order_hash_from_typed_data,
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
# The canonical default-deny, typed-data-only Mode-B wallet policy
# ---------------------------------------------------------------------------

#: The ONE Mode-B policy: default-DENY with EXACTLY two allow-rules — ``eth_signTypedData_v4`` for the V2
#: ``Order`` and for the L1 ``ClobAuth``. Raw ``secp256k1_sign`` and ``eth_sendTransaction`` are never
#: allow-listed, so they are denied at custody. Owned by a key-quorum (never app-secret-updatable).
TYPED_DATA_ONLY: PrivyWalletPolicy = PrivyWalletPolicy(
    rules=(
        PolicyRule(method=ALLOWED_SIGN_METHOD, primary_type=ORDER_PRIMARY_TYPE, effect="ALLOW"),
        PolicyRule(method=ALLOWED_SIGN_METHOD, primary_type=CLOB_AUTH_PRIMARY_TYPE, effect="ALLOW"),
        PolicyRule(method="secp256k1_sign", primary_type="*", effect="DENY"),
        PolicyRule(method="eth_sendTransaction", primary_type="*", effect="DENY"),
    ),
    default_action="DENY",
    owner_type="quorum",
)


# ---------------------------------------------------------------------------
# Pure-stdlib secp256k1 public-key RECOVERY (recover-and-require). NO signing, NO private key.
# ---------------------------------------------------------------------------

_SECP256K1_P: int = 2**256 - 2**32 - 977
_SECP256K1_N: int = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_SECP256K1_GX: int = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
_SECP256K1_GY: int = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8
_G: tuple[int, int] = (_SECP256K1_GX, _SECP256K1_GY)

# A curve point in affine coordinates, or ``None`` for the point at infinity.
_Point = tuple[int, int] | None


def _inv_mod(a: int, m: int) -> int:
    return pow(a % m, m - 2, m)


def _point_add(p1: _Point, p2: _Point) -> _Point:
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and (y1 + y2) % _SECP256K1_P == 0:
        return None
    if p1 == p2:
        lam = (3 * x1 * x1) * _inv_mod(2 * y1, _SECP256K1_P) % _SECP256K1_P
    else:
        lam = (y2 - y1) * _inv_mod((x2 - x1) % _SECP256K1_P, _SECP256K1_P) % _SECP256K1_P
    x3 = (lam * lam - x1 - x2) % _SECP256K1_P
    y3 = (lam * (x1 - x3) - y1) % _SECP256K1_P
    return (x3, y3)


def _point_mul(k: int, p: _Point) -> _Point:
    result: _Point = None
    k %= _SECP256K1_N
    while k:
        if k & 1:
            result = _point_add(result, p)
        p = _point_add(p, p)
        k >>= 1
    return result


def _address_from_point(point: _Point) -> str:
    if point is None:
        raise FailClosed("recovered a point at infinity — invalid signature")
    x, y = point
    raw = x.to_bytes(32, "big") + y.to_bytes(32, "big")
    return "0x" + keccak256(raw)[-20:].hex()


def recover_signer_address(digest: bytes, signature: bytes) -> str:
    """Recover the EVM address that produced ``signature`` over the 32-byte ``digest`` (fail closed).

    Pure stdlib secp256k1 ``ecrecover`` — the PUBLIC verification side (no private key, no signing).
    ``signature`` is 65 bytes ``r || s || v`` with ``v`` in ``{27, 28}`` (or ``{0, 1}``). Low-``s`` is
    accepted or rejected by the caller; here we only reject structurally invalid ``r``/``s``/``x``.
    """
    if len(digest) != 32:
        raise FailClosed(f"digest must be 32 bytes, got {len(digest)}")
    if len(signature) != 65:
        raise FailClosed(f"signature must be 65 bytes (r||s||v), got {len(signature)}")
    z = int.from_bytes(digest, "big")
    r = int.from_bytes(signature[0:32], "big")
    s = int.from_bytes(signature[32:64], "big")
    v = signature[64]
    rec_id = v - 27 if v >= 27 else v
    if rec_id not in (0, 1, 2, 3):
        raise FailClosed(f"invalid recovery id {rec_id}")
    if not (1 <= r < _SECP256K1_N and 1 <= s < _SECP256K1_N):
        raise FailClosed("signature r/s out of range")
    x = r + (rec_id >> 1) * _SECP256K1_N
    if x >= _SECP256K1_P:
        raise FailClosed("signature x-coordinate out of field")
    y_sq = (pow(x, 3, _SECP256K1_P) + 7) % _SECP256K1_P
    y = pow(y_sq, (_SECP256K1_P + 1) // 4, _SECP256K1_P)
    if (y * y - y_sq) % _SECP256K1_P != 0:
        raise FailClosed("no square root — point not on curve")
    if (y & 1) != (rec_id & 1):
        y = _SECP256K1_P - y
    big_r: _Point = (x, y)
    r_inv = _inv_mod(r, _SECP256K1_N)
    q = _point_mul(r_inv, _point_add(_point_mul(s, big_r), _point_mul((-z) % _SECP256K1_N, _G)))
    return _address_from_point(q)


# ---------------------------------------------------------------------------
# L1 ``ClobAuth`` EIP-712 (E3-T0 §7a) — the typed-data signed to create/derive L2 creds.
# Domain STAYS ``version:"1"`` in V2 (only the exchange order domain bumped to "2").
# ---------------------------------------------------------------------------

#: §7a ClobAuth domain (NO ``verifyingContract`` — only name/version/chainId).
_CLOB_AUTH_DOMAIN_NAME: str = "ClobAuthDomain"
_CLOB_AUTH_DOMAIN_VERSION: str = "1"
_CLOB_AUTH_CHAIN_ID: int = 137
#: §7a fixed attestation message.
CLOB_AUTH_MESSAGE: str = "This message attests that I control the given wallet"

# EIP-712 type strings for the ClobAuth domain (no verifyingContract) + the ClobAuth struct.
_CLOB_AUTH_DOMAIN_TYPE_STRING: str = "EIP712Domain(string name,string version,uint256 chainId)"
_CLOB_AUTH_TYPE_STRING: str = (
    "ClobAuth(address address,string timestamp,uint256 nonce,string message)"
)
_CLOB_AUTH_DOMAIN_TYPE_HASH: bytes = keccak256(_CLOB_AUTH_DOMAIN_TYPE_STRING.encode("utf-8"))
_CLOB_AUTH_TYPE_HASH: bytes = keccak256(_CLOB_AUTH_TYPE_STRING.encode("utf-8"))


def _kstr(value: str) -> bytes:
    """EIP-712 encoding of a dynamic ``string`` value: ``keccak256(utf-8 bytes)``."""
    return keccak256(str(value).encode("utf-8"))


def build_clob_auth_typed_data(*, address: str, timestamp: str, nonce: int = 0) -> dict[str, Any]:
    """Build the §7a L1 ``ClobAuth`` typed data for ``address`` (default nonce 0)."""
    return {
        "primaryType": CLOB_AUTH_PRIMARY_TYPE,
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
            ],
            "ClobAuth": [
                {"name": "address", "type": "address"},
                {"name": "timestamp", "type": "string"},
                {"name": "nonce", "type": "uint256"},
                {"name": "message", "type": "string"},
            ],
        },
        "domain": {
            "name": _CLOB_AUTH_DOMAIN_NAME,
            "version": _CLOB_AUTH_DOMAIN_VERSION,
            "chainId": _CLOB_AUTH_CHAIN_ID,
        },
        "message": {
            "address": address,
            "timestamp": str(timestamp),
            "nonce": int(nonce),
            "message": CLOB_AUTH_MESSAGE,
        },
    }


def clob_auth_hash_from_typed_data(typed_data: dict[str, Any]) -> str:
    """Compute the §7a L1 ``ClobAuth`` EIP-712 digest = ``keccak(0x1901 || domainSep || structHash)``.

    Pure stdlib keccak (via :func:`veridex.dust_execution.signing_compiler.keccak256`); NO local key.
    """
    domain = typed_data["domain"]
    message = typed_data["message"]
    domain_separator = keccak256(
        _CLOB_AUTH_DOMAIN_TYPE_HASH
        + _kstr(domain["name"])
        + _kstr(domain["version"])
        + _u256(int(domain["chainId"]))
    )
    struct_hash = keccak256(
        _CLOB_AUTH_TYPE_HASH
        + _addr32(str(message["address"]))
        + _kstr(message["timestamp"])
        + _u256(int(message["nonce"]))
        + _kstr(message["message"])
    )
    return "0x" + keccak256(b"\x19\x01" + domain_separator + struct_hash).hex()


# ---------------------------------------------------------------------------
# Authorization context (owner/quorum + signed replay-expiry + idempotency)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrivyAuthContext:
    """The signed authorization wrapper for one sensitive ``/rpc`` action (provider-neutral).

    ``request_expiry_ms`` is the SIGNED ``privy-request-expiry`` replay guard (the action is refused
    once the clock passes it). ``quorum_signatures`` is the SET of authorization-key signatures over the
    request; the SET size must meet ``quorum_threshold`` (a single signer is not enough). ``idempotency_key``
    de-duplicates mutations. These are the OPERATOR's authorization bytes — never a local wallet key.
    """

    request_expiry_ms: int
    quorum_signatures: tuple[str, ...]
    quorum_threshold: int
    idempotency_key: str


@dataclass(frozen=True)
class TypedDataSignature:
    """The opaque, NON-SECRET result of a typed-data signature (never a key, never a secret)."""

    method: str
    signature: str
    eip712_digest: str
    recovered_address: str


# ---------------------------------------------------------------------------
# The recording-fake Privy client boundary (Protocol; NEVER a live Privy client in tests)
# ---------------------------------------------------------------------------


@runtime_checkable
class RecordingPrivyClient(Protocol):
    """Provider boundary for the Privy wallet ``/rpc`` + wallet-mint surface.

    ONLY ever a RECORDING-FAKE in tests (no live Privy). ``policy`` exposes the wallet's attached
    default-deny policy for arming; ``sign_typed_data`` performs the ``eth_signTypedData_v4`` intent;
    ``mint_wallet`` is the idempotent mutation. There is deliberately NO ``sign_raw_hash`` /
    ``send_transaction`` on the boundary — those are refused in the control plane before any call.
    """

    @property
    def policy(self) -> PrivyWalletPolicy: ...

    def sign_typed_data(
        self, *, wallet_ref: str, typed_data: dict[str, Any], auth: PrivyAuthContext
    ) -> dict[str, Any]: ...

    def mint_wallet(self, *, idempotency_key: str) -> Any: ...


# ---------------------------------------------------------------------------
# The control plane
# ---------------------------------------------------------------------------


class PrivyEvmWalletControlPlane:
    """Mode-B (``PRIVY_EVM``) control plane — typed-data signing over a fail-closed custody boundary."""

    mode: str = "PRIVY_EVM"

    def __init__(
        self,
        *,
        client: RecordingPrivyClient,
        binding: ExecutionWalletBinding | None = None,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        # The injected client is ALWAYS a recording-fake in tests. This control plane holds NO key,
        # NO signer, and NO local-key constructor anywhere in its object graph.
        self._client = client
        self._binding = binding
        self._now_ms = now_ms if now_ms is not None else (lambda: int(time.time() * 1000))
        # Idempotency cache for MUTATIONS (mint_wallet): same key → same object identity.
        self._minted: dict[str, Any] = {}

    # -- refused-at-custody methods (default-deny policy allows ONLY the two typed-data rules) --------

    def sign_raw_hash(self, digest: bytes) -> TypedDataSignature:
        """REFUSED. Raw ``secp256k1_sign`` is not in the typed-data-only policy — fail closed pre-I/O."""
        raise FailClosed(
            "secp256k1_sign (raw-hash signing) is refused at custody: the default-deny Mode-B policy "
            "allows ONLY eth_signTypedData_v4 for the V2 order and the L1 ClobAuth (REQ-018a)"
        )

    def send_transaction(self, tx: Any) -> Any:
        """REFUSED. ``eth_sendTransaction`` is denied at custody — fail closed before any I/O."""
        raise FailClosed(
            "eth_sendTransaction is denied at custody: the Mode-B wallet policy is typed-data-only "
            "default-deny; broadcasting a transaction is never authorized (REQ-018a)"
        )

    # -- the ONE signing path -----------------------------------------------------------------------

    def sign_typed_data(
        self,
        compiled_payload: CompiledSigningPayload,
        *,
        binding: ExecutionWalletBinding,
        auth: PrivyAuthContext,
    ) -> TypedDataSignature:
        """Sign a compiled V2 order via ``eth_signTypedData_v4`` and recover-and-require the signer.

        Order of operations is load-bearing (refuse-before-I/O): validate auth (expiry + quorum) →
        recompute the local digest → call the client → recover the signer → require it equals BOTH the
        binding address and the compiled order signer. A provider error freezes (FailClosed), never a
        local-key fallback.
        """
        # (1) AUTH MECHANICS first — refuse before any provider I/O. The request's self-declared
        #     quorum_threshold MUST equal the PINNED binding threshold (a request cannot downgrade it).
        self._validate_auth(auth, pinned_threshold=binding.quorum_threshold)

        typed_data = compiled_payload.canonical_v2_typed_data
        # Default-deny domain guard: this path signs ONLY the V2 ``Order`` domain. Any other
        # ``primaryType`` (a non-order EIP-712 domain) is refused at custody before any I/O.
        if typed_data.get("primaryType") != ORDER_PRIMARY_TYPE:
            raise FailClosed(
                f"default-deny: sign_typed_data signs ONLY the V2 {ORDER_PRIMARY_TYPE!r} domain, got "
                f"primaryType {typed_data.get('primaryType')!r} (REQ-018a)"
            )
        message = typed_data.get("message", {})
        order_signer = str(message.get("signer", ""))

        # (2) Recompute the EIP-712 digest LOCALLY (never trust a passed-in digest) and require it
        #     equals the compiled payload's own digest (fail closed on any drift).
        local_digest_hex = order_hash_from_typed_data(typed_data)
        if local_digest_hex.lower() != compiled_payload.eip712_digest.lower():
            raise FailClosed("locally recomputed EIP-712 digest disagrees with the compiled payload")

        # (3) Pre-bind: the compiled order's signer MUST already be the bound wallet address.
        if order_signer.lower() != binding.wallet_address.lower():
            raise FailClosed(
                "compiled order signer does not equal the bound wallet address (custody mismatch)"
            )

        # (4) SIGN via the recording-fake Privy client. ANY provider error FREEZES (fail closed) —
        #     there is no local-key fallback path in this object graph.
        try:
            response = self._client.sign_typed_data(
                wallet_ref=binding.wallet_ref, typed_data=typed_data, auth=auth
            )
        except FailClosed:
            raise
        except Exception as exc:  # provider outage / any error → freeze, NEVER local
            raise FailClosed(
                "Privy provider error while signing — freezing (Mode-B never falls back to a local key)"
            ) from exc

        signature_hex = self._extract_signature(response)

        # (5) RECOVER-AND-REQUIRE: recover the signer from the signature over the LOCAL digest and
        #     require it equals BOTH the bound wallet address AND the compiled order's signer.
        digest_bytes = bytes.fromhex(local_digest_hex[2:] if local_digest_hex.startswith("0x") else local_digest_hex)
        recovered = recover_signer_address(digest_bytes, bytes.fromhex(_strip0x(signature_hex)))
        if recovered.lower() != binding.wallet_address.lower():
            raise FailClosed(
                "recovered signer does not equal binding.wallet_address (custody mismatch, fail closed)"
            )
        if recovered.lower() != order_signer.lower():
            raise FailClosed(
                "recovered signer does not equal the compiled order signer (fail closed)"
            )

        return TypedDataSignature(
            method=ALLOWED_SIGN_METHOD,
            signature=signature_hex,
            eip712_digest=local_digest_hex,
            recovered_address=recovered,
        )

    # -- the L1 ClobAuth signing path (create/derive L2 creds) ---------------------------------------

    def sign_clob_auth(
        self,
        clob_auth_typed_data: dict[str, Any],
        *,
        binding: ExecutionWalletBinding,
        auth: PrivyAuthContext,
    ) -> TypedDataSignature:
        """Sign the §7a L1 ``ClobAuth`` typed data via ``eth_signTypedData_v4`` (recover-and-require).

        Same fail-closed order as :meth:`sign_typed_data`: validate auth → default-deny primaryType
        guard (ONLY ``ClobAuth``) → recompute the L1 digest LOCALLY → sign via the recording-fake Privy
        client → recover the signer and REQUIRE it equals BOTH ``binding.wallet_address`` AND the
        ClobAuth ``address`` field. A provider error FREEZES (never a local-key fallback).
        """
        # AUTH MECHANICS first (refuse-before-I/O): the request's quorum_threshold MUST equal the
        # PINNED binding threshold on THIS path too — the ClobAuth request cannot self-downgrade it.
        self._validate_auth(auth, pinned_threshold=binding.quorum_threshold)

        if clob_auth_typed_data.get("primaryType") != CLOB_AUTH_PRIMARY_TYPE:
            raise FailClosed(
                f"default-deny: sign_clob_auth signs ONLY the L1 {CLOB_AUTH_PRIMARY_TYPE!r} domain, got "
                f"primaryType {clob_auth_typed_data.get('primaryType')!r} (REQ-018f)"
            )
        message = clob_auth_typed_data.get("message", {})
        clob_address = str(message.get("address", ""))

        local_digest_hex = clob_auth_hash_from_typed_data(clob_auth_typed_data)
        if clob_address.lower() != binding.wallet_address.lower():
            raise FailClosed(
                "ClobAuth address does not equal the bound wallet address (custody mismatch)"
            )

        try:
            response = self._client.sign_typed_data(
                wallet_ref=binding.wallet_ref, typed_data=clob_auth_typed_data, auth=auth
            )
        except FailClosed:
            raise
        except Exception as exc:  # provider outage / any error → freeze, NEVER local
            raise FailClosed(
                "Privy provider error while signing ClobAuth — freezing (Mode-B never falls back to a "
                "local key)"
            ) from exc

        signature_hex = self._extract_signature(response)
        digest_bytes = bytes.fromhex(_strip0x(local_digest_hex))
        recovered = recover_signer_address(digest_bytes, bytes.fromhex(_strip0x(signature_hex)))
        if recovered.lower() != binding.wallet_address.lower():
            raise FailClosed(
                "recovered ClobAuth signer does not equal binding.wallet_address (fail closed)"
            )
        if recovered.lower() != clob_address.lower():
            raise FailClosed(
                "recovered ClobAuth signer does not equal the ClobAuth address field (fail closed)"
            )
        return TypedDataSignature(
            method=ALLOWED_SIGN_METHOD,
            signature=signature_hex,
            eip712_digest=local_digest_hex,
            recovered_address=recovered,
        )

    # -- idempotent mutation ------------------------------------------------------------------------

    def mint_wallet(self, *, idempotency_key: str) -> Any:
        """Mint (or return the already-minted) wallet for ``idempotency_key`` — idempotent on mutation.

        The SAME idempotency key returns the SAME object identity (``a is a``); the underlying client
        mint is invoked at most once per key.
        """
        if not idempotency_key:
            raise FailClosed("mint_wallet requires a non-empty idempotency_key (mutation replay guard)")
        cached = self._minted.get(idempotency_key)
        if cached is not None:
            return cached
        minted = self._client.mint_wallet(idempotency_key=idempotency_key)
        self._minted[idempotency_key] = minted
        return minted

    # -- internals ----------------------------------------------------------------------------------

    def _validate_auth(self, auth: PrivyAuthContext, *, pinned_threshold: int) -> None:
        """Validate the auth wrapper against the PINNED quorum threshold (refuse-before-I/O).

        ``pinned_threshold`` is the server-resolved ``binding.quorum_threshold`` — NOT the request's
        own value. The request may not self-downgrade the quorum: its ``auth.quorum_threshold`` MUST
        equal the pinned threshold, and distinct signatures are counted against that PINNED value.
        """
        if auth.request_expiry_ms <= self._now_ms():
            raise FailClosed(
                "privy-request-expiry has passed — refusing (signed replay guard, fail closed)"
            )
        if pinned_threshold < 1:
            raise FailClosed("pinned quorum_threshold must be >= 1 (a signature set is required)")
        if auth.quorum_threshold != pinned_threshold:
            raise FailClosed(
                "authorization quorum_threshold does not equal the pinned binding threshold — a "
                "request may not self-downgrade the pinned quorum (fail closed before any I/O)"
            )
        if len(set(auth.quorum_signatures)) < pinned_threshold:
            raise FailClosed(
                "authorization quorum not met: fewer distinct signatures than the PINNED threshold"
            )
        if not auth.idempotency_key:
            raise FailClosed("auth is missing an idempotency_key")

    @staticmethod
    def _extract_signature(response: dict[str, Any]) -> str:
        # Privy shape: {"method": "eth_signTypedData_v4", "data": {"signature": "0x…", "encoding":"hex"}}
        data = response.get("data", {}) if isinstance(response, dict) else {}
        signature = data.get("signature") if isinstance(data, dict) else None
        if not isinstance(signature, str) or not signature:
            raise FailClosed("Privy response carried no signature — fail closed")
        return signature


def _strip0x(value: str) -> str:
    return value[2:] if value.startswith("0x") else value


# ---------------------------------------------------------------------------
# Arming gate: policy content hash + quorum ownership, verified against the pinned binding
# ---------------------------------------------------------------------------


def arm_mode_b(
    *,
    binding: ExecutionWalletBinding,
    live_policy: PrivyWalletPolicy | None = None,
    live_quorum: AuthorizationQuorum | None = None,
) -> None:
    """Verify the LIVE wallet policy + quorum against the pinned binding; fail closed on any weakening.

    Checks (all fail closed):

    * ``binding.chain_id`` is ``eip155:137`` (EVM Polygon, not a stray chain);
    * the live policy's CONTENT hash equals the pinned ``binding.privy_policy_content_hash`` — a policy
      whose ref is unchanged but whose content was weakened (e.g. via an app-secret update) is caught;
    * the live policy is typed-data-only default-deny (structural, defence-in-depth);
    * the policy resource is QUORUM-owned, not app-secret-updatable (v0.6.3 Codex-m1 / Fable-m5);
    * the live quorum's ref + content hash match the pinned quorum, AND the separately-pinned
      ``binding.quorum_threshold`` equals the live quorum's ``threshold``.

    Both ``live_policy`` AND ``live_quorum`` are MANDATORY live observations: this production arming API
    refuses to arm without reading BOTH (Gate#2 MAJOR-2). A missing observation fails closed — there is
    NO canonical ``TYPED_DATA_ONLY`` substitution and NO quorum-check skip. An offline unit test that
    wants to exercise a single check must supply a genuine passing fixture for the OTHER observation.
    """
    if live_policy is None:
        raise FailClosed(
            "arm_mode_b requires a LIVE policy observation — refusing to arm without reading the live "
            "policy (no canonical TYPED_DATA_ONLY substitution for an unobserved policy; REQ-018b)"
        )
    if live_quorum is None:
        raise FailClosed(
            "arm_mode_b requires a LIVE quorum observation — refusing to arm without reading the live "
            "quorum (a missing live quorum can no longer skip the quorum re-check; REQ-018b2)"
        )
    policy = live_policy

    if binding.chain_id != CHAIN_ID_POLYGON:
        raise FailClosed(f"binding.chain_id must be {CHAIN_ID_POLYGON}, got {binding.chain_id!r}")

    if policy.content_hash() != binding.privy_policy_content_hash:
        raise FailClosed(
            "live Privy policy CONTENT hash does not match the pinned binding hash — the policy was "
            "weakened (ref unchanged, content changed); refusing to arm (REQ-018b)"
        )
    if not policy.is_typed_data_only_default_deny():
        raise FailClosed(
            "live Privy policy is not typed-data-only default-deny (an allow-rule targets a "
            "non-typed-data method); refusing to arm"
        )
    if policy.owner_type != "quorum":
        raise FailClosed(
            "Privy policy resource is not quorum-owned (it is app-secret-updatable); refusing to arm "
            "(a resource an app secret can weaken is not a real custody control — v0.6.3 Codex-m1)"
        )

    if live_quorum.quorum_ref != binding.authorization_quorum_ref:
        raise FailClosed("live authorization quorum ref does not match the pinned binding")
    if live_quorum.content_hash() != binding.authorization_quorum_content_hash:
        raise FailClosed(
            "live authorization quorum CONTENT hash does not match the pinned binding "
            "(threshold/keys weakened); refusing to arm"
        )
    if binding.quorum_threshold != live_quorum.threshold:
        raise FailClosed(
            "pinned binding.quorum_threshold does not equal the live quorum threshold — the "
            "separately-pinned threshold and the observed quorum content disagree; refusing to arm"
        )


# ---------------------------------------------------------------------------
# Manifest binding: the explicit execution_wallet_binding_hash field (Codex-M3)
# ---------------------------------------------------------------------------

#: A minimal, valid Mode-B (``live_guarded``) manifest scaffold; :func:`pin_manifest` overrides the
#: binding hash. Kept here (not in a fixture) so the pin/execute round-trip is self-contained.
_MODE_B_MANIFEST_DEFAULTS: dict[str, Any] = {
    "strategy_id": "dust-maker-mode-b",
    "strategy_config_hash": "cfg" * 4,
    "evidence_class": "EXPERIMENTAL_DUST",
    "market": "0xcondition",
    "universe": ("0xtokenYES", "0xtokenNO"),
    "mode": "live_guarded",
    "max_orders": 3,
    "max_notional": 5.0,
    "max_session_loss": 2.0,
    "max_daily_loss": 4.0,
    "session_window": (1_700_000_000_000, 1_700_000_600_000),
    "required_inputs": ("fair_value", "venue_book"),
    "permitted_intent_kinds": ("make",),
    "market_fee_snapshot_hash": "fee" * 4,
    "operator_authorization": "op-ref-mode-b",
    "forbidden_claims": ("PROVEN_EDGE", "CALIBRATED"),
}


def pin_manifest(
    *, binding: ExecutionWalletBinding, **overrides: Any
) -> StrategyExperimentManifest:
    """Pin a Mode-B manifest whose explicit ``execution_wallet_binding_hash`` equals ``binding.binding_hash()``.

    Because the binding hash is an EXPLICIT frozen field, it lives INSIDE ``manifest_hash()`` — it can
    never be a checked-separately sidecar that is dropped after restart.
    """
    fields = {**_MODE_B_MANIFEST_DEFAULTS, **overrides}
    fields["execution_wallet_binding_hash"] = binding.binding_hash()
    return StrategyExperimentManifest(**fields)


def execute_with(
    manifest: StrategyExperimentManifest, *, live_binding: ExecutionWalletBinding
) -> None:
    """Admission/restart gate: recompute the live binding hash and require it equals the pinned field.

    A Mode-B manifest MUST carry a non-``None`` ``execution_wallet_binding_hash``; a live binding whose
    recomputed hash differs from the pinned one is a reroute attempt → fail closed.
    """
    pinned = manifest.execution_wallet_binding_hash
    if pinned is None:
        raise FailClosed(
            "Mode-B manifest is missing execution_wallet_binding_hash — refusing to execute "
            "(the custody binding must be pinned inside the manifest hash)"
        )
    if live_binding.binding_hash() != pinned:
        raise FailClosed(
            "live execution-wallet binding hash does not match the pinned manifest field — refusing "
            "to reroute to a different wallet/policy (fail closed, REQ-018c)"
        )


# ---------------------------------------------------------------------------
# L2 credential lifecycle: create/derive HMAC creds via a CLOB-V2 auth client (§7a endpoints)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class L2ApiCredentials:
    """The L2 HMAC credentials derived from an L1 ClobAuth signature (§7b) — SECRETS held in memory.

    ``api_key`` (the ``owner`` UUID), ``api_secret`` (the base64url HMAC secret) and ``api_passphrase``
    are SECRETS: NEVER persisted to the manifest/ledger, NEVER logged, NEVER placed in evidence. Only
    the NON-secret ``derivation_ref`` + ``derivation_nonce`` are persistable — enough to RE-DERIVE the
    creds via the L1 path, never the creds themselves. ``__repr__``/``__str__`` are scrubbed so a stray
    log line can never leak a secret.
    """

    api_key: str
    api_secret: str
    api_passphrase: str
    derivation_ref: str
    derivation_nonce: int = 0

    def __repr__(self) -> str:  # never leak secrets through repr/str
        return (
            f"L2ApiCredentials(derivation_ref={self.derivation_ref!r}, "
            f"derivation_nonce={self.derivation_nonce}, <secrets redacted>)"
        )

    __str__ = __repr__

    def persistable_reference(self) -> dict[str, Any]:
        """The ONLY thing that may be persisted: the non-secret re-derivation reference."""
        return {"derivation_ref": self.derivation_ref, "derivation_nonce": self.derivation_nonce}

    def secret_values(self) -> tuple[str, ...]:
        """The raw secret values — for a store-scan assertion ONLY (never a persistence sink)."""
        return (self.api_key, self.api_secret, self.api_passphrase)


@runtime_checkable
class ClobAuthClient(Protocol):
    """The small CLOB-V2 L1 auth client (§7a ``POST /auth/api-key`` / ``GET /auth/derive-api-key``).

    ONLY ever a RECORDING-FAKE in tests. Given the L1 ``POLY_*`` headers, it returns the derived
    ``ApiCreds`` mapping ``{api_key, api_secret, api_passphrase}``.
    """

    def create_or_derive_api_key(self, *, l1_headers: dict[str, str]) -> dict[str, str]: ...


def build_l1_headers(
    signature: TypedDataSignature, *, address: str, timestamp: str, nonce: int = 0
) -> dict[str, str]:
    """Build the §7a L1 ``POLY_*`` headers from a ClobAuth signature (``POLY_SIGNATURE`` is the sig)."""
    return {
        "POLY_ADDRESS": address,
        "POLY_SIGNATURE": signature.signature,
        "POLY_TIMESTAMP": str(timestamp),
        "POLY_NONCE": str(nonce),
    }


def mint_l2_credentials(
    control_plane: PrivyEvmWalletControlPlane,
    *,
    binding: ExecutionWalletBinding,
    auth: PrivyAuthContext,
    clob_auth_client: ClobAuthClient,
    timestamp: str,
    nonce: int = 0,
) -> L2ApiCredentials:
    """Mint L2 HMAC creds: sign L1 ClobAuth via Privy → derive ``ApiCreds`` via the auth client.

    The L1 ``ClobAuth`` typed data is signed through the Privy control plane (recover-and-require), the
    resulting §7a ``POLY_*`` headers drive the auth client's create/derive, and the returned secrets are
    wrapped in an in-memory :class:`L2ApiCredentials` (never persisted; only the nonce/ref is).
    """
    typed_data = build_clob_auth_typed_data(
        address=binding.wallet_address, timestamp=timestamp, nonce=nonce
    )
    signature = control_plane.sign_clob_auth(typed_data, binding=binding, auth=auth)
    l1_headers = build_l1_headers(
        signature, address=binding.wallet_address, timestamp=timestamp, nonce=nonce
    )
    creds = clob_auth_client.create_or_derive_api_key(l1_headers=l1_headers)
    missing = {"api_key", "api_secret", "api_passphrase"} - set(creds)
    if missing:
        raise FailClosed(f"CLOB-V2 auth client returned no {sorted(missing)} — cannot derive L2 creds")
    return L2ApiCredentials(
        api_key=creds["api_key"],
        api_secret=creds["api_secret"],
        api_passphrase=creds["api_passphrase"],
        derivation_ref=f"{binding.wallet_ref}:{nonce}",
        derivation_nonce=nonce,
    )


# ---------------------------------------------------------------------------
# Operator-gated Privy preflight + pUSD/approvals/gas provisioning (ok=None until operator-run)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrivyPreflightResult:
    """The operator-gated Privy signing preflight verdict (ok=None until an operator runs it).

    When run, it signs the allowed-domain-but-signed-field-INVALID fixture (Codex-M4: bad token + zero
    amounts — NOT an expired wrapper, since V2 ``expiration`` is unsigned), exercising BOTH the L1
    ``ClobAuth`` and V2 ``Order`` rules, and verifies recovery. It NEVER submits and NEVER persists.
    """

    ok: bool | None
    detail: str
    exercised_rules: tuple[str, ...] = ()
    recovery_verified: bool = False
    submitted: bool = False
    persisted: bool = False


def operator_privy_preflight(
    control_plane: PrivyEvmWalletControlPlane,
    *,
    binding: ExecutionWalletBinding,
    invalid_order_payload: CompiledSigningPayload,
    order_auth: PrivyAuthContext,
    clob_auth: PrivyAuthContext,
    timestamp: str = "0",
    nonce: int = 0,
    operator_ran: bool = False,
) -> PrivyPreflightResult:
    """Operator-gated Privy preflight — ``ok=None`` until an operator runs it (OUT of CI).

    When ``operator_ran`` is True it signs the L1 ``ClobAuth`` AND the signed-field-invalid V2 order
    (allowed domains, non-executable message fields), relying on the control plane's recover-and-require
    to verify recovery. It NEVER calls a submit/persist surface. ``ok=None`` while pending.
    """
    if not operator_ran:
        return PrivyPreflightResult(
            ok=None,
            detail=(
                "operator-pending: the Privy signing preflight is OPERATOR-RUN and OUT of CI — it has "
                "not been run, so ok=None (never auto-True; signs a signed-field-invalid fixture only)"
            ),
        )

    clob_typed = build_clob_auth_typed_data(
        address=binding.wallet_address, timestamp=timestamp, nonce=nonce
    )
    # Exercise BOTH allowed rules; recover-and-require inside each call verifies recovery.
    control_plane.sign_clob_auth(clob_typed, binding=binding, auth=clob_auth)
    control_plane.sign_typed_data(invalid_order_payload, binding=binding, auth=order_auth)
    return PrivyPreflightResult(
        ok=True,
        detail=(
            "operator-confirmed: signed the allowed-domain, signed-field-invalid fixture (bad token + "
            "zero amounts) for BOTH the ClobAuth and V2 Order rules; recovery verified; never submitted"
        ),
        exercised_rules=(CLOB_AUTH_PRIMARY_TYPE, ORDER_PRIMARY_TYPE),
        recovery_verified=True,
        submitted=False,
        persisted=False,
    )


@dataclass(frozen=True)
class ProvisioningResult:
    """The operator-gated pUSD/approvals/gas one-time provisioning verdict (ok=None until run).

    Asserts (when run): pUSD balance ≥ session need, approvals present, the default-deny policy is
    restored, and the policy content hash is re-pinned to the binding's pinned hash.
    """

    ok: bool | None
    detail: str


def operator_pusd_provisioning_preflight(
    *,
    pusd_balance: float,
    session_need: float,
    approvals_present: bool,
    default_deny_restored: bool,
    live_policy_content_hash: str,
    pinned_policy_content_hash: str,
    operator_ran: bool = False,
) -> ProvisioningResult:
    """Operator-gated pUSD/approvals/gas provisioning preflight — ``ok=None`` until an operator runs it.

    Fail-closed AND of: balance ≥ session need, approvals present, default-deny policy restored, and the
    live policy content hash re-pinned to the pinned binding hash. ``ok=None`` while pending.
    """
    if not operator_ran:
        return ProvisioningResult(
            ok=None,
            detail=(
                "operator-pending: the one-time pUSD/approvals/gas provisioning is OPERATOR-RUN and OUT "
                "of CI — it has not been run, so ok=None (never auto-True)"
            ),
        )
    reasons: list[str] = []
    if not (pusd_balance + 1e-9 >= session_need):
        reasons.append(f"pUSD balance {pusd_balance:g} < session need {session_need:g}")
    if not approvals_present:
        reasons.append("required approvals are not present")
    if not default_deny_restored:
        reasons.append("default-deny policy was not restored")
    if live_policy_content_hash != pinned_policy_content_hash:
        reasons.append("policy content hash was not re-pinned to the binding's pinned hash")
    ok = not reasons
    return ProvisioningResult(
        ok=ok,
        detail=(
            "operator-confirmed: pUSD balance ≥ session need, approvals present, default-deny restored, "
            "content hash re-pinned"
            if ok
            else "provisioning FAILED: " + "; ".join(reasons)
        ),
    )


# ---------------------------------------------------------------------------
# Mode-B factory (factory-shape: params ⊆ {binding, privy_client, l2_creds, http})
# ---------------------------------------------------------------------------


def build_mode_b(
    *,
    binding: ExecutionWalletBinding,
    privy_client: RecordingPrivyClient,
    l2_creds: Any = None,
    http: Any = None,
) -> PrivyEvmWalletControlPlane:
    """Build a Mode-B control plane. The parameter SET is deliberately narrow (no key/signer param).

    ``l2_creds`` (opaque L2 auth material for the write transport) and ``http`` (the injected async
    transport) are accepted for wiring downstream E3-T8 pieces but are NEVER a local signing key — the
    control plane signs ONLY through the injected Privy client.
    """
    return PrivyEvmWalletControlPlane(client=privy_client, binding=binding)


__all__ = [
    "CLOB_AUTH_MESSAGE",
    "TYPED_DATA_ONLY",
    "ClobAuthClient",
    "L2ApiCredentials",
    "PrivyAuthContext",
    "PrivyEvmWalletControlPlane",
    "PrivyPreflightResult",
    "ProvisioningResult",
    "RecordingPrivyClient",
    "TypedDataSignature",
    "arm_mode_b",
    "build_clob_auth_typed_data",
    "build_l1_headers",
    "build_mode_b",
    "clob_auth_hash_from_typed_data",
    "execute_with",
    "mint_l2_credentials",
    "operator_privy_preflight",
    "operator_pusd_provisioning_preflight",
    "pin_manifest",
    "recover_signer_address",
]
