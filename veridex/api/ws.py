"""WebSocket arena — read-only spectator projection (Phase 2A Task 7 / P2A-7).

The arena streams the canonical competition event log to spectators over a WebSocket. It is a
strict **projection** and is **read-only**:

* It streams ONLY persisted :class:`~veridex.competition.events.CompetitionEvent`\\ s — replayed
  from the store and (in the 2B seam) fanned out live via the broadcaster. It NEVER originates
  truth, mutates state, or creates proof evidence (CON-203 / REQ-213).
* Inbound client frames are consumed purely to observe the disconnect; they are never acted on.

Decoupled fanout (REQ-214 / REQ-2B-30):
    Each connection owns a **bounded** :class:`asyncio.Queue`. :meth:`ArenaConnectionManager.broadcast`
    enqueues with non-blocking ``put_nowait`` and is NEVER allowed to ``await`` and stall the
    producer or other clients. A slow client whose bounded queue is FULL is **disconnected**
    (dropped from the registry) rather than silently skipping one event — a silent skip would
    leave a hidden mid-stream sequence gap. Overflow signals the owning route, which closes the
    socket and cancels any blocked sender so the gap is observable and the client can reconnect. A
    client that raises on enqueue is likewise dropped (error isolation), so a dead client can never
    abort the run loop or skip persistence.

Single-instance scope:
    This is the **in-memory, single-process** broadcaster for Phase 2A — NO Redis, NO sticky
    sessions, NO clustering (deferred to 2B/scaling).

Gapless replay→live handoff:
    The route registers its queue BEFORE replaying the store, so any event broadcast during
    replay is captured rather than lost. The live drain then dedupes at the seq boundary
    (skipping ``seq <= last_sent_seq``) so the spectator sees a gapless, duplicate-free stream
    across the replay→live seam.

    Producer-ordering contract (2B/2D, DEC-2D-4): every live producer MUST persist an event to
    the store BEFORE calling :meth:`ArenaConnectionManager.broadcast` for it. The
    register-before-replay design closes the consumer-side race; persist-before-broadcast is the
    matching producer-side requirement. If a producer broadcast before persisting, an event could
    land after a spectator's replay snapshot but before its queue registration and be lost from
    BOTH paths — a gap. Both live producers honor this: the ``/start`` EXECUTION block and (as of
    Phase 2D Task 9) the evidence sink each append to the store first, then broadcast via the
    service's ``_safe_broadcast`` helper, so a spectator connected mid-run now sees evidence
    events live rather than only on reconnect/replay.

Heartbeat:
    Liveness/disconnect detection relies on the ASGI server's (uvicorn) built-in WebSocket
    ping/pong keepalive PLUS an active server-side receive loop that observes the disconnect
    frame — we do not trust raw TCP keepalive and do not add a custom application ping, keeping
    the read-only projection simple.

TRUST PATH note: this async shell MUST NOT import any LLM SDK (enforced by
``veridex.verifier.import_audit``), even though it is not in the scoring/proof trust path.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from veridex.competition.events import CompetitionEvent

if TYPE_CHECKING:  # keep the runtime import surface minimal (store reaches psycopg lazily)
    from veridex.store import Store

# Per-client queue capacity. A client that fails to keep up fills this and then has further
# events dropped — bounded memory per connection, never backpressure onto the producer.
_CLIENT_QUEUE_MAXSIZE = 1000


class _SupportsSendJson(Protocol):
    """Structural type for the send side of a websocket (eases unit-testing :func:`_forward_live`)."""

    async def send_json(self, data: Any) -> None:
        """Send one JSON-serializable payload to the client."""
        ...


class ArenaConnectionManager:
    """In-memory, single-instance fanout registry of per-client bounded queues.

    Holds, per ``competition_id``, the set of live spectator queues. Broadcasting is decoupled:
    each event is offered to every registered queue with a non-blocking ``put_nowait`` so one
    slow consumer can never stall the producer or its peers (REQ-214). This is deliberately
    process-local — clustering/Redis is a Phase-2B concern.
    """

    def __init__(self, max_queue_size: int = _CLIENT_QUEUE_MAXSIZE) -> None:
        """Initialise an empty registry.

        Args:
            max_queue_size: Bounded capacity for each per-client queue.
        """
        self._clients: dict[str, set[asyncio.Queue[CompetitionEvent]]] = {}
        self._overflow_signals: dict[asyncio.Queue[CompetitionEvent], asyncio.Event] = {}
        self._max_queue_size = max_queue_size

    def connect(self, competition_id: str) -> asyncio.Queue[CompetitionEvent]:
        """Register a fresh bounded queue for a new spectator and return it.

        Args:
            competition_id: The competition the spectator is subscribing to.

        Returns:
            A fresh bounded :class:`asyncio.Queue` registered for ``competition_id``.
        """
        queue: asyncio.Queue[CompetitionEvent] = asyncio.Queue(maxsize=self._max_queue_size)
        self._clients.setdefault(competition_id, set()).add(queue)
        self._overflow_signals[queue] = asyncio.Event()
        return queue

    def _overflow_signal(self, queue: asyncio.Queue[CompetitionEvent]) -> asyncio.Event:
        """Return the route-owned signal created alongside ``queue``."""
        return self._overflow_signals[queue]

    def disconnect(self, competition_id: str, queue: asyncio.Queue[CompetitionEvent]) -> None:
        """Deregister a spectator's queue, dropping the competition key when it empties.

        Idempotent: disconnecting an already-removed queue (or unknown competition) is a no-op,
        so a ``finally``-block teardown can never raise (REQ-214).

        Args:
            competition_id: The competition the spectator was subscribed to.
            queue: The queue returned by :meth:`connect`.
        """
        self._overflow_signals.pop(queue, None)
        queues = self._clients.get(competition_id)
        if queues is None:
            return
        queues.discard(queue)
        if not queues:
            del self._clients[competition_id]

    async def broadcast(self, competition_id: str, event: CompetitionEvent) -> None:
        """Fan ``event`` out to every registered client queue without blocking.

        Uses non-blocking ``put_nowait`` per client. Two failure modes are handled so neither the
        run loop nor any healthy peer is ever affected (REQ-2B-30, persist-before-broadcast):

        * **Full queue** (:class:`asyncio.QueueFull`): the slow client is DISCONNECTED (dropped
          from the registry) rather than silently skipping the event. Silently dropping a single
          event would leave a hidden mid-stream gap (the client keeps receiving later seqs and
          never knows it missed one); dropping the whole client instead ends its live stream, so
          the gap is observable and it must reconnect (replaying from ``since_seq``).
        * **Any other error** (e.g. a dead/raising client): swallowed per-client and that client
          is dropped — a raising broadcast NEVER aborts the run loop or skips persistence.

        ``async`` is part of the contract so the live producer can ``await`` the broadcast
        uniformly.

        Args:
            competition_id: The competition whose spectators should receive ``event``.
            event: The persisted canonical event to fan out.
        """
        # Snapshot the set so a concurrent connect/disconnect can't mutate it mid-iteration.
        for queue in list(self._clients.get(competition_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Wake the owning route before deregistration so it can close the actual socket.
                signal = self._overflow_signals.get(queue)
                if signal is not None:
                    signal.set()
                self.disconnect(competition_id, queue)
            except Exception:
                # Error isolation: a raising client must not abort the run/persistence — drop it.
                self.disconnect(competition_id, queue)


async def _forward_live(
    websocket: _SupportsSendJson,
    queue: asyncio.Queue[CompetitionEvent],
    last_sent_seq: int,
) -> None:
    """Forward live-broadcast events from ``queue`` to ``websocket``, deduping the seq boundary.

    Runs until cancelled (or until the client send side fails). Events with
    ``seq <= last_sent_seq`` were already delivered during replay and are skipped, guaranteeing a
    gapless, duplicate-free stream across the replay→live handoff.

    A send to a vanished client raises :class:`~fastapi.WebSocketDisconnect` (or a
    connection-closed :class:`RuntimeError`); the loop ends cleanly via ``return`` rather than
    propagating, so the route's awaited cleanup never has to absorb a send-side error.

    Args:
        websocket: The connected client's send side.
        queue: The per-client queue fed by :meth:`ArenaConnectionManager.broadcast`.
        last_sent_seq: The highest ``seq`` already sent to this client during replay.
    """
    while True:
        event = await queue.get()
        if event.seq <= last_sent_seq:
            continue  # already replayed — dedupe the boundary
        try:
            await websocket.send_json(event.model_dump(mode="json"))
        except (WebSocketDisconnect, RuntimeError):
            return  # client gone mid-send — end the forwarder cleanly
        last_sent_seq = event.seq


async def _close_on_overflow(websocket: WebSocket, overflow_signal: asyncio.Event) -> None:
    """Close ``websocket`` once its bounded fanout queue overflows."""
    await overflow_signal.wait()
    with contextlib.suppress(WebSocketDisconnect, RuntimeError):
        await websocket.close(code=1013, reason="client too slow; reconnect with since_seq")


async def _run_arena(
    websocket: WebSocket,
    *,
    store: Store,
    manager: ArenaConnectionManager,
    competition_id: str,
    since_seq: int,
) -> None:
    """Drive one spectator connection: accept → replay → live drain → guaranteed cleanup.

    Flow: accept → register queue (BEFORE replay, so concurrent broadcasts are captured) →
    replay persisted events with ``seq > since_seq`` → drain live events (seq-deduped) while a
    receive loop observes the disconnect → ALWAYS tear the queue down.

    Cleanup is unconditional: :meth:`ArenaConnectionManager.disconnect` runs in a nested
    ``finally`` so it executes even if awaiting the forwarder re-raises a non-``CancelledError``
    (e.g. a send-side error that ended the forwarder) — never leaking a queue (REQ-214).

    Args:
        websocket: The spectator connection (un-accepted; this coroutine accepts it).
        store: The async store providing the persisted event log for replay.
        manager: The connection manager owning per-client broadcast queues.
        competition_id: The competition to spectate.
        since_seq: Exclusive lower bound; only events with a strictly greater ``seq`` stream.
    """
    await websocket.accept()
    # Register FIRST so any event broadcast during replay lands in the queue (gapless seam).
    queue = manager.connect(competition_id)
    overflow_signal = manager._overflow_signal(queue)
    forwarder: asyncio.Task[None] | None = None
    overflow_closer = asyncio.create_task(_close_on_overflow(websocket, overflow_signal))
    owner = asyncio.current_task()
    assert owner is not None

    def wake_owner(_completed: asyncio.Future[None]) -> None:
        if not owner.done():
            owner.cancel()

    overflow_closer.add_done_callback(wake_owner)
    try:
        # 1. Replay the persisted tail (strict seq > since_seq), tracking the boundary.
        replayed = await store.list_competition_events(competition_id, since_seq=since_seq)
        last_sent_seq = since_seq
        for event in replayed:
            await websocket.send_json(event.model_dump(mode="json"))
            last_sent_seq = event.seq

        # 2. Hand off to the live drain (queue → socket, seq-deduped against replay).
        forwarder = asyncio.create_task(_forward_live(websocket, queue, last_sent_seq))
        forwarder.add_done_callback(wake_owner)

        # 3. Read-only liveness loop: consume inbound frames purely to detect disconnect.
        #    Inbound client data is intentionally ignored — it can never mutate state (REQ-213).
        #    The overflow closer runs independently so it can close a socket even while its live
        #    forwarder is blocked in send_json.
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
    except asyncio.CancelledError:
        # A terminal child wakes the receive supervisor by cancellation. Preserve true external
        # cancellation when neither child completed, and surface unexpected child exceptions.
        completed = [task for task in (forwarder, overflow_closer) if task is not None and task.done()]
        if not completed:
            raise
        for task in completed:
            await task
    except (WebSocketDisconnect, RuntimeError):
        pass  # client vanished mid-send — fall through to cleanup
    finally:
        # Cancel and join every child task so blocked send/receive operations cannot leak.
        try:
            tasks = [task for task in (forwarder, overflow_closer) if task is not None]
            for task in tasks:
                task.remove_done_callback(wake_owner)
            for task in tasks:
                if not task.done():
                    task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        finally:
            manager.disconnect(competition_id, queue)


def register_arena_routes(app: FastAPI, *, store: Store, manager: ArenaConnectionManager) -> None:
    """Register the read-only WebSocket arena route on ``app``.

    The route closes over ``store`` (for replay) and ``manager`` (for live fanout), mirroring how
    the REST endpoints close over the resolved store in ``create_app``.

    Args:
        app: The FastAPI application to mount the route on.
        store: The async store providing the persisted event log for replay.
        manager: The connection manager owning per-client broadcast queues.
    """

    @app.websocket("/competitions/{competition_id}/arena")
    async def arena(websocket: WebSocket, competition_id: str, since_seq: int = 0) -> None:
        """Stream the competition's canonical event log to a spectator (read-only).

        Args:
            websocket: The accepted spectator connection.
            competition_id: The competition to spectate.
            since_seq: Exclusive lower bound; only events with a strictly greater ``seq`` stream.
        """
        await _run_arena(
            websocket,
            store=store,
            manager=manager,
            competition_id=competition_id,
            since_seq=since_seq,
        )
