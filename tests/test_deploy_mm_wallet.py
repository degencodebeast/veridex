"""II-5b RED suite — Privy execution-wallet provisioning (LEVELS 1-2, OFFLINE).

The MOST custody-sensitive slice: inside the II-5 deploy saga we provision ONE idempotent,
policy-bound, keyless Privy EVM execution wallet per MM deploy, persist the reviewed
``ExecutionWalletBinding`` plus a NET-NEW immutable provisioning record, and bind it to R4-A ONLY.

ZERO real Privy, ZERO credentials, ZERO network. Every test drives a RECORDING provider fake
(``RecordingProvisioningProvider``) + ``InMemoryStore``. Each test names the Codex custody
requirement (one of the 4 REQUIRED corrections, the provider contract, the policy/quorum admission,
the store CAS, or the R4-A capability boundary) it proves.

Level 3 (operator-run live sandbox acceptance) is deliberately NOT here — it is the operator's job
with the II-5c HTTP adapter. The final honest status is CODE_READY / CLAIM_WITHHELD.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from veridex.deploy.attempt import AttemptStatus, DeploymentAttempt
from veridex.deploy.instance import AgentInstance, DeployStatus
from veridex.dust_execution.privy_provisioning import (
    IDEMPOTENCY_WINDOW_S,
    AmbiguousWalletError,
    IdempotencyKeyConflictError,
    PinnedProvisioningConfig,
    PolicyAdmissionError,
    ProviderKeyQuorum,
    ProviderPolicy,
    ProviderUnavailableError,
    ProvisionedWallet,
    ProvisioningRequestAuth,
    RecordingProvisioningProvider,
    ResponseValidationError,
    WalletCreateUncertain,
    WalletNotFound,
    admit_policy_and_quorum,
    derive_external_id,
    is_evm_address,
    provision_execution_wallet,
    verify_provisioned_wallet,
)
from veridex.dust_execution.provisioning_record import ExecutionWalletProvisioningRecord
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
from veridex.store import DeploymentAttemptTransitionError, InMemoryStore

# ---------------------------------------------------------------------------
# Canonical pinned custody fixtures (all non-secret; hashes computed from the reviewed verifiers)
# ---------------------------------------------------------------------------

_ORDER_RULE = PolicyRule(method=ALLOWED_SIGN_METHOD, primary_type=ORDER_PRIMARY_TYPE, effect="ALLOW")
_CLOB_RULE = PolicyRule(method=ALLOWED_SIGN_METHOD, primary_type=CLOB_AUTH_PRIMARY_TYPE, effect="ALLOW")
_CANONICAL_POLICY = PrivyWalletPolicy(
    rules=(_ORDER_RULE, _CLOB_RULE), default_action="DENY", owner_type="quorum"
)
_CANONICAL_QUORUM = AuthorizationQuorum(
    quorum_ref="kq_pinned", authorization_key_refs=("keyA", "keyB"), threshold=2
)

_POLICY_ID = "pol_pinned"
_QUORUM_ID = "kq_pinned"
_ADDR = "0x" + "a" * 40


def _pinned() -> PinnedProvisioningConfig:
    return PinnedProvisioningConfig(
        policy_id=_POLICY_ID,
        quorum_id=_QUORUM_ID,
        owner_id=_QUORUM_ID,
        policy_content_hash=_CANONICAL_POLICY.content_hash(),
        quorum_content_hash=_CANONICAL_QUORUM.content_hash(),
        quorum_threshold=2,
        authorization_key_id="authkey-ref-abc123",
        policy_full_content_hash=_provider_policy().full_content_hash(),
    )


def _typed_condition(primary_type: str, *, value: str = "0xmaker") -> dict[str, Any]:
    """A real Privy typed-data policy condition (primary_type nested in ``typed_data``; grounding §5)."""
    return {
        "field_source": "ethereum_typed_data_message",
        "typed_data": {"primary_type": primary_type},
        "field": "maker",
        "operator": "eq",
        "value": value,
    }


def _policy_payload(**overrides: Any) -> dict[str, Any]:
    """A raw Privy ``Policy`` response JSON (real wire shape: rules[]:{method, conditions[], action})."""
    base: dict[str, Any] = {
        "id": _POLICY_ID,
        "version": "1.0",
        "name": "veridex-exec-policy",
        "chain_type": "ethereum",
        "rules": [
            {"method": ALLOWED_SIGN_METHOD, "action": "ALLOW", "conditions": [_typed_condition(ORDER_PRIMARY_TYPE)]},
            {"method": ALLOWED_SIGN_METHOD, "action": "ALLOW", "conditions": [_typed_condition(CLOB_AUTH_PRIMARY_TYPE)]},
        ],
    }
    base.update(overrides)
    return base


def _quorum_payload(**overrides: Any) -> dict[str, Any]:
    """A raw Privy ``KeyQuorum`` response JSON (real wire shape: authorization_keys[]:{public_key})."""
    base: dict[str, Any] = {
        "id": _QUORUM_ID,
        "display_name": "veridex-exec-quorum",
        "authorization_threshold": 2,
        "authorization_keys": [{"public_key": "keyA"}, {"public_key": "keyB"}],
        "user_ids": [],
        "key_quorum_ids": [],
    }
    base.update(overrides)
    return base


def _provider_policy(**overrides: Any) -> ProviderPolicy:
    return ProviderPolicy.from_provider(_policy_payload(**overrides))


def _provider_quorum(**overrides: Any) -> ProviderKeyQuorum:
    return ProviderKeyQuorum.from_provider(_quorum_payload(**overrides))


def _wallet(external_id: str, **overrides: Any) -> ProvisionedWallet:
    base: dict[str, Any] = {
        "wallet_id": "wal_1",
        "address": _ADDR,
        "chain_type": "ethereum",
        "owner_id": _QUORUM_ID,
        "policy_ids": (_POLICY_ID,),
        "external_id": external_id,
    }
    base.update(overrides)
    return ProvisionedWallet(**base)


def _auth() -> ProvisioningRequestAuth:
    return ProvisioningRequestAuth(authorization_key_id="authkey-ref-abc123")


def _now_fn(dt: datetime | None = None):
    fixed = dt if dt is not None else datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)
    return lambda: fixed


def _attempt(attempt_id: str = "att1", *, status: AttemptStatus = AttemptStatus.PENDING,
             external_id: str | None = None, created_at: str | None = None) -> DeploymentAttempt:
    return DeploymentAttempt(
        attempt_id=attempt_id,
        operator_id="did:privy:op",
        idempotency_key=f"idem-{attempt_id}",
        config_fingerprint="cfg",
        status=status,
        created_at=created_at or datetime(2026, 7, 18, 11, 0, 0, tzinfo=UTC).isoformat(),
        instance_id=f"inst_{attempt_id}",
        external_id=external_id,
    )


def _instance(attempt_id: str = "att1") -> AgentInstance:
    now = datetime(2026, 7, 18, 11, 0, 0, tzinfo=UTC).isoformat()
    return AgentInstance(
        instance_id=f"inst_{attempt_id}",
        template_id="t",
        agent_id="a",
        submitted_config={},
        effective_config={},
        config_hash="c" * 8,
        policy_hash="p" * 8,
        source_mode="replay",
        execution_mode="dry_run",
        run_id="run_" + attempt_id,
        status=DeployStatus.PENDING,
        operator_id="did:privy:op",
        created_at=now,
        updated_at=now,
    )


async def _seed(store: InMemoryStore, attempt: DeploymentAttempt) -> None:
    await store.persist_deployment_attempt(attempt)


def _provider(**kwargs: Any) -> RecordingProvisioningProvider:
    defaults: dict[str, Any] = {"policy": _provider_policy(), "quorum": _provider_quorum()}
    defaults.update(kwargs)
    return RecordingProvisioningProvider(**defaults)


# ===========================================================================
# LEVEL 1 — unit / contract
# ===========================================================================


def test_evm_address_validation() -> None:
    assert is_evm_address(_ADDR)
    assert not is_evm_address("0x" + "a" * 39)  # too short
    assert not is_evm_address("a" * 42)  # no 0x
    assert not is_evm_address("0x" + "g" * 40)  # non-hex
    assert not is_evm_address("")


def test_provisioned_wallet_strict_parse_reads_privy_wire_shape() -> None:
    # Privy's Wallet response uses `id` (not wallet_id) + optional authorization_threshold (grounding §2).
    good = ProvisionedWallet.from_provider(
        {
            "id": "wal_1",
            "address": _ADDR,
            "chain_type": "ethereum",
            "owner_id": _QUORUM_ID,
            "policy_ids": [_POLICY_ID],
            "external_id": "ext-1",
            "authorization_threshold": 2,
        }
    )
    assert good.wallet_id == "wal_1"  # mapped from Privy `id`
    assert good.policy_ids == (_POLICY_ID,)
    assert good.authorization_threshold == 2
    # Missing required field → fail closed.
    with pytest.raises(ResponseValidationError):
        ProvisionedWallet.from_provider({"id": "wal_1"})
    # Wrong type → fail closed.
    with pytest.raises(ResponseValidationError):
        ProvisionedWallet.from_provider(
            {
                "id": "wal_1",
                "address": _ADDR,
                "chain_type": "ethereum",
                "owner_id": _QUORUM_ID,
                "policy_ids": "not-a-list",
                "external_id": "ext-1",
            }
        )


def test_provider_policy_rejects_unknown_security_relevant_field() -> None:
    # An extra/unmodeled key anywhere in the Privy policy — top-level, per-rule, or per-CONDITION —
    # must be REJECTED (no lossy projection): hashing a simplified local view would let a dropped
    # condition (an unverified policy semantic = a custody hole) pass silently.
    with pytest.raises(ResponseValidationError):
        ProviderPolicy.from_provider(_policy_payload(some_unmodeled_top_level={"x": 1}))
    bad_rule = _policy_payload()
    bad_rule["rules"][0]["unmodeled_rule_key"] = True
    with pytest.raises(ResponseValidationError):
        ProviderPolicy.from_provider(bad_rule)
    bad_cond = _policy_payload()
    bad_cond["rules"][0]["conditions"][0]["unmodeled_condition_key"] = "danger"
    with pytest.raises(ResponseValidationError):
        ProviderPolicy.from_provider(bad_cond)


def test_provider_policy_extracts_primary_type_from_conditions_and_maps_to_reviewed_hash() -> None:
    parsed = ProviderPolicy.from_provider(_policy_payload())
    # primary_type is extracted from each rule's condition.typed_data.primary_type (grounding §5).
    assert {r.primary_type for r in parsed.rules} == {ORDER_PRIMARY_TYPE, CLOB_AUTH_PRIMARY_TYPE}
    wp = parsed.to_wallet_policy(owner_type="quorum")
    assert wp.is_typed_data_only_default_deny()
    assert wp.content_hash() == _CANONICAL_POLICY.content_hash()


def test_full_policy_hash_catches_field_condition_drift_coarse_hash_misses() -> None:
    # A1: the reviewed coarse content_hash sees only (method, primary_type, effect); a change to a
    # per-field condition VALUE (e.g. a widened spend cap / swapped verifyingContract) leaves the
    # coarse hash unchanged but MUST change the full_content_hash (caught at the record layer).
    base = ProviderPolicy.from_provider(_policy_payload())
    drifted_rules = [
        {"method": ALLOWED_SIGN_METHOD, "action": "ALLOW", "conditions": [_typed_condition(ORDER_PRIMARY_TYPE, value="0xEVILADDRESS")]},
        {"method": ALLOWED_SIGN_METHOD, "action": "ALLOW", "conditions": [_typed_condition(CLOB_AUTH_PRIMARY_TYPE)]},
    ]
    drifted = ProviderPolicy.from_provider(_policy_payload(rules=drifted_rules))
    assert base.to_wallet_policy(owner_type="quorum").content_hash() == drifted.to_wallet_policy(owner_type="quorum").content_hash()
    assert base.full_content_hash() != drifted.full_content_hash()


def test_key_quorum_from_provider_reads_public_keys() -> None:
    q = ProviderKeyQuorum.from_provider(_quorum_payload())
    assert q.authorization_key_refs == ("keyA", "keyB")  # from authorization_keys[].public_key
    assert q.to_authorization_quorum().content_hash() == _CANONICAL_QUORUM.content_hash()
    with pytest.raises(ResponseValidationError):
        ProviderKeyQuorum.from_provider(_quorum_payload(unmodeled="x"))


def test_verify_provisioned_wallet_rejects_each_mismatch() -> None:
    pinned = _pinned()
    ext = "ext-1"
    # Baseline valid wallet passes.
    verify_provisioned_wallet(_wallet(ext), pinned=pinned, expected_external_id=ext)
    # chain_type not ethereum.
    with pytest.raises(ResponseValidationError):
        verify_provisioned_wallet(_wallet(ext, chain_type="solana"), pinned=pinned, expected_external_id=ext)
    # external_id mismatch.
    with pytest.raises(ResponseValidationError):
        verify_provisioned_wallet(_wallet("other"), pinned=pinned, expected_external_id=ext)
    # owner mismatch.
    with pytest.raises(ResponseValidationError):
        verify_provisioned_wallet(_wallet(ext, owner_id="kq_other"), pinned=pinned, expected_external_id=ext)
    # extra policy (superset, not exact singleton).
    with pytest.raises(ResponseValidationError):
        verify_provisioned_wallet(
            _wallet(ext, policy_ids=(_POLICY_ID, "pol_extra")), pinned=pinned, expected_external_id=ext
        )
    # bad address.
    with pytest.raises(ResponseValidationError):
        verify_provisioned_wallet(_wallet(ext, address="0xnothex"), pinned=pinned, expected_external_id=ext)
    # empty wallet id.
    with pytest.raises(ResponseValidationError):
        verify_provisioned_wallet(_wallet(ext, wallet_id=""), pinned=pinned, expected_external_id=ext)
    # wallet-reported authorization_threshold disagreeing with the pinned quorum threshold.
    with pytest.raises(ResponseValidationError):
        verify_provisioned_wallet(_wallet(ext, authorization_threshold=1), pinned=pinned, expected_external_id=ext)


async def test_admit_policy_and_quorum_happy_path() -> None:
    provider = _provider()
    result = await admit_policy_and_quorum(provider, _pinned(), request_auth=_auth())
    assert result.wallet_policy.content_hash() == _CANONICAL_POLICY.content_hash()
    assert result.auth_quorum.content_hash() == _CANONICAL_QUORUM.content_hash()
    assert result.policy_full_content_hash == _provider_policy().full_content_hash()


async def test_admit_rejects_pinned_full_policy_hash_mismatch() -> None:
    # When the operator pins the full-fidelity policy hash, a live policy whose per-field conditions
    # differ (same coarse hash) is rejected — field-level drift is caught at admission, not just record.
    pinned = PinnedProvisioningConfig(
        policy_id=_POLICY_ID, quorum_id=_QUORUM_ID, owner_id=_QUORUM_ID,
        policy_content_hash=_CANONICAL_POLICY.content_hash(),
        quorum_content_hash=_CANONICAL_QUORUM.content_hash(), quorum_threshold=2,
        authorization_key_id="authkey-ref-abc123",
        policy_full_content_hash="0" * 64,  # will never match the live full hash
    )
    with pytest.raises(PolicyAdmissionError):
        await admit_policy_and_quorum(_provider(), pinned, request_auth=_auth())


async def test_full_policy_hash_required_when_provisioning_enabled_fails_closed() -> None:
    # The full-fidelity policy hash is the ONLY gate that catches a field-level policy substitution
    # (same coarse hash). It must be REQUIRED when provisioning is enabled, not opt-in — otherwise the
    # hole stays open unless the operator remembers to pin it. Fail closed at BOTH layers.
    from veridex.config import require_privy_provisioning

    # (a) config-resolution: provisioning enabled but the full hash unpinned → clear config error.
    with pytest.raises(ValueError):
        require_privy_provisioning(_provisioning_settings(PRIVY_EXECUTION_POLICY_FULL_CONTENT_HASH=None))
    # (b) admission defense-in-depth: a pinned config lacking the full hash is rejected before admission.
    pinned_no_full = PinnedProvisioningConfig(
        policy_id=_POLICY_ID, quorum_id=_QUORUM_ID, owner_id=_QUORUM_ID,
        policy_content_hash=_CANONICAL_POLICY.content_hash(),
        quorum_content_hash=_CANONICAL_QUORUM.content_hash(), quorum_threshold=2,
        authorization_key_id="authkey-ref-abc123", policy_full_content_hash=None,
    )
    with pytest.raises(PolicyAdmissionError):
        await admit_policy_and_quorum(_provider(), pinned_no_full, request_auth=_auth())


async def test_owner_id_must_equal_the_verified_quorum_id_fails_closed() -> None:
    # MAJOR-1: the wallet owner we ADMIT against (pinned.owner_id) must be the SAME quorum whose
    # threshold/keys/content-hash we VERIFY (pinned.quorum_id). A divergent operator-set owner id would
    # admit a wallet owned by a NEVER-verified quorum while the record claims quorum_id owns it.
    # (a) config-resolution guard: a divergent PRIVY_EXECUTION_OWNER_ID → clear config error, no provisioning.
    from veridex.config import require_privy_provisioning

    with pytest.raises(ValueError):
        require_privy_provisioning(_provisioning_settings(PRIVY_EXECUTION_OWNER_ID="kq_DIFFERENT"))
    # (b) defense-in-depth at admission: a directly-built divergent pinned config is rejected AND the
    # saga writes NO provisioning record (never admits a wallet owned by an unverified quorum).
    store = InMemoryStore()
    att = _attempt("attOWN")
    await _seed(store, att)
    provider = _provider()
    provider.arm_wallet(_wallet(derive_external_id("attOWN")))
    bad_pinned = PinnedProvisioningConfig(
        policy_id=_POLICY_ID,
        quorum_id=_QUORUM_ID,
        owner_id="kq_DIFFERENT",
        policy_content_hash=_CANONICAL_POLICY.content_hash(),
        quorum_content_hash=_CANONICAL_QUORUM.content_hash(),
        quorum_threshold=2,
        authorization_key_id="authkey-ref-abc123",
    )
    with pytest.raises(PolicyAdmissionError):
        await provision_execution_wallet(
            store=store, provider=provider, instance=_instance("attOWN"),
            pinned=bad_pinned, request_auth=_auth(), now_fn=_now_fn(),
        )
    assert await store.get_provisioning_record("inst_attOWN") is None


@pytest.mark.parametrize(
    "policy_over,quorum_over",
    [
        # a widened ALLOW rule for a raw-signing method → not typed-data-only.
        ({"rules": _policy_payload()["rules"] + [{"method": "secp256k1_sign", "action": "ALLOW", "conditions": []}]}, {}),
        # policy chain_type not ethereum.
        ({"chain_type": "solana"}, {}),
        # missing the ClobAuth allow rule → allowed set mismatch.
        ({"rules": _policy_payload()["rules"][:1]}, {}),
        # threshold-1 is NOT quorum custody.
        ({}, {"authorization_threshold": 1, "authorization_keys": [{"public_key": "keyA"}]}),
        # authorization-key drift (content-hash mismatch).
        ({}, {"authorization_keys": [{"public_key": "keyA"}, {"public_key": "keyZ"}]}),
        # threshold drift vs pinned.
        ({}, {"authorization_threshold": 3}),
    ],
)
async def test_admit_policy_and_quorum_rejects_drift(policy_over: dict, quorum_over: dict) -> None:
    provider = _provider(policy=_provider_policy(**policy_over), quorum=_provider_quorum(**quorum_over))
    with pytest.raises(PolicyAdmissionError):
        await admit_policy_and_quorum(provider, _pinned(), request_auth=_auth())


def test_request_auth_carries_no_private_key_and_no_secret_repr() -> None:
    auth = _auth()
    fields = {f.name for f in inspect.signature(ProvisioningRequestAuth).parameters.values()}
    # ONLY a non-secret key REFERENCE — never private key material.
    assert fields == {"authorization_key_id"}
    text = repr(auth)
    assert "authkey-ref-abc123" in text  # a non-secret ref is fine
    # No private-key material may ever appear in the repr.
    for banned in ("BEGIN", "PRIVATE", "-----"):
        assert banned not in text


# ---- store CAS (Level 1) --------------------------------------------------


async def test_store_cas_forward_transition_and_external_id_write_once() -> None:
    store = InMemoryStore()
    att = _attempt("attX")
    await _seed(store, att)
    # PENDING -> WALLET_REQUESTED, writing the immutable external_id.
    ext = derive_external_id("attX")
    updated = await store.advance_deployment_attempt(
        "attX", expected=AttemptStatus.PENDING, new=AttemptStatus.WALLET_REQUESTED, external_id=ext
    )
    assert updated.status == AttemptStatus.WALLET_REQUESTED
    assert updated.external_id == ext
    # A stale expected status (someone already advanced) → CAS conflict, NO write.
    with pytest.raises(DeploymentAttemptTransitionError):
        await store.advance_deployment_attempt(
            "attX", expected=AttemptStatus.PENDING, new=AttemptStatus.WALLET_CREATED
        )
    # Rewind is rejected.
    with pytest.raises((DeploymentAttemptTransitionError, ValueError)):
        await store.advance_deployment_attempt(
            "attX", expected=AttemptStatus.WALLET_REQUESTED, new=AttemptStatus.PENDING
        )
    # external_id is write-once: a DIFFERENT value on a later transition is refused.
    with pytest.raises((DeploymentAttemptTransitionError, ValueError)):
        await store.advance_deployment_attempt(
            "attX",
            expected=AttemptStatus.WALLET_REQUESTED,
            new=AttemptStatus.WALLET_CREATE_UNCERTAIN,
            external_id="a-different-external-id",
        )
    # But the SAME external_id is fine (idempotent recovery).
    ok = await store.advance_deployment_attempt(
        "attX",
        expected=AttemptStatus.WALLET_REQUESTED,
        new=AttemptStatus.WALLET_CREATE_UNCERTAIN,
        external_id=ext,
    )
    assert ok.status == AttemptStatus.WALLET_CREATE_UNCERTAIN
    assert ok.external_id == ext


async def test_provisioning_record_persist_is_immutable() -> None:
    store = InMemoryStore()
    record = _make_record()
    await store.persist_provisioning_record(record)
    loaded = await store.get_provisioning_record(record.instance_id)
    assert loaded is not None
    assert loaded.provisioning_record_hash() == record.provisioning_record_hash()
    # A DIFFERENT record for the same instance is refused (immutable).
    tampered = _make_record(wallet_id="wal_DIFFERENT")
    with pytest.raises(ValueError):
        await store.persist_provisioning_record(tampered)
    # Re-persisting the IDENTICAL record is idempotent (no raise).
    await store.persist_provisioning_record(record)


def _make_record(**wallet_over: Any) -> ExecutionWalletProvisioningRecord:
    pinned = _pinned()
    binding = ExecutionWalletBinding(
        provider="privy",
        wallet_ref=wallet_over.get("wallet_id", "wal_1"),
        wallet_address=_ADDR,
        chain_id=CHAIN_ID_POLYGON,
        venue="polymarket",
        privy_policy_content_hash=pinned.policy_content_hash,
        authorization_quorum_ref=pinned.quorum_id,
        authorization_quorum_content_hash=pinned.quorum_content_hash,
        quorum_threshold=pinned.quorum_threshold,
    )
    return ExecutionWalletProvisioningRecord(
        instance_id="inst_attX",
        external_id="ext-1",
        wallet_id=wallet_over.get("wallet_id", "wal_1"),
        wallet_address=_ADDR,
        chain_type="ethereum",
        policy_id=pinned.policy_id,
        quorum_id=pinned.quorum_id,
        binding=binding,
        binding_hash=binding.binding_hash(),
        policy_content_hash=pinned.policy_content_hash,
        policy_full_content_hash=_provider_policy().full_content_hash(),
        quorum_content_hash=pinned.quorum_content_hash,
        provider_verification_ts="2026-07-18T12:00:00+00:00",
        provider_version="recording-fake",
    )


def test_provisioning_record_pins_exact_policy_id_beyond_binding_hash() -> None:
    # binding_hash pins policy CONTENT but not the policy RESOURCE id: two records with the SAME
    # binding but DIFFERENT pinned policy_id must hash DIFFERENTLY (content-equivalent substitution
    # is caught at the record layer, not the binding layer).
    a = _make_record()
    b = ExecutionWalletProvisioningRecord(
        instance_id=a.instance_id,
        external_id=a.external_id,
        wallet_id=a.wallet_id,
        wallet_address=a.wallet_address,
        chain_type=a.chain_type,
        policy_id="pol_SUBSTITUTED",
        quorum_id=a.quorum_id,
        binding=a.binding,
        binding_hash=a.binding_hash,
        policy_content_hash=a.policy_content_hash,
        policy_full_content_hash=a.policy_full_content_hash,
        quorum_content_hash=a.quorum_content_hash,
        provider_verification_ts=a.provider_verification_ts,
        provider_version=a.provider_version,
    )
    assert a.binding.binding_hash() == b.binding.binding_hash()  # same binding
    assert a.provisioning_record_hash() != b.provisioning_record_hash()  # but different record


def test_provisioning_record_serialization_has_no_secret() -> None:
    record = _make_record()
    # Assert over the PERSISTED surface (``to_json_dict`` — exactly what the store writes) AND the raw
    # object repr, so a secret leaking only through the serialized form cannot slip past.
    persisted = record.to_json_dict()
    for surface in (repr(persisted), repr(record)):
        for banned in ("BEGIN", "PRIVATE KEY", "authkey-ref", "secret"):
            assert banned not in surface


# ===========================================================================
# LEVEL 2 — mocked saga + failure recovery
# ===========================================================================


async def test_saga_happy_path_persists_binding_and_record() -> None:
    store = InMemoryStore()
    att = _attempt("attH")
    await _seed(store, att)
    provider = _provider()
    provider.arm_wallet(_wallet(derive_external_id("attH")))
    record = await provision_execution_wallet(
        store=store, provider=provider, instance=_instance("attH"),
        pinned=_pinned(), request_auth=_auth(), now_fn=_now_fn(),
    )
    assert record.wallet_id == "wal_1"
    assert len(provider.create_calls) == 1
    final = await store.get_deployment_attempt("attH")
    assert final.status == AttemptStatus.BINDING_PERSISTED
    assert final.external_id == derive_external_id("attH")
    persisted = await store.get_provisioning_record("inst_attH")
    assert persisted is not None
    assert persisted.binding.chain_id == CHAIN_ID_POLYGON


async def test_saga_persists_external_id_before_first_provider_mutation() -> None:
    # CORRECTION 1: the deterministic non-secret external_id lands in the attempt claim BEFORE the
    # first create_wallet. We assert the ordering by making create_wallet observe the stored attempt.
    store = InMemoryStore()
    att = _attempt("attO")
    await _seed(store, att)
    provider = _provider()
    provider.arm_wallet(_wallet(derive_external_id("attO")))
    seen_external_ids: list[str | None] = []

    async def _hook() -> None:
        got = await store.get_deployment_attempt("attO")
        seen_external_ids.append(got.external_id if got else None)

    provider.before_create = _hook
    await provision_execution_wallet(
        store=store, provider=provider, instance=_instance("attO"),
        pinned=_pinned(), request_auth=_auth(), now_fn=_now_fn(),
    )
    assert seen_external_ids == [derive_external_id("attO")]


async def test_saga_idempotent_replay_reconciles_same_wallet_no_second_create() -> None:
    # Re-running the completed saga returns the SAME record and issues NO further create.
    store = InMemoryStore()
    att = _attempt("attR")
    await _seed(store, att)
    provider = _provider()
    provider.arm_wallet(_wallet(derive_external_id("attR")))
    first = await provision_execution_wallet(
        store=store, provider=provider, instance=_instance("attR"),
        pinned=_pinned(), request_auth=_auth(), now_fn=_now_fn(),
    )
    second = await provision_execution_wallet(
        store=store, provider=provider, instance=_instance("attR"),
        pinned=_pinned(), request_auth=_auth(), now_fn=_now_fn(),
    )
    assert first.provisioning_record_hash() == second.provisioning_record_hash()
    assert len(provider.create_calls) == 1, "a completed provisioning must never re-create"


async def test_saga_timeout_after_accept_recovers_via_external_id_lookup() -> None:
    # CORRECTION 2: create times out AFTER the provider accepted; reconcile by EXACT external-id
    # lookup finds exactly one coherent wallet → recover it, NEVER mint a second.
    store = InMemoryStore()
    att = _attempt("attT")
    await _seed(store, att)
    provider = _provider()
    ext = derive_external_id("attT")
    provider.arm_wallet(_wallet(ext))
    provider.create_behavior = "timeout_after_accept"
    record = await provision_execution_wallet(
        store=store, provider=provider, instance=_instance("attT"),
        pinned=_pinned(), request_auth=_auth(), now_fn=_now_fn(),
    )
    assert record.wallet_id == "wal_1"
    final = await store.get_deployment_attempt("attT")
    assert final.status == AttemptStatus.BINDING_PERSISTED
    assert provider.get_by_external_calls, "must reconcile via get_wallet_by_external_id"


async def test_saga_timeout_before_accept_within_window_retries_identically() -> None:
    # Timeout BEFORE accept, idempotency window certainly valid, zero wallet visible → an identical
    # retry (same key/body) is allowed and succeeds.
    store = InMemoryStore()
    att = _attempt("attB")
    await _seed(store, att)
    provider = _provider()
    ext = derive_external_id("attB")
    provider.arm_wallet(_wallet(ext))
    provider.create_behavior = "timeout_before_accept_then_ok"
    record = await provision_execution_wallet(
        store=store, provider=provider, instance=_instance("attB"),
        pinned=_pinned(), request_auth=_auth(), now_fn=_now_fn(),
    )
    assert record.wallet_id == "wal_1"
    # Two create attempts, both with the SAME idempotency key + body.
    assert len(provider.create_calls) == 2
    assert provider.create_calls[0] == provider.create_calls[1]


async def test_saga_ambiguous_lookup_is_a_custody_incident_stop() -> None:
    # CORRECTION 2: >1 wallet for one external_id → STOP. Never pick/sort/mint.
    store = InMemoryStore()
    att = _attempt("attA")
    await _seed(store, att)
    provider = _provider()
    ext = derive_external_id("attA")
    provider.arm_wallet(_wallet(ext, wallet_id="wal_1"))
    provider.arm_wallet(_wallet(ext, wallet_id="wal_2"))  # duplicate → ambiguous
    provider.create_behavior = "timeout_after_accept"
    with pytest.raises((AmbiguousWalletError, WalletCreateUncertain)):
        await provision_execution_wallet(
            store=store, provider=provider, instance=_instance("attA"),
            pinned=_pinned(), request_auth=_auth(), now_fn=_now_fn(),
        )
    final = await store.get_deployment_attempt("attA")
    assert final.status in {AttemptStatus.WALLET_REQUESTED, AttemptStatus.WALLET_CREATE_UNCERTAIN}


async def test_saga_idempotency_window_expired_never_remints() -> None:
    # CORRECTION 4: an ambiguous create timeout with a possibly-EXPIRED 24h idempotency record →
    # NEVER a fresh mutation. Remain WALLET_CREATE_UNCERTAIN; require operator reconciliation.
    store = InMemoryStore()
    # created_at is far in the past relative to now → window expired.
    old = datetime(2026, 7, 1, 0, 0, 0, tzinfo=UTC).isoformat()
    att = _attempt("attE", created_at=old)
    await _seed(store, att)
    provider = _provider()
    # No wallet visible + timeout before accept → zero on reconcile.
    provider.create_behavior = "timeout_before_accept"
    now_far = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)  # >> 24h after created_at
    assert (now_far - datetime.fromisoformat(old)).total_seconds() > IDEMPOTENCY_WINDOW_S
    with pytest.raises(WalletCreateUncertain):
        await provision_execution_wallet(
            store=store, provider=provider, instance=_instance("attE"),
            pinned=_pinned(), request_auth=_auth(), now_fn=_now_fn(now_far),
        )
    final = await store.get_deployment_attempt("attE")
    assert final.status == AttemptStatus.WALLET_CREATE_UNCERTAIN
    # Exactly one create attempt was made — no re-mint after the window may have expired.
    assert len(provider.create_calls) == 1


async def test_provider_rejects_same_idem_key_different_body() -> None:
    provider = _provider()
    provider.arm_wallet(_wallet("ext-1"))
    await provider.create_wallet(
        external_id="ext-1", owner_id=_QUORUM_ID, policy_ids=(_POLICY_ID,),
        idempotency_key="k1", request_auth=_auth(),
    )
    with pytest.raises(IdempotencyKeyConflictError):
        await provider.create_wallet(
            external_id="ext-1", owner_id="kq_DIFFERENT", policy_ids=(_POLICY_ID,),
            idempotency_key="k1", request_auth=_auth(),
        )


@pytest.mark.parametrize("wallet_over", [
    {"chain_type": "solana"},
    {"owner_id": "kq_other"},
    {"policy_ids": (_POLICY_ID, "pol_extra")},
    {"address": "0xdeadbeef"},
    {"external_id": "wrong-ext"},
])
async def test_saga_rejects_wrong_wallet_fields_fail_closed(wallet_over: dict) -> None:
    store = InMemoryStore()
    att = _attempt("attW")
    await _seed(store, att)
    provider = _provider()
    ext = derive_external_id("attW")
    # Build on the correct external_id, then apply the override (which may itself set external_id).
    fields = {"external_id": ext, **wallet_over}
    w = _wallet(fields.pop("external_id"), **fields)
    provider.arm_wallet(w)
    with pytest.raises((ResponseValidationError, WalletCreateUncertain)):
        await provision_execution_wallet(
            store=store, provider=provider, instance=_instance("attW"),
            pinned=_pinned(), request_auth=_auth(), now_fn=_now_fn(),
        )
    persisted = await store.get_provisioning_record("inst_attW")
    assert persisted is None, "a bad wallet must never yield a persisted binding"


async def test_saga_startup_valid_then_policy_drift_before_binding_fails_closed() -> None:
    # CORRECTION 3 / admission: policy is admitted at start, then drifts before binding creation →
    # the second admission (at binding) catches it and fails closed.
    store = InMemoryStore()
    att = _attempt("attD")
    await _seed(store, att)
    provider = _provider()
    provider.arm_wallet(_wallet(derive_external_id("attD")))
    # Flip the policy to a widened one right before binding admission.
    provider.drift_policy_after_create = _provider_policy(
        rules=_policy_payload()["rules"] + [{"method": "secp256k1_sign", "action": "ALLOW", "conditions": []}]
    )
    with pytest.raises(PolicyAdmissionError):
        await provision_execution_wallet(
            store=store, provider=provider, instance=_instance("attD"),
            pinned=_pinned(), request_auth=_auth(), now_fn=_now_fn(),
        )
    assert await store.get_provisioning_record("inst_attD") is None


async def test_saga_provider_outage_fails_closed_readiness_unavailable() -> None:
    store = InMemoryStore()
    att = _attempt("attU")
    await _seed(store, att)
    provider = _provider()
    provider.policy_behavior = "outage"
    with pytest.raises(ProviderUnavailableError):
        await provision_execution_wallet(
            store=store, provider=provider, instance=_instance("attU"),
            pinned=_pinned(), request_auth=_auth(), now_fn=_now_fn(),
        )
    assert await store.get_provisioning_record("inst_attU") is None


async def test_saga_binding_persist_failure_then_retry_binding_only() -> None:
    # Wallet exists but binding/record persist fails once → BINDING_PERSIST_FAILED; a retry persists
    # the binding deterministically and NEVER creates a second wallet.
    store = InMemoryStore()
    att = _attempt("attP")
    await _seed(store, att)
    provider = _provider()
    provider.arm_wallet(_wallet(derive_external_id("attP")))

    calls = {"n": 0}
    real_persist = store.persist_provisioning_record

    async def _flaky(record: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated persistence outage")
        await real_persist(record)

    store.persist_provisioning_record = _flaky  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        await provision_execution_wallet(
            store=store, provider=provider, instance=_instance("attP"),
            pinned=_pinned(), request_auth=_auth(), now_fn=_now_fn(),
        )
    mid = await store.get_deployment_attempt("attP")
    assert mid.status in {AttemptStatus.WALLET_BOUND, AttemptStatus.BINDING_PERSIST_FAILED}

    # Retry — binding persistence only, no second create.
    record = await provision_execution_wallet(
        store=store, provider=provider, instance=_instance("attP"),
        pinned=_pinned(), request_auth=_auth(), now_fn=_now_fn(),
    )
    assert record.wallet_id == "wal_1"
    assert len(provider.create_calls) == 1
    final = await store.get_deployment_attempt("attP")
    assert final.status == AttemptStatus.BINDING_PERSISTED


@pytest.mark.parametrize("resume_state", [
    AttemptStatus.WALLET_REQUESTED,
    AttemptStatus.WALLET_CREATE_UNCERTAIN,
    AttemptStatus.WALLET_CREATED,
    AttemptStatus.WALLET_BOUND,
    AttemptStatus.BINDING_PERSIST_FAILED,
])
async def test_saga_restart_from_every_reserved_state(resume_state: AttemptStatus) -> None:
    # Restart from every reserved saga state converges to BINDING_PERSISTED with exactly one wallet.
    store = InMemoryStore()
    ext = derive_external_id("attS")
    att = _attempt("attS", status=resume_state, external_id=ext)
    await _seed(store, att)
    provider = _provider()
    provider.preexist_wallet(_wallet(ext))  # the wallet already exists (recovered by external_id)
    record = await provision_execution_wallet(
        store=store, provider=provider, instance=_instance("attS"),
        pinned=_pinned(), request_auth=_auth(), now_fn=_now_fn(),
    )
    assert record.wallet_id == "wal_1"
    # Recovery must NEVER mint when the wallet is already recoverable by external_id.
    assert len(provider.create_calls) == 0
    final = await store.get_deployment_attempt("attS")
    assert final.status == AttemptStatus.BINDING_PERSISTED


# ===========================================================================
# R4-A-only enforcement — capability NON-REACHABILITY (import / dependency boundary)
# ===========================================================================

_FORBIDDEN_KEY_LIBS = ("eth_account", "eth_keys", "coincurve", "web3")


def _module_source(module: Any) -> str:
    return Path(inspect.getfile(module)).read_text(encoding="utf-8")


def test_provisioning_modules_import_no_local_key_crypto() -> None:
    import veridex.dust_execution.privy_provisioning as pp
    import veridex.dust_execution.provisioning_record as pr

    for mod in (pp, pr):
        src = _module_source(mod)
        for lib in _FORBIDDEN_KEY_LIBS:
            assert f"import {lib}" not in src and f"from {lib}" not in src, (
                f"{mod.__name__} must never import local-key crypto ({lib})"
            )


def test_provisioning_modules_do_not_import_llm_or_signing_execution() -> None:
    import veridex.dust_execution.privy_provisioning as pp
    import veridex.dust_execution.provisioning_record as pr

    banned = ("veridex_agent", "openai", "agno", "execution_adapter", "orchestration")
    for mod in (pp, pr):
        src = _module_source(mod)
        for name in banned:
            assert name not in src, f"{mod.__name__} must not reach {name} (R4-A capability boundary)"


def test_provisioning_provider_has_no_signing_or_send_capability() -> None:
    provider = _provider()
    # The provisioning boundary is create/read only — NEVER a signing or transaction surface.
    for banned in ("sign_typed_data", "sign_raw_hash", "send_transaction", "sign"):
        assert not hasattr(provider, banned), f"provider must not expose {banned}"


async def test_persisting_binding_makes_zero_signing_calls() -> None:
    store = InMemoryStore()
    att = _attempt("attZ")
    await _seed(store, att)
    provider = _provider()
    provider.arm_wallet(_wallet(derive_external_id("attZ")))
    await provision_execution_wallet(
        store=store, provider=provider, instance=_instance("attZ"),
        pinned=_pinned(), request_auth=_auth(), now_fn=_now_fn(),
    )
    # The recording provider tracks any signing attempt; provisioning must make NONE.
    assert provider.sign_calls == []


def test_wallet_not_found_is_a_typed_sentinel_not_a_wallet() -> None:
    nf = WalletNotFound(external_id="ext-x")
    assert not isinstance(nf, ProvisionedWallet)
    assert nf.external_id == "ext-x"


# ===========================================================================
# HTTP integration — the deploy.py _launch_mm wiring (provisioning gated + fail-closed)
# ===========================================================================


class _EchoProvisioningProvider:
    """A minimal server-side offline fake that echoes the SERVER-derived external_id back on create.

    Realistic: a real provider mints a wallet carrying the requested external_id/owner/policy set. It
    is create/read only (no signing surface) and records that provisioning made zero signing calls.
    """

    provider_version = "test-echo"

    def __init__(self) -> None:
        self._wallets: dict[str, ProvisionedWallet] = {}
        self.sign_calls: list[Any] = []
        self.create_calls: list[str] = []

    async def create_wallet(self, *, external_id, owner_id, policy_ids, idempotency_key, request_auth):  # type: ignore[no-untyped-def]
        self.create_calls.append(external_id)
        wallet = ProvisionedWallet(
            wallet_id=f"wal_{external_id[-8:]}",
            address="0x" + "b" * 40,
            chain_type="ethereum",
            owner_id=owner_id,
            policy_ids=tuple(policy_ids),
            external_id=external_id,
        )
        self._wallets[external_id] = wallet
        return wallet

    async def get_wallet_by_external_id(self, external_id, *, request_auth):  # type: ignore[no-untyped-def]
        w = self._wallets.get(external_id)
        return w if w is not None else WalletNotFound(external_id=external_id)

    async def get_wallet(self, wallet_id, *, request_auth):  # type: ignore[no-untyped-def]
        for w in self._wallets.values():
            if w.wallet_id == wallet_id:
                return w
        raise ResponseValidationError("no such wallet")

    async def get_policy(self, policy_id, *, request_auth):  # type: ignore[no-untyped-def]
        return _provider_policy()

    async def get_key_quorum(self, quorum_id, *, request_auth):  # type: ignore[no-untyped-def]
        return _provider_quorum()


def _provisioning_settings(**overrides: Any):
    from veridex.config import Settings

    base: dict[str, Any] = {
        "AUTH_MODE": "dev",
        "PRIVY_PROVISIONING_ENABLED": True,
        "PRIVY_EXECUTION_POLICY_ID": _POLICY_ID,
        "PRIVY_EXECUTION_QUORUM_ID": _QUORUM_ID,
        "PRIVY_EXECUTION_POLICY_CONTENT_HASH": _CANONICAL_POLICY.content_hash(),
        "PRIVY_EXECUTION_POLICY_FULL_CONTENT_HASH": _provider_policy().full_content_hash(),
        "PRIVY_EXECUTION_QUORUM_CONTENT_HASH": _CANONICAL_QUORUM.content_hash(),
        "PRIVY_EXECUTION_QUORUM_THRESHOLD": 2,
        "PRIVY_EXECUTION_AUTHORIZATION_KEY_ID": "authkey-ref-abc123",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _mm_deploy_app(provider: Any, settings: Any):
    from fastapi import FastAPI

    from tests.test_deploy_mm import _resolver
    from tests.test_mm_strategy_integration import _warm_seed_state
    from veridex.api.deploy import DeployDeps, register_deploy_routes
    from veridex.mm_strategy.offline_proposer import OfflineRecordingProposer

    store = InMemoryStore()
    deps = DeployDeps(
        anchor_fn=None,
        mm_tape_resolver=_resolver(),
        mm_proposer=OfflineRecordingProposer(),
        mm_seed_state=_warm_seed_state(),
        provisioning_provider=provider,
    )
    app = FastAPI()
    register_deploy_routes(app, store=store, settings=settings, deploy_deps=deps)
    return app, store


async def test_http_deploy_provisions_binding_and_seals_with_zero_signing() -> None:
    from tests.test_deploy_mm import _drain, _mm_payload, _transport

    provider = _EchoProvisioningProvider()
    app, store = _mm_deploy_app(provider, _provisioning_settings())
    async with _transport(app) as client:
        resp = await client.post("/agents/deploy", json=_mm_payload())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    await _drain(app)

    instance = await store.get_agent_instance(body["instance_id"])
    assert instance.status == DeployStatus.SEALED, instance.last_failure_reason
    # The binding + immutable record were persisted (R4-A only; the CLAIM stays withheld).
    record = await store.get_provisioning_record(body["instance_id"])
    assert record is not None
    assert record.binding.chain_id == CHAIN_ID_POLYGON
    assert record.policy_id == _POLICY_ID
    assert len(provider.create_calls) == 1
    # Persisting a binding NEVER arms signing — the provisioning provider made zero signing calls.
    assert provider.sign_calls == []


async def test_http_provisioning_failure_fails_closed_no_run() -> None:
    from tests.test_deploy_mm import _drain, _mm_payload, _transport

    provider = _EchoProvisioningProvider()
    # Drifted pinned hash → admission fails closed → the instance is marked FAILED, no run seals.
    bad = _provisioning_settings(PRIVY_EXECUTION_POLICY_CONTENT_HASH="0" * 64)
    app, store = _mm_deploy_app(provider, bad)
    async with _transport(app) as client:
        resp = await client.post("/agents/deploy", json=_mm_payload())
    assert resp.status_code == 200, resp.text  # preflight passes; provisioning fails in the background
    body = resp.json()
    await _drain(app)
    instance = await store.get_agent_instance(body["instance_id"])
    assert instance.status == DeployStatus.FAILED
    assert await store.get_provisioning_record(body["instance_id"]) is None


async def test_http_live_guarded_rejected_regardless_of_wallet() -> None:
    from tests.test_deploy_mm import _mm_payload, _transport

    provider = _EchoProvisioningProvider()
    app, store = _mm_deploy_app(provider, _provisioning_settings())
    async with _transport(app) as client:
        resp = await client.post("/agents/deploy", json=_mm_payload(execution_mode="live_guarded"))
    # live_guarded is refused at preflight BEFORE any provisioning — independent of any wallet.
    assert resp.status_code == 422, resp.text
    assert (await store.list_agent_instances()) == []
    assert provider.create_calls == []


# ===========================================================================
# Codex Gate-2 recovery/composition regressions (3 Major findings)
# ===========================================================================


async def test_crash_after_record_commit_before_status_cas_recovers() -> None:
    # MAJOR-1: a crash AFTER the immutable record is committed but BEFORE the terminal status CAS must
    # be recoverable. A naive retry that rebuilds the record with a fresh provider_verification_ts would
    # mint a DIFFERENT record hash → the immutable store rejects it → permanently unrecoverable. Re-entry
    # must LOAD + REUSE the already-committed record and just complete the status transition.
    store = InMemoryStore()
    att = _attempt("attCR")
    await _seed(store, att)
    provider = _provider()
    provider.arm_wallet(_wallet(derive_external_id("attCR")))
    real_advance = store.advance_deployment_attempt

    async def _fail_final(attempt_id: str, *, expected: Any, new: Any, external_id: Any = None) -> Any:
        if new == AttemptStatus.BINDING_PERSISTED:
            raise RuntimeError("crash after record commit, before status CAS")
        return await real_advance(attempt_id, expected=expected, new=new, external_id=external_id)

    store.advance_deployment_attempt = _fail_final  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await provision_execution_wallet(
            store=store, provider=provider, instance=_instance("attCR"),
            pinned=_pinned(), request_auth=_auth(), now_fn=_now_fn(datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)),
        )
    mid = await store.get_deployment_attempt("attCR")
    assert mid.status == AttemptStatus.WALLET_BOUND  # record written, status not yet advanced
    rec1 = await store.get_provisioning_record("inst_attCR")
    assert rec1 is not None

    # Retry with a DIFFERENT clock: a rebuild would change provider_verification_ts → different hash.
    store.advance_deployment_attempt = real_advance  # type: ignore[method-assign]
    rec2 = await provision_execution_wallet(
        store=store, provider=provider, instance=_instance("attCR"),
        pinned=_pinned(), request_auth=_auth(), now_fn=_now_fn(datetime(2026, 7, 19, 9, 30, 0, tzinfo=UTC)),
    )
    final = await store.get_deployment_attempt("attCR")
    assert final.status == AttemptStatus.BINDING_PERSISTED  # recovered, NOT binding_persist_failed
    assert rec2.provisioning_record_hash() == rec1.provisioning_record_hash()  # REUSED, not rebuilt
    assert rec2.provider_verification_ts == rec1.provider_verification_ts
    assert len(provider.create_calls) == 1  # no second wallet


def test_settings_enabled_without_pins_fails_closed() -> None:
    # MAJOR-3(a): Settings must fail closed at construction if provisioning is enabled but the pinned
    # custody contract is absent — not silently accept an "enabled but unpinned" config.
    from veridex.config import Settings

    with pytest.raises(ValueError):
        Settings(_env_file=None, AUTH_MODE="dev", PRIVY_PROVISIONING_ENABLED=True)


async def test_mm_deploy_enabled_without_provider_fails_closed() -> None:
    # MAJOR-3(c): provisioning ENABLED (complete pins) but NO provider wired must FAIL CLOSED at deploy —
    # never a silent skip that seals the run with no wallet binding.
    from tests.test_deploy_mm import _drain, _mm_payload, _transport

    app, store = _mm_deploy_app(None, _provisioning_settings())  # enabled, but provisioning_provider=None
    async with _transport(app) as client:
        resp = await client.post("/agents/deploy", json=_mm_payload())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    await _drain(app)
    instance = await store.get_agent_instance(body["instance_id"])
    assert instance.status == DeployStatus.FAILED  # fail closed, not silently sealed
    assert await store.get_provisioning_record(body["instance_id"]) is None


async def test_http_retry_redrives_incomplete_provisioning_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    # MAJOR-2: the REAL HTTP retry path must re-enter wallet recovery for an MM deploy whose provisioning
    # saga is INCOMPLETE (nonterminal wallet state). Seed an existing instance + a WALLET_REQUESTED attempt
    # whose provider already holds the external-id wallet (a prior process died mid-saga); a same-key retry
    # must re-drive recovery → reconcile the SAME wallet → BINDING_PERSISTED, with NO new run and NO new wallet.
    from tests.test_deploy_mm import _drain, _mm_payload, _transport
    from veridex.deploy.preflight import DeployConfig
    from veridex.runtime import agentos_service as svc

    provider = _provider()
    app, store = _mm_deploy_app(provider, _provisioning_settings())

    op = "did:privy:dev"
    idem = "idem-retry-1"
    attempt_id = "attRETRY"
    inst_id = f"inst_{attempt_id}"
    ext = derive_external_id(attempt_id)
    provider.preexist_wallet(_wallet(ext))  # the wallet already exists at the provider

    cfg = DeployConfig(**_mm_payload())
    fp = cfg.config_hash()
    await store.persist_deployment_attempt(
        DeploymentAttempt(
            attempt_id=attempt_id, operator_id=op, idempotency_key=idem, config_fingerprint=fp,
            status=AttemptStatus.WALLET_REQUESTED, created_at="2026-07-18T11:00:00+00:00",
            instance_id=inst_id, external_id=ext,
        )
    )
    now = "2026-07-18T11:00:00+00:00"
    await store.persist_agent_instance(
        AgentInstance(
            instance_id=inst_id, template_id="quoteguard-mm-template", agent_id="studio-mm-agent",
            submitted_config=_mm_payload(), effective_config={}, config_hash=fp, policy_hash="p" * 8,
            source_mode="replay", execution_mode="dry_run", run_id="run_seed",
            status=DeployStatus.RUNNING, operator_id=op, created_at=now, updated_at=now,
        )
    )

    # Prove recovery starts NO run.
    run_starts: list[int] = []
    real_start = svc.start_owned_instance_run

    async def _spy(*a: Any, **k: Any) -> Any:
        run_starts.append(1)
        return await real_start(*a, **k)

    monkeypatch.setattr(svc, "start_owned_instance_run", _spy)

    async with _transport(app) as client:
        resp = await client.post("/agents/deploy", json=_mm_payload(), headers={"Idempotency-Key": idem})
    assert resp.status_code == 200, resp.text
    assert resp.json()["instance_id"] == inst_id  # reconciled to the SAME instance
    await _drain(app)

    final = await store.get_deployment_attempt(attempt_id)
    assert final.status == AttemptStatus.BINDING_PERSISTED  # recovery re-drove + completed the saga
    assert await store.get_provisioning_record(inst_id) is not None
    assert provider.create_calls == []  # wallet pre-existed → reconciled, never minted
    assert provider.get_by_external_calls  # reconciliation via external_id happened
    assert run_starts == []  # recovery started NO second run


# ===========================================================================
# Codex Gate-2 re-gate round 2 (3 deeper Major findings)
# ===========================================================================


async def test_http_retry_redrives_pending_provisioning_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    # MAJOR-1 (round 2): a crash AFTER the instance is persisted but BEFORE _launch_mm's first advance
    # leaves the attempt at PENDING with no external_id. The same-key retry must re-drive PROVISIONING-ONLY
    # recovery (safe: the saga derives+persists external_id before its first mutation) — not return as if
    # complete. Exactly one wallet, zero new runs.
    from tests.test_deploy_mm import _drain, _mm_payload, _transport
    from veridex.deploy.preflight import DeployConfig
    from veridex.runtime import agentos_service as svc

    provider = _provider()
    app, store = _mm_deploy_app(provider, _provisioning_settings())
    op, idem, attempt_id = "did:privy:dev", "idem-pending-1", "attPEND"
    inst_id = f"inst_{attempt_id}"
    ext = derive_external_id(attempt_id)
    provider.arm_wallet(_wallet(ext))  # wallet NOT yet created (PENDING) — the saga must create it
    fp = DeployConfig(**_mm_payload()).config_hash()
    await store.persist_deployment_attempt(
        DeploymentAttempt(
            attempt_id=attempt_id, operator_id=op, idempotency_key=idem, config_fingerprint=fp,
            status=AttemptStatus.PENDING, created_at="2026-07-18T11:00:00+00:00", instance_id=inst_id, external_id=None,
        )
    )
    now = "2026-07-18T11:00:00+00:00"
    await store.persist_agent_instance(
        AgentInstance(
            instance_id=inst_id, template_id="quoteguard-mm-template", agent_id="studio-mm-agent",
            submitted_config=_mm_payload(), effective_config={}, config_hash=fp, policy_hash="p" * 8,
            source_mode="replay", execution_mode="dry_run", run_id="run_seed",
            status=DeployStatus.RUNNING, operator_id=op, created_at=now, updated_at=now,
        )
    )
    run_starts: list[int] = []
    real_start = svc.start_owned_instance_run

    async def _spy(*a: Any, **k: Any) -> Any:
        run_starts.append(1)
        return await real_start(*a, **k)

    monkeypatch.setattr(svc, "start_owned_instance_run", _spy)
    async with _transport(app) as client:
        resp = await client.post("/agents/deploy", json=_mm_payload(), headers={"Idempotency-Key": idem})
    assert resp.status_code == 200, resp.text
    assert resp.json()["instance_id"] == inst_id
    await _drain(app)

    final = await store.get_deployment_attempt(attempt_id)
    assert final.status == AttemptStatus.BINDING_PERSISTED  # recovery re-drove from PENDING
    assert final.external_id == ext  # derived + persisted during recovery
    assert await store.get_provisioning_record(inst_id) is not None
    assert len(provider.create_calls) == 1  # exactly one wallet minted
    assert run_starts == []  # zero new runs


async def test_record_reuse_rejects_stale_full_policy_after_readmission() -> None:
    # MAJOR-2 (round 2): on crash-recovery re-entry the record is reused only if it still describes the
    # CURRENT live policy. A field-level policy substitution (different policy_full_content_hash but SAME
    # coarse hash / policy_id / quorum / binding_hash / wallet) must be caught at the reuse boundary.
    store = InMemoryStore()
    att = _attempt("attSTALE")
    await _seed(store, att)
    provider = _provider()  # policy A
    provider.arm_wallet(_wallet(derive_external_id("attSTALE")))
    real_advance = store.advance_deployment_attempt

    async def _fail_final(attempt_id: str, *, expected: Any, new: Any, external_id: Any = None) -> Any:
        if new == AttemptStatus.BINDING_PERSISTED:
            raise RuntimeError("crash before final CAS")
        return await real_advance(attempt_id, expected=expected, new=new, external_id=external_id)

    store.advance_deployment_attempt = _fail_final  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await provision_execution_wallet(
            store=store, provider=provider, instance=_instance("attSTALE"),
            pinned=_pinned(), request_auth=_auth(), now_fn=_now_fn(),
        )
    rec1 = await store.get_provisioning_record("inst_attSTALE")
    assert rec1 is not None

    # The live policy drifts at the FIELD level (different full hash, IDENTICAL coarse hash); the operator
    # re-pins the new full hash. The committed record still describes the OLD full policy.
    policy_b = _provider_policy(
        rules=[
            {"method": ALLOWED_SIGN_METHOD, "action": "ALLOW", "conditions": [_typed_condition(ORDER_PRIMARY_TYPE, value="0xEVIL")]},
            {"method": ALLOWED_SIGN_METHOD, "action": "ALLOW", "conditions": [_typed_condition(CLOB_AUTH_PRIMARY_TYPE)]},
        ]
    )
    assert policy_b.to_wallet_policy(owner_type="quorum").content_hash() == _CANONICAL_POLICY.content_hash()  # coarse SAME
    assert policy_b.full_content_hash() != rec1.policy_full_content_hash  # full DIFFERENT
    provider._policy = policy_b  # the live policy now returns B
    pinned_b = PinnedProvisioningConfig(
        policy_id=_POLICY_ID, quorum_id=_QUORUM_ID, owner_id=_QUORUM_ID,
        policy_content_hash=_CANONICAL_POLICY.content_hash(),
        quorum_content_hash=_CANONICAL_QUORUM.content_hash(), quorum_threshold=2,
        authorization_key_id="authkey-ref-abc123",
        policy_full_content_hash=policy_b.full_content_hash(),  # operator re-pinned to B
    )
    store.advance_deployment_attempt = real_advance  # type: ignore[method-assign]
    with pytest.raises(ResponseValidationError):
        await provision_execution_wallet(
            store=store, provider=provider, instance=_instance("attSTALE"),
            pinned=pinned_b, request_auth=_auth(), now_fn=_now_fn(),
        )
    final = await store.get_deployment_attempt("attSTALE")
    assert final.status != AttemptStatus.BINDING_PERSISTED  # fail closed — never advanced on a stale record


async def test_provisioning_readiness_probe_passes_and_fails_closed() -> None:
    # MAJOR-3 (round 2): the readiness probe re-admits the LIVE policy/quorum — not just provider presence.
    from veridex.api.deploy import check_provisioning_readiness
    from veridex.config import Settings

    settings = _provisioning_settings()
    # (1) enabled + good live policy/quorum → passes.
    await check_provisioning_readiness(settings, _provider())
    # (2) enabled + provider unreachable → fails closed.
    outage = _provider()
    outage.policy_behavior = "outage"
    with pytest.raises(ProviderUnavailableError):
        await check_provisioning_readiness(settings, outage)
    # (3) enabled + drifted policy (widened → not typed-data-only) → fails closed.
    drifted = _provider(
        policy=_provider_policy(rules=_policy_payload()["rules"] + [{"method": "secp256k1_sign", "action": "ALLOW", "conditions": []}])
    )
    with pytest.raises(PolicyAdmissionError):
        await check_provisioning_readiness(settings, drifted)
    # enabled + no provider wired → fails closed.
    with pytest.raises(RuntimeError):
        await check_provisioning_readiness(settings, None)
    # disabled → fully inert (no provider needed, no raise).
    await check_provisioning_readiness(Settings(_env_file=None, AUTH_MODE="dev"), None)


async def test_provisioning_readyz_route_fails_closed_on_drift() -> None:
    # MAJOR-3 (round 2), route-level: /agents/provisioning/readyz is 200 when the live policy/quorum admit,
    # 503 on drift — so an orchestrator never advertises ready while custody is unverifiable.
    from tests.test_deploy_mm import _transport

    app_ok, _ = _mm_deploy_app(_provider(), _provisioning_settings())
    async with _transport(app_ok) as client:
        r_ok = await client.get("/agents/provisioning/readyz")
    assert r_ok.status_code == 200, r_ok.text

    drifted = _provider(policy=_provider_policy(rules=_policy_payload()["rules"][:1]))  # missing ClobAuth
    app_bad, _ = _mm_deploy_app(drifted, _provisioning_settings())
    async with _transport(app_bad) as client:
        r_bad = await client.get("/agents/provisioning/readyz")
    assert r_bad.status_code == 503, r_bad.text


# ===========================================================================
# Codex Gate-2 re-gate round 3 — concurrency: recovery wins CAS, original must still seal
# ===========================================================================


async def test_recovery_wins_cas_original_launch_still_seals(monkeypatch: pytest.MonkeyPatch) -> None:
    # NEW MAJOR: a same-key duplicate deploy schedules recovery while the ORIGINAL _launch_mm is still
    # admitting the SAME PENDING attempt. If recovery WINS the PENDING->WALLET_REQUESTED CAS, the original
    # must treat that CAS loss as IDEMPOTENT RE-ENTRY (reconcile from the winner's state) and STILL seal
    # the run — never mark the shared instance FAILED. Forced ordering via a gate on the original's
    # startup admission.
    import asyncio

    from tests.test_deploy_mm import _drain, _mm_payload, _transport
    from veridex.runtime import agentos_service as svc

    gate = asyncio.Event()

    class _GatedEchoProvider:
        provider_version = "gated-echo"

        def __init__(self) -> None:
            self._wallets: dict[str, ProvisionedWallet] = {}
            self.create_calls: list[str] = []
            self.sign_calls: list[Any] = []
            self._policy_call = 0

        async def create_wallet(self, *, external_id, owner_id, policy_ids, idempotency_key, request_auth):  # type: ignore[no-untyped-def]
            self.create_calls.append(external_id)
            w = ProvisionedWallet(
                wallet_id=f"wal_{external_id[-8:]}", address="0x" + "b" * 40, chain_type="ethereum",
                owner_id=owner_id, policy_ids=tuple(policy_ids), external_id=external_id,
            )
            self._wallets[external_id] = w
            return w

        async def get_wallet_by_external_id(self, external_id, *, request_auth):  # type: ignore[no-untyped-def]
            w = self._wallets.get(external_id)
            return w if w is not None else WalletNotFound(external_id=external_id)

        async def get_wallet(self, wallet_id, *, request_auth):  # type: ignore[no-untyped-def]
            for w in self._wallets.values():
                if w.wallet_id == wallet_id:
                    return w
            raise ResponseValidationError("no such wallet")

        async def get_policy(self, policy_id, *, request_auth):  # type: ignore[no-untyped-def]
            self._policy_call += 1
            if self._policy_call == 1:  # HOLD the ORIGINAL launch's startup admission at the gate
                await gate.wait()
            return _provider_policy()

        async def get_key_quorum(self, quorum_id, *, request_auth):  # type: ignore[no-untyped-def]
            return _provider_quorum()

    provider = _GatedEchoProvider()
    app, store = _mm_deploy_app(provider, _provisioning_settings())

    run_starts: list[int] = []
    real_start = svc.start_owned_instance_run

    async def _spy(*a: Any, **k: Any) -> Any:
        run_starts.append(1)
        return await real_start(*a, **k)

    monkeypatch.setattr(svc, "start_owned_instance_run", _spy)

    idem = "idem-race-1"
    async with _transport(app) as client:
        # POST #1 (original): fresh deploy → creates instance + PENDING attempt + schedules _launch_mm.
        first = await client.post("/agents/deploy", json=_mm_payload(), headers={"Idempotency-Key": idem})
        assert first.status_code == 200, first.text
        inst_id = first.json()["instance_id"]
        attempt_id = inst_id.removeprefix("inst_")
        # Let _launch_mm start and PARK at the gate (its startup get_policy, call #1).
        for _ in range(50):
            await asyncio.sleep(0)
            if provider._policy_call >= 1:
                break
        assert provider._policy_call == 1  # the original launch is parked at the gate

        # POST #2 (duplicate, same key): finds the existing instance + PENDING attempt → schedules recovery.
        second = await client.post("/agents/deploy", json=_mm_payload(), headers={"Idempotency-Key": idem})
        assert second.status_code == 200, second.text
        assert second.json()["instance_id"] == inst_id
        # Let RECOVERY run to completion (NOT gated) → wins the PENDING->WALLET_REQUESTED CAS and drives
        # provisioning to BINDING_PERSISTED.
        for _ in range(400):
            await asyncio.sleep(0)
            att = await store.get_deployment_attempt(attempt_id)
            if att is not None and att.status == AttemptStatus.BINDING_PERSISTED:
                break
        att = await store.get_deployment_attempt(attempt_id)
        assert att is not None and att.status == AttemptStatus.BINDING_PERSISTED  # recovery completed provisioning

        # Release the gate → the ORIGINAL launch resumes, hits the CAS conflict, re-enters, and seals.
        gate.set()
        await _drain(app)

    instance = await store.get_agent_instance(inst_id)
    assert instance.status == DeployStatus.SEALED, instance.last_failure_reason  # NOT FAILED
    final = await store.get_deployment_attempt(attempt_id)
    assert final is not None and final.status == AttemptStatus.BINDING_PERSISTED
    assert len(provider.create_calls) == 1  # exactly one wallet (recovery); the original re-entry reconciled
    assert len(run_starts) == 1  # exactly one run started (the original launch)
