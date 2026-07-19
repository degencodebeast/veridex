"""II-5b — Privy execution-wallet PROVISIONING boundary + the deterministic recovery saga (offline).

THE MOST CUSTODY-SENSITIVE SLICE of the Post-2D integration. Inside the II-5 deploy saga this module
provisions ONE idempotent, policy-bound, KEYLESS Privy EVM execution wallet per MM deploy, reconciles
it deterministically after any crash, and persists the reviewed :class:`ExecutionWalletBinding` plus a
NET-NEW immutable :class:`ExecutionWalletProvisioningRecord`. It binds the wallet to R4-A ONLY.

SCOPE (Codex II-5b ruling). This module owns the PROVIDER PROTOCOL + typed request/response models, the
response-validation + policy/quorum-admission logic, a RECORDING provider fake, and the durable saga.
It does NOT own the concrete Privy HTTP / request-authorization adapter — that is II-5c
(``privy_http_client.py``, deliberately absent here). This module is PURE and OFFLINE: ZERO real Privy,
ZERO credentials, ZERO network, NO local key material, and NO local-key crypto library (never imports
``eth_account`` / ``eth_keys`` / ``coincurve`` / ``web3``). Every persisted / logged / hashed value is a
NON-SECRET id, address, or hash.

PRIVY API GROUNDING. The typed models below map to the REAL docs.privy.io server-wallet API (verified
2026-07-18; see ``.omc/implementation-state/tasks/ii5b-privy-api-grounding.md``). The wire field names
are Privy's actual names, NOT guesses:
  * create: ``POST /v1/wallets`` body ``{chain_type:"ethereum", external_id, policy_ids:[<one>],
    owner_id:<quorum id>}``; idempotency via the ``privy-idempotency-key`` header (24h retention).
  * wallet response ``Wallet``: ``id`` (→ wallet_ref), ``address``, ``chain_type``, ``policy_ids[]``,
    ``owner_id`` (the key-quorum id of the owner → authorization_quorum_ref), ``external_id``,
    ``authorization_threshold``.
  * exact lookup: ``GET /v1/wallets/ext_wal_<external_id>`` (external_id is UNIQUE-PER-APP → 0-or-1);
    the ``?external_id=`` list form is handled defensively (>1 = custody incident → STOP).
  * policy: ``GET /v1/policies/{id}`` → ``{id, version, name, chain_type, rules[]:{method,
    conditions[], action}}``; the typed-data ``primary_type`` lives at ``condition.typed_data.
    primary_type``; default-deny is STRUCTURAL. Policies are MUTABLE → re-admit at startup + binding.
  * key quorum: ``GET /v1/key_quorums/{id}`` → ``{id, authorization_threshold,
    authorization_keys[]:{public_key}, user_ids[], key_quorum_ids[]}``. Verified SEPARATELY.

The FOUR REQUIRED custody corrections are enforced here (each has a RED test):
1. A deterministic, non-secret ``external_id`` is persisted in the attempt claim BEFORE the first
   provider mutation — the recovery key, never learned after ``create_wallet``.
2. Reconciliation goes through an EXACT typed ``get_wallet_by_external_id`` with explicit
   zero / one / ambiguous(>1) handling. Ambiguous is a custody incident → STOP (never pick/sort/mint).
3. The key QUORUM is fetched + verified SEPARATELY from the policy; the wallet must be owned by that
   quorum (``owner_id``) and carry EXACTLY the pinned singleton policy.
4. After an ambiguous create timeout, once the provider's 24h idempotency record MAY have expired we
   NEVER issue a fresh mutation — we stay ``WALLET_CREATE_UNCERTAIN`` and require operator reconciliation.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from veridex.deploy.attempt import _STATUS_RANK, AttemptStatus  # forward-only rank (read-only reuse)
from veridex.dust_execution.provisioning_record import (
    ExecutionWalletProvisioningRecord,
    _sha256_canonical,  # single canonical-hash definition (imported to keep hash parity, not duplicated)
)
from veridex.dust_execution.wallet_binding import (
    CHAIN_ID_POLYGON,
    AuthorizationQuorum,
    ExecutionWalletBinding,
    PolicyEffect,
    PolicyOwnerType,
    PolicyRule,
    PrivyWalletPolicy,
)
from veridex.runtime.evidence import serialize_payload

if TYPE_CHECKING:
    from veridex.deploy.instance import AgentInstance

#: Privy's idempotency response retention window (documented 24h bound). Past this the idempotency
#: record MAY be gone and a fresh create would re-execute (re-mint) → fail-closed no-retry.
IDEMPOTENCY_WINDOW_S: int = 24 * 60 * 60

#: The only wallet ``chain_type`` a Mode-B EVM wallet may report (Privy's coarse EVM family marker;
#: the venue network is pinned SEPARATELY as ``eip155:137`` in the binding — never inferred from this).
CHAIN_TYPE_ETHEREUM: str = "ethereum"

#: The deployed venue the binding pins (Polymarket CLOB on Polygon).
_VENUE: str = "polymarket"


# ---------------------------------------------------------------------------
# Fail-closed custody error taxonomy (every path here fails CLOSED, never open)
# ---------------------------------------------------------------------------


class ProvisioningError(Exception):
    """Base class for every execution-wallet provisioning failure (all fail closed)."""


class ResponseValidationError(ProvisioningError):
    """A provider response failed strict typed parsing or exact-field admission (untrusted input)."""


class PolicyAdmissionError(ProvisioningError):
    """The live policy and/or key quorum did not match the pinned custody contract (drift/mismatch)."""


class AmbiguousWalletError(ProvisioningError):
    """More than one wallet resolved for a single ``external_id`` — a custody incident. STOP."""


class WalletCreateUncertain(ProvisioningError):
    """Wallet creation is unresolved and cannot be safely retried — requires operator reconciliation."""


class ProviderTimeoutError(ProvisioningError):
    """``create_wallet`` timed out with acceptance UNKNOWN — reconcile by ``external_id`` before retry."""


class ProviderUnavailableError(ProvisioningError):
    """The provider is unreachable — custody READINESS is unavailable (must not arm; fails closed)."""


class IdempotencyKeyConflictError(ProvisioningError):
    """The same idempotency key was reused with a DIFFERENT request body — refused (Privy 400)."""


# ---------------------------------------------------------------------------
# Deterministic, non-secret recovery identifiers (CORRECTION 1)
# ---------------------------------------------------------------------------


def derive_external_id(attempt_id: str) -> str:
    """Derive the deterministic, NON-SECRET external recovery id from the durable attempt identity.

    Pure function of the non-secret ``attempt_id`` (recomputable identically on every retry / restart
    WITHOUT reading post-create provider state) — the RECOVERY KEY (CORRECTION 1), persisted in the
    attempt claim before the first mutation. The output is URL-safe ``[a-zA-Z0-9_-]`` and ≤ 64 chars,
    matching Privy's ``external_id`` constraints (write-once, UNIQUE-PER-APP; grounding §4).

    Args:
        attempt_id: The durable deployment-attempt identifier.

    Returns:
        A stable ``exwallet-<hex>`` handle (non-secret, deterministic, URL-safe).
    """
    digest = hashlib.sha256(f"veridex-exec-wallet-v1:{attempt_id}".encode()).hexdigest()
    return f"exwallet-{digest[:32]}"


def provider_idempotency_key(attempt_id: str) -> str:
    """Derive the deterministic provider idempotency key (stable across identical retries).

    Sent by II-5c as the ``privy-idempotency-key`` header value; within 24h an identical key + body
    returns the stored response (never re-mints), and a different body is refused (Privy 400).
    """
    digest = hashlib.sha256(f"veridex-exec-wallet-create-v1:{attempt_id}".encode()).hexdigest()
    return f"provcreate-{digest[:32]}"


def is_evm_address(value: str) -> bool:
    """True iff ``value`` is a syntactically valid ``0x``-prefixed 20-byte EVM address (hex)."""
    if not isinstance(value, str) or len(value) != 42 or not value.startswith("0x"):
        return False
    body = value[2:]
    return len(body) == 40 and all(c in "0123456789abcdefABCDEF" for c in body)


# ---------------------------------------------------------------------------
# Typed provider request/response models (additive — NEVER overload the signing path types)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProvisioningRequestAuth:
    """Provisioning request-authorization CONTEXT — a NON-SECRET reference only.

    Distinct from the reviewed typed-data-signing :class:`PrivyAuthContext` (never overloaded). Carries
    ONLY a non-secret ``authorization_key_id`` reference; the actual P-256 authorization key material
    lives exclusively in the secret manager and is consumed by the II-5c HTTP adapter (which signs the
    ``privy-authorization-signature`` header) — never here, never persisted, never logged. There is
    deliberately no private-key field.
    """

    authorization_key_id: str

    def __repr__(self) -> str:  # explicit: prove no secret can hide in the repr
        return f"ProvisioningRequestAuth(authorization_key_id={self.authorization_key_id!r})"


@dataclass(frozen=True)
class WalletNotFound:
    """Typed sentinel: an exact ``external_id`` lookup resolved to ZERO wallets (not a wallet)."""

    external_id: str


@dataclass(frozen=True)
class ProvisionedWallet:
    """A strictly-typed, NON-SECRET view of a provisioned Privy EVM wallet (the ``Wallet`` object).

    ``wallet_id`` maps from Privy's ``id`` (the primary wallet identifier). ``owner_id`` is the
    key-quorum id of the wallet's owner (grounding §2). ``authorization_threshold`` is the wallet's
    reported owner-quorum threshold (a secondary cross-check; the authoritative threshold is verified
    against the ``GET /v1/key_quorums`` resource).
    """

    wallet_id: str
    address: str
    chain_type: str
    owner_id: str
    policy_ids: tuple[str, ...]
    external_id: str
    authorization_threshold: int | None = None

    @classmethod
    def from_provider(cls, payload: Mapping[str, Any]) -> ProvisionedWallet:
        """Strictly parse an untrusted Privy ``Wallet`` payload (fail closed on any malformation).

        Reads Privy's real field names: ``id`` (→ ``wallet_id``), ``address``, ``chain_type``,
        ``owner_id``, ``policy_ids``, ``external_id``, ``authorization_threshold``. ``external_id`` is
        REQUIRED here — II-5b only ever admits a wallet that carries our deterministic recovery id.
        """
        if not isinstance(payload, Mapping):
            raise ResponseValidationError("wallet payload must be a mapping")
        try:
            wallet_id = payload["id"]
            address = payload["address"]
            chain_type = payload["chain_type"]
            owner_id = payload["owner_id"]
            policy_ids = payload["policy_ids"]
            external_id = payload["external_id"]
        except (KeyError, TypeError) as exc:
            raise ResponseValidationError(f"wallet payload missing required field: {exc}") from exc
        if not all(isinstance(v, str) for v in (wallet_id, address, chain_type, owner_id, external_id)):
            raise ResponseValidationError("wallet payload has a non-string scalar field")
        if not isinstance(policy_ids, (list, tuple)) or not all(isinstance(p, str) for p in policy_ids):
            raise ResponseValidationError("wallet payload 'policy_ids' must be a list of strings")
        threshold = payload.get("authorization_threshold")
        if threshold is not None and (not isinstance(threshold, int) or isinstance(threshold, bool)):
            raise ResponseValidationError("wallet authorization_threshold must be an int when present")
        return cls(
            wallet_id=wallet_id,
            address=address,
            chain_type=chain_type,
            owner_id=owner_id,
            policy_ids=tuple(policy_ids),
            external_id=external_id,
            authorization_threshold=threshold,
        )


# The Privy policy-rule condition keys we explicitly model. Any OTHER key on a condition fails closed
# (Codex "reject any condition you do not explicitly model" — no lossy projection). ``typed_data`` holds
# the EIP-712 ``primary_type`` we pin; ``field``/``operator``/``value`` are the per-field restrictions.
_ALLOWED_CONDITION_KEYS = frozenset({"field_source", "typed_data", "field", "operator", "value"})
# Top-level Privy ``Policy`` keys we model (grounding §5). ``owner_id`` is TOLERATED-if-present but the
# policy GET is NOT guaranteed to surface the policy's owner — see the ASSUMPTION in admit_policy_and_quorum.
_ALLOWED_POLICY_KEYS = frozenset({"id", "version", "name", "chain_type", "rules", "owner_id"})
_ALLOWED_RULE_KEYS = frozenset({"id", "name", "method", "conditions", "action"})


@dataclass(frozen=True)
class ProviderPolicyRule:
    """One faithfully-parsed Privy policy rule.

    ``primary_type`` is EXTRACTED from the rule's condition ``typed_data.primary_type`` (grounding §5);
    ``conditions`` holds each condition's canonical JSON string so the FULL rule (incl per-field
    ``field``/``operator``/``value`` restrictions) is captured for :meth:`ProviderPolicy.full_content_hash`
    — the reviewed :class:`PrivyWalletPolicy` content hash is coarser and does NOT see these (see A1).
    """

    method: str
    action: PolicyEffect
    primary_type: str
    conditions: tuple[str, ...]


@dataclass(frozen=True)
class ProviderPolicy:
    """A strictly-typed, NON-SECRET view of a Privy wallet policy (COMPLETE rule set; no projection)."""

    policy_id: str
    chain_type: str
    rules: tuple[ProviderPolicyRule, ...]
    version: str = "1.0"
    name: str = ""

    @classmethod
    def from_provider(cls, payload: Mapping[str, Any]) -> ProviderPolicy:
        """Strictly parse a Privy ``Policy`` — REJECT any unknown security-relevant field.

        Hashing only a simplified local projection would let provider-side semantics drift (e.g. a new
        conditional allow, or a widened ``value`` on an existing condition) while the local content hash
        stays unchanged. So an unmodeled top-level key, an unmodeled per-rule key, or an unmodeled
        per-condition key fails closed rather than being dropped. The typed-data ``primary_type`` is
        extracted from ``condition.typed_data.primary_type``; default-deny is STRUCTURAL (Privy denies
        anything not matched by an ALLOW rule).
        """
        if not isinstance(payload, Mapping):
            raise ResponseValidationError("policy payload must be a mapping")
        unknown = set(payload) - _ALLOWED_POLICY_KEYS
        if unknown:
            raise ResponseValidationError(f"policy payload has unmodeled security-relevant field(s): {sorted(unknown)}")
        try:
            policy_id = payload["id"]
            chain_type = payload["chain_type"]
            raw_rules = payload["rules"]
        except KeyError as exc:
            raise ResponseValidationError(f"policy payload missing required field: {exc}") from exc
        if not isinstance(raw_rules, (list, tuple)):
            raise ResponseValidationError("policy 'rules' must be a list")
        rules = tuple(cls._parse_rule(raw) for raw in raw_rules)
        return cls(
            policy_id=policy_id,
            chain_type=chain_type,
            rules=rules,
            version=str(payload.get("version", "1.0")),
            name=str(payload.get("name", "")),
        )

    @staticmethod
    def _parse_rule(raw: Any) -> ProviderPolicyRule:
        if isinstance(raw, ProviderPolicyRule):
            return raw
        if not isinstance(raw, Mapping):
            raise ResponseValidationError("each policy rule must be a mapping")
        unknown = set(raw) - _ALLOWED_RULE_KEYS
        if unknown:
            raise ResponseValidationError(f"policy rule has unmodeled field(s): {sorted(unknown)}")
        method = raw.get("method")
        action = raw.get("action")
        if not isinstance(method, str):
            raise ResponseValidationError("policy rule 'method' must be a string")
        if action not in ("ALLOW", "DENY"):
            raise ResponseValidationError(f"policy rule 'action' must be ALLOW|DENY, got {action!r}")
        raw_conditions = raw.get("conditions", [])
        if not isinstance(raw_conditions, (list, tuple)):
            raise ResponseValidationError("policy rule 'conditions' must be a list")
        primary_types: set[str] = set()
        canonical_conditions: list[str] = []
        for cond in raw_conditions:
            if not isinstance(cond, Mapping):
                raise ResponseValidationError("each policy rule condition must be a mapping")
            unknown_cond = set(cond) - _ALLOWED_CONDITION_KEYS
            if unknown_cond:
                raise ResponseValidationError(f"policy rule condition has unmodeled field(s): {sorted(unknown_cond)}")
            typed_data = cond.get("typed_data")
            if typed_data is not None:
                if not isinstance(typed_data, Mapping) or "primary_type" not in typed_data:
                    raise ResponseValidationError("condition 'typed_data' must carry a 'primary_type'")
                pt = typed_data["primary_type"]
                if not isinstance(pt, str):
                    raise ResponseValidationError("condition primary_type must be a string")
                primary_types.add(pt)
            canonical_conditions.append(serialize_payload(_to_plain(cond)))
        if len(primary_types) > 1:
            raise ResponseValidationError(f"policy rule mixes primary types: {sorted(primary_types)}")
        # ``*`` = no typed-data primary_type pinned; such a rule can never match the pinned Order/ClobAuth
        # allow-set, so is_typed_data_only_default_deny() rejects it (fail closed) rather than admitting it.
        primary_type = next(iter(primary_types)) if primary_types else "*"
        return ProviderPolicyRule(
            method=method,
            action=action,
            primary_type=primary_type,
            conditions=tuple(canonical_conditions),
        )

    def to_wallet_policy(self, *, owner_type: PolicyOwnerType) -> PrivyWalletPolicy:
        """Project onto the reviewed provider-neutral :class:`PrivyWalletPolicy` (structural check).

        Maps each Privy rule to ``(method, primary_type, effect)`` with a STRUCTURAL default-deny (Privy
        denies anything not matched by an ALLOW rule). This is the COARSE structural admission — the
        per-field ``conditions`` are captured separately by :meth:`full_content_hash` (see A1).
        """
        rules = tuple(PolicyRule(method=r.method, primary_type=r.primary_type, effect=r.action) for r in self.rules)
        return PrivyWalletPolicy(rules=rules, default_action="DENY", owner_type=owner_type)

    def full_content_hash(self) -> str:
        """Deterministic hash over the COMPLETE policy — incl every per-field condition (see A1).

        The reviewed ``PrivyWalletPolicy.content_hash()`` only pins ``(method, primary_type, effect)`` +
        default action, so a real Privy policy that narrows a condition ``value`` (e.g. a spend cap or a
        pinned ``verifyingContract``) would NOT change that coarse hash. This full hash DOES change, so a
        field-level policy substitution is caught at the provisioning-record layer.
        """
        return _sha256_canonical(
            {
                "chain_type": self.chain_type,
                "version": self.version,
                "rules": sorted(
                    [r.method, r.action, r.primary_type, sorted(r.conditions)] for r in self.rules
                ),
            }
        )


_ALLOWED_QUORUM_KEYS = frozenset(
    {"id", "display_name", "authorization_threshold", "authorization_keys", "user_ids", "key_quorum_ids"}
)


@dataclass(frozen=True)
class ProviderKeyQuorum:
    """A strictly-typed, NON-SECRET view of a Privy ``KeyQuorum`` (threshold + P-256 auth-key refs)."""

    quorum_id: str
    threshold: int
    authorization_key_refs: tuple[str, ...]
    user_ids: tuple[str, ...] = ()
    key_quorum_ids: tuple[str, ...] = ()

    @classmethod
    def from_provider(cls, payload: Mapping[str, Any]) -> ProviderKeyQuorum:
        """Strictly parse a Privy ``KeyQuorum`` — REJECT unknown fields; read the P-256 public keys.

        ``authorization_key_refs`` are the ``authorization_keys[].public_key`` values (non-secret P-256
        SPKI public keys — never private material). Nested member quorums (``key_quorum_ids``) and
        ``user_ids`` are captured so nested ownership can be accounted for (grounding §6).
        """
        if not isinstance(payload, Mapping):
            raise ResponseValidationError("quorum payload must be a mapping")
        unknown = set(payload) - _ALLOWED_QUORUM_KEYS
        if unknown:
            raise ResponseValidationError(f"quorum payload has unmodeled field(s): {sorted(unknown)}")
        try:
            quorum_id = payload["id"]
            threshold = payload["authorization_threshold"]
            raw_keys = payload["authorization_keys"]
        except KeyError as exc:
            raise ResponseValidationError(f"quorum payload missing required field: {exc}") from exc
        if not isinstance(threshold, int) or isinstance(threshold, bool):
            raise ResponseValidationError("quorum authorization_threshold must be an int")
        if not isinstance(raw_keys, (list, tuple)):
            raise ResponseValidationError("quorum authorization_keys must be a list")
        key_refs: list[str] = []
        for entry in raw_keys:
            if isinstance(entry, str):
                key_refs.append(entry)
            elif isinstance(entry, Mapping) and isinstance(entry.get("public_key"), str):
                key_refs.append(entry["public_key"])
            else:
                raise ResponseValidationError("each authorization_key must carry a string 'public_key'")
        user_ids = tuple(payload.get("user_ids", ()) or ())
        nested = tuple(payload.get("key_quorum_ids", ()) or ())
        return cls(
            quorum_id=quorum_id,
            threshold=threshold,
            authorization_key_refs=tuple(key_refs),
            user_ids=user_ids,
            key_quorum_ids=nested,
        )

    def to_authorization_quorum(self) -> AuthorizationQuorum:
        """Project onto the reviewed :class:`AuthorizationQuorum` (content-hash parity)."""
        return AuthorizationQuorum(
            quorum_ref=self.quorum_id,
            authorization_key_refs=self.authorization_key_refs,
            threshold=self.threshold,
        )


def _to_plain(value: Any) -> Any:
    """Recursively convert a mapping/sequence into plain dict/list for stable canonical serialization."""
    if isinstance(value, Mapping):
        return {k: _to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Pinned custody config (operator-supplied; fail-closed if absent when II-5b is enabled)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PinnedProvisioningConfig:
    """The operator-pinned custody contract every binding is admitted against (all NON-SECRET).

    The VALUES are supplied by the operator via settings (never invented in code); the wallet claim
    stays withheld until they are present AND Level-3 acceptance has run (CODE_READY / CLAIM_WITHHELD).

    ``policy_full_content_hash`` is REQUIRED whenever provisioning is enabled (config resolution +
    admission both fail closed if it is absent): the reviewed ``PrivyWalletPolicy.content_hash`` is
    coarse (ignores per-field conditions), so it is the ONLY gate that catches a field-level policy
    substitution the coarse hash cannot see (A1). The field is typed ``str | None`` only so a fail-closed
    test can construct an unpinned config and prove admission rejects it — production never carries None.
    """

    policy_id: str
    quorum_id: str
    owner_id: str
    policy_content_hash: str
    quorum_content_hash: str
    quorum_threshold: int
    authorization_key_id: str
    policy_full_content_hash: str | None = None

    def request_auth(self) -> ProvisioningRequestAuth:
        """Build the NON-SECRET request-auth context (a key reference only, never key material)."""
        return ProvisioningRequestAuth(authorization_key_id=self.authorization_key_id)


# ---------------------------------------------------------------------------
# The provider protocol (additive — NEVER the signing-path RecordingPrivyClient)
# ---------------------------------------------------------------------------


@runtime_checkable
class PrivyProvisioningProvider(Protocol):
    """The keyless wallet-PROVISIONING boundary (create + read only; NO signing / transaction surface).

    Deliberately create/read only: there is no ``sign_typed_data`` / ``sign_raw_hash`` /
    ``send_transaction`` here — provisioning can never move money, and signing goes to a SEPARATE
    ``POST /v1/wallets/{id}/rpc`` boundary consumed only by the reviewed R4-A ``arm_mode_b`` /
    ``execute_with`` path this module never touches. II-5c implements these against the Privy HTTP API
    (``get_wallet_by_external_id`` SHOULD prefer ``GET /v1/wallets/ext_wal_<external_id>``, resolving
    0-or-1, and raise :class:`AmbiguousWalletError` if the defensive list form ever returns >1).
    """

    async def create_wallet(
        self,
        *,
        external_id: str,
        owner_id: str,
        policy_ids: tuple[str, ...],
        idempotency_key: str,
        request_auth: ProvisioningRequestAuth,
    ) -> ProvisionedWallet: ...

    async def get_wallet_by_external_id(
        self, external_id: str, *, request_auth: ProvisioningRequestAuth
    ) -> ProvisionedWallet | WalletNotFound: ...

    async def get_wallet(
        self, wallet_id: str, *, request_auth: ProvisioningRequestAuth
    ) -> ProvisionedWallet: ...

    async def get_policy(
        self, policy_id: str, *, request_auth: ProvisioningRequestAuth
    ) -> ProviderPolicy: ...

    async def get_key_quorum(
        self, quorum_id: str, *, request_auth: ProvisioningRequestAuth
    ) -> ProviderKeyQuorum: ...


# ---------------------------------------------------------------------------
# Admission + response validation (CORRECTION 2 + 3; fail-closed on ANY mismatch)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdmissionResult:
    """The verified live custody resources admitted against the pinned contract."""

    wallet_policy: PrivyWalletPolicy
    auth_quorum: AuthorizationQuorum
    policy_full_content_hash: str


def verify_provisioned_wallet(
    wallet: ProvisionedWallet, *, pinned: PinnedProvisioningConfig, expected_external_id: str
) -> None:
    """Fail-closed exact-field admission of one provisioned wallet (no field may drift).

    Raises:
        ResponseValidationError: if chain type is not Ethereum; the external id / owner / policy set /
            address are not EXACTLY the pinned values; the wallet id is empty; or (when the wallet
            reports one) its ``authorization_threshold`` disagrees with the pinned quorum threshold.
    """
    if wallet.chain_type != CHAIN_TYPE_ETHEREUM:
        raise ResponseValidationError(f"wallet chain_type must be {CHAIN_TYPE_ETHEREUM!r}, got {wallet.chain_type!r}")
    if wallet.external_id != expected_external_id:
        raise ResponseValidationError("wallet external_id does not equal the durable requested value")
    if wallet.owner_id != pinned.owner_id:
        raise ResponseValidationError("wallet owner_id does not equal the pinned authorization-quorum id")
    if tuple(wallet.policy_ids) != (pinned.policy_id,):
        raise ResponseValidationError("wallet policy set is not EXACTLY the pinned singleton policy")
    if not wallet.wallet_id:
        raise ResponseValidationError("wallet id is empty")
    if not is_evm_address(wallet.address):
        raise ResponseValidationError(f"wallet address is not a valid EVM address: {wallet.address!r}")
    if wallet.authorization_threshold is not None and wallet.authorization_threshold != pinned.quorum_threshold:
        raise ResponseValidationError("wallet authorization_threshold does not equal the pinned quorum threshold")


def _assert_record_matches(
    record: ExecutionWalletProvisioningRecord,
    *,
    wallet: ProvisionedWallet,
    binding: ExecutionWalletBinding,
    pinned: PinnedProvisioningConfig,
    admission: AdmissionResult,
    external_id: str,
) -> None:
    """Fail closed if an already-committed provisioning record disagrees with the reconciled wallet
    OR the CURRENT live admission.

    Used on crash-recovery re-entry (MAJOR-1): the record committed by a prior attempt must describe
    the SAME wallet / binding / policy / quorum we just reconciled. A divergence means the recovered
    wallet is not the one the immutable record pinned — a custody incident, never silently reused.

    Critically (MAJOR-2), the reviewed ``binding_hash`` only pins the COARSE policy content hash, so a
    field-level policy substitution (different ``policy_full_content_hash`` but identical coarse hash,
    ``policy_id``, quorum, and wallet) would slip through a binding-only check. We therefore ALSO
    compare the record's ``policy_full_content_hash`` against the CURRENT live admission's full hash
    (plus the coarse policy + quorum content hashes) — reusing a record that describes a different live
    policy than the one just re-admitted is exactly the drift the full-policy hash exists to catch.
    """
    if (
        record.wallet_id != wallet.wallet_id
        or record.wallet_address.lower() != wallet.address.lower()
        or record.external_id != external_id
        or record.policy_id != pinned.policy_id
        or record.quorum_id != pinned.quorum_id
        or record.binding_hash != binding.binding_hash()
    ):
        raise ResponseValidationError(
            "committed provisioning record does not match the reconciled wallet/binding (custody incident)"
        )
    if (
        record.policy_full_content_hash != admission.policy_full_content_hash
        or record.policy_content_hash != pinned.policy_content_hash
        or record.quorum_content_hash != pinned.quorum_content_hash
    ):
        raise ResponseValidationError(
            "committed provisioning record describes a different live policy/quorum than the current "
            "admission (full-policy or content-hash drift) — fail closed, never reuse a stale record"
        )


def _assert_consistent(created: ProvisionedWallet, reread: ProvisionedWallet) -> None:
    """Fail closed if a create result and its subsequent exact re-read disagree on any field."""
    if (
        created.wallet_id != reread.wallet_id
        or created.address.lower() != reread.address.lower()
        or created.owner_id != reread.owner_id
        or tuple(created.policy_ids) != tuple(reread.policy_ids)
        or created.external_id != reread.external_id
        or created.chain_type != reread.chain_type
    ):
        raise ResponseValidationError("create result and re-read disagree — inconsistent provider state")


async def admit_policy_and_quorum(
    provider: PrivyProvisioningProvider,
    pinned: PinnedProvisioningConfig,
    *,
    request_auth: ProvisioningRequestAuth,
) -> AdmissionResult:
    """Fetch + verify the pinned policy AND (separately) its key quorum (CORRECTION 3; fail-closed).

    Verifies default-deny + only the approved typed-data methods/primary types + pinned policy content
    hash (and, when pinned, the full-fidelity policy hash), THEN — as a separate live resource — the
    quorum threshold + complete auth-key set + pinned quorum content hash, and that it is a genuine
    multi-party quorum (threshold >= 2 with enough distinct keys). A policy response alone can never
    prove the live threshold + authorization-key set, so the two are always fetched independently.

    ASSUMPTION (A: policy owner). Privy's ``GET /v1/policies/{id}`` response is NOT guaranteed to
    surface the policy's OWNER (the key quorum permitted to update it); the grounding lists
    ``{id, version, name, chain_type, rules}``. So owner-of-the-POLICY is NOT verified here — the
    verified owner binding is the WALLET's ``owner_id`` == the pinned quorum id (in
    :func:`verify_provisioned_wallet`) plus the separately-verified quorum resource. If II-5c confirms
    the policy response carries an owner reference, add an exact policy-owner == pinned-quorum check.

    Raises:
        ProviderUnavailableError: if the provider is unreachable (readiness unavailable).
        PolicyAdmissionError: on any policy/quorum drift or mismatch versus the pinned contract.
    """
    # MAJOR-1 (custody invariant, defense-in-depth): the wallet owner we admit against MUST be the
    # SAME quorum we verify here. A divergent pinned owner would admit a wallet owned by a quorum whose
    # threshold/keys/content-hash were never checked — fail closed (config resolution enforces this too).
    if pinned.owner_id != pinned.quorum_id:
        raise PolicyAdmissionError(
            f"pinned owner_id {pinned.owner_id!r} must equal the verified quorum_id {pinned.quorum_id!r}"
        )

    policy = await provider.get_policy(pinned.policy_id, request_auth=request_auth)
    if policy.policy_id != pinned.policy_id:
        raise PolicyAdmissionError("returned policy id does not equal the pinned policy id")
    if policy.chain_type != CHAIN_TYPE_ETHEREUM:
        raise PolicyAdmissionError(f"policy chain_type must be {CHAIN_TYPE_ETHEREUM!r}, got {policy.chain_type!r}")
    wallet_policy = policy.to_wallet_policy(owner_type="quorum")
    if not wallet_policy.is_typed_data_only_default_deny():
        raise PolicyAdmissionError("policy is not default-deny / typed-data-only")
    if wallet_policy.content_hash() != pinned.policy_content_hash:
        raise PolicyAdmissionError("policy content hash does not match the pinned value")
    full_hash = policy.full_content_hash()
    # REQUIRED (not opt-in): the full-fidelity policy hash is the ONLY gate that catches a field-level
    # policy substitution the coarse content hash cannot see, so a pinned config that lacks it fails
    # closed here (config resolution enforces this too when provisioning is enabled).
    if pinned.policy_full_content_hash is None:
        raise PolicyAdmissionError("full policy content hash must be pinned when provisioning is enabled")
    if full_hash != pinned.policy_full_content_hash:
        raise PolicyAdmissionError("full policy content hash does not match the pinned value")

    quorum = await provider.get_key_quorum(pinned.quorum_id, request_auth=request_auth)
    if quorum.quorum_id != pinned.quorum_id:
        raise PolicyAdmissionError("returned quorum id does not equal the pinned quorum id")
    auth_quorum = quorum.to_authorization_quorum()
    if auth_quorum.content_hash() != pinned.quorum_content_hash:
        raise PolicyAdmissionError("quorum content hash does not match the pinned value")
    if quorum.threshold != pinned.quorum_threshold:
        raise PolicyAdmissionError("quorum threshold does not match the pinned value")
    # "quorum-owned" honesty: a genuine multi-party quorum needs threshold >= 2 AND enough distinct keys.
    # A threshold-1 pinned quorum is single-authority and must be labeled as such, never quorum custody.
    if quorum.threshold < 2:
        raise PolicyAdmissionError("threshold-1 owner is not quorum custody")
    if len(set(quorum.authorization_key_refs)) < quorum.threshold:
        raise PolicyAdmissionError("quorum has fewer distinct authorization keys than its threshold")
    return AdmissionResult(wallet_policy=wallet_policy, auth_quorum=auth_quorum, policy_full_content_hash=full_hash)


# ---------------------------------------------------------------------------
# The recording provider fake (ONLY ever offline in tests — never a live Privy client)
# ---------------------------------------------------------------------------


class RecordingProvisioningProvider:
    """An OFFLINE recording fake of :class:`PrivyProvisioningProvider` (records calls; injects faults).

    Deliberately has NO signing / transaction surface (``sign_calls`` stays empty — it exists only so
    a test can ASSERT provisioning made zero signing calls). Fault injection covers the full Level-2
    matrix: create timeouts before/after acceptance, provider outage, ambiguous lookups, idempotency
    conflicts, and post-create policy drift.
    """

    def __init__(
        self,
        *,
        policy: ProviderPolicy,
        quorum: ProviderKeyQuorum,
        provider_version: str = "recording-fake",
    ) -> None:
        self._policy = policy
        self._quorum = quorum
        self.provider_version = provider_version
        # Fault-injection knobs (default = healthy).
        self.create_behavior = "ok"
        self.policy_behavior = "ok"
        self.drift_policy_after_create: ProviderPolicy | None = None
        self.before_create: Callable[[], Awaitable[None]] | None = None
        # Recorders.
        self.create_calls: list[dict[str, Any]] = []
        self.get_by_external_calls: list[str] = []
        self.sign_calls: list[Any] = []  # ALWAYS empty — there is no signing surface.
        # Backing state.
        self._canned: list[ProvisionedWallet] = []  # what a successful create commits + returns
        self._committed: dict[str, list[ProvisionedWallet]] = {}  # external_id -> visible wallets
        self._idem_bodies: dict[str, dict[str, Any]] = {}
        self._create_done = False

    # -- test setup helpers --------------------------------------------------

    def arm_wallet(self, wallet: ProvisionedWallet) -> None:
        """Register the wallet a successful ``create_wallet`` will commit + return (not yet visible)."""
        self._canned.append(wallet)

    def preexist_wallet(self, wallet: ProvisionedWallet) -> None:
        """Register a wallet that ALREADY exists at the provider (visible via lookup; a prior process)."""
        self._committed.setdefault(wallet.external_id, []).append(wallet)

    def _commit(self, wallet: ProvisionedWallet) -> None:
        self._committed.setdefault(wallet.external_id, []).append(wallet)

    # -- provider protocol ---------------------------------------------------

    async def create_wallet(
        self,
        *,
        external_id: str,
        owner_id: str,
        policy_ids: tuple[str, ...],
        idempotency_key: str,
        request_auth: ProvisioningRequestAuth,
    ) -> ProvisionedWallet:
        if self.before_create is not None:
            await self.before_create()
        body = {"external_id": external_id, "owner_id": owner_id, "policy_ids": tuple(policy_ids)}
        prior = self._idem_bodies.get(idempotency_key)
        if prior is not None and prior != body:
            raise IdempotencyKeyConflictError("same idempotency key reused with a different request body")
        self._idem_bodies[idempotency_key] = body
        self.create_calls.append({**body, "idempotency_key": idempotency_key})

        behavior = self.create_behavior
        if behavior == "timeout_before_accept":
            raise ProviderTimeoutError("create timed out before acceptance")
        if behavior == "timeout_before_accept_then_ok" and not self._create_done:
            self._create_done = True  # mark the first (failed) call so the retry commits
            raise ProviderTimeoutError("create timed out before acceptance (first attempt)")
        # Commit the canned wallet(s) — the mutation reached the backend.
        for wallet in self._canned:
            self._commit(wallet)
        self._create_done = True
        if behavior == "timeout_after_accept":
            raise ProviderTimeoutError("create accepted but response timed out")
        if not self._canned:
            raise ProviderTimeoutError("no canned wallet configured for create")
        return self._canned[0]

    async def get_wallet_by_external_id(
        self, external_id: str, *, request_auth: ProvisioningRequestAuth
    ) -> ProvisionedWallet | WalletNotFound:
        self.get_by_external_calls.append(external_id)
        wallets = self._committed.get(external_id, [])
        if len(wallets) == 0:
            return WalletNotFound(external_id=external_id)
        if len(wallets) > 1:
            raise AmbiguousWalletError(f"{len(wallets)} wallets resolved for external_id {external_id!r}")
        return wallets[0]

    async def get_wallet(self, wallet_id: str, *, request_auth: ProvisioningRequestAuth) -> ProvisionedWallet:
        for wallets in self._committed.values():
            for wallet in wallets:
                if wallet.wallet_id == wallet_id:
                    return wallet
        raise ResponseValidationError(f"no wallet with id {wallet_id!r}")

    async def get_policy(self, policy_id: str, *, request_auth: ProvisioningRequestAuth) -> ProviderPolicy:
        if self.policy_behavior == "outage":
            raise ProviderUnavailableError("policy provider unreachable")
        if self._create_done and self.drift_policy_after_create is not None:
            return self.drift_policy_after_create
        return self._policy

    async def get_key_quorum(self, quorum_id: str, *, request_auth: ProvisioningRequestAuth) -> ProviderKeyQuorum:
        if self.policy_behavior == "outage":
            raise ProviderUnavailableError("quorum provider unreachable")
        return self._quorum


# ---------------------------------------------------------------------------
# The durable provisioning saga (re-entrant; drives the reserved AttemptStatus states)
# ---------------------------------------------------------------------------


def _rank(status: AttemptStatus) -> int:
    return _STATUS_RANK[status]


def _window_valid(created_at_iso: str, now: datetime) -> bool:
    """True iff the idempotency window is CERTAINLY still valid relative to the claim time.

    Uses the attempt's claim time as a conservative lower bound on the wallet-request time (claim
    precedes the request), so the window can only appear OLDER — biasing toward fail-closed no-retry.
    """
    try:
        created = datetime.fromisoformat(created_at_iso)
    except ValueError:
        return False  # unparseable → treat as expired (fail closed)
    return (now - created).total_seconds() < IDEMPOTENCY_WINDOW_S


async def _to_uncertain(store: Any, attempt_id: str, external_id: str) -> None:
    """Advance the attempt to ``WALLET_CREATE_UNCERTAIN`` if not already at/after it (idempotent)."""
    current = await store.get_deployment_attempt(attempt_id)
    if current is not None and _rank(current.status) < _rank(AttemptStatus.WALLET_CREATE_UNCERTAIN):
        await store.advance_deployment_attempt(
            attempt_id,
            expected=current.status,
            new=AttemptStatus.WALLET_CREATE_UNCERTAIN,
            external_id=external_id,
        )


async def _try_reconcile(
    provider: PrivyProvisioningProvider,
    external_id: str,
    pinned: PinnedProvisioningConfig,
    request_auth: ProvisioningRequestAuth,
) -> ProvisionedWallet | None:
    """Exact ``external_id`` lookup → validated wallet, or ``None`` if zero. Ambiguity raises (STOP)."""
    result = await provider.get_wallet_by_external_id(external_id, request_auth=request_auth)
    if isinstance(result, WalletNotFound):
        return None
    verify_provisioned_wallet(result, pinned=pinned, expected_external_id=external_id)
    return result


async def _obtain_wallet(
    store: Any,
    provider: PrivyProvisioningProvider,
    attempt: Any,
    external_id: str,
    pinned: PinnedProvisioningConfig,
    request_auth: ProvisioningRequestAuth,
    now_fn: Callable[[], datetime],
) -> ProvisionedWallet:
    """Obtain a coherent, validated wallet — creating it if fresh, else reconciling by external id."""
    attempt_id = attempt.attempt_id
    idem_key = provider_idempotency_key(attempt_id)
    resuming = _rank(attempt.status) >= _rank(AttemptStatus.WALLET_REQUESTED)

    if not resuming:
        # CORRECTION 1: persist the immutable external_id BEFORE the first provider mutation.
        await store.advance_deployment_attempt(
            attempt_id, expected=attempt.status, new=AttemptStatus.WALLET_REQUESTED, external_id=external_id
        )
    else:
        # Resuming after a crash: reconcile FIRST — never (re)create before an exact lookup.
        found = await _try_reconcile(provider, external_id, pinned, request_auth)
        if found is not None:
            return found
        # CORRECTION 4: no wallet visible + idempotency window may have expired → NEVER re-mint.
        if not _window_valid(attempt.created_at, now_fn()):
            await _to_uncertain(store, attempt_id, external_id)
            raise WalletCreateUncertain(f"idempotency window may have expired for {external_id!r}; operator must reconcile")

    # Create (first time, or an identical-key/body retry while the window is certainly valid).
    for _ in range(2):
        try:
            created = await provider.create_wallet(
                external_id=external_id,
                owner_id=pinned.owner_id,
                policy_ids=(pinned.policy_id,),
                idempotency_key=idem_key,
                request_auth=request_auth,
            )
        except ProviderTimeoutError:
            # CORRECTION 2: reconcile by EXACT lookup before any retry; ambiguity is a STOP.
            await _to_uncertain(store, attempt_id, external_id)
            found = await _try_reconcile(provider, external_id, pinned, request_auth)
            if found is not None:
                return found
            # CORRECTION 4: only retry the identical create while the window is CERTAINLY valid.
            if not _window_valid(attempt.created_at, now_fn()):
                raise WalletCreateUncertain(
                    f"create uncertain and idempotency window may have expired for {external_id!r}; operator must reconcile"
                ) from None
            continue
        # Success → re-read by external id and validate consistency before admitting.
        reread = await _try_reconcile(provider, external_id, pinned, request_auth)
        if reread is None:
            await _to_uncertain(store, attempt_id, external_id)
            raise WalletCreateUncertain(f"created wallet not visible on exact re-read for {external_id!r}")
        _assert_consistent(created, reread)
        return reread

    await _to_uncertain(store, attempt_id, external_id)
    raise WalletCreateUncertain(f"wallet creation did not converge for {external_id!r}; operator must reconcile")


async def provision_execution_wallet(
    *,
    store: Any,
    provider: PrivyProvisioningProvider,
    instance: AgentInstance,
    pinned: PinnedProvisioningConfig,
    request_auth: ProvisioningRequestAuth,
    now_fn: Callable[[], datetime],
) -> ExecutionWalletProvisioningRecord:
    """Idempotently provision ONE policy-bound execution wallet for ``instance`` and persist its record.

    Drives the reserved forward-only :class:`AttemptStatus` saga
    (``WALLET_REQUESTED → WALLET_CREATE_UNCERTAIN → WALLET_CREATED → WALLET_BOUND →
    BINDING_PERSIST_FAILED → BINDING_PERSISTED``) through the store's atomic CAS forward-transition,
    so the flow is crash-recoverable from every state. Fail-closed at every boundary; the wallet CLAIM
    stays withheld (this only persists the binding + record — it never arms live execution).

    Args:
        store: The durable store (CAS forward-transition + provisioning-record persistence).
        provider: The keyless provisioning provider (a recording fake at Levels 1-2).
        instance: The deployed instance (its ``instance_id`` encodes the durable ``attempt_id``).
        pinned: The operator-pinned custody contract to admit every resource against.
        request_auth: The NON-SECRET request-authorization reference.
        now_fn: Injectable UTC clock (idempotency-window math + verification timestamp).

    Returns:
        The persisted :class:`ExecutionWalletProvisioningRecord`.

    Raises:
        ProvisioningError: (or a subclass) fail-closed on any custody violation, drift, ambiguity, or
            unrecoverable create — the caller marks the deploy FAILED and launches no run.
    """
    attempt_id = instance.instance_id.removeprefix("inst_")
    external_id = derive_external_id(attempt_id)

    attempt = await store.get_deployment_attempt(attempt_id)
    if attempt is None:
        raise ProvisioningError(f"no deployment attempt for id {attempt_id!r}")

    # Already complete → return the persisted record (idempotent replay; no admission/create).
    if _rank(attempt.status) >= _rank(AttemptStatus.BINDING_PERSISTED):
        existing = await store.get_provisioning_record(instance.instance_id)
        if existing is not None:
            return existing
        raise ProvisioningError(f"attempt {attempt_id!r} is past provisioning but has no record")

    # Startup readiness: admit the live policy + quorum before touching the wallet (fail-closed).
    await admit_policy_and_quorum(provider, pinned, request_auth=request_auth)

    # Phase 1 — obtain a coherent, validated wallet.
    if _rank(attempt.status) < _rank(AttemptStatus.WALLET_CREATED):
        wallet = await _obtain_wallet(store, provider, attempt, external_id, pinned, request_auth, now_fn)
        cur = await store.get_deployment_attempt(attempt_id)
        if _rank(cur.status) < _rank(AttemptStatus.WALLET_CREATED):
            await store.advance_deployment_attempt(
                attempt_id, expected=cur.status, new=AttemptStatus.WALLET_CREATED, external_id=external_id
            )
    else:
        # Resuming at/after WALLET_CREATED — the wallet must be recoverable by exact external id.
        wallet = await _try_reconcile(provider, external_id, pinned, request_auth)
        if wallet is None:
            await _to_uncertain(store, attempt_id, external_id)
            raise WalletCreateUncertain(f"recorded wallet not recoverable by external id {external_id!r}")

    # Phase 2 — re-admit the policy + quorum at binding time (catches startup-valid-then-drift), then
    # verify the wallet is owned by the quorum with EXACTLY the pinned policy, and build the binding.
    admission = await admit_policy_and_quorum(provider, pinned, request_auth=request_auth)
    verify_provisioned_wallet(wallet, pinned=pinned, expected_external_id=external_id)

    binding = ExecutionWalletBinding(
        provider="privy",
        wallet_ref=wallet.wallet_id,
        wallet_address=wallet.address,
        chain_id=CHAIN_ID_POLYGON,
        venue=_VENUE,
        privy_policy_content_hash=pinned.policy_content_hash,
        authorization_quorum_ref=pinned.quorum_id,
        authorization_quorum_content_hash=pinned.quorum_content_hash,
        quorum_threshold=pinned.quorum_threshold,
    )

    cur = await store.get_deployment_attempt(attempt_id)
    if _rank(cur.status) < _rank(AttemptStatus.WALLET_BOUND):
        await store.advance_deployment_attempt(
            attempt_id, expected=cur.status, new=AttemptStatus.WALLET_BOUND, external_id=external_id
        )

    # Crash-recovery of the immutable record (MAJOR-1). If a record was ALREADY committed on a prior
    # attempt (e.g. the process died after the record write but before the terminal status CAS), LOAD +
    # REUSE it verbatim — never rebuild it. Rebuilding would mint a fresh ``provider_verification_ts`` →
    # a different record hash → the immutable store would REJECT it, trapping the saga in
    # BINDING_PERSIST_FAILED forever. The reconciled wallet/binding must match the committed record
    # (a divergence is a custody incident → fail closed).
    existing_record = await store.get_provisioning_record(instance.instance_id)
    if existing_record is not None:
        _assert_record_matches(
            existing_record, wallet=wallet, binding=binding, pinned=pinned,
            admission=admission, external_id=external_id,
        )
        record = existing_record
    else:
        record = ExecutionWalletProvisioningRecord(
            instance_id=instance.instance_id,
            external_id=external_id,
            wallet_id=wallet.wallet_id,
            wallet_address=wallet.address,
            chain_type=wallet.chain_type,
            policy_id=pinned.policy_id,
            quorum_id=pinned.quorum_id,
            binding=binding,
            binding_hash=binding.binding_hash(),
            policy_content_hash=pinned.policy_content_hash,
            policy_full_content_hash=admission.policy_full_content_hash,
            quorum_content_hash=pinned.quorum_content_hash,
            provider_verification_ts=now_fn().isoformat(),
            provider_version=getattr(provider, "provider_version", "unknown"),
        )
        # Persist the record deterministically. A persist failure → BINDING_PERSIST_FAILED; a retry then
        # reconciles the committed record above and NEVER creates another wallet.
        try:
            await store.persist_provisioning_record(record)
        except Exception:
            cur = await store.get_deployment_attempt(attempt_id)
            if cur is not None and _rank(cur.status) < _rank(AttemptStatus.BINDING_PERSIST_FAILED):
                await store.advance_deployment_attempt(
                    attempt_id, expected=cur.status, new=AttemptStatus.BINDING_PERSIST_FAILED, external_id=external_id
                )
            raise

    cur = await store.get_deployment_attempt(attempt_id)
    if _rank(cur.status) < _rank(AttemptStatus.BINDING_PERSISTED):
        await store.advance_deployment_attempt(
            attempt_id, expected=cur.status, new=AttemptStatus.BINDING_PERSISTED, external_id=external_id
        )
    return record


__all__ = [
    "CHAIN_TYPE_ETHEREUM",
    "IDEMPOTENCY_WINDOW_S",
    "AdmissionResult",
    "AmbiguousWalletError",
    "IdempotencyKeyConflictError",
    "PinnedProvisioningConfig",
    "PolicyAdmissionError",
    "PrivyProvisioningProvider",
    "ProviderKeyQuorum",
    "ProviderPolicy",
    "ProviderPolicyRule",
    "ProviderTimeoutError",
    "ProviderUnavailableError",
    "ProvisionedWallet",
    "ProvisioningError",
    "ProvisioningRequestAuth",
    "RecordingProvisioningProvider",
    "ResponseValidationError",
    "WalletCreateUncertain",
    "WalletNotFound",
    "admit_policy_and_quorum",
    "derive_external_id",
    "is_evm_address",
    "provider_idempotency_key",
    "provision_execution_wallet",
    "verify_provisioned_wallet",
]
