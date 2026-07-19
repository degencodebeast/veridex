"""Phase-2A Task 7 — WebSocket arena tests (TDD).

Two layers of coverage:

* **Route layer** (FastAPI ``TestClient`` + ``InMemoryStore``): the read-only spectator
  endpoint ``GET (ws) /competitions/{id}/arena`` — replay parity with the REST event log,
  multi-client isolation, read-only enforcement, and ``since_seq`` tail/empty behaviour.
* **Manager layer** (no ``TestClient``): the decoupled-fanout machinery the synchronous-start
  REST flow can't exercise — gapless replay→live handoff with seq dedupe, per-client bounded
  backpressure (drop only the slow client), and leak-free disconnect.

Fully offline: no network, no LLM, no DB. The competition is driven to FINALIZED via the
existing REST endpoints, so the entire canonical log is persisted before any spectator connects.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from fastapi.testclient import TestClient

from veridex.api.router import create_app
from veridex.api.ws import ArenaConnectionManager, _forward_live, _run_arena
from veridex.competition.events import CompetitionEvent, EventType
from veridex.store import InMemoryStore

# ---------------------------------------------------------------------------
# Shared fixtures and helpers
# ---------------------------------------------------------------------------

_COMPETITION_CONFIG = {
    "competition_type": "replay_arena",
    "source_mode": "replay",
    "execution_mode": "paper",
    "market_scope": "WC:TEST",
    "roster_size": 2,
}

_AGENT_ENTRY_A = {
    "agent_id": "agent-alpha",
    "owner": "team-a",
    "strategy": "baseline",
    "model": None,
    "proof_mode": "reproducible",
}

_AGENT_ENTRY_B = {
    "agent_id": "agent-beta",
    "owner": "team-b",
    "strategy": "contrarian",
    "model": None,
    "proof_mode": "reproducible",
}


def _fully_run_client() -> tuple[TestClient, str]:
    """Create a client, run a full competition to FINALIZED, return (client, competition_id)."""
    store = InMemoryStore()
    client = TestClient(create_app(store=store))
    comp_id = client.post("/competitions", json=_COMPETITION_CONFIG).json()["competition_id"]
    client.post(f"/competitions/{comp_id}/agents", json=_AGENT_ENTRY_A)
    client.post(f"/competitions/{comp_id}/agents", json=_AGENT_ENTRY_B)
    resp = client.post(f"/competitions/{comp_id}/start")
    assert resp.status_code == 200, resp.text
    return client, comp_id


def _make_event(competition_id: str, seq: int) -> CompetitionEvent:
    """Build a minimal, valid synthetic event for manager-level unit tests."""
    return CompetitionEvent(
        competition_id=competition_id,
        run_id="r_test",
        seq=seq,
        event_type=EventType.MARKET_TICK,
        event_ts=0,
        evidence=False,
        source_sequence_no=None,
        payload={"seq": seq},
        payload_hash="0" * 64,
    )


class _FakeWebSocket:
    """Minimal websocket stub recording everything sent via ``send_json``."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)


class _RaisingArenaWebSocket:
    """Fake WebSocket whose first LIVE ``send_json`` raises — exercises route cleanup robustness.

    ``accept`` and ``receive`` cooperate so the route runs its full flow: replay (empty) →
    forwarder → liveness loop. The forwarder's send raises; ``receive`` then reports a disconnect
    so the liveness loop ends and the cleanup path executes.
    """

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.accepted = False
        self._send_attempted = asyncio.Event()

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, data: dict) -> None:
        self._send_attempted.set()
        raise self._exc

    async def receive(self) -> dict:
        # Block until the (failing) live send has been attempted, then report disconnect so the
        # route's read-only liveness loop ends and its guaranteed cleanup runs.
        await self._send_attempted.wait()
        await asyncio.sleep(0)  # let the forwarder finish failing before teardown
        return {"type": "websocket.disconnect"}


