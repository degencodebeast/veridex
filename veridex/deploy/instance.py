"""Durable deployed-instance record — the Store-backed source of truth for a Studio deploy.

A deployed :class:`AgentInstance` is a PERSISTED record (Store/Postgres-backed), NOT an ephemeral
in-memory entry: it survives a process restart and is the authoritative deployment identity
(config_hash + policy_hash + template + allowlists + modes + run_id + lifecycle status). Only the
live background TASK HANDLE (cancellable on shutdown) is process-local — the record itself lives in
the Store, persisted AFTER preflight passes (persist-then-launch) and advanced to a terminal status
when the deployed run seals or fails.

Pure value objects: an enum + a Pydantic model + a small helper. No I/O, no async, no framework
imports — the FastAPI route (:mod:`veridex.api.deploy`) builds + persists these; the Store
(:mod:`veridex.store`) round-trips them. Credentials never live here (COM-001).
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class DeployStatus(str, Enum):
    """Lifecycle state of a deployed :class:`AgentInstance`.

    The deploy sequence (persist-then-launch): a row is persisted as ``PENDING`` only AFTER preflight
    passes; the launched run advances it to ``RUNNING``; the background task then reaches a terminal
    ``SEALED`` (the window sealed, verified, and persisted) or ``FAILED`` (a pre-seal error). A
    shutdown-cancelled run is neither sealed nor failed — it is left in its last non-terminal state
    (honest: it was running when the process was cancelled).

    ``last_failure_reason`` on the record is populated ONLY for ``FAILED`` and carries a bounded,
    honest reason string — NEVER a raw framework trace (raw diagnostics go to the server log only).
    """

    PENDING = "pending"
    RUNNING = "running"
    SEALED = "sealed"
    FAILED = "failed"


def deploy_status_values() -> tuple[str, ...]:
    """Return the full set of valid ``agent_instances.status`` strings.

    The single source of truth for the store CHECK-constraint vocabulary (a drift-guard test asserts
    the store's literal tuple equals this), so the lifecycle states are never hardcoded twice.

    Returns:
        The ``DeployStatus`` values in enum-definition order.
    """
    return tuple(m.value for m in DeployStatus)


class AgentInstance(BaseModel):
    """The DURABLE, PINNED deployment record — the instance IS the deployment (no separate id).

    Persisted to the Store as the source of truth (survives an app.state clear / process restart).
    Types are enforced at the pydantic boundary; ``config_hash`` / ``policy_hash`` are pinned by the
    route only AFTER the fail-closed preflight passes.

    Attributes:
        instance_id: Stable identifier for this deployed instance (``inst_...``).
        template_id: The strategy-archetype template the instance was configured from.
        agent_id: The deployed agent's identifier.
        submitted_config: The submitted (validated) Studio config as received (non-secret snapshot).
        effective_config: The normalized config the runner actually builds the agent from.
        config_hash: SHA-256 of the submitted config — pinned only after preflight.
        policy_hash: SHA-256 of the committed policy envelope.
        source_mode: ``replay`` or ``live``.
        execution_mode: ``paper`` | ``dry_run`` | ``live_guarded``.
        market_allowlist: The pinned market universe.
        venue_allowlist: The pinned venue universe.
        run_id: The run this instance launched (known before the seal — the async handle).
        status: Lifecycle state (:class:`DeployStatus`); durably updated by the background task.
        last_failure_reason: Bounded, honest reason set only when ``status == FAILED``; else ``None``.
        created_at: ISO-8601 UTC timestamp the record was persisted.
        updated_at: ISO-8601 UTC timestamp of the last durable write.
    """

    instance_id: str
    template_id: str
    agent_id: str
    submitted_config: dict[str, Any]
    effective_config: dict[str, Any]
    config_hash: str
    policy_hash: str
    source_mode: str
    execution_mode: str
    market_allowlist: list[str] = Field(default_factory=list)
    venue_allowlist: list[str] = Field(default_factory=list)
    run_id: str
    status: DeployStatus = DeployStatus.PENDING
    last_failure_reason: str | None = None
    created_at: str
    updated_at: str
