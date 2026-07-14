"""E3-T6 — PURE CLOB-V2 signing compiler: admitted intent -> typed-data + digest + commitment.

MONEY-NETWORK BOUNDARY. This compiler is PURE and self-contained: NO network, NO Privy, NO
private-key type, NO real signing, and NO third-party crypto/SDK import. It maps an admitted
post-rounding intent to a frozen :class:`CompiledSigningPayload` and NEVER signs or submits.

It implements the EIP-712 V2 order hash itself with a stdlib-only Keccak-256 (Ethereum keccak, 0x01
domain suffix — NOT NIST SHA3), so:

* ``eip712_digest`` / ``venue_order_key`` are the REAL V2 ``orderHash`` — cross-validated byte-for-byte
  against the official ``py_clob_client_v2`` ``ExchangeOrderBuilderV2.build_order_hash`` fixture
  (``tests/fixtures/dust_execution/clobv2/order_digest_v2.json``), E3-T0 §13#1 RESOLVED.
* the module imports ONLY stdlib + intra-lane ``veridex.dust_execution.*`` — the purity guard
  (no ``httpx`` / ``privy`` / ``eth_account`` / ``eth_keys``) holds structurally.

The order struct + domain (§1a/§1b) are the E3-T0 pins in
:mod:`veridex.dust_execution.clobv2_gate`; the signed/unsigned partition + the integrity commitment
live in :mod:`veridex.dust_execution.order_commitment`. Signing itself (turning ``eip712_digest`` into
a signature) and wiring the persist-pre-sign + submit-time byte-verify into the live path are E3-T7/T8.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import field_validator

from veridex.dust_execution.clobv2_gate import (
    _CHAIN_ID,
    _EXCHANGE_DOMAIN_NAME,
    _EXCHANGE_DOMAIN_VERSION_V2,
    _ORDER_TYPES,
    _RESTING_ORDER_TYPES,
    _SUPPORTED_SIGNATURE_TYPE,
    _V2_VERIFYING_CONTRACTS,
)
from veridex.dust_execution.contracts import _FrozenModel, _reject_price_out_of_unit_interval
from veridex.dust_execution.order_commitment import integrity_commitment_over
from veridex.dust_execution.risk import FailClosed

# §1a V2 exchange verifyingContract addresses (public, pinned by E3-T0 §1a). Selected by neg-risk.
# Asserted to be members of the gate's V2 set so a mistyped address fails at import (fail-closed).
_V2_STANDARD_EXCHANGE: str = "0xE111180000d2663C0091e4f400237545B87B996B"
_V2_NEG_RISK_EXCHANGE: str = "0xe2222d279d744050d28e00520010520000310F59"
assert _V2_STANDARD_EXCHANGE.lower() in _V2_VERIFYING_CONTRACTS
assert _V2_NEG_RISK_EXCHANGE.lower() in _V2_VERIFYING_CONTRACTS

# bytes32 zero = 0x followed by 64 hex zeros (metadata/builder default; §1b "zero if none").
_BYTES32_ZERO: str = "0x" + "0" * 64

# §1b Solidity struct type string (exact, verbatim from the V2 builder) — the EIP-712 Order typeHash.
_ORDER_TYPE_STRING: str = (
    "Order(uint256 salt,address maker,address signer,uint256 tokenId,"
    "uint256 makerAmount,uint256 takerAmount,uint8 side,uint8 signatureType,"
    "uint256 timestamp,bytes32 metadata,bytes32 builder)"
)
_DOMAIN_TYPE_STRING: str = (
    "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
)


# ---------------------------------------------------------------------------
# Stdlib-only Keccak-256 (Ethereum keccak: pad10*1 with 0x01 domain suffix)
# ---------------------------------------------------------------------------

_KECCAK_RC: tuple[int, ...] = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)
_KECCAK_ROT: tuple[tuple[int, ...], ...] = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)
_MASK64: int = (1 << 64) - 1


def _rotl64(x: int, n: int) -> int:
    return ((x << n) | (x >> (64 - n))) & _MASK64


def _keccak_f1600(a: list[list[int]]) -> None:
    """The Keccak-f[1600] permutation over a 5x5 lane state (in place)."""
    for rnd in range(24):
        c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rotl64(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                a[x][y] ^= d[x]
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rotl64(a[x][y], _KECCAK_ROT[x][y])
        for x in range(5):
            for y in range(5):
                a[x][y] = b[x][y] ^ ((~b[(x + 1) % 5][y]) & b[(x + 2) % 5][y])
        a[0][0] ^= _KECCAK_RC[rnd]


def keccak256(data: bytes) -> bytes:
    """Keccak-256 (Ethereum variant) of ``data`` — stdlib only, no external crypto dependency.

    Uses the original Keccak padding (``0x01`` domain suffix), NOT NIST SHA3's ``0x06``; this matches
    ``eth_utils.keccak`` and the Polymarket V2 order hash. Verified byte-for-byte in the test suite.
    """
    rate = 136  # bytes = 1088 bits (capacity 512, 256-bit output)
    a = [[0] * 5 for _ in range(5)]
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % rate != 0:
        padded.append(0x00)
    padded[-1] ^= 0x80
    for off in range(0, len(padded), rate):
        block = padded[off:off + rate]
        for i in range(rate // 8):
            a[i % 5][i // 5] ^= int.from_bytes(block[i * 8:i * 8 + 8], "little")
        _keccak_f1600(a)
    out = bytearray()
    while len(out) < 32:
        for y in range(5):
            for x in range(5):
                out += a[x][y].to_bytes(8, "little")
    return bytes(out[:32])


def _u256(value: int) -> bytes:
    return int(value).to_bytes(32, "big")


def _addr32(value: str) -> bytes:
    """ABI-encode an address as a left-padded 32-byte word (case-insensitive, per EIP-712)."""
    return int(value, 16).to_bytes(32, "big")


def _bytes32(value: str) -> bytes:
    return bytes.fromhex(value.replace("0x", "").zfill(64))


_ORDER_TYPE_HASH: bytes = keccak256(_ORDER_TYPE_STRING.encode("utf-8"))
_DOMAIN_TYPE_HASH: bytes = keccak256(_DOMAIN_TYPE_STRING.encode("utf-8"))


def order_hash_from_typed_data(typed_data: dict[str, Any]) -> str:
    """Compute the V2 EIP-712 ``orderHash`` = ``keccak(0x1901 || domainSeparator || structHash)``.

    Independent, pure recompute path used both by the compiler and by cross-validation. ``domain`` and
    ``message`` values are read tolerantly (``int(...)`` accepts str or int) so a JSON-canonical typed
    data hashes identically to the SDK's int-valued message.
    """
    domain = typed_data["domain"]
    message = typed_data["message"]
    domain_separator = keccak256(
        _DOMAIN_TYPE_HASH
        + keccak256(str(domain["name"]).encode("utf-8"))
        + keccak256(str(domain["version"]).encode("utf-8"))
        + _u256(int(domain["chainId"]))
        + _addr32(str(domain["verifyingContract"]))
    )
    struct_hash = keccak256(
        _ORDER_TYPE_HASH
        + _u256(int(message["salt"]))
        + _addr32(str(message["maker"]))
        + _addr32(str(message["signer"]))
        + _u256(int(message["tokenId"]))
        + _u256(int(message["makerAmount"]))
        + _u256(int(message["takerAmount"]))
        + _u256(int(message["side"]))
        + _u256(int(message["signatureType"]))
        + _u256(int(message["timestamp"]))
        + _bytes32(str(message["metadata"]))
        + _bytes32(str(message["builder"]))
    )
    return "0x" + keccak256(b"\x19\x01" + domain_separator + struct_hash).hex()


# ---------------------------------------------------------------------------
# Compiler inputs (frozen, extra="forbid") — the admitted post-rounding intent + bindings
# ---------------------------------------------------------------------------

WireSide = Literal["BUY", "SELL"]


class AdmittedPostRoundingIntent(_FrozenModel):
    """An E5-admitted, POST-ROUNDING order intent (a fixture-shaped input; E5 is not built yet).

    Amounts are the ALREADY-resolved venue-precision integer strings (6-decimal fixed math) so the
    compiler never re-rounds a fund-touching amount; ``native_price``/``size`` are carried for
    provenance/audit only (native ``[0, 1]`` price, CON-004). ``tif`` is the §2a order-type;
    ``post_only`` is valid ONLY with GTC/GTD (§6, enforced at compile time).
    """

    side: WireSide
    maker_amount: str
    taker_amount: str
    native_price: float
    size: float
    tif: str
    post_only: bool = False
    defer_exec: bool = False
    expiration: str = "0"

    @field_validator("native_price")
    @classmethod
    def _price_in_unit_interval(cls, value: float) -> float:
        return _reject_price_out_of_unit_interval(value)

    @field_validator("maker_amount", "taker_amount", "expiration")
    @classmethod
    def _non_negative_integer_string(cls, value: str) -> str:
        if not value.isdigit():
            raise ValueError(f"amount/expiration must be a non-negative integer string, got {value!r}")
        return value

    @field_validator("tif")
    @classmethod
    def _known_tif(cls, value: str) -> str:
        if value not in _ORDER_TYPES:
            raise ValueError(f"tif {value!r} is not a known V2 order type {sorted(_ORDER_TYPES)}")
        return value


class OrderMarket(_FrozenModel):
    """Market-level facts the order binds to: the CTF ``token_id`` and the neg-risk flag (§1a).

    ``neg_risk`` selects the V2 verifyingContract (standard vs neg-risk exchange), which changes the
    domain separator and therefore the order hash — a load-bearing V2 pin (E3-T0 §3d/§11).
    """

    token_id: str
    neg_risk: bool = False


class SignerBinding(_FrozenModel):
    """The per-order signer/identity binding (§1b/§2a). ``owner`` is a SECRET (never persisted raw).

    ``maker``/``signer`` are the single EOA (R4-A ``signatureType=0``); ``owner`` is the L2 API-key
    UUID that travels UNSIGNED in the wire wrapper and is committed to (never persisted) by the
    integrity commitment. ``salt``/``timestamp`` are the pinned uniqueness fields (§1b).
    """

    salt: str
    maker: str
    owner: str
    timestamp: str
    signer: str | None = None
    signature_type: int = _SUPPORTED_SIGNATURE_TYPE
    metadata: str = _BYTES32_ZERO
    builder: str = _BYTES32_ZERO


# ---------------------------------------------------------------------------
# The frozen compiled payload
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompiledSigningPayload:
    """The frozen output of the pure compiler (E3-T6).

    Attributes:
        canonical_v2_typed_data: The full EIP-712 typed data (§1a domain + §1b Order + message) — the
            exact structure the signer (E3-T7) hands to ``eth_signTypedData_v4``.
        eip712_digest: The V2 EIP-712 ``orderHash`` (``0x``-hex) — what gets signed. Equals
            ``venue_order_key`` (the §3d join key).
        wire_wrapper: The E3-T0 schema-derived UNSIGNED set (``expiration``, ``orderType``,
            ``postOnly``, ``owner``, ``deferExec``) — carries the SECRET ``owner`` transiently.
        integrity_commitment_hash: The one-way digest over the ENTIRE POST body incl. ``owner``
            (Codex-M1) — persisted digest-only pre-sign.
        venue_order_key: The venue-recognized V2 order hash (reconciliation join key, Codex-M2),
            DISTINCT from ``integrity_commitment_hash``.
    """

    canonical_v2_typed_data: dict[str, Any]
    eip712_digest: str
    wire_wrapper: dict[str, Any]
    integrity_commitment_hash: str
    venue_order_key: str
    _post_body: dict[str, Any] = field(repr=False, compare=False, default_factory=dict)

    def post_body(self) -> dict[str, Any]:
        """A fresh deep copy of the intended CLOB-V2 ``SendOrder`` POST body (owner INCLUDED).

        Returns a copy each call so a caller (e.g. the submit-time byte-verify) may mutate/normalize
        the body without corrupting this frozen payload's committed state.
        """
        import copy

        return copy.deepcopy(self._post_body)


class PolymarketV2SigningCompiler:
    """PURE CLOB-V2 signing compiler (E3-T6, REQ-018a): intent -> typed-data + digest + commitment.

    Stateless and side-effect-free: :meth:`compile` maps an admitted post-rounding intent + market +
    signer binding to a frozen :class:`CompiledSigningPayload`. It NEVER touches a network, a wallet, a
    private key, or a signer — only pure EIP-712 hashing + canonical digesting.
    """

    def compile(
        self,
        intent: AdmittedPostRoundingIntent,
        *,
        market: OrderMarket,
        binding: SignerBinding,
    ) -> CompiledSigningPayload:
        """Compile an admitted intent into a frozen signing payload (pure; fail-closed).

        Raises:
            FailClosed: If ``signature_type`` is not the R4-A EOA type (0), or ``post_only`` is combined
                with a non-resting order type (§6).
        """
        if int(binding.signature_type) != _SUPPORTED_SIGNATURE_TYPE:
            raise FailClosed(
                f"signatureType {binding.signature_type!r} is not the R4-A EOA type "
                f"({_SUPPORTED_SIGNATURE_TYPE}); POLY_PROXY/GNOSIS/1271 are out of scope (fail-closed)"
            )
        if intent.post_only and intent.tif not in _RESTING_ORDER_TYPES:
            raise FailClosed(
                f"postOnly is valid only with GTC/GTD, not orderType {intent.tif!r} (§6, fail-closed)"
            )

        verifying_contract = _V2_NEG_RISK_EXCHANGE if market.neg_risk else _V2_STANDARD_EXCHANGE
        signer = binding.signer if binding.signer is not None else binding.maker
        side_int = 0 if intent.side == "BUY" else 1

        # §1b signed message (JSON-canonical: uint fields as strings; side/signatureType as ints;
        # metadata/builder as 0x-hex bytes32). order_hash_from_typed_data reads these tolerantly.
        message: dict[str, Any] = {
            "salt": binding.salt,
            "maker": binding.maker,
            "signer": signer,
            "tokenId": market.token_id,
            "makerAmount": intent.maker_amount,
            "takerAmount": intent.taker_amount,
            "side": side_int,
            "signatureType": int(binding.signature_type),
            "timestamp": binding.timestamp,
            "metadata": binding.metadata,
            "builder": binding.builder,
        }
        typed_data: dict[str, Any] = {
            "primaryType": "Order",
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "Order": [
                    {"name": "salt", "type": "uint256"},
                    {"name": "maker", "type": "address"},
                    {"name": "signer", "type": "address"},
                    {"name": "tokenId", "type": "uint256"},
                    {"name": "makerAmount", "type": "uint256"},
                    {"name": "takerAmount", "type": "uint256"},
                    {"name": "side", "type": "uint8"},
                    {"name": "signatureType", "type": "uint8"},
                    {"name": "timestamp", "type": "uint256"},
                    {"name": "metadata", "type": "bytes32"},
                    {"name": "builder", "type": "bytes32"},
                ],
            },
            "domain": {
                "name": _EXCHANGE_DOMAIN_NAME,
                "version": _EXCHANGE_DOMAIN_VERSION_V2,
                "chainId": _CHAIN_ID,
                "verifyingContract": verifying_contract,
            },
            "message": message,
        }

        eip712_digest = order_hash_from_typed_data(typed_data)

        # §2a UNSIGNED wrapper (schema-derived set; carries the SECRET owner transiently).
        wire_wrapper: dict[str, Any] = {
            "expiration": intent.expiration,
            "orderType": intent.tif,
            "postOnly": intent.post_only,
            "owner": binding.owner,
            "deferExec": intent.defer_exec,
        }

        # §2b full SendOrder POST body (pre-sign: no signature yet). Wire ``side`` is the string.
        post_body: dict[str, Any] = {
            "order": {
                "salt": binding.salt,
                "maker": binding.maker,
                "signer": signer,
                "tokenId": market.token_id,
                "makerAmount": intent.maker_amount,
                "takerAmount": intent.taker_amount,
                "side": intent.side,
                "expiration": intent.expiration,
                "timestamp": binding.timestamp,
                "metadata": binding.metadata,
                "builder": binding.builder,
                "signatureType": int(binding.signature_type),
            },
            "owner": binding.owner,
            "orderType": intent.tif,
            "postOnly": intent.post_only,
            "deferExec": intent.defer_exec,
        }

        integrity_commitment_hash = integrity_commitment_over(post_body)

        return CompiledSigningPayload(
            canonical_v2_typed_data=typed_data,
            eip712_digest=eip712_digest,
            wire_wrapper=wire_wrapper,
            integrity_commitment_hash=integrity_commitment_hash,
            venue_order_key=eip712_digest,  # §3d: the V2 orderHash IS the reconciliation join key.
            _post_body=post_body,
        )


__all__ = [
    "AdmittedPostRoundingIntent",
    "CompiledSigningPayload",
    "OrderMarket",
    "PolymarketV2SigningCompiler",
    "SignerBinding",
    "keccak256",
    "order_hash_from_typed_data",
]