class _ControlledArenaWebSocket:
    """Route-level WebSocket fake that can become slow after a bounded number of sends."""

    def __init__(self, *, block_after: int | None = None, disconnect_after: int | None = None) -> None:
        self.sent: list[dict] = []
        self.accepted = False
        self.closed = asyncio.Event()
        self.send_blocked = asyncio.Event()
        self._block_after = block_after
        self._disconnect_after = disconnect_after
        self._disconnect_ready = asyncio.Event()
        self._never_unblock = asyncio.Event()

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, data: dict) -> None:
        if self._block_after is not None and len(self.sent) >= self._block_after:
            self.send_blocked.set()
            await self._never_unblock.wait()
        self.sent.append(data)
        if self._disconnect_after is not None and len(self.sent) >= self._disconnect_after:
            self._disconnect_ready.set()

    async def receive(self) -> dict:
        disconnects = [asyncio.create_task(self.closed.wait())]
        if self._disconnect_after is not None:
            disconnects.append(asyncio.create_task(self._disconnect_ready.wait()))
        try:
            done, _ = await asyncio.wait(disconnects, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        finally:
            for task in disconnects:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*disconnects, return_exceptions=True)
        return {"type": "websocket.disconnect"}

    async def close(self, *, code: int = 1000, reason: str | None = None) -> None:
        del code, reason
        self.closed.set()


async def _wait_until(predicate, *, timeout: float = 1.0) -> None:
    """Cooperatively yield until ``predicate()`` is true or ``timeout`` elapses."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met within timeout")
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Route layer — replay parity + lifecycle
# ---------------------------------------------------------------------------


def test_ws_replay_matches_rest() -> None:
    """AC-204: the WS arena replay is byte-faithful to the REST event log (seq parity)."""
    client, cid = _fully_run_client()
    rest = client.get(f"/competitions/{cid}/events?since_seq=0").json()
    assert len(rest) > 0

    with client.websocket_connect(f"/competitions/{cid}/arena?since_seq=0") as ws:
        ws_events = [ws.receive_json() for _ in range(len(rest))]

    assert [e["seq"] for e in ws_events] == [e["seq"] for e in rest]


def test_ws_two_clients_one_disconnects() -> None:
    """AC-206: one client disconnecting does not affect another client's replay."""
    client, cid = _fully_run_client()
    rest = client.get(f"/competitions/{cid}/events?since_seq=0").json()
    n = len(rest)
    assert n > 1

    with client.websocket_connect(f"/competitions/{cid}/arena?since_seq=0") as ws_survivor:
        with client.websocket_connect(f"/competitions/{cid}/arena?since_seq=0") as ws_leaver:
            ws_leaver.receive_json()  # leaver consumes at least one event
        # ws_leaver disconnected here; the survivor must still get its full replay.
        survivor_events = [ws_survivor.receive_json() for _ in range(n)]

    assert [e["seq"] for e in survivor_events] == [e["seq"] for e in rest]


def test_ws_is_read_only() -> None:
    """AC-207: a client send cannot mutate competition state (REQ-213)."""
    client, cid = _fully_run_client()
    before = client.get(f"/competitions/{cid}").json()
    rest = client.get(f"/competitions/{cid}/events?since_seq=0").json()

    with client.websocket_connect(f"/competitions/{cid}/arena?since_seq=0") as ws:
        for _ in range(len(rest)):
            ws.receive_json()
        # Attempt a mutation over the read-only channel — server must ignore it.
        ws.send_json({"type": "agent_action", "evil": True, "seq": 999})

    after = client.get(f"/competitions/{cid}").json()
    assert after == before
    # The event log is unchanged — no spurious event was appended.
    assert client.get(f"/competitions/{cid}/events?since_seq=0").json() == rest


