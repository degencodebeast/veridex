"""OPS-channel stores for RuntimeEvents (Agent Ops drawer source).

Two stores, one channel:

* :class:`RuntimeEventStore` — a per-``agent_id`` in-memory ring buffer (Task 15). Lossy by design;
  kept as the lightweight I-4 cut-fallback and for its unit tests.
* :class:`DurableRuntimeEventStore` — the I-4 (AC-13/14/15) crash-safe spool: a two-tier WAL in
  front of a durable :class:`~veridex.store.Store`. ``enqueue`` (the sync decision-loop sink) appends
  one JSONL line to a WAL file under ``WAL_DIR`` and ``flush()``es it to the OS before returning
  (a SIGKILL after the ack cannot lose it), while an async flusher ``fsync``es per BATCH and commits
  to Postgres, advancing a durable cursor only AFTER commit. On restart the WAL tail beyond the
  cursor is replayed; delivery is at-least-once and dedup happens at the DB
  (``UNIQUE(event_uuid)`` + ``ON CONFLICT DO NOTHING``), so a process/container crash loses nothing
  (residual window = OS/host power-loss between fsync batches, ≤one batch, DISCLOSED).

Both are the OPS channel ONLY: there is no code path from here into ``compute_evidence_hash`` or the
canonical competition event log, so SEC-003 holds by construction — the proof record is the canonical
log, never these buffers.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
import threading
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from veridex.runtime.runtime_events import RuntimeEvent, RuntimeEventSink
from veridex.store import RuntimeEventRow, Store


@dataclass(frozen=True)
class _PendingEntry:
    """One queued event plus whether its WAL append actually succeeded.

    ``wal_backed`` is the crux of the durable-cursor invariant: the cursor is a WAL-LINE index, so
    committing an in-memory-only entry (WAL append failed) must advance it by ZERO — otherwise the
    cursor over-counts vs the WAL and a later crash skips a real WAL line (silent loss).
    """

    row: RuntimeEventRow
    wal_backed: bool


class RuntimeEventStore:
    """A bounded, per-agent in-memory buffer of OPS-channel RuntimeEvents.

    Attributes:
        capacity: Max events retained per ``agent_id`` (oldest evicted past this).
    """

    def __init__(self, *, capacity: int = 2000) -> None:
        self._capacity = capacity
        self._by_agent: dict[str, deque[RuntimeEvent]] = {}

    def record(self, event: RuntimeEvent) -> None:
        """Append ``event`` to its agent's ring buffer (this IS the ``RuntimeEventSink``)."""
        buf = self._by_agent.get(event.agent_id)
        if buf is None:
            buf = deque(maxlen=self._capacity)
            self._by_agent[event.agent_id] = buf
        buf.append(event)

    def sink(self) -> RuntimeEventSink:
        """Return the ``record`` callable as a ``RuntimeEventSink`` (wire into an AgnoRuntime)."""
        return self.record

    def list_for_agent(self, agent_id: str, *, since: int = 0, limit: int | None = None) -> list[RuntimeEvent]:
        """Return one agent's buffered events with ``ts >= since`` (oldest→newest), last ``limit``.

        Args:
            agent_id: The agent whose telemetry to read.
            since: ``ts`` lower bound (ms); ``0`` returns everything retained.
            limit: When set, return only the most-recent ``limit`` matching events.

        Returns:
            The matching :class:`~veridex.runtime.runtime_events.RuntimeEvent` list.
        """
        events = [e for e in self._by_agent.get(agent_id, ()) if e.ts >= since]
        if limit is not None:
            events = events[-limit:]
        return events


def _resolve_wal_dir(wal_dir: str | Path | None) -> Path:
    """Resolve the WAL spool directory: explicit arg → ``WAL_DIR`` env (I-5) → an ephemeral temp dir.

    I-5 provisions ``WAL_DIR`` as a mounted, persistent volume in the deployed api-runtime (the durable
    path); tests pass an explicit ``tmp_path``. When NEITHER is set (bare local/dev), fall back to a
    fresh unique temp dir per instance — the durability guarantee needs a real volume, and an ISOLATED
    ephemeral dir keeps a dev/test app from crashing AND from replaying another instance's stale spool.
    """
    if wal_dir is not None:
        resolved = Path(wal_dir)
    elif env := os.environ.get("WAL_DIR"):
        resolved = Path(env)
    else:
        return Path(tempfile.mkdtemp(prefix="veridex-runtime-wal-"))  # ephemeral + isolated (mkdtemp creates it)
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


