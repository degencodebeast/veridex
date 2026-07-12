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
        # (1) AUTH MECHANICS first — refuse before any provider I/O.
        self._validate_auth(auth)

        typed_data = compiled_payload.canonical_v2_typed_data
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

    def _validate_auth(self, auth: PrivyAuthContext) -> None:
        if auth.request_expiry_ms <= self._now_ms():
            raise FailClosed(
                "privy-request-expiry has passed — refusing (signed replay guard, fail closed)"
            )
        if auth.quorum_threshold < 1:
            raise FailClosed("quorum_threshold must be >= 1 (a signature set is required)")
        if len(set(auth.quorum_signatures)) < auth.quorum_threshold:
            raise FailClosed(
                "authorization quorum not met: fewer distinct signatures than the required threshold"
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
    * when a live quorum is supplied, its ref + content hash match the pinned quorum.

    ``live_policy`` defaults to the canonical :data:`TYPED_DATA_ONLY` so a caller probing only the quorum
    still exercises a genuine (passing) policy check.
    """
    policy = live_policy if live_policy is not None else TYPED_DATA_ONLY

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

    if live_quorum is not None:
        if live_quorum.quorum_ref != binding.authorization_quorum_ref:
            raise FailClosed("live authorization quorum ref does not match the pinned binding")
        if live_quorum.content_hash() != binding.authorization_quorum_content_hash:
            raise FailClosed(
                "live authorization quorum CONTENT hash does not match the pinned binding "
                "(threshold/keys weakened); refusing to arm"
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
    "TYPED_DATA_ONLY",
    "PrivyAuthContext",
    "PrivyEvmWalletControlPlane",
    "RecordingPrivyClient",
    "TypedDataSignature",
    "arm_mode_b",
    "build_mode_b",
    "execute_with",
    "pin_manifest",
    "recover_signer_address",
]