def test_ws_since_seq_replay_tail() -> None:
    """since_seq>0 streams only seq>since_seq; since_seq beyond max closes cleanly with nothing."""
    client, cid = _fully_run_client()
    full = client.get(f"/competitions/{cid}/events?since_seq=0").json()
    assert len(full) > 2

    mid = full[len(full) // 2]["seq"]
    rest_tail = client.get(f"/competitions/{cid}/events?since_seq={mid}").json()
    assert len(rest_tail) > 0

    with client.websocket_connect(f"/competitions/{cid}/arena?since_seq={mid}") as ws:
        ws_tail = [ws.receive_json() for _ in range(len(rest_tail))]
    assert [e["seq"] for e in ws_tail] == [e["seq"] for e in rest_tail]
    assert all(e["seq"] > mid for e in ws_tail)

    # since_seq beyond the max persisted seq → empty replay, clean close (no error).
    beyond = full[-1]["seq"] + 100
    with client.websocket_connect(f"/competitions/{cid}/arena?since_seq={beyond}"):
        pass  # connect + immediate close; server must not raise


# ---------------------------------------------------------------------------
# Manager layer — decoupled fanout machinery (the 2B seam)
# ---------------------------------------------------------------------------


async def test_manager_gapless_handoff_no_gap_no_dupe() -> None:
    """Replay→live handoff dedupes at the seq boundary: no gap, no duplicate, ordered."""
    manager = ArenaConnectionManager()
    cid = "c_gapless"
    queue = manager.connect(cid)

    # Replay already delivered seq 0..3 (last_sent_seq=3). A live broadcast overlaps the
    # boundary with seqs 2,3 (already replayed → must be dropped) then 4,5 (new).
    for seq in (2, 3, 4, 5):
        await manager.broadcast(cid, _make_event(cid, seq))

    fake = _FakeWebSocket()
    forwarder = asyncio.create_task(_forward_live(fake, queue, last_sent_seq=3))
    await _wait_until(lambda: len(fake.sent) >= 2)
    forwarder.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await forwarder

    assert [e["seq"] for e in fake.sent] == [4, 5]


async def test_manager_backpressure_drops_for_full_client_only() -> None:
    """A full/slow client's queue dropping events never raises and never affects a healthy one."""
    manager = ArenaConnectionManager(max_queue_size=2)
    cid = "c_backpressure"
    slow = manager.connect(cid)  # never drained → fills and drops
    healthy = manager.connect(cid)  # drained each tick → keeps up

    received_by_healthy: list[int] = []
    for seq in range(5):  # 5 > maxsize 2
        await manager.broadcast(cid, _make_event(cid, seq))  # must NOT raise
        received_by_healthy.append((await healthy.get()).seq)

    assert received_by_healthy == [0, 1, 2, 3, 4]  # healthy client unaffected
    assert slow.qsize() == 2  # slow client capped — surplus dropped, no exception


async def test_route_overflow_closes_slow_socket_without_affecting_healthy_client() -> None:
    """Overflow is observable on the slow socket while a healthy route receives every event."""
    store = InMemoryStore()
    manager = ArenaConnectionManager(max_queue_size=1)
    cid = "c_overflow_close"
    slow = _ControlledArenaWebSocket(block_after=0)
    healthy = _ControlledArenaWebSocket(disconnect_after=3)
    slow_task = asyncio.create_task(_run_arena(slow, store=store, manager=manager, competition_id=cid, since_seq=0))
    healthy_task = asyncio.create_task(
        _run_arena(healthy, store=store, manager=manager, competition_id=cid, since_seq=0)
    )

    try:
        await _wait_until(lambda: len(manager._clients.get(cid, ())) == 2)
        first = _make_event(cid, 1)
        await store.append_competition_events(cid, [first])
        await manager.broadcast(cid, first)
        await asyncio.wait_for(slow.send_blocked.wait(), timeout=1)
        await _wait_until(lambda: [event["seq"] for event in healthy.sent] == [1])

        for seq in (2, 3):
            event = _make_event(cid, seq)
            await store.append_competition_events(cid, [event])
            await manager.broadcast(cid, event)
            await _wait_until(lambda expected=seq: healthy.sent[-1]["seq"] == expected)

        await asyncio.wait_for(healthy_task, timeout=1)
        await asyncio.wait_for(slow.closed.wait(), timeout=1)
        await asyncio.wait_for(slow_task, timeout=1)
    finally:
        for task in (slow_task, healthy_task):
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    assert [event["seq"] for event in healthy.sent] == [1, 2, 3]
    assert cid not in manager._clients


async def test_overflow_reconnect_since_seq_replays_exact_missing_tail() -> None:
    """A client closed on overflow can replay seq > last_seen with no gap or duplicate."""
    store = InMemoryStore()
    manager = ArenaConnectionManager(max_queue_size=1)
    cid = "c_overflow_reconnect"
    first_socket = _ControlledArenaWebSocket(block_after=1)
    first_task = asyncio.create_task(
        _run_arena(first_socket, store=store, manager=manager, competition_id=cid, since_seq=0)
    )

    try:
        await _wait_until(lambda: cid in manager._clients)
        first = _make_event(cid, 1)
        await store.append_competition_events(cid, [first])
        await manager.broadcast(cid, first)
        await _wait_until(lambda: [event["seq"] for event in first_socket.sent] == [1])

        second = _make_event(cid, 2)
        await store.append_competition_events(cid, [second])
        await manager.broadcast(cid, second)
        await asyncio.wait_for(first_socket.send_blocked.wait(), timeout=1)

        for seq in (3, 4):
            event = _make_event(cid, seq)
            await store.append_competition_events(cid, [event])
            await manager.broadcast(cid, event)

        await asyncio.wait_for(first_socket.closed.wait(), timeout=1)
        await asyncio.wait_for(first_task, timeout=1)
    finally:
        if not first_task.done():
            first_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await first_task

    last_seen = first_socket.sent[-1]["seq"]
    reconnect = _ControlledArenaWebSocket(disconnect_after=3)
    await asyncio.wait_for(
        _run_arena(
            reconnect,
            store=store,
            manager=manager,
            competition_id=cid,
            since_seq=last_seen,
        ),
        timeout=1,
    )

    replayed = [event["seq"] for event in reconnect.sent]
    assert last_seen == 1
    assert replayed == [2, 3, 4]
    assert len(replayed) == len(set(replayed))


def test_manager_disconnect_cleans_up() -> None:
    """After disconnect the queue/registry entry is gone; an emptied competition key is dropped."""
    manager = ArenaConnectionManager()
    cid = "c_cleanup"
    queue = manager.connect(cid)
    assert cid in manager._clients
    assert queue in manager._clients[cid]

    manager.disconnect(cid, queue)
    assert cid not in manager._clients  # empty set → key removed, no leak

    # Disconnecting again is a harmless no-op (idempotent teardown).
    manager.disconnect(cid, queue)


async def test_route_cleanup_runs_even_if_forwarder_raises() -> None:
    """Unconditional teardown (REQ-214): disconnect runs even when the forwarder raises — no leak."""
    store = InMemoryStore()
    manager = ArenaConnectionManager()
    cid = "c_forwarder_raise"
    ws = _RaisingArenaWebSocket(ValueError("send boom"))

    # since_seq=0 + unknown competition → empty replay; the forwarder then drains the live queue.
    task = asyncio.create_task(_run_arena(ws, store=store, manager=manager, competition_id=cid, since_seq=0))
    # Once the connection has registered its queue, broadcast a live event (seq>0) that drives the
    # forwarder into the raising send.
    await _wait_until(lambda: cid in manager._clients)
    await manager.broadcast(cid, _make_event(cid, seq=1))

    # The forwarder raised a non-CancelledError; the nested finally still ran disconnect BEFORE
    # the exception propagated out of _run_arena.
    with pytest.raises(ValueError, match="send boom"):
        await task

    assert cid not in manager._clients  # queue removed despite the raise — no leak
    assert ws.accepted is True
