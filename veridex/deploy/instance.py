"""Durable deployed-instance record — the Store-backed source of truth for a Studio deploy.

A deployed :class:`AgentInstance` is a PERSISTED record (Store/Postgres-backed), NOT an ephemeral
in-memory entry: it survives a process restart and is the authoritative deployment identity
(config_hash + policy_hash + template + allowlists + modes + run_id + lifecycle status). Only the
live background TASK HANDLE (cancellable on shutdown) is process-local — the record itself lives in
the Store, persisted AFTER preflight passes (persist-then-launch) and advanced to a terminal status
when the deployed run seals or fails.

Pure value objects: two enums + a Pydantic model + a small helper. No I/O, no async, no framework
imports — the FastAPI route (:mod:`veridex.api.deploy`) builds + persists these; the Store
(:mod:`veridex.store`) round-trips them. Credentials never live here (COM-001).
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from veridex.deploy.preflight import PreflightCheck


class DeployStatus(str, Enum):
    """Lifecycle state of a deployed :class:`AgentInstance`.

    The deploy sequence (persist-then-launch): a row is persisted as ``PENDING`` only AFTER preflight
    passes; the launched run advances it to ``RUNNING``; the background task then reaches a terminal
    ``SEALED`` (the window sealed, verified, and persisted) or ``FAILED`` (a pre-seal error). A
    shutdown-cancelled run is neither sealed nor failed — it is left in its last non-terminal state
    (honest: it was running when the process was cancelled).

    ``last_failure_reason`` on the record is populated ONLY for ``FAILED`` and carries a controlled
    :class:`DeployFailureReason` taxonomy value — NEVER a raw framework trace (raw diagnostics go to
    the server log only, so no trace shape can leak through the durable record or the response).
    """

    PENDING = "pending"
    RUNNING = "running"
    SEALED = "sealed"
    FAILED = "failed"


class DeployFailureReason(str, Enum):
    """The tiny, controlled failure vocabulary for a deployed instance's ``last_failure_reason``.

    A SHORT taxonomy value — never a raw exception repr / stack trace — so the durable record (and
    any narrow response derived from it) can never leak a framework trace shape (AC-2D-701A). Raw
    diagnostics are logged with ``exc_info`` by the deploy route's done-callback, never persisted.

    Values:
        PREFLIGHT_FAILED: A refused deploy (named in the 422 response; a refused deploy pins NO row,
            so this value is vocabulary only — it never lands on a stored instance).
        SEAL_FAILED: The launched run failed on the seal / verify / anchor / persist path.
        LANE_FAILED: The execution lane failed (isolated inside the runner; vocabulary for the audit).
        RUNTIME_ERROR: An agent-construction / runtime error before the run reached the seal path.
    """

    PREFLIGHT_FAILED = "preflight_failed"
    SEAL_FAILED = "seal_failed"
    LANE_FAILED = "lane_failed"
    RUNTIME_ERROR = "runtime_error"


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
        preflight_checks: The named preflight verdicts that GATED this launch (each check's name +
            pass/fail + short reason) — the durable "why did this agent launch?" audit trail, linked
            to ``run_id``. Only ever carries PASSING/not-applicable checks (a failing preflight pins
            no row).
        status: Lifecycle state (:class:`DeployStatus`); durably updated by the background task.
        last_failure_reason: Controlled :class:`DeployFailureReason` set only when ``status ==
            FAILED``; else ``None``. Never a raw trace.
        operator_id: The SERVER-DERIVED owner identity (the authenticated Privy principal's
            ``did:privy:...``), persisted by the deploy path (AC-18). **Optional** — legacy rows
            persisted before this field existed lack it, and the store validates stored
            ``record_json`` into the CURRENT model on read (``store.py``), so a REQUIRED field would
            raise on those rows; it MUST default to ``None``. Fail-closed: ``None`` means UNOWNED —
            such a row is never listed for, or inherited by, any caller.
        runtime_handle: Provider-neutral pointer to the REPLACEABLE runtime infra
            (``{runtime_kind, runtime_agent_id, session_id, run_id}``; ``runtime_kind="agentos"``
            today, ``"inprocess"`` for the fallback). Created AFTER the instance and may be re-minted
            on restart under the SAME ``run_id`` — it is NEVER the ownership / result / Gate-B
            authority. ``run_id`` remains the authoritative Veridex result/evidence identity.
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
    preflight_checks: list[PreflightCheck] = Field(default_factory=list)
    status: DeployStatus = DeployStatus.PENDING
    last_failure_reason: DeployFailureReason | None = None
    # Optional so legacy record_json (pre-field) still validates on read; None == UNOWNED (fail-closed).
    operator_id: str | None = None
    runtime_handle: dict[str, Any] | None = None
    # R-4: the FROZEN production-replay identity a ``replay`` deploy committed to BEFORE launch —
    # ``{pack_id, fixture_id, content_hash}``, server-derived from the verified R-2 catalog. Persisted
    # durably so an idempotent retry REUSES it (never re-selects) and the sealed run's tape identity is
    # observable. ``None`` for a live deploy or a legacy record written before R-4.
    replay_binding: dict[str, Any] | None = None
    created_at: str
    updated_at: str
