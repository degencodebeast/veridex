"""B5 — async repository persistence for run results and competition data (REQ-105 / AC-105, gate CON-006).

Two implementations behind one async :class:`Store` protocol:

  * :class:`InMemoryStore` — pure stdlib, dict-backed; the offline test suite uses ONLY this
    (NO network / DB). Round-trips a :class:`~veridex.runtime.orchestrator.RunResult` exactly.
  * :class:`PostgresStore` — raw **psycopg3 ``AsyncConnection``** (NO SQLAlchemy / sqlite).
    ``psycopg`` is imported LAZILY so ``import veridex.store`` works without it installed.
    Writes are batched via ``executemany`` inside ONE transaction; all SQL is parameterized.

CON-010: this is the async SHELL — the deterministic core never imports it. Schema: six tables
(``runs``, ``run_events``, ``score_rows``, ``competitions``, ``competition_events``,
``execution_records``). Each event table has an index on its parent-id column and a unique
``(parent_id, seq)`` constraint.

Phase-2A adds seven competition/event methods to the protocol; the CHECK-constraint literal
tuples (``_COMPETITION_STATUS_VALUES``, ``_EVENT_TYPE_VALUES``) are module-level so drift-guard
tests can import and compare them against the canonical enums.

Phase-2B Task 5 adds three execution-record methods (``append_execution_record``,
``get_execution_record``, ``list_executions``) with a matching ``_EXECUTION_STATUS_VALUES``
literal tuple and ``execution_records`` table. Execution records are NON-SCORING — they never
touch evidence or score columns; that boundary is enforced by the executor lane (Task 6).
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from veridex.competition.events import CompetitionEvent
from veridex.competition.models import AgentEntry, Competition, CompetitionConfig, CompetitionStatus
from veridex.config import get_settings, require_database_url
from veridex.deploy.attempt import DeploymentAttempt, DuplicateAttemptError
from veridex.deploy.instance import AgentInstance, DeployFailureReason, DeployStatus
from veridex.execution.models import ExecutionRecord
from veridex.runtime.evidence import serialize_payload

if TYPE_CHECKING:  # avoid an import cycle (orchestrator type-hints Store)
    from veridex.runtime.orchestrator import RunResult


# ---------------------------------------------------------------------------
# CHECK-constraint literal tuples — module-level so drift-guard tests can import them.
# Values MUST stay in enum-definition order; the drift-guard tests assert exact tuple equality.
# ---------------------------------------------------------------------------

_COMPETITION_STATUS_VALUES: tuple[str, ...] = ("draft", "open", "running", "finalized")

_EVENT_TYPE_VALUES: tuple[str, ...] = (
    "competition_started",
    "market_tick",
    "window_config",
    "agent_action",
    "law_result",
    "policy_result",
    "execution_submitted",
    "execution_receipt",
    "approval_audit",
    "score_update",
    "proof_anchor",
    "execution_route",
    "payout_status",
    "competition_finalized",
)

# CHECK-constraint literal tuple for execution_records.status. Values MUST stay in
# ExecutionStatus enum-definition order; the drift-guard test asserts exact tuple equality.
_EXECUTION_STATUS_VALUES: tuple[str, ...] = (
    "proposed",
    "law_approved",
    "awaiting_human",
    "policy_approved",
    "submitted",
    "accepted",
    "rejected",
    "filled",
    "partial",
    "cancelled",
    "expired",
    "settled",
    "voided",
    "unresolved",
)

# CHECK-constraint literal tuple for agent_instances.status (Phase-2D deploy — REQ-2D-701A).
# Values MUST stay in DeployStatus enum-definition order; the drift-guard test asserts exact
# equality with veridex.deploy.instance.deploy_status_values().
_INSTANCE_STATUS_VALUES: tuple[str, ...] = ("pending", "running", "sealed", "failed")


# ---------------------------------------------------------------------------
# Realized-fill ledger row (SAF-002b/c — durable, append-only, fee-inclusive)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RealizedFillLedgerRow:
    """One immutable row of the append-only realized-fill/PnL ledger.

    Mirrors the ``competition_events`` APPEND-ONLY shape (INSERT-only, unique monotonic
    ``seq``), NOT the ``execution_records`` upsert shape: rows are never updated or deleted.
    The store persists only primitive columns so it stays decoupled from the dust_execution
    lane; :mod:`veridex.dust_execution.ledger` adapts these rows to/from
    :class:`~veridex.dust_execution.risk.RealizedFillRecord`.

    Attributes:
        seq: Store-assigned monotonic unique sequence (append order across all sessions).
        session_id: Session identity the fill belongs to.
        realized_pnl: Signed realized PnL for the fill in payout dollars (loss is negative).
        fee: Venue fee for the fill in payout dollars (``>= 0``); fee-inclusive loss uses
            ``net = realized_pnl - fee``.
        fill_ts_ms: Venue fill time in integer epoch **milliseconds** (UTC) for the UTC-day boundary.
        source: Provenance marker (real venue-reconciled source).
    """

    seq: int
    session_id: str
    realized_pnl: float
    fee: float
    fill_ts_ms: int
    source: str


# ---------------------------------------------------------------------------
# Pre-submit ledger row (IDM-005 — durable, append-only compound pre-submit record)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreSubmitLedgerRow:
    """One immutable row of the append-only pre-submit ledger (IDM-005).

    The durable backing for the compound :class:`~veridex.dust_execution.contracts.PreSubmitRecord`
    persisted BEFORE a live wire POST, so an order stays identifiable in complete venue truth even
    if the ACK is lost or it never opens. Mirrors the ``realized_fill_ledger`` /
    ``competition_events`` APPEND-ONLY shape (INSERT-only, unique monotonic ``seq``), NOT the
    ``execution_records`` upsert: rows are never updated or deleted.

    Stores ONLY the non-secret fields of the compound record — the one-way
    ``integrity_commitment_hash`` (a sha256 digest, safe to store; the raw ``owner`` cannot be
    recovered from it), the public ``venue_order_key`` (the venue-recognized V2 order hash the
    reconciler joins fill history on), and an optional captured venue id. NO raw owner / L2 cred /
    signature ever enters a row.

    Attributes:
        seq: Store-assigned monotonic unique sequence (append order across all sessions).
        session_id: Session identity the pre-submit record belongs to.
        integrity_commitment_hash: Veridex's PRIVATE one-way digest over the intended POST body
            (a sha256 digest — NOT a venue join key; reconciliation NEVER matches on it).
        venue_order_key: The venue-recognized V2 order hash/id that order/trade/fill responses are
            keyed by — the reconciliation join key (Codex-M2).
        captured_id: A captured venue order id when the POST returned one (Mode A fake receipt id or
            a Mode B venue id); ``None`` otherwise.
    """

    seq: int
    session_id: str
    integrity_commitment_hash: str
    venue_order_key: str
    captured_id: str | None


# ---------------------------------------------------------------------------
# Runtime-event row (I-4 / AC-13/14/15 — durable OPS-channel telemetry, INSERT-only + dedup)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeEventRow:
    """One durable OPS-channel :class:`~veridex.runtime.runtime_events.RuntimeEvent` (I-4).

    The persistent backing for the Agent-Ops drawer feed. INSERT-only and de-duplicated at the DB
    by the client-generated ``event_uuid`` (``UNIQUE(event_uuid)`` + ``ON CONFLICT DO NOTHING``), so
    the durable spool's at-least-once replay lands each event exactly once. The read cursor clients
    page on is the DB-assigned ``BIGSERIAL`` ``id`` — a monotonic commit-order sequence, NOT any
    evidence ``sequence_no``.

    SEC-003 by construction: the row carries NO ``sequence_no`` / ``payload_hash`` / ``evidence``
    column, so an OPS event can never masquerade as a sealed RunEvent / CompetitionEvent — exactly
    the three fields the evidence path requires are structurally absent.

    Attributes:
        id: DB-assigned monotonic commit-order cursor (``BIGSERIAL``); ``0`` before persistence.
        event_uuid: Client-generated idempotency key (the dedup identity; never a venue/evidence key).
        agent_id: The agent this telemetry is about (the owner-scoped read filters on it).
        run_id: Correlation id for the run, when known.
        session_id: Runtime session id, when known.
        event_type: The :class:`~veridex.runtime.runtime_events.RuntimeEventType` value.
        ts: Wall-clock emit time (ms) — NON-deterministic, precisely why it is never sealed.
        channel: Always ``"OPS"``.
        payload: Free-form, secret-free telemetry detail.
    """

    id: int
    event_uuid: str
    agent_id: str
    run_id: str | None
    session_id: str | None
    event_type: str
    ts: int
    channel: str
    payload: dict[str, Any]


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Store(Protocol):
    """Async repository contract for persisting and loading run results and competition data."""

    async def persist_run(self, run_result: RunResult) -> None:
        """Persist a run (events + score rows + source_mode + evidence_hash)."""
        ...

    async def load_run(self, run_id: str) -> RunResult:
        """Load a previously persisted run by id, reconstructing the ``RunResult``."""
        ...

    async def create_competition(self, competition: Competition) -> None:
        """Persist a new competition record.

        Args:
            competition: The competition to persist.

        Raises:
            ValueError: If a competition with ``competition_id`` already exists.
        """
        ...

    async def get_competition(self, competition_id: str) -> Competition:
        """Load a competition by id.

        Args:
            competition_id: The id used at :meth:`create_competition`.

        Returns:
            The reconstructed :class:`~veridex.competition.models.Competition`.

        Raises:
            KeyError: If no competition with ``competition_id`` exists.
        """
        ...

    async def add_agent_entry(self, competition_id: str, entry: AgentEntry) -> None:
        """Append an agent entry to a competition's roster.

        Args:
            competition_id: The owning competition.
            entry: The agent entry to append.

        Raises:
            KeyError: If no competition with ``competition_id`` exists.
        """
        ...

    async def update_competition_status(self, competition_id: str, status: CompetitionStatus) -> None:
        """Overwrite the lifecycle status of a competition.

        Args:
            competition_id: The competition to update.
            status: The new :class:`~veridex.competition.models.CompetitionStatus`.

        Raises:
            KeyError: If no competition with ``competition_id`` exists.
        """
        ...

    async def update_competition_run_id(self, competition_id: str, run_id: str) -> None:
        """Persist the run_id on a competition (called once, when the run is pre-generated).

        Args:
            competition_id: The competition to update.
            run_id: The sealed Phase-1 run identifier to store.

        Raises:
            KeyError: If no competition with ``competition_id`` exists.
        """
        ...

    async def update_competition_config(self, competition_id: str, config: CompetitionConfig) -> None:
        """Overwrite the immutable-config snapshot of a competition.

        Used by the Task-7 control-plane kill-switch to persist a mutated
        ``config.policy_envelope`` (the rest of the config is unchanged).

        Args:
            competition_id: The competition to update.
            config: The new :class:`~veridex.competition.models.CompetitionConfig` snapshot.

        Raises:
            KeyError: If no competition with ``competition_id`` exists.
        """
        ...

    async def append_competition_events(self, competition_id: str, events: list[CompetitionEvent]) -> None:
        """Append competition events to a competition's event log (append-only).

        Args:
            competition_id: The owning competition.
            events: Events to append; must have unique ``seq`` values within the competition.
        """
        ...

    async def list_competition_events(self, competition_id: str, since_seq: int = 0) -> list[CompetitionEvent]:
        """Return events with ``seq > since_seq``, ordered by ``seq`` ascending.

        Mirrors :func:`~veridex.competition.events.replay_from` semantics (strict-greater bound).
        The default ``since_seq=0`` therefore returns all events with ``seq >= 1``, deliberately
        excluding the ``COMPETITION_STARTED`` event at ``seq=0``. Pass ``since_seq=-1`` to include
        ``seq=0`` in the result.

        Args:
            competition_id: The owning competition.
            since_seq: Exclusive lower bound on ``seq`` (default ``0`` → seq ≥ 1; use ``-1`` to
                include the ``seq=0`` ``COMPETITION_STARTED`` event).

        Returns:
            Matching :class:`~veridex.competition.events.CompetitionEvent` objects, sorted by seq.
        """
        ...

    async def list_competitions(self, status: CompetitionStatus | None = None) -> list[Competition]:
        """Return all competitions, optionally filtered by status.

        Args:
            status: When given, only competitions with this status are returned.

        Returns:
            List of :class:`~veridex.competition.models.Competition` objects (any order).
        """
        ...

    async def append_execution_record(self, record: ExecutionRecord) -> None:
        """Upsert an execution record (idempotent on ``execution_id``).

        Re-appending the same ``execution_id`` with an updated record (e.g. as the status
        advances ``proposed → … → filled``) overwrites the stored entry. Execution records
        are NON-SCORING — this method never touches evidence or score columns.

        Args:
            record: The :class:`~veridex.execution.models.ExecutionRecord` to persist or update.
        """
        ...

    async def get_execution_record(self, execution_id: str) -> ExecutionRecord:
        """Load an execution record by id.

        Args:
            execution_id: The id used at :meth:`append_execution_record`.

        Returns:
            The stored :class:`~veridex.execution.models.ExecutionRecord`.

        Raises:
            KeyError: If no record with ``execution_id`` exists.
        """
        ...

    async def list_executions(self, competition_id: str) -> list[ExecutionRecord]:
        """Return all execution records for a competition in deterministic order.

        Args:
            competition_id: The owning competition.

        Returns:
            :class:`~veridex.execution.models.ExecutionRecord` objects sorted by
            ``execution_id`` ascending.
        """
        ...

    async def append_realized_fill(
        self,
        *,
        session_id: str,
        realized_pnl: float,
        fee: float,
        fill_ts_ms: int,
        source: str = "venue_reconciled",
    ) -> int:
        """Append ONE real, venue-reconciled realized fill to the durable, append-only ledger.

        Mirrors the ``competition_events`` APPEND-ONLY contract (INSERT-only; never updated or
        deleted). The store assigns a monotonic unique ``seq`` and returns it. Persists both
        ``realized_pnl`` AND ``fee`` so loss reconstruction stays fee-inclusive
        (``net = realized_pnl - fee``).

        Args:
            session_id: Session identity the fill belongs to.
            realized_pnl: Signed realized PnL for the fill (loss is negative).
            fee: Venue fee (``>= 0``); reduces PnL.
            fill_ts_ms: Venue fill time in integer epoch milliseconds (UTC).
            source: Provenance marker (real venue-reconciled source).

        Returns:
            The store-assigned monotonic unique ``seq`` for the appended row.
        """
        ...

    async def list_realized_fills(self, session_id: str) -> list[RealizedFillLedgerRow]:
        """Return every persisted realized-fill row for ``session_id``, ordered by ``seq`` ascending.

        Args:
            session_id: The session whose ledger to read.

        Returns:
            :class:`RealizedFillLedgerRow` objects for this session, sorted by ``seq`` ascending.
        """
        ...

    async def append_presubmit(
        self,
        *,
        session_id: str,
        integrity_commitment_hash: str,
        venue_order_key: str,
        captured_id: str | None = None,
    ) -> int:
        """Append ONE compound pre-submit record to the durable, append-only ledger (IDM-005).

        The durable backing for a :class:`~veridex.dust_execution.contracts.PreSubmitRecord` written
        BEFORE a live wire POST. Mirrors the ``realized_fill_ledger`` / ``competition_events``
        APPEND-ONLY contract (INSERT-only; never updated or deleted). The store assigns a monotonic
        unique ``seq`` and returns it. Persists ONLY the non-secret fields — the one-way integrity
        digest, the public venue join key, and an optional captured id (NO raw owner / cred / sig).

        Args:
            session_id: Session identity the pre-submit record belongs to.
            integrity_commitment_hash: Veridex's PRIVATE one-way digest (safe to store; not a join key).
            venue_order_key: The venue-recognized V2 order hash (the reconciliation join key).
            captured_id: Captured venue order id if the POST returned one (else ``None``).

        Returns:
            The store-assigned monotonic unique ``seq`` for the appended row.
        """
        ...

    async def list_presubmit(self, session_id: str) -> list[PreSubmitLedgerRow]:
        """Return every persisted pre-submit row for ``session_id``, ordered by ``seq`` ascending.

        A fresh read reconstructs the durable compound records after a restart (IDM-005).

        Args:
            session_id: The session whose pre-submit ledger to read.

        Returns:
            :class:`PreSubmitLedgerRow` objects for this session, sorted by ``seq`` ascending.
        """
        ...

    async def append_runtime_events(self, rows: list[RuntimeEventRow]) -> int:
        """INSERT durable OPS-channel runtime events, de-duplicated by ``event_uuid`` (I-4).

        The durable spool delivers at-least-once and relies on the STORE to dedup: each row is
        inserted ``ON CONFLICT (event_uuid) DO NOTHING`` (never client-side alone), so a WAL replay
        of an already-committed batch is a no-op. INSERT-only — rows are never updated or deleted.

        Args:
            rows: The events to append. Their ``id`` is ignored (DB-assigned ``BIGSERIAL``).

        Returns:
            The number of rows NEWLY inserted (duplicates skipped).
        """
        ...

    async def list_runtime_events(
        self, agent_id: str, *, since: int = 0, limit: int | None = None
    ) -> list[RuntimeEventRow]:
        """Return one agent's durable OPS events with ``id > since``, ascending commit order (I-4).

        Cursor paging: ``since`` is the last ``id`` the client consumed (``0`` returns everything);
        the durable ``BIGSERIAL`` ``id`` guarantees no loss / no duplication across a reconnect.

        Args:
            agent_id: The agent whose OPS telemetry to read.
            since: The last consumed ``id`` (exclusive lower bound); ``0`` returns from the start.
            limit: When set, cap to the FIRST ``limit`` rows after ``since`` (forward paging).

        Returns:
            Matching :class:`RuntimeEventRow` objects, sorted by ``id`` ascending.
        """
        ...

    async def persist_agent_instance(self, instance: AgentInstance) -> None:
        """Persist a deployed :class:`~veridex.deploy.instance.AgentInstance` (source of truth).

        Called by the deploy route AFTER preflight passes and BEFORE the run is launched
        (persist-then-launch): a preflight failure therefore never reaches this method, so no
        row exists for a rejected deploy.

        Args:
            instance: The pinned instance record to persist (keyed by ``instance_id``).
        """
        ...

    async def get_agent_instance(self, instance_id: str) -> AgentInstance:
        """Load a persisted deployed instance by id.

        Args:
            instance_id: The id used at :meth:`persist_agent_instance`.

        Returns:
            The reconstructed :class:`~veridex.deploy.instance.AgentInstance`.

        Raises:
            KeyError: If no instance with ``instance_id`` exists.
        """
        ...

    async def update_agent_instance_status(
        self,
        instance_id: str,
        status: DeployStatus,
        *,
        last_failure_reason: DeployFailureReason | None = None,
        updated_at: str,
    ) -> None:
        """Durably update a deployed instance's lifecycle status (background success/failure).

        The background run task calls this to advance the STORED record to ``running`` then to a
        terminal ``sealed`` / ``failed`` — so the outcome survives beyond process memory. On
        ``failed`` the caller supplies a controlled :class:`~veridex.deploy.instance.DeployFailureReason`
        (never a raw framework trace).

        Args:
            instance_id: The instance to update.
            status: The new :class:`~veridex.deploy.instance.DeployStatus`.
            last_failure_reason: Controlled taxonomy value to persist (only meaningful for
                ``failed``); when ``None`` the stored reason is left unchanged.
            updated_at: ISO-8601 UTC timestamp of this write (caller owns the clock).

        Raises:
            KeyError: If no instance with ``instance_id`` exists.
        """
        ...

    async def list_agent_instances(self) -> list[AgentInstance]:
        """Return every persisted deployed instance (a dumb enumerator — no owner filtering here).

        Owner-scoping is deliberately NOT applied at this layer: the owner-scoped route
        (:mod:`veridex.api.deploy`) applies the fail-closed owner filter so the trust boundary lives
        in one reviewable place. An UNOWNED / legacy row (``operator_id is None``) is therefore
        returned here but excluded by the route from every caller's listing.

        Returns:
            All :class:`~veridex.deploy.instance.AgentInstance` records (any order).
        """
        ...

    async def persist_deployment_attempt(self, attempt: DeploymentAttempt) -> None:
        """INSERT a durable deployment-attempt claim (the deploy-saga idempotency backbone).

        The attempt is persisted BEFORE any deploy side effect, into an append-only ledger table with
        ``UNIQUE(operator_id, idempotency_key)``: a second INSERT for an already-claimed pair is the
        idempotency signal, surfaced as :class:`~veridex.deploy.attempt.DuplicateAttemptError`.

        Args:
            attempt: The write-once claim to persist (keyed by ``(operator_id, idempotency_key)``).

        Raises:
            DuplicateAttemptError: If ``(operator_id, idempotency_key)`` is already claimed.
        """
        ...

    async def get_deployment_attempt_by_key(
        self, operator_id: str, idempotency_key: str
    ) -> DeploymentAttempt | None:
        """Load the deployment-attempt claim for ``(operator_id, idempotency_key)``, if any.

        Args:
            operator_id: The SERVER-DERIVED owner the claim is scoped to.
            idempotency_key: The caller-scoped idempotency key.

        Returns:
            The recorded :class:`~veridex.deploy.attempt.DeploymentAttempt`, or ``None`` if unclaimed.
        """
        ...


# ---------------------------------------------------------------------------
# Private reconstruction helpers
# ---------------------------------------------------------------------------


def _build_run_result(
    *,
    run_id: str,
    source_mode: str,
    agent_ids: list[str],
    run_events: list[dict[str, Any]],
    score_rows: list[dict[str, Any]],
    evidence_hash: str,
    proof_mode_map: dict[str, str],
) -> RunResult:
    """Construct a :class:`RunResult` with a lazy import (breaks the store↔orchestrator cycle)."""
    from veridex.runtime.orchestrator import RunResult

    return RunResult(
        run_id=run_id,
        source_mode=source_mode,
        agent_ids=agent_ids,
        run_events=run_events,
        score_rows=score_rows,
        evidence_hash=evidence_hash,
        proof_mode_map=proof_mode_map,
    )


def _build_competition(
    competition_id: str,
    config_json: str,
    status: str,
    run_id: str | None,
    entries_json: str,
) -> Competition:
    """Reconstruct a :class:`Competition` from raw Postgres column values.

    Args:
        competition_id: Primary key value.
        config_json: JSON-serialized :class:`~veridex.competition.models.CompetitionConfig`.
        status: Raw status string (one of ``_COMPETITION_STATUS_VALUES``).
        run_id: Optional run correlation id (may be ``None``).
        entries_json: JSON-serialized list of :class:`~veridex.competition.models.AgentEntry` dicts.

    Returns:
        The reconstructed :class:`~veridex.competition.models.Competition`.
    """
    return Competition(
        competition_id=competition_id,
        config=CompetitionConfig.model_validate(json.loads(config_json)),
        status=CompetitionStatus(status),
        entries=[AgentEntry.model_validate(e) for e in json.loads(entries_json)],
        run_id=run_id,
    )


# ---------------------------------------------------------------------------
# InMemoryStore
# ---------------------------------------------------------------------------


class InMemoryStore:
    """Dict-backed async store for the offline suite. Deep-copies on write and read."""

    def __init__(self) -> None:
        """Initialise an empty, in-process registry for runs, competitions, and executions."""
        self._runs: dict[str, dict[str, Any]] = {}
        self._competitions: dict[str, Competition] = {}
        self._competition_events: dict[str, list[CompetitionEvent]] = {}
        self._execution_records: dict[str, ExecutionRecord] = {}
        self._agent_instances: dict[str, AgentInstance] = {}
        # Append-only deployment-attempt ledger, keyed by (operator_id, idempotency_key) — the
        # in-code mirror of the Postgres UNIQUE(operator_id, idempotency_key) claim (I-3).
        self._deployment_attempts: dict[tuple[str, str], DeploymentAttempt] = {}
        # Append-only realized-fill ledger + its monotonic unique-seq counter (SAF-002b/c).
        self._realized_fills: list[RealizedFillLedgerRow] = []
        self._realized_fill_seq: int = 0
        # Append-only pre-submit ledger + its monotonic unique-seq counter (IDM-005).
        self._presubmit_rows: list[PreSubmitLedgerRow] = []
        self._presubmit_seq: int = 0
        # Durable OPS-channel runtime events (I-4) + monotonic id + the event_uuid dedup set that
        # mirrors the Postgres UNIQUE(event_uuid) + ON CONFLICT DO NOTHING at-least-once contract.
        self._runtime_events: list[RuntimeEventRow] = []
        self._runtime_event_id: int = 0
        self._runtime_event_uuids: set[str] = set()

    # --- run methods (Phase-1, unchanged) ---

    async def persist_run(self, run_result: RunResult) -> None:
        """Store a deep copy of the run keyed by ``run_id``.

        Args:
            run_result: The run to persist.
        """
        self._runs[run_result.run_id] = {
            "source_mode": run_result.source_mode,
            "agent_ids": list(run_result.agent_ids),
            "run_events": copy.deepcopy(run_result.run_events),
            "score_rows": copy.deepcopy(run_result.score_rows),
            "evidence_hash": run_result.evidence_hash,
            "proof_mode_map": dict(run_result.proof_mode_map),
        }

    async def load_run(self, run_id: str) -> RunResult:
        """Reconstruct a previously persisted run.

        Args:
            run_id: The id passed to :meth:`persist_run`.

        Returns:
            The reconstructed :class:`RunResult`.

        Raises:
            KeyError: If no run with ``run_id`` was persisted.
        """
        if run_id not in self._runs:
            raise KeyError(f"no run persisted with run_id={run_id!r}")
        data = self._runs[run_id]
        return _build_run_result(
            run_id=run_id,
            source_mode=data["source_mode"],
            agent_ids=list(data["agent_ids"]),
            run_events=copy.deepcopy(data["run_events"]),
            score_rows=copy.deepcopy(data["score_rows"]),
            evidence_hash=data["evidence_hash"],
            proof_mode_map=dict(data["proof_mode_map"]),
        )

    # --- competition methods (Phase-2A) ---

    async def create_competition(self, competition: Competition) -> None:
        """Store a deep copy of the competition and initialise its event list.

        Args:
            competition: The competition to persist.

        Raises:
            ValueError: If a competition with ``competition_id`` already exists.
        """
        if competition.competition_id in self._competitions:
            raise ValueError(f"competition already exists: {competition.competition_id!r}")
        self._competitions[competition.competition_id] = competition.model_copy(deep=True)
        self._competition_events.setdefault(competition.competition_id, [])

    async def get_competition(self, competition_id: str) -> Competition:
        """Return a deep copy of the stored competition.

        Args:
            competition_id: The id used at :meth:`create_competition`.

        Returns:
            A fresh deep copy of the stored :class:`~veridex.competition.models.Competition`.

        Raises:
            KeyError: If no competition with ``competition_id`` exists.
        """
        if competition_id not in self._competitions:
            raise KeyError(f"no competition with competition_id={competition_id!r}")
        return self._competitions[competition_id].model_copy(deep=True)

    async def add_agent_entry(self, competition_id: str, entry: AgentEntry) -> None:
        """Append a deep copy of ``entry`` to the stored competition's roster.

        Args:
            competition_id: The owning competition.
            entry: The agent entry to append.

        Raises:
            KeyError: If no competition with ``competition_id`` exists.
        """
        if competition_id not in self._competitions:
            raise KeyError(f"no competition with competition_id={competition_id!r}")
        self._competitions[competition_id].entries.append(entry.model_copy(deep=True))

    async def update_competition_status(self, competition_id: str, status: CompetitionStatus) -> None:
        """Overwrite the stored competition's status.

        Args:
            competition_id: The competition to update.
            status: The new status.

        Raises:
            KeyError: If no competition with ``competition_id`` exists.
        """
        if competition_id not in self._competitions:
            raise KeyError(f"no competition with competition_id={competition_id!r}")
        self._competitions[competition_id].status = status

    async def update_competition_run_id(self, competition_id: str, run_id: str) -> None:
        """Store the run_id on the competition (deep-copy semantics preserved via direct field set).

        Args:
            competition_id: The competition to update.
            run_id: The sealed Phase-1 run identifier to store.

        Raises:
            KeyError: If no competition with ``competition_id`` exists.
        """
        if competition_id not in self._competitions:
            raise KeyError(f"no competition with competition_id={competition_id!r}")
        self._competitions[competition_id].run_id = run_id

    async def update_competition_config(self, competition_id: str, config: CompetitionConfig) -> None:
        """Overwrite the stored competition's config snapshot (deep-copied).

        Args:
            competition_id: The competition to update.
            config: The new config snapshot.

        Raises:
            KeyError: If no competition with ``competition_id`` exists.
        """
        if competition_id not in self._competitions:
            raise KeyError(f"no competition with competition_id={competition_id!r}")
        self._competitions[competition_id].config = config.model_copy(deep=True)

    async def append_competition_events(self, competition_id: str, events: list[CompetitionEvent]) -> None:
        """Append deep copies of ``events`` to the competition's event list.

        Mirrors the Postgres ``UNIQUE (competition_id, seq)`` constraint: raises
        :class:`ValueError` if any incoming event has a ``seq`` already present for
        this ``competition_id``, or if the batch itself contains a duplicate ``seq``.

        Args:
            competition_id: The owning competition.
            events: Events to append; each ``seq`` must be unique within the competition.

        Raises:
            ValueError: If any event's ``seq`` collides with an already-stored seq or with
                another event in the same batch (REQ-2B-31).
        """
        bucket = self._competition_events.setdefault(competition_id, [])
        existing_seqs: set[int] = {e.seq for e in bucket}
        # Pre-validate the entire batch before mutating (mirrors Postgres transaction atomicity).
        seen: set[int] = set()
        for event in events:
            if event.seq in existing_seqs or event.seq in seen:
                raise ValueError(f"duplicate (competition_id, seq): ({competition_id!r}, {event.seq!r})")
            seen.add(event.seq)
        # All seqs are unique — safe to extend atomically.
        bucket.extend(event.model_copy(deep=True) for event in events)

    async def list_competition_events(self, competition_id: str, since_seq: int = 0) -> list[CompetitionEvent]:
        """Return deep copies of events with ``seq > since_seq``, sorted by ``seq``.

        The bound is exclusive (strict-greater). Default ``since_seq=0`` excludes the
        ``seq=0`` ``COMPETITION_STARTED`` event; pass ``since_seq=-1`` to include it.

        Args:
            competition_id: The owning competition.
            since_seq: Exclusive lower bound (default ``0`` → seq ≥ 1; use ``-1`` to include seq=0).

        Returns:
            Matching events, sorted ascending by ``seq``.
        """
        stored = self._competition_events.get(competition_id, [])
        return [e.model_copy(deep=True) for e in sorted(stored, key=lambda e: e.seq) if e.seq > since_seq]

    async def list_competitions(self, status: CompetitionStatus | None = None) -> list[Competition]:
        """Return deep copies of all competitions, optionally filtered by status.

        Args:
            status: When given, only competitions with this status are included.

        Returns:
            Deep copies of matching competitions (any order).
        """
        comps = self._competitions.values()
        if status is not None:
            return [c.model_copy(deep=True) for c in comps if c.status is status]
        return [c.model_copy(deep=True) for c in comps]

    # --- execution-record methods (Phase-2B Task 5) ---

    async def append_execution_record(self, record: ExecutionRecord) -> None:
        """Upsert a deep copy of ``record`` keyed by ``execution_id`` (idempotent).

        Re-appending the same ``execution_id`` overwrites the stored entry so callers
        can advance the status (``proposed → … → filled``) without inserting duplicates.

        Args:
            record: The execution record to persist or update.
        """
        self._execution_records[record.execution_id] = record.model_copy(deep=True)

    async def get_execution_record(self, execution_id: str) -> ExecutionRecord:
        """Return a deep copy of the stored execution record.

        Args:
            execution_id: The id used at :meth:`append_execution_record`.

        Returns:
            A fresh deep copy of the stored :class:`~veridex.execution.models.ExecutionRecord`.

        Raises:
            KeyError: If no record with ``execution_id`` exists.
        """
        if execution_id not in self._execution_records:
            raise KeyError(f"no execution record with execution_id={execution_id!r}")
        return self._execution_records[execution_id].model_copy(deep=True)

    async def list_executions(self, competition_id: str) -> list[ExecutionRecord]:
        """Return deep copies of all execution records for a competition, sorted by ``execution_id``.

        Args:
            competition_id: The owning competition.

        Returns:
            Deep copies of matching :class:`~veridex.execution.models.ExecutionRecord` objects,
            sorted ascending by ``execution_id``.
        """
        return [
            rec.model_copy(deep=True)
            for rec in sorted(
                (r for r in self._execution_records.values() if r.competition_id == competition_id),
                key=lambda r: r.execution_id,
            )
        ]

    # --- realized-fill ledger methods (SAF-002b/c — append-only, fee-inclusive) ---

    async def append_realized_fill(
        self,
        *,
        session_id: str,
        realized_pnl: float,
        fee: float,
        fill_ts_ms: int,
        source: str = "venue_reconciled",
    ) -> int:
        """Append one realized-fill row (append-only) and return its monotonic unique ``seq``.

        Mirrors the ``competition_events`` append-only shape: rows are only ever added, never
        updated or deleted. ``seq`` is a process-global monotonic counter (unique across sessions).

        Args:
            session_id: Session identity the fill belongs to.
            realized_pnl: Signed realized PnL (loss is negative).
            fee: Venue fee (``>= 0``).
            fill_ts_ms: Venue fill time in integer epoch milliseconds (UTC).
            source: Provenance marker.

        Returns:
            The assigned monotonic unique ``seq``.
        """
        self._realized_fill_seq += 1
        row = RealizedFillLedgerRow(
            seq=self._realized_fill_seq,
            session_id=session_id,
            realized_pnl=float(realized_pnl),
            fee=float(fee),
            fill_ts_ms=int(fill_ts_ms),
            source=source,
        )
        self._realized_fills.append(row)
        return row.seq

    async def list_realized_fills(self, session_id: str) -> list[RealizedFillLedgerRow]:
        """Return this session's realized-fill rows, sorted by ``seq`` ascending.

        Rows are frozen dataclasses, so returning references is safe (immutable).

        Args:
            session_id: The session whose ledger to read.

        Returns:
            Matching :class:`RealizedFillLedgerRow` objects, sorted by ``seq`` ascending.
        """
        return [row for row in sorted(self._realized_fills, key=lambda r: r.seq) if row.session_id == session_id]

    # --- pre-submit ledger methods (IDM-005 — append-only, non-secret compound record) ---

    async def append_presubmit(
        self,
        *,
        session_id: str,
        integrity_commitment_hash: str,
        venue_order_key: str,
        captured_id: str | None = None,
    ) -> int:
        """Append one pre-submit row (append-only) and return its monotonic unique ``seq``.

        Mirrors the ``realized_fill_ledger`` append-only shape: rows are only ever added, never
        updated or deleted. ``seq`` is a process-global monotonic counter (unique across sessions).

        Args:
            session_id: Session identity the pre-submit record belongs to.
            integrity_commitment_hash: Veridex's private one-way digest (safe to store; not a join key).
            venue_order_key: The venue-recognized V2 order hash (the reconciliation join key).
            captured_id: Captured venue order id if the POST returned one (else ``None``).

        Returns:
            The assigned monotonic unique ``seq``.
        """
        self._presubmit_seq += 1
        row = PreSubmitLedgerRow(
            seq=self._presubmit_seq,
            session_id=session_id,
            integrity_commitment_hash=integrity_commitment_hash,
            venue_order_key=venue_order_key,
            captured_id=captured_id,
        )
        self._presubmit_rows.append(row)
        return row.seq

    async def list_presubmit(self, session_id: str) -> list[PreSubmitLedgerRow]:
        """Return this session's pre-submit rows, sorted by ``seq`` ascending.

        Rows are frozen dataclasses, so returning references is safe (immutable).

        Args:
            session_id: The session whose pre-submit ledger to read.

        Returns:
            Matching :class:`PreSubmitLedgerRow` objects, sorted by ``seq`` ascending.
        """
        return [row for row in sorted(self._presubmit_rows, key=lambda r: r.seq) if row.session_id == session_id]

    # --- runtime-event methods (I-4 — durable OPS channel, INSERT-only + event_uuid dedup) ---

    async def append_runtime_events(self, rows: list[RuntimeEventRow]) -> int:
        """Append durable OPS events, skipping any whose ``event_uuid`` was already stored.

        Mirrors the Postgres ``ON CONFLICT (event_uuid) DO NOTHING`` dedup: a WAL replay of an
        already-committed batch inserts nothing. A monotonic ``id`` is assigned to each NEW row.

        Args:
            rows: The events to append (their input ``id`` is ignored; a fresh one is assigned).

        Returns:
            The number of rows newly inserted.
        """
        inserted = 0
        for row in rows:
            if row.event_uuid in self._runtime_event_uuids:
                continue  # ON CONFLICT DO NOTHING — at-least-once replay is idempotent
            self._runtime_event_uuids.add(row.event_uuid)
            self._runtime_event_id += 1
            self._runtime_events.append(
                RuntimeEventRow(
                    id=self._runtime_event_id,
                    event_uuid=row.event_uuid,
                    agent_id=row.agent_id,
                    run_id=row.run_id,
                    session_id=row.session_id,
                    event_type=row.event_type,
                    ts=row.ts,
                    channel=row.channel,
                    payload=copy.deepcopy(row.payload),
                )
            )
            inserted += 1
        return inserted

    async def list_runtime_events(
        self, agent_id: str, *, since: int = 0, limit: int | None = None
    ) -> list[RuntimeEventRow]:
        """Return this agent's durable OPS events with ``id > since``, ascending, capped at ``limit``.

        Args:
            agent_id: The agent whose OPS telemetry to read.
            since: The last consumed ``id`` (exclusive); ``0`` returns from the start.
            limit: When set, the FIRST ``limit`` rows after ``since`` (forward cursor paging).

        Returns:
            Matching :class:`RuntimeEventRow` objects, sorted by ``id`` ascending.
        """
        rows = [row for row in self._runtime_events if row.agent_id == agent_id and row.id > since]
        rows.sort(key=lambda r: r.id)
        if limit is not None:
            rows = rows[:limit]
        return rows

    # --- agent-instance methods (Phase-2D deploy — REQ-2D-701A) ---

    async def persist_agent_instance(self, instance: AgentInstance) -> None:
        """Store a deep copy of the deployed instance keyed by ``instance_id``.

        Args:
            instance: The pinned instance record to persist.
        """
        self._agent_instances[instance.instance_id] = instance.model_copy(deep=True)

    async def get_agent_instance(self, instance_id: str) -> AgentInstance:
        """Return a deep copy of the stored deployed instance.

        Args:
            instance_id: The id used at :meth:`persist_agent_instance`.

        Returns:
            A fresh deep copy of the stored :class:`~veridex.deploy.instance.AgentInstance`.

        Raises:
            KeyError: If no instance with ``instance_id`` exists.
        """
        if instance_id not in self._agent_instances:
            raise KeyError(f"no agent instance with instance_id={instance_id!r}")
        return self._agent_instances[instance_id].model_copy(deep=True)

    async def update_agent_instance_status(
        self,
        instance_id: str,
        status: DeployStatus,
        *,
        last_failure_reason: DeployFailureReason | None = None,
        updated_at: str,
    ) -> None:
        """Update the stored instance's status (and optional failure reason) in place.

        Args:
            instance_id: The instance to update.
            status: The new lifecycle status.
            last_failure_reason: Controlled taxonomy value to persist; ``None`` leaves it unchanged.
            updated_at: ISO-8601 UTC timestamp of this write.

        Raises:
            KeyError: If no instance with ``instance_id`` exists.
        """
        if instance_id not in self._agent_instances:
            raise KeyError(f"no agent instance with instance_id={instance_id!r}")
        stored = self._agent_instances[instance_id]
        stored.status = status
        if last_failure_reason is not None:
            stored.last_failure_reason = last_failure_reason
        stored.updated_at = updated_at

    async def list_agent_instances(self) -> list[AgentInstance]:
        """Return deep copies of every stored deployed instance (owner filtering is the route's job).

        Returns:
            Deep copies of all :class:`~veridex.deploy.instance.AgentInstance` records (any order).
        """
        return [inst.model_copy(deep=True) for inst in self._agent_instances.values()]

    # --- deployment-attempt ledger methods (I-3 — write-once idempotency claim) ---

    async def persist_deployment_attempt(self, attempt: DeploymentAttempt) -> None:
        """Store a deep copy of the attempt, enforcing the ``(operator_id, idempotency_key)`` claim.

        Mirrors the Postgres ``UNIQUE(operator_id, idempotency_key)`` in code: a second persist for an
        already-claimed pair raises :class:`~veridex.deploy.attempt.DuplicateAttemptError`.

        Args:
            attempt: The write-once claim to persist.

        Raises:
            DuplicateAttemptError: If ``(operator_id, idempotency_key)`` is already claimed.
        """
        key = (attempt.operator_id, attempt.idempotency_key)
        if key in self._deployment_attempts:
            raise DuplicateAttemptError(f"deployment attempt already claimed: {key!r}")
        self._deployment_attempts[key] = attempt.model_copy(deep=True)

    async def get_deployment_attempt_by_key(
        self, operator_id: str, idempotency_key: str
    ) -> DeploymentAttempt | None:
        """Return a deep copy of the recorded attempt for ``(operator_id, idempotency_key)``, or None.

        Args:
            operator_id: The owner the claim is scoped to.
            idempotency_key: The caller-scoped idempotency key.

        Returns:
            A fresh deep copy of the recorded :class:`~veridex.deploy.attempt.DeploymentAttempt`, or
            ``None`` if the pair is unclaimed.
        """
        recorded = self._deployment_attempts.get((operator_id, idempotency_key))
        return recorded.model_copy(deep=True) if recorded is not None else None


# ---------------------------------------------------------------------------
# PostgresStore column helpers
# ---------------------------------------------------------------------------

_RUN_EVENT_COLUMNS = (
    "sequence_no",
    "event_type",
    "state_snapshot_json",
    "action_payload_json",
    "validation_payload_json",
    "result_payload_json",
)

# Column order for the ``competitions`` table SELECT list. Must match the positional
# signature of ``_build_competition`` exactly — a reorder here is caught at call-site.
_COMPETITION_COLUMNS: tuple[str, ...] = (
    "competition_id",
    "config_json",
    "status",
    "run_id",
    "entries_json",
)


# ---------------------------------------------------------------------------
# PostgresStore
# ---------------------------------------------------------------------------


class PostgresStore:
    """Raw psycopg3 ``AsyncConnection`` store. ``psycopg`` is imported lazily.

    DSN resolution: the explicit ``dsn`` argument, else ``require_database_url(get_settings())``.

    Connection lifecycle: when a ``pool`` (``psycopg_pool.AsyncConnectionPool``) is supplied the
    store ACQUIRES a connection from it per method and RETURNS it to the pool afterwards (the
    long-lived-service path wired by ``veridex.api.server``). Without a pool it falls back to the
    Phase-1 behaviour: open a FRESH ``AsyncConnection`` per method and close it (used by the gated
    Postgres tests, which pass a bare ``dsn``). ``psycopg``/``psycopg_pool`` are never imported at
    module level — the pool is created by the caller and passed in.

    Setup asymmetry: ``init_db(conn)`` is a one-time DDL step that takes a CALLER-supplied
    connection, whereas the DML methods self-connect; ``persist_run`` assumes ``init_db`` has
    already created the schema.
    """

    def __init__(self, *, dsn: str | None = None, pool: Any = None) -> None:
        """Configure the store.

        Args:
            dsn: Optional Postgres DSN; resolved from config at use-time when omitted.
            pool: Optional ``psycopg_pool.AsyncConnectionPool`` (duck-typed: ``getconn`` /
                ``putconn``). When present, every method acquires from / returns to it instead of
                opening a fresh connection per call. Its lifecycle (open / init_db / close) is
                owned by the caller (``veridex.api.server``), not this store.
        """
        self._dsn = dsn
        self._pool = pool

    def _resolve_dsn(self) -> str:
        """Return the configured DSN or fall back to the config-derived ``DATABASE_URL``."""
        if self._dsn is not None:
            return self._dsn
        return require_database_url(get_settings())

    async def _connect(self) -> Any:
        """Acquire a connection: from the pool when configured, else open a fresh one (lazy psycopg)."""
        if self._pool is not None:
            return await self._pool.getconn()
        import psycopg

        return await psycopg.AsyncConnection.connect(self._resolve_dsn())

    async def _release(self, conn: Any) -> None:
        """Release a connection back to the pool when pooled, else close it (mirror of ``_connect``)."""
        if self._pool is not None:
            await self._pool.putconn(conn)
        else:
            await conn.close()

    async def init_db(self, conn: Any) -> None:
        """Create all five tables + indices + unique constraints (idempotent).

        Tables: ``runs``, ``run_events``, ``score_rows``, ``competitions``,
        ``competition_events``. CHECK constraint values for status / event_type come from the
        module-level literal tuples (``_COMPETITION_STATUS_VALUES``,
        ``_EVENT_TYPE_VALUES``), not from the enums, to avoid coupling / circular concerns.

        Args:
            conn: An open psycopg3 ``AsyncConnection``.
        """
        # Build IN-lists from trusted module-level literals (NOT user input — safe to inline).
        status_in = ", ".join(f"'{v}'" for v in _COMPETITION_STATUS_VALUES)
        event_type_in = ", ".join(f"'{v}'" for v in _EVENT_TYPE_VALUES)

        async with conn.cursor() as cur:
            # --- Phase-1 tables ---
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    source_mode TEXT NOT NULL,
                    evidence_hash TEXT NOT NULL,
                    agent_ids TEXT NOT NULL,
                    proof_mode_map TEXT NOT NULL
                )
                """
            )
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS run_events (
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    sequence_no INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    state_snapshot_json TEXT,
                    action_payload_json TEXT,
                    validation_payload_json TEXT,
                    result_payload_json TEXT,
                    CONSTRAINT uq_run_events_run_seq UNIQUE (run_id, sequence_no)
                )
                """
            )
            await cur.execute("CREATE INDEX IF NOT EXISTS idx_run_events_run_id ON run_events(run_id)")
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS score_rows (
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    row_index INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    CONSTRAINT uq_score_rows_run_index UNIQUE (run_id, row_index)
                )
                """
            )

            # --- Phase-2A tables ---
            await cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS competitions (
                    competition_id TEXT PRIMARY KEY,
                    config_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ({status_in})),
                    run_id TEXT,
                    entries_json TEXT NOT NULL DEFAULT '[]'
                )
                """
            )
            await cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS competition_events (
                    competition_id TEXT NOT NULL REFERENCES competitions(competition_id),
                    seq INTEGER NOT NULL,
                    event_type TEXT NOT NULL CHECK (event_type IN ({event_type_in})),
                    event_json TEXT NOT NULL,
                    CONSTRAINT uq_competition_events_comp_seq UNIQUE (competition_id, seq)
                )
                """
            )
            await cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_competition_events_comp_id ON competition_events(competition_id)"
            )

            # --- Phase-2B Task 5 table ---
            exec_status_in = ", ".join(f"'{v}'" for v in _EXECUTION_STATUS_VALUES)
            await cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS execution_records (
                    execution_id TEXT PRIMARY KEY,
                    competition_id TEXT NOT NULL REFERENCES competitions(competition_id),
                    run_id TEXT,
                    agent_id TEXT,
                    status TEXT NOT NULL CHECK (status IN ({exec_status_in})),
                    record_json TEXT NOT NULL
                )
                """
            )
            await cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_execution_records_comp_id ON execution_records(competition_id)"
            )

            # --- Phase-2D deploy table (REQ-2D-701A) ---
            instance_status_in = ", ".join(f"'{v}'" for v in _INSTANCE_STATUS_VALUES)
            await cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS agent_instances (
                    instance_id TEXT PRIMARY KEY,
                    run_id TEXT,
                    agent_id TEXT,
                    template_id TEXT,
                    status TEXT NOT NULL CHECK (status IN ({instance_status_in})),
                    record_json TEXT NOT NULL
                )
                """
            )
            await cur.execute("CREATE INDEX IF NOT EXISTS idx_agent_instances_run_id ON agent_instances(run_id)")

            # --- realized-fill ledger (SAF-002b/c — durable, APPEND-ONLY, fee-inclusive) ---
            # BIGSERIAL PK gives a monotonic unique seq; INSERT-only (never UPDATE/DELETE),
            # mirroring the competition_events append-only pattern (NOT the execution_records upsert).
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS realized_fill_ledger (
                    seq BIGSERIAL PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    realized_pnl DOUBLE PRECISION NOT NULL,
                    fee DOUBLE PRECISION NOT NULL,
                    fill_ts_ms BIGINT NOT NULL,
                    source TEXT NOT NULL
                )
                """
            )
            await cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_realized_fill_ledger_session ON realized_fill_ledger(session_id)"
            )

            # --- pre-submit ledger (IDM-005 — durable, APPEND-ONLY compound pre-submit record) ---
            # BIGSERIAL PK gives a monotonic unique seq; INSERT-only (never UPDATE/DELETE), mirroring
            # the realized_fill_ledger / competition_events append-only pattern. Columns hold ONLY the
            # non-secret compound-record fields (the one-way integrity digest + the public venue join
            # key + an optional captured id) — NO raw owner / L2 cred / signature.
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS presubmit_ledger (
                    seq BIGSERIAL PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    integrity_commitment_hash TEXT NOT NULL,
                    venue_order_key TEXT NOT NULL,
                    captured_id TEXT
                )
                """
            )
            await cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_presubmit_ledger_session ON presubmit_ledger(session_id)"
            )

            # --- durable runtime-event spool (I-4 / AC-13/14/15 — OPS channel, INSERT-only + dedup) ---
            # BIGSERIAL id is the client-facing read cursor (monotonic commit order); the durable WAL
            # spool delivers at-least-once and dedups HERE via UNIQUE(event_uuid) + ON CONFLICT DO
            # NOTHING (never client-side alone). SEC-003 by construction: NO sequence_no / payload_hash
            # column, so an OPS event can never masquerade as a sealed RunEvent / CompetitionEvent.
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_events (
                    id BIGSERIAL PRIMARY KEY,
                    event_uuid TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    run_id TEXT,
                    session_id TEXT,
                    event_type TEXT NOT NULL,
                    ts BIGINT NOT NULL,
                    channel TEXT NOT NULL DEFAULT 'OPS',
                    payload_json TEXT NOT NULL,
                    CONSTRAINT uq_runtime_events_uuid UNIQUE (event_uuid)
                )
                """
            )
            await cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_runtime_events_agent_id ON runtime_events(agent_id, id)"
            )

            # --- deployment-attempt ledger (I-3 — durable idempotency claim, INSERT-only) ---
            # One row per (operator_id, idempotency_key) claim, written ONCE before any deploy side
            # effect; the UNIQUE constraint is the idempotency guarantee (a duplicate INSERT is the
            # signal a retry / concurrent duplicate must reconcile on, NOT mint a second instance).
            # Business columns (NOT a record_json blob): the recovery state is these explicit fields.
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS deployment_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    operator_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    config_fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    instance_id TEXT,
                    external_id TEXT,
                    CONSTRAINT uq_deployment_attempts_operator_key UNIQUE (operator_id, idempotency_key)
                )
                """
            )
        await conn.commit()

    # --- run methods (Phase-1, unchanged) ---

    async def persist_run(self, run_result: RunResult) -> None:
        """Persist a run in ONE transaction with batched ``executemany`` writes.

        Args:
            run_result: The run to persist.
        """
        conn = await self._connect()
        try:
            async with conn.transaction(), conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO runs (run_id, source_mode, evidence_hash, agent_ids, proof_mode_map) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (
                        run_result.run_id,
                        run_result.source_mode,
                        run_result.evidence_hash,
                        serialize_payload(run_result.agent_ids),
                        serialize_payload(run_result.proof_mode_map),
                    ),
                )
                await cur.executemany(
                    "INSERT INTO run_events "
                    "(run_id, sequence_no, event_type, state_snapshot_json, action_payload_json, "
                    "validation_payload_json, result_payload_json) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    [
                        (run_result.run_id, *(event.get(col) for col in _RUN_EVENT_COLUMNS))
                        for event in run_result.run_events
                    ],
                )
                await cur.executemany(
                    "INSERT INTO score_rows (run_id, row_index, payload) VALUES (%s, %s, %s)",
                    [
                        (run_result.run_id, index, serialize_payload(row))
                        for index, row in enumerate(run_result.score_rows)
                    ],
                )
        finally:
            await self._release(conn)

    async def load_run(self, run_id: str) -> RunResult:
        """Reconstruct a run from the three Phase-1 tables.

        Args:
            run_id: The id used at :meth:`persist_run`.

        Returns:
            The reconstructed :class:`RunResult`.

        Raises:
            KeyError: If no run with ``run_id`` exists.
        """
        conn = await self._connect()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT source_mode, evidence_hash, agent_ids, proof_mode_map FROM runs WHERE run_id = %s",
                    (run_id,),
                )
                run_row = await cur.fetchone()
                if run_row is None:
                    raise KeyError(f"no run persisted with run_id={run_id!r}")
                source_mode, evidence_hash, agent_ids_json, proof_mode_map_json = run_row

                await cur.execute(
                    "SELECT sequence_no, event_type, state_snapshot_json, action_payload_json, "
                    "validation_payload_json, result_payload_json "
                    "FROM run_events WHERE run_id = %s ORDER BY sequence_no",
                    (run_id,),
                )
                event_rows = await cur.fetchall()

                await cur.execute(
                    "SELECT payload FROM score_rows WHERE run_id = %s ORDER BY row_index",
                    (run_id,),
                )
                score_rows_raw = await cur.fetchall()
        finally:
            await self._release(conn)

        run_events = [dict(zip(_RUN_EVENT_COLUMNS, row, strict=True)) for row in event_rows]
        score_rows = [json.loads(row[0]) for row in score_rows_raw]

        return _build_run_result(
            run_id=run_id,
            source_mode=source_mode,
            agent_ids=json.loads(agent_ids_json),
            run_events=run_events,
            score_rows=score_rows,
            evidence_hash=evidence_hash,
            proof_mode_map=json.loads(proof_mode_map_json),
        )

    # --- competition methods (Phase-2A) ---

    async def create_competition(self, competition: Competition) -> None:
        """Insert a competition row.

        Args:
            competition: The competition to persist.

        Raises:
            ValueError: If a competition with ``competition_id`` already exists (maps the
                ``UniqueViolation`` SQL error to the same contract as :class:`InMemoryStore`).
        """
        conn = await self._connect()
        try:
            async with conn.transaction(), conn.cursor() as cur:
                try:
                    await cur.execute(
                        "INSERT INTO competitions (competition_id, config_json, status, run_id, entries_json) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (
                            competition.competition_id,
                            serialize_payload(competition.config.model_dump(mode="json")),
                            competition.status.value,
                            competition.run_id,
                            serialize_payload([e.model_dump(mode="json") for e in competition.entries]),
                        ),
                    )
                except Exception as exc:
                    # psycopg is already in sys.modules (imported inside _connect).
                    import psycopg.errors  # noqa: PLC0415 (lazy, not at module level)

                    if isinstance(exc, psycopg.errors.UniqueViolation):
                        raise ValueError(f"competition already exists: {competition.competition_id!r}") from exc
                    raise
        finally:
            await self._release(conn)

    async def get_competition(self, competition_id: str) -> Competition:
        """Fetch and reconstruct a competition by id.

        Args:
            competition_id: The id used at :meth:`create_competition`.

        Returns:
            The reconstructed :class:`~veridex.competition.models.Competition`.

        Raises:
            KeyError: If no competition with ``competition_id`` exists.
        """
        _cols = ", ".join(_COMPETITION_COLUMNS)
        conn = await self._connect()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT {_cols} FROM competitions WHERE competition_id = %s",
                    (competition_id,),
                )
                row = await cur.fetchone()
        finally:
            await self._release(conn)

        if row is None:
            raise KeyError(f"no competition with competition_id={competition_id!r}")
        return _build_competition(*row)

    async def add_agent_entry(self, competition_id: str, entry: AgentEntry) -> None:
        """Read-modify-write the roster column, appending ``entry`` in one transaction.

        Args:
            competition_id: The owning competition.
            entry: The agent entry to append.

        Raises:
            KeyError: If no competition with ``competition_id`` exists.
        """
        conn = await self._connect()
        try:
            async with conn.transaction(), conn.cursor() as cur:
                await cur.execute(
                    "SELECT entries_json FROM competitions WHERE competition_id = %s FOR UPDATE",
                    (competition_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    raise KeyError(f"no competition with competition_id={competition_id!r}")
                entries: list[dict[str, Any]] = json.loads(row[0])
                entries.append(entry.model_dump(mode="json"))
                await cur.execute(
                    "UPDATE competitions SET entries_json = %s WHERE competition_id = %s",
                    (serialize_payload(entries), competition_id),
                )
        finally:
            await self._release(conn)

    async def update_competition_status(self, competition_id: str, status: CompetitionStatus) -> None:
        """Update the status column for a competition.

        Args:
            competition_id: The competition to update.
            status: The new status value.

        Raises:
            KeyError: If no competition with ``competition_id`` exists (detected via ``rowcount``).
        """
        conn = await self._connect()
        try:
            async with conn.transaction(), conn.cursor() as cur:
                await cur.execute(
                    "UPDATE competitions SET status = %s WHERE competition_id = %s",
                    (status.value, competition_id),
                )
                if cur.rowcount == 0:
                    raise KeyError(f"no competition persisted with competition_id={competition_id!r}")
        finally:
            await self._release(conn)

    async def update_competition_run_id(self, competition_id: str, run_id: str) -> None:
        """Set the run_id column for a competition.

        Args:
            competition_id: The competition to update.
            run_id: The sealed Phase-1 run identifier to store.

        Raises:
            KeyError: If no competition with ``competition_id`` exists (detected via ``rowcount``).
        """
        conn = await self._connect()
        try:
            async with conn.transaction(), conn.cursor() as cur:
                await cur.execute(
                    "UPDATE competitions SET run_id = %s WHERE competition_id = %s",
                    (run_id, competition_id),
                )
                if cur.rowcount == 0:
                    raise KeyError(f"no competition persisted with competition_id={competition_id!r}")
        finally:
            await self._release(conn)

    async def update_competition_config(self, competition_id: str, config: CompetitionConfig) -> None:
        """Overwrite the config_json column for a competition.

        Args:
            competition_id: The competition to update.
            config: The new config snapshot (canonically serialized).

        Raises:
            KeyError: If no competition with ``competition_id`` exists (detected via ``rowcount``).
        """
        conn = await self._connect()
        try:
            async with conn.transaction(), conn.cursor() as cur:
                await cur.execute(
                    "UPDATE competitions SET config_json = %s WHERE competition_id = %s",
                    (serialize_payload(config.model_dump(mode="json")), competition_id),
                )
                if cur.rowcount == 0:
                    raise KeyError(f"no competition persisted with competition_id={competition_id!r}")
        finally:
            await self._release(conn)

    async def append_competition_events(self, competition_id: str, events: list[CompetitionEvent]) -> None:
        """Batch-insert competition events in one transaction (append-only).

        The ``UNIQUE (competition_id, seq)`` constraint acts as a last-resort idempotency guard.
        Caller is responsible for providing events in correct sequential order.

        Args:
            competition_id: The owning competition.
            events: Events to append; each ``seq`` must be unique within the competition.
        """
        conn = await self._connect()
        try:
            async with conn.transaction(), conn.cursor() as cur:
                await cur.executemany(
                    "INSERT INTO competition_events (competition_id, seq, event_type, event_json) "
                    "VALUES (%s, %s, %s, %s)",
                    [
                        (
                            competition_id,
                            event.seq,
                            event.event_type.value,
                            serialize_payload(event.model_dump(mode="json")),
                        )
                        for event in events
                    ],
                )
        finally:
            await self._release(conn)

    async def list_competition_events(self, competition_id: str, since_seq: int = 0) -> list[CompetitionEvent]:
        """Return events with ``seq > since_seq``, ordered by ``seq`` ascending.

        The bound is exclusive. Default ``since_seq=0`` excludes ``seq=0``; pass ``since_seq=-1``
        to include the ``COMPETITION_STARTED`` event at ``seq=0``.

        Args:
            competition_id: The owning competition.
            since_seq: Exclusive lower bound (default ``0`` → seq ≥ 1; use ``-1`` to include seq=0).

        Returns:
            Reconstructed :class:`~veridex.competition.events.CompetitionEvent` objects.
        """
        conn = await self._connect()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT event_json FROM competition_events WHERE competition_id = %s AND seq > %s ORDER BY seq",
                    (competition_id, since_seq),
                )
                rows = await cur.fetchall()
        finally:
            await self._release(conn)

        return [CompetitionEvent.model_validate(json.loads(row[0])) for row in rows]

    async def list_competitions(self, status: CompetitionStatus | None = None) -> list[Competition]:
        """Return all competitions, optionally filtered by status.

        Args:
            status: When given, only competitions with this status are returned.

        Returns:
            Reconstructed :class:`~veridex.competition.models.Competition` objects (any order).
        """
        _cols = ", ".join(_COMPETITION_COLUMNS)
        conn = await self._connect()
        try:
            async with conn.cursor() as cur:
                if status is None:
                    await cur.execute(f"SELECT {_cols} FROM competitions")
                else:
                    await cur.execute(
                        f"SELECT {_cols} FROM competitions WHERE status = %s",
                        (status.value,),
                    )
                rows = await cur.fetchall()
        finally:
            await self._release(conn)

        return [_build_competition(*row) for row in rows]

    # --- execution-record methods (Phase-2B Task 5) ---

    async def append_execution_record(self, record: ExecutionRecord) -> None:
        """Upsert an execution record in ``execution_records`` (idempotent on ``execution_id``).

        Uses ``INSERT … ON CONFLICT (execution_id) DO UPDATE`` so re-appending an already-stored
        execution_id overwrites all mutable columns (status, record_json, etc.) without error.
        All SQL is parameterized; psycopg stays lazy.

        Args:
            record: The execution record to persist or update.
        """
        conn = await self._connect()
        try:
            async with conn.transaction(), conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO execution_records
                        (execution_id, competition_id, run_id, agent_id, status, record_json)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (execution_id) DO UPDATE SET
                        competition_id = EXCLUDED.competition_id,
                        run_id         = EXCLUDED.run_id,
                        agent_id       = EXCLUDED.agent_id,
                        status         = EXCLUDED.status,
                        record_json    = EXCLUDED.record_json
                    """,
                    (
                        record.execution_id,
                        record.competition_id,
                        record.run_id,
                        record.agent_id,
                        record.status.value,
                        serialize_payload(record.model_dump(mode="json")),
                    ),
                )
        finally:
            await self._release(conn)

    async def get_execution_record(self, execution_id: str) -> ExecutionRecord:
        """Fetch and reconstruct an execution record by id.

        Args:
            execution_id: The id used at :meth:`append_execution_record`.

        Returns:
            The reconstructed :class:`~veridex.execution.models.ExecutionRecord`.

        Raises:
            KeyError: If no record with ``execution_id`` exists.
        """
        conn = await self._connect()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT record_json FROM execution_records WHERE execution_id = %s",
                    (execution_id,),
                )
                row = await cur.fetchone()
        finally:
            await self._release(conn)

        if row is None:
            raise KeyError(f"no execution record with execution_id={execution_id!r}")
        return ExecutionRecord.model_validate(json.loads(row[0]))

    async def list_executions(self, competition_id: str) -> list[ExecutionRecord]:
        """Return all execution records for a competition, ordered by ``execution_id``.

        Args:
            competition_id: The owning competition.

        Returns:
            Reconstructed :class:`~veridex.execution.models.ExecutionRecord` objects,
            sorted ascending by ``execution_id``.
        """
        conn = await self._connect()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT record_json FROM execution_records WHERE competition_id = %s ORDER BY execution_id",
                    (competition_id,),
                )
                rows = await cur.fetchall()
        finally:
            await self._release(conn)

        return [ExecutionRecord.model_validate(json.loads(row[0])) for row in rows]

    # --- realized-fill ledger methods (SAF-002b/c — append-only, fee-inclusive) ---

    async def append_realized_fill(
        self,
        *,
        session_id: str,
        realized_pnl: float,
        fee: float,
        fill_ts_ms: int,
        source: str = "venue_reconciled",
    ) -> int:
        """INSERT one realized-fill row (append-only) and return the DB-assigned ``seq``.

        Uses ``INSERT … RETURNING seq`` against the ``BIGSERIAL`` primary key — never an UPDATE
        or DELETE, mirroring the ``competition_events`` append-only pattern. All SQL is
        parameterized; psycopg stays lazy.

        Args:
            session_id: Session identity the fill belongs to.
            realized_pnl: Signed realized PnL (loss is negative).
            fee: Venue fee (``>= 0``).
            fill_ts_ms: Venue fill time in integer epoch milliseconds (UTC).
            source: Provenance marker.

        Returns:
            The DB-assigned monotonic unique ``seq``.
        """
        conn = await self._connect()
        try:
            async with conn.transaction(), conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO realized_fill_ledger (session_id, realized_pnl, fee, fill_ts_ms, source) "
                    "VALUES (%s, %s, %s, %s, %s) RETURNING seq",
                    (session_id, realized_pnl, fee, fill_ts_ms, source),
                )
                row = await cur.fetchone()
        finally:
            await self._release(conn)
        return int(row[0])

    async def list_realized_fills(self, session_id: str) -> list[RealizedFillLedgerRow]:
        """Return this session's realized-fill rows, ordered by ``seq`` ascending.

        Args:
            session_id: The session whose ledger to read.

        Returns:
            Reconstructed :class:`RealizedFillLedgerRow` objects, sorted by ``seq`` ascending.
        """
        conn = await self._connect()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT seq, session_id, realized_pnl, fee, fill_ts_ms, source "
                    "FROM realized_fill_ledger WHERE session_id = %s ORDER BY seq",
                    (session_id,),
                )
                rows = await cur.fetchall()
        finally:
            await self._release(conn)
        return [
            RealizedFillLedgerRow(
                seq=int(r[0]),
                session_id=r[1],
                realized_pnl=float(r[2]),
                fee=float(r[3]),
                fill_ts_ms=int(r[4]),
                source=r[5],
            )
            for r in rows
        ]

    # --- pre-submit ledger methods (IDM-005 — append-only, non-secret compound record) ---

    async def append_presubmit(
        self,
        *,
        session_id: str,
        integrity_commitment_hash: str,
        venue_order_key: str,
        captured_id: str | None = None,
    ) -> int:
        """INSERT one pre-submit row (append-only) and return the DB-assigned ``seq``.

        Uses ``INSERT … RETURNING seq`` against the ``BIGSERIAL`` primary key — never an UPDATE or
        DELETE, mirroring the ``realized_fill_ledger`` append-only pattern. All SQL is parameterized;
        psycopg stays lazy. Only the non-secret compound-record fields are written.

        Args:
            session_id: Session identity the pre-submit record belongs to.
            integrity_commitment_hash: Veridex's private one-way digest (safe to store; not a join key).
            venue_order_key: The venue-recognized V2 order hash (the reconciliation join key).
            captured_id: Captured venue order id if the POST returned one (else ``None``).

        Returns:
            The DB-assigned monotonic unique ``seq``.
        """
        conn = await self._connect()
        try:
            async with conn.transaction(), conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO presubmit_ledger "
                    "(session_id, integrity_commitment_hash, venue_order_key, captured_id) "
                    "VALUES (%s, %s, %s, %s) RETURNING seq",
                    (session_id, integrity_commitment_hash, venue_order_key, captured_id),
                )
                row = await cur.fetchone()
        finally:
            await self._release(conn)
        return int(row[0])

    async def list_presubmit(self, session_id: str) -> list[PreSubmitLedgerRow]:
        """Return this session's pre-submit rows, ordered by ``seq`` ascending.

        Args:
            session_id: The session whose pre-submit ledger to read.

        Returns:
            Reconstructed :class:`PreSubmitLedgerRow` objects, sorted by ``seq`` ascending.
        """
        conn = await self._connect()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT seq, session_id, integrity_commitment_hash, venue_order_key, captured_id "
                    "FROM presubmit_ledger WHERE session_id = %s ORDER BY seq",
                    (session_id,),
                )
                rows = await cur.fetchall()
        finally:
            await self._release(conn)
        return [
            PreSubmitLedgerRow(
                seq=int(r[0]),
                session_id=r[1],
                integrity_commitment_hash=r[2],
                venue_order_key=r[3],
                captured_id=r[4],
            )
            for r in rows
        ]

    # --- runtime-event methods (I-4 — durable OPS channel, INSERT-only + event_uuid dedup) ---

    async def append_runtime_events(self, rows: list[RuntimeEventRow]) -> int:
        """INSERT durable OPS events in ONE transaction, de-duplicated by ``event_uuid``.

        ``INSERT … ON CONFLICT (event_uuid) DO NOTHING`` makes the durable spool's at-least-once
        replay idempotent at the DB — re-flushing an already-committed batch inserts nothing. The
        DB assigns the ``BIGSERIAL`` id (the read cursor). All SQL is parameterized; psycopg stays
        lazy.

        Args:
            rows: The events to append (their input ``id`` is ignored).

        Returns:
            The number of rows newly inserted (conflicts skipped).
        """
        if not rows:
            return 0
        conn = await self._connect()
        try:
            inserted = 0
            async with conn.transaction(), conn.cursor() as cur:
                for row in rows:
                    await cur.execute(
                        "INSERT INTO runtime_events "
                        "(event_uuid, agent_id, run_id, session_id, event_type, ts, channel, payload_json) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (event_uuid) DO NOTHING",
                        (
                            row.event_uuid,
                            row.agent_id,
                            row.run_id,
                            row.session_id,
                            row.event_type,
                            row.ts,
                            row.channel,
                            json.dumps(row.payload, default=str),
                        ),
                    )
                    inserted += cur.rowcount
        finally:
            await self._release(conn)
        return inserted

    async def list_runtime_events(
        self, agent_id: str, *, since: int = 0, limit: int | None = None
    ) -> list[RuntimeEventRow]:
        """Return this agent's durable OPS events with ``id > since``, ascending, capped at ``limit``.

        Args:
            agent_id: The agent whose OPS telemetry to read.
            since: The last consumed ``id`` (exclusive); ``0`` returns from the start.
            limit: When set, the FIRST ``limit`` rows after ``since`` (forward cursor paging).

        Returns:
            Reconstructed :class:`RuntimeEventRow` objects, sorted by ``id`` ascending.
        """
        sql = (
            "SELECT id, event_uuid, agent_id, run_id, session_id, event_type, ts, channel, payload_json "
            "FROM runtime_events WHERE agent_id = %s AND id > %s ORDER BY id"
        )
        params: tuple[Any, ...] = (agent_id, since)
        if limit is not None:
            sql += " LIMIT %s"
            params = (agent_id, since, limit)
        conn = await self._connect()
        try:
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
                rows = await cur.fetchall()
        finally:
            await self._release(conn)
        return [
            RuntimeEventRow(
                id=int(r[0]),
                event_uuid=r[1],
                agent_id=r[2],
                run_id=r[3],
                session_id=r[4],
                event_type=r[5],
                ts=int(r[6]),
                channel=r[7],
                payload=json.loads(r[8]),
            )
            for r in rows
        ]

    # --- agent-instance methods (Phase-2D deploy — REQ-2D-701A) ---

    async def persist_agent_instance(self, instance: AgentInstance) -> None:
        """Insert the deployed instance row (queryable columns + full ``record_json`` blob).

        Mirrors the ``execution_records`` idiom: a few extracted columns for querying plus the
        canonical JSON of the whole record. Uses ``ON CONFLICT (instance_id) DO UPDATE`` so a
        re-persist is idempotent. All SQL is parameterized; psycopg stays lazy.

        Args:
            instance: The pinned instance record to persist.
        """
        conn = await self._connect()
        try:
            async with conn.transaction(), conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO agent_instances
                        (instance_id, run_id, agent_id, template_id, status, record_json)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (instance_id) DO UPDATE SET
                        run_id      = EXCLUDED.run_id,
                        agent_id    = EXCLUDED.agent_id,
                        template_id = EXCLUDED.template_id,
                        status      = EXCLUDED.status,
                        record_json = EXCLUDED.record_json
                    """,
                    (
                        instance.instance_id,
                        instance.run_id,
                        instance.agent_id,
                        instance.template_id,
                        instance.status.value,
                        serialize_payload(instance.model_dump(mode="json")),
                    ),
                )
        finally:
            await self._release(conn)

    async def get_agent_instance(self, instance_id: str) -> AgentInstance:
        """Fetch and reconstruct a deployed instance by id.

        Args:
            instance_id: The id used at :meth:`persist_agent_instance`.

        Returns:
            The reconstructed :class:`~veridex.deploy.instance.AgentInstance`.

        Raises:
            KeyError: If no instance with ``instance_id`` exists.
        """
        conn = await self._connect()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT record_json FROM agent_instances WHERE instance_id = %s",
                    (instance_id,),
                )
                row = await cur.fetchone()
        finally:
            await self._release(conn)

        if row is None:
            raise KeyError(f"no agent instance with instance_id={instance_id!r}")
        return AgentInstance.model_validate(json.loads(row[0]))

    async def update_agent_instance_status(
        self,
        instance_id: str,
        status: DeployStatus,
        *,
        last_failure_reason: DeployFailureReason | None = None,
        updated_at: str,
    ) -> None:
        """Read-modify-write the instance's status column + ``record_json`` in one transaction.

        Loads the row ``FOR UPDATE``, mutates the reconstructed record's ``status`` /
        ``last_failure_reason`` / ``updated_at``, and writes both the extracted ``status`` column
        and the re-serialized blob so the two stay consistent.

        Args:
            instance_id: The instance to update.
            status: The new lifecycle status.
            last_failure_reason: Controlled taxonomy value to persist; ``None`` leaves it unchanged.
            updated_at: ISO-8601 UTC timestamp of this write.

        Raises:
            KeyError: If no instance with ``instance_id`` exists.
        """
        conn = await self._connect()
        try:
            async with conn.transaction(), conn.cursor() as cur:
                await cur.execute(
                    "SELECT record_json FROM agent_instances WHERE instance_id = %s FOR UPDATE",
                    (instance_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    raise KeyError(f"no agent instance with instance_id={instance_id!r}")
                record = AgentInstance.model_validate(json.loads(row[0]))
                record.status = status
                if last_failure_reason is not None:
                    record.last_failure_reason = last_failure_reason
                record.updated_at = updated_at
                await cur.execute(
                    "UPDATE agent_instances SET status = %s, record_json = %s WHERE instance_id = %s",
                    (status.value, serialize_payload(record.model_dump(mode="json")), instance_id),
                )
        finally:
            await self._release(conn)

    async def list_agent_instances(self) -> list[AgentInstance]:
        """Reconstruct every deployed instance from its ``record_json`` blob (no owner filter here).

        A plain ``SELECT record_json`` over the existing table — ``operator_id`` / ``runtime_handle``
        ride inside the blob (ZERO DDL, no dedicated column). The owner-scoped route applies the
        fail-closed owner filter, so an UNOWNED / legacy row is returned here but never listed.

        Returns:
            All :class:`~veridex.deploy.instance.AgentInstance` records (any order).
        """
        conn = await self._connect()
        try:
            async with conn.cursor() as cur:
                await cur.execute("SELECT record_json FROM agent_instances")
                rows = await cur.fetchall()
        finally:
            await self._release(conn)
        return [AgentInstance.model_validate(json.loads(row[0])) for row in rows]

    # --- deployment-attempt ledger methods (I-3 — write-once idempotency claim) ---

    async def persist_deployment_attempt(self, attempt: DeploymentAttempt) -> None:
        """INSERT one deployment-attempt row (append-only); the UNIQUE claim signals a duplicate.

        Maps the Postgres ``UniqueViolation`` on ``UNIQUE(operator_id, idempotency_key)`` to
        :class:`~veridex.deploy.attempt.DuplicateAttemptError` — the same contract as
        :class:`InMemoryStore` — so the deploy route reconciles on ONE backend-agnostic signal. All
        SQL is parameterized; psycopg stays lazy.

        Args:
            attempt: The write-once claim to persist.

        Raises:
            DuplicateAttemptError: If ``(operator_id, idempotency_key)`` is already claimed.
        """
        conn = await self._connect()
        try:
            async with conn.transaction(), conn.cursor() as cur:
                try:
                    await cur.execute(
                        "INSERT INTO deployment_attempts "
                        "(attempt_id, operator_id, idempotency_key, config_fingerprint, status, "
                        "created_at, instance_id, external_id) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        (
                            attempt.attempt_id,
                            attempt.operator_id,
                            attempt.idempotency_key,
                            attempt.config_fingerprint,
                            attempt.status.value,
                            attempt.created_at,
                            attempt.instance_id,
                            attempt.external_id,
                        ),
                    )
                except Exception as exc:
                    # psycopg is already in sys.modules (imported inside _connect).
                    import psycopg.errors  # noqa: PLC0415 (lazy, not at module level)

                    if isinstance(exc, psycopg.errors.UniqueViolation):
                        raise DuplicateAttemptError(
                            f"deployment attempt already claimed: "
                            f"({attempt.operator_id!r}, {attempt.idempotency_key!r})"
                        ) from exc
                    raise
        finally:
            await self._release(conn)

    async def get_deployment_attempt_by_key(
        self, operator_id: str, idempotency_key: str
    ) -> DeploymentAttempt | None:
        """Fetch and reconstruct the deployment-attempt claim for the ``(operator_id, key)`` pair.

        Args:
            operator_id: The owner the claim is scoped to.
            idempotency_key: The caller-scoped idempotency key.

        Returns:
            The reconstructed :class:`~veridex.deploy.attempt.DeploymentAttempt`, or ``None`` if the
            pair is unclaimed.
        """
        conn = await self._connect()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT attempt_id, operator_id, idempotency_key, config_fingerprint, status, "
                    "created_at, instance_id, external_id "
                    "FROM deployment_attempts WHERE operator_id = %s AND idempotency_key = %s",
                    (operator_id, idempotency_key),
                )
                row = await cur.fetchone()
        finally:
            await self._release(conn)

        if row is None:
            return None
        return DeploymentAttempt(
            attempt_id=row[0],
            operator_id=row[1],
            idempotency_key=row[2],
            config_fingerprint=row[3],
            status=row[4],
            created_at=row[5],
            instance_id=row[6],
            external_id=row[7],
        )
