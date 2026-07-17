"""Durable deployment-attempt record — the idempotency + crash-recovery backbone of the deploy saga.

A :class:`DeploymentAttempt` is a PERSISTED, write-once idempotency claim: it is inserted (once) into
an append-only ledger table BEFORE any external side effect of a deploy, under a
``UNIQUE(operator_id, idempotency_key)`` constraint. It CLAIMS the (operator, key) pair and
pre-allocates the deterministic ``instance_id`` the deploy targets — so a retry (or a concurrent
duplicate) reconciles to the SAME instance via the recorded state rather than blindly re-executing a
side effect. The unique-violation IS the idempotency signal (:class:`DuplicateAttemptError`).

Pure value objects: one enum + one exception + a Pydantic model. No I/O, no async, no framework
imports — the deploy route (:mod:`veridex.api.deploy`) builds + persists these; the Store
(:mod:`veridex.store`) round-trips them.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class DuplicateAttemptError(Exception):
    """Raised when persisting an attempt whose ``(operator_id, idempotency_key)`` is already claimed.

    The single, store-agnostic idempotency signal: :class:`~veridex.store.PostgresStore` maps the
    Postgres ``UniqueViolation`` to this, and :class:`~veridex.store.InMemoryStore` raises it in code
    from the same collision — so the deploy route reconciles on ONE contract regardless of backend.
    """


class AttemptStatus(str, Enum):
    """Write-once, FORWARD-ONLY lifecycle of a :class:`DeploymentAttempt` (never rewinds).

    The members are declared in SAGA order; :meth:`DeploymentAttempt.transition` treats a later
    member as strictly forward and refuses any same-or-earlier move (write-once). The RESERVED
    ``wallet_*`` / ``binding_*`` states are DEFINED here for II-5b (the Privy-wallet provisioning slot
    that extends the saga) but carry NO logic in I-3 — no wallet flow is built around them yet.

    The recovery guarantee holds EXACTLY for this enumerated model — nothing stronger is claimed.
    """

    PENDING = "pending"
    # --- RESERVED for II-5b (Privy wallet provisioning slot) — defined-only in I-3 ---
    WALLET_REQUESTED = "wallet_requested"
    WALLET_CREATE_UNCERTAIN = "wallet_create_uncertain"
    WALLET_CREATED = "wallet_created"
    WALLET_BOUND = "wallet_bound"
    BINDING_PERSIST_FAILED = "binding_persist_failed"
    BINDING_PERSISTED = "binding_persisted"
    # --- I-3 active states ---
    INSTANCE_CREATED = "instance_created"
    RUNTIME_LAUNCH_FAILED = "runtime_launch_failed"
    FAILED_RECOVERABLE = "failed_recoverable"
    SUCCEEDED = "succeeded"
    FAILED_TERMINAL = "failed_terminal"


# The forward-only rank: a status may only advance to one declared LATER in this tuple. Terminal
# states sit last, so nothing advances past them. Declaration order IS the single source of order.
_STATUS_ORDER: tuple[AttemptStatus, ...] = tuple(AttemptStatus)
_STATUS_RANK: dict[AttemptStatus, int] = {status: rank for rank, status in enumerate(_STATUS_ORDER)}


def attempt_status_values() -> tuple[str, ...]:
    """Return the full set of valid ``deployment_attempts.status`` strings (enum-definition order)."""
    return tuple(member.value for member in AttemptStatus)


class DeploymentAttempt(BaseModel):
    """The DURABLE, write-once idempotency claim persisted BEFORE any deploy side effect.

    Attributes:
        attempt_id: Stable identifier for this attempt (also the seed of the deterministic
            ``instance_id`` the deploy targets — see :mod:`veridex.api.deploy`).
        operator_id: The SERVER-DERIVED owner identity (the authenticated Privy principal's
            ``did:privy:...``) — never a client-supplied value.
        idempotency_key: The caller-scoped key that makes a deploy idempotent; unique per operator
            (``UNIQUE(operator_id, idempotency_key)`` in the store).
        config_fingerprint: A stable digest of the submitted config — a reuse of the same key with a
            DIFFERENT fingerprint is a conflict (never silently reused or overwritten).
        status: The write-once, forward-only :class:`AttemptStatus` (see :meth:`transition`).
        created_at: ISO-8601 UTC timestamp the claim was persisted.
        instance_id: The deterministic ``AgentInstance`` id this attempt targets — pre-allocated at
            claim time (write-once); recovery keys on whether that instance exists.
        external_id: Write-once handle for an external side effect's result (RESERVED for II-5b's
            wallet/runtime provisioning slot); ``None`` in I-3.
    """

    attempt_id: str
    operator_id: str
    idempotency_key: str
    config_fingerprint: str
    status: AttemptStatus
    created_at: str
    instance_id: str | None = None
    external_id: str | None = None

    def transition(self, new_status: AttemptStatus) -> DeploymentAttempt:
        """Return a COPY advanced to ``new_status`` (functional; the original is left unchanged).

        Enforces the write-once, forward-only rule: ``new_status`` must rank strictly LATER than the
        current status. A same-or-earlier target (a rewind, or re-writing the same state) raises.

        Args:
            new_status: The status to advance to.

        Returns:
            A new :class:`DeploymentAttempt` with ``status == new_status``.

        Raises:
            ValueError: If ``new_status`` is not strictly forward of the current status.
        """
        if _STATUS_RANK[new_status] <= _STATUS_RANK[self.status]:
            raise ValueError(
                f"illegal attempt-status transition {self.status.value!r} -> {new_status.value!r} "
                "(status is write-once and forward-only)"
            )
        return self.model_copy(update={"status": new_status})