class DurableRuntimeEventStore:
    """Crash-safe two-tier WAL spool in front of a durable :class:`~veridex.store.Store` (I-4).

    The write side of the Agent-Ops feed. ``enqueue`` is the sync ``RuntimeEventSink`` the decision
    loop calls; an async flusher drains the in-memory queue to the store in batches. The read side is
    the store itself (``list_runtime_events``) — the durable ``BIGSERIAL`` id is the client cursor.

    Durability contract (Codex R3#2 / R4#1 / R5#1):

    * ``enqueue`` assigns a client ``event_uuid``, appends ONE JSONL line to the WAL, ``flush()``es to
      the OS, and queues in memory — all without awaiting. A SIGKILL after the ack cannot lose it.
    * the flusher ``fsync()``es the WAL per BATCH (the disclosed ≤1-batch power-loss window), commits
      the batch to the store, then advances a durable line cursor. On restart the WAL tail beyond the
      cursor is replayed; the store dedups by ``event_uuid`` so replay is idempotent.
    * FAIL-CLOSED, never fail-open: a WAL/flush/commit failure raises the visible ``degraded`` flag +
      a ``flush_failures`` counter and re-queues the (already-WAL-durable) batch — it NEVER drops an
      event and the sync ``enqueue`` NEVER blocks or raises into the decision loop.

    Attributes:
        batch_size: Max events committed per flush.
        poll_interval_s: Idle sleep of the background loop when the queue is empty.
        max_backoff_s: Backoff after a failed flush (``0`` disables the sleep — tests).
    """

    def __init__(
        self,
        *,
        store: Store,
        wal_dir: str | Path | None = None,
        batch_size: int = 128,
        poll_interval_s: float = 0.05,
        max_backoff_s: float = 1.0,
    ) -> None:
        self._store = store
        self._batch_size = batch_size
        self._poll_interval_s = poll_interval_s
        self._max_backoff_s = max_backoff_s

        self._dir = _resolve_wal_dir(wal_dir)
        self._wal_path = self._dir / "runtime_events.wal"
        self._cursor_path = self._dir / "runtime_events.cursor"
        # Opened LAZILY on first enqueue: an app whose agents never emit leaks no fd / spool file.
        self._wal: TextIO | None = None

        self._lock = threading.Lock()  # guards WAL writes + the in-memory queue
        self._pending: deque[_PendingEntry] = deque()
        self._committed_lines: int = self._read_cursor()
        self._degraded = False
        self._flush_failures = 0

        self._running = False
        self._loop_task: asyncio.Task[None] | None = None

    # --- the decision-loop sink (sync, non-blocking, never raises) ----------------------------

    def enqueue(self, event: RuntimeEvent) -> None:
        """Assign a uuid, durably append to the WAL (``flush`` to OS), and queue in memory.

        This IS the ``RuntimeEventSink``. It never blocks and never raises: a WAL write failure
        degrades visibly (``degraded`` + ``flush_failures``) but still queues the event in memory, so
        the decision loop is never crashed by telemetry persistence.
        """
        row = RuntimeEventRow(
            id=0,
            event_uuid=uuid.uuid4().hex,
            agent_id=event.agent_id,
            run_id=event.run_id,
            session_id=event.session_id,
            event_type=event.type.value,
            ts=event.ts,
            channel=event.channel,
            payload=dict(event.payload),
        )
        line = json.dumps(
            {
                "event_uuid": row.event_uuid,
                "agent_id": row.agent_id,
                "run_id": row.run_id,
                "session_id": row.session_id,
                "event_type": row.event_type,
                "ts": row.ts,
                "channel": row.channel,
                "payload": row.payload,
            },
            default=str,
        )
        with self._lock:
            wal_backed = True
            try:
                if self._wal is None:
                    self._wal = self._wal_path.open("a", encoding="utf-8")
                self._wal.write(line + "\n")
                self._wal.flush()  # userspace → OS page cache (SIGKILL-safe); fsync is per-BATCH
            except OSError:
                # In-memory-only: NOT WAL-durable. Degrade visibly, still queue (never drop), and mark
                # it so committing it advances the WAL-line cursor by ZERO (else the cursor over-counts).
                wal_backed = False
                self._degraded = True
                self._flush_failures += 1
            self._pending.append(_PendingEntry(row=row, wal_backed=wal_backed))

    def sink(self) -> RuntimeEventSink:
        """Return :meth:`enqueue` as a ``RuntimeEventSink`` — the ONE shared sink II-2 threads too."""
        return self.enqueue

    # --- the async flusher --------------------------------------------------------------------

    async def flush_once(self) -> int:
        """Drain one batch to the store; ``fsync`` the WAL first, advance the cursor after commit.

        Returns:
            The number of events committed (``0`` when the queue is empty OR the store is failing —
            on failure the batch is re-queued and ``degraded`` is raised).
        """
        with self._lock:
            if not self._pending:
                return 0
            batch = [self._pending.popleft() for _ in range(min(self._batch_size, len(self._pending)))]

        # Per-BATCH fsync: push the WAL from OS page cache to disk BEFORE we rely on it (this bounds
        # the disclosed power-loss window to ≤ one batch).
        with self._lock:
            if self._wal is not None:
                with contextlib.suppress(OSError, ValueError):
                    os.fsync(self._wal.fileno())

        try:
            await self._store.append_runtime_events([entry.row for entry in batch])
        except Exception:  # noqa: BLE001 — a transient store/DB outage must never crash the flusher.
            with self._lock:
                self._pending.extendleft(reversed(batch))  # re-queue at the front (already WAL-durable)
                self._degraded = True
                self._flush_failures += 1
            if self._max_backoff_s > 0:
                await asyncio.sleep(self._max_backoff_s)
            return 0

        # Advance the durable cursor by the count of WAL-BACKED rows only — the cursor is a WAL-line
        # index, and an in-memory-only entry (WAL append failed) has no WAL line to skip on replay.
        self._committed_lines += sum(1 for entry in batch if entry.wal_backed)
        self._persist_cursor(self._committed_lines)
        with self._lock:
            if not self._pending:
                self._degraded = False  # fully caught up → visibly recovered
        return len(batch)

    async def drain(self, *, max_passes: int = 10_000) -> None:
        """Flush until the queue is empty (or ``max_passes`` is hit — a stuck-store safety bound)."""
        for _ in range(max_passes):
            with self._lock:
                empty = not self._pending
            if empty:
                return
            await self.flush_once()

    def recover(self) -> None:
        """Replay the WAL tail beyond the durable cursor into the in-memory queue (restart path)."""
        cursor = self._read_cursor()
        self._committed_lines = cursor
        tail = self._read_wal_rows()[cursor:]  # every replayed row IS a WAL line -> wal_backed
        with self._lock:
            self._pending.extend(_PendingEntry(row=row, wal_backed=True) for row in tail)

    async def replay_all(self) -> None:
        """Re-queue EVERY WAL line, ignoring the cursor (forced re-drive; DB dedup makes it safe).

        Used for a full re-replay (e.g. verifying ``ON CONFLICT`` dedup). It does not mix with fresh
        ``enqueue`` traffic — treat the store as fully drained afterward.
        """
        rows = self._read_wal_rows()  # every WAL line is durable -> wal_backed
        with self._lock:
            self._pending.extend(_PendingEntry(row=row, wal_backed=True) for row in rows)

    # --- lifecycle ----------------------------------------------------------------------------

    async def start(self) -> None:
        """Recover the WAL tail, then spawn the background flush loop (idempotent)."""
        if self._running:
            return
        self._running = True
        self.recover()
        self._loop_task = asyncio.create_task(self._run_loop())

    async def aclose(self) -> None:
        """Stop the loop, best-effort drain the queue, and close the WAL handle."""
        self._running = False
        if self._loop_task is not None:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
            self._loop_task = None
        await self.drain(max_passes=200)
        with self._lock:
            if self._wal is not None:
                with contextlib.suppress(OSError):
                    self._wal.close()
                self._wal = None

    async def _run_loop(self) -> None:
        """Background flush loop: drain batches, idle-sleep when empty."""
        while self._running:
            committed = await self.flush_once()
            if committed == 0:
                await asyncio.sleep(self._poll_interval_s)

    # --- observability ------------------------------------------------------------------------

    @property
    def degraded(self) -> bool:
        """``True`` when a WAL/flush failure has left un-committed backlog (visible fail-closed)."""
        return self._degraded

    @property
    def flush_failures(self) -> int:
        """Count of WAL/flush failures observed (monotonic; surfaces a persistent outage)."""
        return self._flush_failures

    @property
    def pending(self) -> int:
        """Number of events queued but not yet committed to the store."""
        with self._lock:
            return len(self._pending)

    @property
    def committed(self) -> int:
        """The durable WAL cursor — count of WAL lines confirmed committed to the store."""
        return self._committed_lines

    # --- WAL / cursor helpers -----------------------------------------------------------------

    def _read_wal_rows(self) -> list[RuntimeEventRow]:
        """Parse the WAL file into rows, skipping a torn trailing line (the disclosed crash window)."""
        if not self._wal_path.exists():
            return []
        rows: list[RuntimeEventRow] = []
        for line in self._wal_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue  # a partial last line from a mid-write power-loss — inside the ≤batch window
            rows.append(
                RuntimeEventRow(
                    id=0,
                    event_uuid=d["event_uuid"],
                    agent_id=d["agent_id"],
                    run_id=d.get("run_id"),
                    session_id=d.get("session_id"),
                    event_type=d["event_type"],
                    ts=int(d["ts"]),
                    channel=d.get("channel", "OPS"),
                    payload=d.get("payload") or {},
                )
            )
        return rows

    def _read_cursor(self) -> int:
        """Read the durable line cursor (``0`` when absent or unreadable)."""
        try:
            return int(self._cursor_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return 0

    def _persist_cursor(self, n: int) -> None:
        """Atomically persist the cursor (temp + ``fsync`` + ``os.replace``); degrade on failure."""
        try:
            tmp = self._cursor_path.with_suffix(".tmp")
            tmp.write_text(str(n), encoding="utf-8")
            fd = os.open(tmp, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, self._cursor_path)
        except OSError:
            self._degraded = True
            self._flush_failures += 1
