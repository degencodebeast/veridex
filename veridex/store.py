"""B5 — async repository persistence for run results and competition data (REQ-105 / AC-105, gate CON-006).

Two implementations behind one async :class:`Store` protocol:

  * :class:`InMemoryStore` — pure stdlib, dict-backed; the offline test suite uses ONLY this
    (NO network / DB). Round-trips a :class:`~veridex.runtime.orchestrator.RunResult` exactly.
  * :class:`PostgresStore` — raw **psycopg3 ``AsyncConnection``** (NO SQLAlchemy / sqlite).
    ``psycopg`` is imported LAZILY so ``import veridex.store`` works without it installed.
    Writes are batched via ``executemany`` inside ONE transaction; all SQL is parameterized.

CON-010: this is the async SHELL — the deterministic core never imports it. Schema: five tables
(``runs``, ``run_events``, ``score_rows``, ``competitions``, ``competition_events``). Each event
table has an index on its parent-id column and a unique ``(parent_id, seq)`` constraint.

Phase-2A adds seven competition/event methods to the protocol; the CHECK-constraint literal
tuples (``_COMPETITION_STATUS_VALUES``, ``_EVENT_TYPE_VALUES``) are module-level so drift-guard
tests can import and compare them against the canonical enums.
"""

from __future__ import annotations

import copy
import json
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from veridex.competition.events import CompetitionEvent
from veridex.competition.models import AgentEntry, Competition, CompetitionConfig, CompetitionStatus
from veridex.config import get_settings, require_database_url
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
    "agent_action",
    "law_result",
    "policy_result",
    "execution_submitted",
    "execution_receipt",
    "score_update",
    "proof_anchor",
    "payout_status",
    "competition_finalized",
)


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
        """Initialise an empty, in-process registry for runs and competitions."""
        self._runs: dict[str, dict[str, Any]] = {}
        self._competitions: dict[str, Competition] = {}
        self._competition_events: dict[str, list[CompetitionEvent]] = {}

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

    async def append_competition_events(self, competition_id: str, events: list[CompetitionEvent]) -> None:
        """Append deep copies of ``events`` to the competition's event list.

        Args:
            competition_id: The owning competition.
            events: Events to append.
        """
        bucket = self._competition_events.setdefault(competition_id, [])
        for event in events:
            bucket.append(event.model_copy(deep=True))

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

    Connection lifecycle: each method opens a FRESH ``AsyncConnection`` and closes it before
    returning — there is NO connection pooling. This is an INTENTIONAL Phase-1 simplicity
    choice (low write volume, no long-lived service), not an oversight.

    Setup asymmetry: ``init_db(conn)`` is a one-time DDL step that takes a CALLER-supplied
    connection, whereas the DML methods self-connect; ``persist_run`` assumes ``init_db`` has
    already created the schema.
    """

    def __init__(self, *, dsn: str | None = None) -> None:
        """Configure the store.

        Args:
            dsn: Optional Postgres DSN; resolved from config at use-time when omitted.
        """
        self._dsn = dsn

    def _resolve_dsn(self) -> str:
        """Return the configured DSN or fall back to the config-derived ``DATABASE_URL``."""
        if self._dsn is not None:
            return self._dsn
        return require_database_url(get_settings())

    async def _connect(self) -> Any:
        """Open a fresh async connection (imports ``psycopg`` lazily)."""
        import psycopg

        return await psycopg.AsyncConnection.connect(self._resolve_dsn())

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
            await conn.close()

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
            await conn.close()

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
            await conn.close()

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
            await conn.close()

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
            await conn.close()

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
            await conn.close()

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
            await conn.close()

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
            await conn.close()

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
            await conn.close()

        return [_build_competition(*row) for row in rows]
