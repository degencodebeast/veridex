"""B5 — async repository persistence for run results (REQ-105 / AC-105, gate CON-006).

Two implementations behind one async :class:`Store` protocol:

  * :class:`InMemoryStore` — pure stdlib, dict-backed; the offline test suite uses ONLY this
    (NO network / DB). Round-trips a :class:`~veridex.runtime.orchestrator.RunResult` exactly.
  * :class:`PostgresStore` — raw **psycopg3 ``AsyncConnection``** (NO SQLAlchemy / sqlite).
    ``psycopg`` is imported LAZILY so ``import veridex.store`` works without it installed.
    Writes are batched via ``executemany`` inside ONE transaction; all SQL is parameterized.

CON-010: this is the async SHELL — the deterministic core never imports it. Schema: three
tables (``runs``, ``run_events``, ``score_rows``) with an index on ``run_events(run_id)`` and a
unique ``(run_id, sequence_no)`` constraint (the evidence-determinism invariant, enforced in SQL).
"""

from __future__ import annotations

import copy
import json
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from veridex.config import get_settings, require_database_url
from veridex.runtime.evidence import serialize_payload

if TYPE_CHECKING:  # avoid an import cycle (orchestrator type-hints Store)
    from veridex.runtime.orchestrator import RunResult


@runtime_checkable
class Store(Protocol):
    """Async repository contract for persisting and loading a competition run."""

    async def persist_run(self, run_result: RunResult) -> None:
        """Persist a run (events + score rows + source_mode + evidence_hash)."""
        ...

    async def load_run(self, run_id: str) -> RunResult:
        """Load a previously persisted run by id, reconstructing the ``RunResult``."""
        ...


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


class InMemoryStore:
    """Dict-backed async store for the offline suite. Deep-copies on write and read."""

    def __init__(self) -> None:
        """Initialise an empty, in-process run registry."""
        self._runs: dict[str, dict[str, Any]] = {}

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


_RUN_EVENT_COLUMNS = (
    "sequence_no",
    "event_type",
    "state_snapshot_json",
    "action_payload_json",
    "validation_payload_json",
    "result_payload_json",
)


class PostgresStore:
    """Raw psycopg3 ``AsyncConnection`` store. ``psycopg`` is imported lazily.

    DSN resolution: the explicit ``dsn`` argument, else ``require_database_url(get_settings())``.

    Connection lifecycle: ``persist_run`` / ``load_run`` each open a FRESH ``AsyncConnection``
    and close it before returning — there is NO connection pooling. This is an INTENTIONAL
    Phase-1 simplicity choice (low write volume, no long-lived service), not an oversight; do
    not "optimize" it into a shared/long-lived connection without a deliberate redesign.

    Setup asymmetry: ``init_db(conn)`` is a one-time DDL step that takes a CALLER-supplied
    connection, whereas ``persist_run`` / ``load_run`` self-connect; ``persist_run`` assumes
    ``init_db`` has already created the schema.
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
        """Create the three tables + index + unique constraint (idempotent).

        Args:
            conn: An open psycopg3 ``AsyncConnection``.
        """
        async with conn.cursor() as cur:
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
        await conn.commit()

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
        """Reconstruct a run from the three tables.

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
