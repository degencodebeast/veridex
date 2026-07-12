"""E3-T7 — ``ExecutionWalletBinding``: the manifest-bound custody pin for Mode-B (REQ-018b/c).

MONEY-NETWORK BOUNDARY. This module is PURE and OFFLINE: NO network, NO Privy call, NO real
credentials, and — load-bearing — NO local key material and NO local-key crypto library
(``eth_account`` / ``eth_keys`` / ``coincurve`` / ``web3`` are never imported anywhere in the
Mode-B graph; see :mod:`tests.test_dust_execution_sec_isolation`). It only declares:

* :class:`ExecutionWalletBinding` — the frozen custody binding whose :meth:`binding_hash` becomes the
  EXPLICIT ``execution_wallet_binding_hash`` manifest field (Codex-M3). The binding pins the Privy
  wallet address, the ``eip155:137`` chain, the venue, the Privy **policy CONTENT hash** (not a mutable
  ref), and the authorization-quorum ref + its content hash. Recomputing the binding hash from the live
  binding at admission AND restart and comparing to the pinned manifest field makes a silent reroute to a
  different wallet/policy STRUCTURALLY impossible (any different live binding → a different hash → fail
  closed).

* :class:`PrivyWalletPolicy` — a provider-neutral view of the wallet's authorization policy: a
  **default-deny** policy whose ONLY allow-rules are the two ``eth_signTypedData_v4`` typed-data rules
  (the V2 ``Order`` and the L1 ``ClobAuth``). Its :meth:`content_hash` pins WHAT the policy authorizes
  (rules + default action), so a policy whose *ref is unchanged but whose content was weakened* (e.g.
  via an app-secret update that adds a ``secp256k1_sign`` allow-rule) is caught at arming. ``owner_type``
  is tracked SEPARATELY (not in the content hash) so the arming gate can additionally require the policy
  resource to be **quorum-owned**, never app-secret-updatable (v0.6.3 Codex-m1 / Fable-m5).

* :class:`AuthorizationQuorum` — the key-quorum that owns the wallet/policy resource, with its own
  content hash (threshold + authorization-key refs) pinned in the binding.

The provider-neutral policy shape here is DELIBERATELY separate from any Privy wire serialization (the
Agent-Rank pattern: policy engine ≠ provider serialization) — only content hashes cross the boundary.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Literal

from veridex.runtime.evidence import serialize_payload

#: Polygon PoS in CAIP-2 form — the ONLY chain a Mode-B binding may pin (EVM, adapted from the
#: Solana ``eip155``-less reference). NOT a Solana ``solana:`` chain id.
CHAIN_ID_POLYGON: str = "eip155:137"

#: The ONLY RPC method the Mode-B wallet policy may ever allow (typed-data signing). Raw
#: ``secp256k1_sign`` and ``eth_sendTransaction`` are never allow-listed → denied at custody.
ALLOWED_SIGN_METHOD: str = "eth_signTypedData_v4"

#: The two typed-data ``primaryType``s the wallet may sign: the V2 order and the L1 ClobAuth.
ORDER_PRIMARY_TYPE: str = "Order"
CLOB_AUTH_PRIMARY_TYPE: str = "ClobAuth"

PolicyEffect = Literal["ALLOW", "DENY"]
#: ``quorum`` = the policy resource is owned by a key-quorum (immutable without quorum signatures).
#: ``app_secret`` = the resource is updatable with the app secret alone — REJECTED at arming.
PolicyOwnerType = Literal["quorum", "app_secret"]


def _sha256_canonical(payload: Any) -> str:
    """``sha256`` hexdigest over the canonical (sorted-key, compact) serialization of ``payload``."""
    return hashlib.sha256(serialize_payload(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PolicyRule:
    """One authorization rule: an ``effect`` on an RPC ``method`` for a typed-data ``primary_type``.

    ``primary_type`` is meaningful only for ``eth_signTypedData_v4`` (the EIP-712 ``primaryType`` the
    rule authorizes); for any other method it is ``"*"`` and the rule is expected to be a DENY.
    """

    method: str
    primary_type: str
    effect: PolicyEffect


@dataclass(frozen=True)
class PrivyWalletPolicy:
    """A provider-neutral default-deny wallet policy (only the two typed-data allow-rules).

    ``content_hash`` covers ONLY the security-relevant *content* — the ordered rule set + the default
    action — so a weakened-but-same-ref policy is caught (content-hash mismatch). ``owner_type`` is
    tracked separately and checked separately at arming (quorum ownership, v0.6.3 Codex-m1).
    """

    rules: tuple[PolicyRule, ...]
    default_action: PolicyEffect
    owner_type: PolicyOwnerType

    def _content(self) -> dict[str, Any]:
        # Order-independent content view of WHAT the policy authorizes (owner_type EXCLUDED on purpose).
        return {
            "default_action": self.default_action,
            "rules": sorted([r.method, r.primary_type, r.effect] for r in self.rules),
        }

    def content_hash(self) -> str:
        """Pin WHAT the policy authorizes (rules + default action). Excludes ``owner_type``."""
        return _sha256_canonical(self._content())

    def is_typed_data_only_default_deny(self) -> bool:
        """True iff the policy is default-DENY and its ONLY allow-rules are the two typed-data rules.

        Any allow-rule targeting a method other than ``eth_signTypedData_v4`` (e.g. ``secp256k1_sign``
        or ``eth_sendTransaction``) makes this False — so a widened policy fails the structural check.
        """
        if self.default_action != "DENY":
            return False
        if any(r.effect == "ALLOW" and r.method != ALLOWED_SIGN_METHOD for r in self.rules):
            return False
        allowed = {(r.method, r.primary_type) for r in self.rules if r.effect == "ALLOW"}
        return allowed == {
            (ALLOWED_SIGN_METHOD, ORDER_PRIMARY_TYPE),
            (ALLOWED_SIGN_METHOD, CLOB_AUTH_PRIMARY_TYPE),
        }


@dataclass(frozen=True)
class AuthorizationQuorum:
    """The key-quorum that owns the wallet/policy resource (non-secret key refs only).

    ``content_hash`` pins the threshold + the SET of authorization-key refs; a live quorum whose
    content differs from the pinned hash (e.g. threshold lowered, key swapped) fails at arming.
    """

    quorum_ref: str
    authorization_key_refs: tuple[str, ...]
    threshold: int

    def _content(self) -> dict[str, Any]:
        return {"threshold": self.threshold, "keys": sorted(self.authorization_key_refs)}

    def content_hash(self) -> str:
        return _sha256_canonical(self._content())


@dataclass(frozen=True)
class ExecutionWalletBinding:
    """The frozen custody binding pinned into the manifest (REQ-018b/c).

    Every field is a NON-SECRET reference or hash — never key material. ``binding_hash`` is the value
    stored in the explicit ``execution_wallet_binding_hash`` manifest field; recomputing it from the
    live binding and comparing at admission AND restart makes a wallet/policy reroute impossible.
    """

    provider: str  # "privy"
    wallet_ref: str  # non-secret Privy wallet id
    wallet_address: str  # the EVM address (0x…) the recovered signer MUST equal
    chain_id: str  # CAIP-2, must be eip155:137
    venue: str  # "polymarket"
    privy_policy_content_hash: str  # PrivyWalletPolicy.content_hash() of the pinned default-deny policy
    authorization_quorum_ref: str
    authorization_quorum_content_hash: str  # AuthorizationQuorum.content_hash()
    quorum_threshold: int

    def binding_hash(self) -> str:
        """Deterministic ``sha256`` over the canonical serialization of the whole binding.

        The address is lower-cased so a checksum-cased vs lower-cased address pin the same custody.
        """
        payload = {
            "provider": self.provider,
            "wallet_ref": self.wallet_ref,
            "wallet_address": self.wallet_address.lower(),
            "chain_id": self.chain_id,
            "venue": self.venue,
            "privy_policy_content_hash": self.privy_policy_content_hash,
            "authorization_quorum_ref": self.authorization_quorum_ref,
            "authorization_quorum_content_hash": self.authorization_quorum_content_hash,
            "quorum_threshold": self.quorum_threshold,
        }
        return _sha256_canonical(payload)


__all__ = [
    "ALLOWED_SIGN_METHOD",
    "CHAIN_ID_POLYGON",
    "CLOB_AUTH_PRIMARY_TYPE",
    "ORDER_PRIMARY_TYPE",
    "AuthorizationQuorum",
    "ExecutionWalletBinding",
    "PolicyEffect",
    "PolicyOwnerType",
    "PolicyRule",
    "PrivyWalletPolicy",
]
