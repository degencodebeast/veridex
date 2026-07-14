"""E3-T1 — provider-neutral signer/write seam for the dust-execution money path (SAF-008, PAT-002).

MONEY-NETWORK BOUNDARY. This is the ONLY dust-lane surface whose I/O could reach a real venue, so
it is built fail-closed:

* ``WalletControlPlane`` / ``Signer`` is a provider-neutral Protocol. This module supplies the
  **Mode-A** ``LocalFakeWalletControlPlane`` — a deterministic, non-secret, offline signer (never a
  real key, never real signing). The **Mode-B** ``PrivyEvmWalletControlPlane`` (E3-T7) implements
  the SAME Protocol later; nothing here hardcodes Privy or a specific wire shape.

* The fail-closed arming gate is **default-deny**: a real-money submit is armed ONLY when ALL THREE
  conditions hold together — write enabled AND not ``dry_run`` AND a write client present. This
  mirrors and CONSUMES ``PolymarketAdapter._require_armed`` (``veridex/venues/polymarket.py``
  lines 472-511): the seam takes an injected ``arm`` gate and calls it FIRST, so when unarmed it
  raises BEFORE touching the signer or the venue wire (refuse-before-I/O). ``ArmingInputs`` +
  :func:`require_armed` provide the same all-three semantics provider-neutrally for callers that do
  not wire a Polymarket adapter.

* No secret is ever logged: this module performs NO logging, and the Mode-A artifact is an opaque,
  non-secret digest — never key material. Diagnostics on the gate are condition-only (booleans),
  never carrying a payload or a key.

Intra-lane imports only (``veridex.dust_execution.*`` + stdlib + pydantic). It does NOT import any
ranked/maker/live_recorder module, and it does NOT import ``veridex.venues`` — the venue arming gate
is INJECTED as a callable, keeping the seam provider-neutral and offline-import-safe.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import field_validator

from veridex.dust_execution.contracts import _FrozenModel, _reject_price_out_of_unit_interval
from veridex.dust_execution.risk import FailClosed

# The closed set of signer modes (boolean-safe telemetry labels — never a secret).
SignerMode = Literal["FAKE_LOCAL", "PRIVY_EVM"]


class SigningPayload(_FrozenModel):
    """Provider-neutral description of the order to sign/submit (frozen, JSON-primitive fields).

    Every field is a primitive so the payload can never smuggle a live client/wallet/key handle
    (mirrors the boundary-safety discipline of ``facade.MMExecutionToolResult``). ``native_price``
    is the venue-native probability price in ``[0, 1]`` (decimal odds are rejected at construction),
    already tick-rounded upstream — the seam submits with ``round_price=False``.
    """

    token_id: str
    side: str
    native_price: float
    size: float
    tif: str
    tick_size: str
    client_order_id: str | None = None

    @field_validator("native_price")
    @classmethod
    def _price_in_unit_interval(cls, value: float) -> float:
        return _reject_price_out_of_unit_interval(value)


class SignedArtifact(_FrozenModel):
    """The opaque, NON-SECRET result of signing (frozen, JSON-primitive fields).

    ``signature`` and ``order_digest`` are opaque strings — for Mode-A a deterministic sha256-based
    digest of the canonical payload, NEVER a private key or seed. A real provider (Mode-B) returns
    its own opaque signature here; the seam never inspects or logs it.
    """

    mode: SignerMode
    signature: str
    order_digest: str


@runtime_checkable
class WalletControlPlane(Protocol):
    """Provider-neutral signing control plane for the dust-execution write path (PAT-002).

    Implemented now by the Mode-A ``LocalFakeWalletControlPlane`` and later by the Mode-B
    ``PrivyEvmWalletControlPlane`` (E3-T7). The Protocol deliberately exposes ONLY a boolean-safe
    ``mode`` label and an async ``sign_order`` returning an opaque :class:`SignedArtifact` — it
    never returns or accepts raw key material, so no implementation can leak a key across this seam.
    """

    @property
    def mode(self) -> SignerMode:
        """Boolean-safe provider label (``"FAKE_LOCAL"`` / ``"PRIVY_EVM"``) — never a secret."""
        ...

    async def sign_order(self, payload: SigningPayload) -> SignedArtifact:
        """Return an opaque signed artifact for ``payload`` (Mode-A: deterministic, offline)."""
        ...


#: Public alias — the signer seam's provider-neutral Protocol is addressable as ``Signer``.
Signer = WalletControlPlane


class LocalFakeWalletControlPlane:
    """Mode-A FAKE/LOCAL signer (PAT-002): deterministic, non-secret, fully offline.

    Produces a stable sha256 digest of the canonical payload so offline tests are replayable. It
    NEVER holds or emits a real private key and performs NO network I/O and NO real signing — it
    exists purely so the seam can be exercised end-to-end offline while Mode-B stays UNARMED.
    """

    mode: SignerMode = "FAKE_LOCAL"

    def __init__(self, *, account_label: str = "fake-local-account") -> None:
        # A non-secret display label only; deliberately NOT a key/address.
        self._account_label = account_label

    async def sign_order(self, payload: SigningPayload) -> SignedArtifact:
        """Deterministically 'sign' by hashing the canonical payload (no key, no network)."""
        digest = hashlib.sha256(
            f"{self._account_label}|{payload.config_hash()}".encode()
        ).hexdigest()
        return SignedArtifact(
            mode="FAKE_LOCAL",
            signature=f"fakesig:{digest}",
            order_digest=payload.config_hash(),
        )


#: An arming gate: returns the armed write client for ``action`` or RAISES (fail-closed) when any
#: real-money condition is missing. In production this is ``PolymarketAdapter._require_armed``
#: (polymarket.py:472-511); provider-neutrally it is :meth:`ArmingInputs.arm_gate`. The returned
#: client is provider-defined (opaque to the seam, which only calls ``limit_order`` on it), so it is
#: typed ``Any`` — the seam couples to no concrete venue client type.
ArmGate = Callable[[str], Any]


@dataclass(frozen=True)
class ArmingInputs:
    """The three real-money arming conditions, mirrored from ``polymarket.py:472-511``.

    All three MUST be positively satisfied for a submit to arm (default-deny): ``write_enabled``
    true AND ``dry_run`` false AND a non-``None`` ``write_client``. Modelled as a frozen snapshot so
    the check cannot be partially mutated into a write.
    """

    write_enabled: bool
    dry_run: bool
    write_client: object | None

    def arm_gate(self) -> ArmGate:
        """Bind these inputs to an :data:`ArmGate` the seam can consume (provider-neutral)."""
        return lambda action: require_armed(self, action=action)


def require_armed(inputs: ArmingInputs, *, action: str) -> Any:
    """Return the write client only when ALL THREE conditions hold, else raise (fail-closed).

    Mirrors ``PolymarketAdapter._require_armed`` (polymarket.py:472-511): it refuses BEFORE
    returning any client, and the diagnostics are condition-only (no payload/secret). Raising
    :class:`FailClosed` — the lane's default-deny signal — leaves the fully-armed case as the ONLY
    non-raising path.
    """
    if not inputs.write_enabled:
        raise FailClosed(
            f"dust-execution {action} refused: write path not enabled (default-deny). "
            "All three of write-enabled AND not-dry-run AND write-client are required to arm."
        )
    if inputs.dry_run:
        raise FailClosed(
            f"dust-execution {action} refused: DRY_RUN active (the safe default). "
            "Arm a real write with write-enabled AND dry_run=False AND a write client."
        )
    if inputs.write_client is None:
        raise FailClosed(
            f"dust-execution {action} refused: no write client injected — the live wire is not "
            "reachable, so the seam fails closed."
        )
    return inputs.write_client


class SignerBackedWriteSeam:
    """Provider-neutral write seam: **arm (fail-closed) -> sign -> write**, in that strict order.

    The seam CONSUMES an injected ``arm`` gate (in production ``PolymarketAdapter._require_armed``,
    polymarket.py:472-511) and a :class:`WalletControlPlane` signer. Because ``arm`` runs FIRST,
    an unarmed submit raises BEFORE the signer or the venue wire is ever touched
    (refuse-before-I/O). A signing (provider) error likewise propagates before any wire I/O, so
    provider errors fail closed too. The seam performs NO logging, so no secret is emitted.
    """

    def __init__(self, *, arm: ArmGate, signer: WalletControlPlane) -> None:
        self._arm = arm
        self._signer = signer

    async def submit(self, payload: SigningPayload) -> object:
        """Arm, then sign, then write. Refuses (raises) before any I/O when unarmed.

        Args:
            payload: The provider-neutral order description to sign and submit.

        Returns:
            The venue write client's raw ``limit_order`` response (only reached when armed).

        Raises:
            FailClosed / PolymarketWriteDisabled: When unarmed — raised by the injected ``arm`` gate
                BEFORE any signing or wire I/O.
        """
        # (1) ARM FIRST — fail-closed. When unarmed this RAISES here, so neither the signer nor the
        #     wire below is reached. This is the refuse-before-I/O guarantee (structural: nothing
        #     with side effects runs before this line).
        client = self._arm("submit")
        # (2) SIGN — Mode-A offline deterministic artifact (Mode-B: a real provider). A provider
        #     error propagates here, so the wire below is never reached (provider errors fail closed).
        await self._signer.sign_order(payload)
        # (3) WRITE — only now does anything reach the venue wire. ``round_price=False``: the price is
        #     already the native tick-rounded value; the client must not re-round it.
        return await client.limit_order(
            ticker=payload.token_id,
            amount=payload.size,
            price=payload.native_price,
            tif=payload.tif,
            round_price=False,
            tick_size=payload.tick_size,
        )


__all__ = [
    "ArmGate",
    "ArmingInputs",
    "LocalFakeWalletControlPlane",
    "SignedArtifact",
    "Signer",
    "SignerBackedWriteSeam",
    "SigningPayload",
    "SignerMode",
    "WalletControlPlane",
    "require_armed",
]
