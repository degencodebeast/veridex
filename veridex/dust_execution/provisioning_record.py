"""II-5b — the additive, immutable execution-wallet PROVISIONING RECORD (NET-NEW; offline).

WHY THIS EXISTS (Codex custody ruling). The reviewed :class:`ExecutionWalletBinding`
(``wallet_binding.py``) pins the Privy policy **CONTENT hash** but NOT the exact policy **resource
id** — so ``binding_hash()`` proves *what* a policy authorizes, never *which* policy resource is
attached. A content-equivalent policy substitution (a different ``policy_id`` with identical rules)
would therefore leave ``binding_hash()`` unchanged. This record closes that gap at the DEPLOYMENT
layer WITHOUT reopening the reviewed R4-A binding: it additionally pins the exact ``policy_id`` +
``quorum_id`` alongside the binding and its hash, and derives its OWN deterministic
``provisioning_record_hash`` that changes whenever the pinned resource identity changes.

This module is PURE and OFFLINE: it imports the reviewed binding by value only, holds NO key
material, NO signer, and NO network. Every field is a NON-SECRET id / address / hash / timestamp —
never a bearer token, authorization signature, or private key (COM-001 / secret hygiene).
"""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass
from typing import Any

from veridex.dust_execution.wallet_binding import ExecutionWalletBinding
from veridex.runtime.evidence import serialize_payload


def _sha256_canonical(payload: Any) -> str:
    """``sha256`` over the canonical (sorted-key, compact) serialization — binding-hash parity."""
    return hashlib.sha256(serialize_payload(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ExecutionWalletProvisioningRecord:
    """The additive, immutable provisioning record pinned into the deployment (NET-NEW for II-5b).

    Holds the reviewed :class:`ExecutionWalletBinding` VERBATIM (by value) plus the resource-identity
    pins the binding deliberately omits — the exact ``policy_id`` and ``quorum_id`` — so a
    content-equivalent policy/quorum substitution is caught here even though ``binding_hash()`` is
    unchanged. All fields are NON-SECRET.

    Attributes:
        instance_id: The deployed instance this record provisions a wallet for (persistence key).
        external_id: The deterministic, non-secret recovery handle (the create idempotency identity).
        wallet_id: The non-secret Privy wallet id.
        wallet_address: The EVM address the recovered signer must equal.
        chain_type: The provider wallet chain type — always ``ethereum`` for a Mode-B EVM wallet.
        policy_id: The EXACT pinned Privy policy resource id (the identity ``binding_hash`` omits).
        quorum_id: The EXACT pinned authorization key-quorum id.
        binding: The reviewed :class:`ExecutionWalletBinding` (pinned verbatim; never mutated here).
        binding_hash: ``binding.binding_hash()`` at provisioning time (pinned for drift detection).
        policy_content_hash: :meth:`PrivyWalletPolicy.content_hash` of the admitted policy (COARSE — it
            pins ``(method, primary_type, effect)`` + default action but NOT the per-field policy
            conditions; see ``policy_full_content_hash``).
        policy_full_content_hash: A full-fidelity hash over the COMPLETE admitted Privy policy incl every
            per-field condition (``field``/``operator``/``value``), so a field-level policy substitution
            the coarse ``policy_content_hash`` cannot see is still caught at this record layer.
        quorum_content_hash: :meth:`AuthorizationQuorum.content_hash` of the admitted quorum.
        provider_verification_ts: ISO-8601 UTC timestamp the provider resources were verified.
        provider_version: Non-secret provider/adapter version marker (audit provenance).
    """

    instance_id: str
    external_id: str
    wallet_id: str
    wallet_address: str
    chain_type: str
    policy_id: str
    quorum_id: str
    binding: ExecutionWalletBinding
    binding_hash: str
    policy_content_hash: str
    policy_full_content_hash: str
    quorum_content_hash: str
    provider_verification_ts: str
    provider_version: str

    def _content(self) -> dict[str, Any]:
        """The deterministic, order-independent content view pinned by the record hash.

        The nested :class:`ExecutionWalletBinding` object is folded in ONLY via its ``binding_hash``
        (a stable digest), while ``policy_id`` / ``quorum_id`` are included EXPLICITLY — that is the
        exact resource identity ``binding_hash`` omits, so a content-equivalent policy substitution
        (same rules, different ``policy_id``) changes THIS hash even though ``binding_hash`` does not.
        """
        return {
            "instance_id": self.instance_id,
            "external_id": self.external_id,
            "wallet_id": self.wallet_id,
            "wallet_address": self.wallet_address.lower(),
            "chain_type": self.chain_type,
            "policy_id": self.policy_id,
            "quorum_id": self.quorum_id,
            "binding_hash": self.binding_hash,
            "policy_content_hash": self.policy_content_hash,
            "policy_full_content_hash": self.policy_full_content_hash,
            "quorum_content_hash": self.quorum_content_hash,
            "provider_verification_ts": self.provider_verification_ts,
            "provider_version": self.provider_version,
        }

    def provisioning_record_hash(self) -> str:
        """Deterministic ``sha256`` over the record's non-secret content (drift-detection anchor)."""
        return _sha256_canonical(self._content())

    def to_json_dict(self) -> dict[str, Any]:
        """A JSON-serializable, NON-SECRET view of the whole record (for durable persistence)."""
        d = dict(self._content())
        d["binding"] = {
            "provider": self.binding.provider,
            "wallet_ref": self.binding.wallet_ref,
            "wallet_address": self.binding.wallet_address,
            "chain_id": self.binding.chain_id,
            "venue": self.binding.venue,
            "privy_policy_content_hash": self.binding.privy_policy_content_hash,
            "authorization_quorum_ref": self.binding.authorization_quorum_ref,
            "authorization_quorum_content_hash": self.binding.authorization_quorum_content_hash,
            "quorum_threshold": self.binding.quorum_threshold,
        }
        return d

    @classmethod
    def from_json_dict(cls, data: dict[str, Any]) -> ExecutionWalletProvisioningRecord:
        """Reconstruct a record from :meth:`to_json_dict` output (durable-load parity)."""
        b = data["binding"]
        binding = ExecutionWalletBinding(
            provider=b["provider"],
            wallet_ref=b["wallet_ref"],
            wallet_address=b["wallet_address"],
            chain_id=b["chain_id"],
            venue=b["venue"],
            privy_policy_content_hash=b["privy_policy_content_hash"],
            authorization_quorum_ref=b["authorization_quorum_ref"],
            authorization_quorum_content_hash=b["authorization_quorum_content_hash"],
            quorum_threshold=b["quorum_threshold"],
        )
        return cls(
            instance_id=data["instance_id"],
            external_id=data["external_id"],
            wallet_id=data["wallet_id"],
            wallet_address=data["wallet_address"],
            chain_type=data["chain_type"],
            policy_id=data["policy_id"],
            quorum_id=data["quorum_id"],
            binding=binding,
            binding_hash=data["binding_hash"],
            policy_content_hash=data["policy_content_hash"],
            policy_full_content_hash=data["policy_full_content_hash"],
            quorum_content_hash=data["quorum_content_hash"],
            provider_verification_ts=data["provider_verification_ts"],
            provider_version=data["provider_version"],
        )

    def copy(self) -> ExecutionWalletProvisioningRecord:
        """Return a deep copy (the in-memory store copies on write/read; the record is immutable)."""
        return copy.deepcopy(self)


__all__ = ["ExecutionWalletProvisioningRecord"]
