"""I-4 — Durable Agent-Ops RuntimeEvents: crash-safe WAL + owner-scoped cursor API + emit-wiring.

Five load-bearing trust properties (all offline unless a live Postgres is provided):

1. **SIGKILL-after-ack (crash durability + DB dedup):** events acked by ``enqueue`` but not yet
   flushed survive a process death — a fresh store REPLAYS the WAL beyond the durable cursor into
   the backing store, all N land, and re-replaying the SAME WAL a second time still yields exactly
   N rows (``UNIQUE(event_uuid)`` + ``ON CONFLICT DO NOTHING`` at the DB, never client-side alone).
2. **Reconnect with ``since`` cursor:** paging by the ``BIGSERIAL`` row id loses and duplicates
   nothing across the cursor boundary.
3. **DB-outage:** the async flusher retries, the sync ``enqueue`` (the decision-loop seam) NEVER
   blocks or raises, a ``degraded`` flag surfaces, and recovery drains the WAL + queue.
4. **Channel purity (SEC-003):** emitted/served events carry NO ``sequence_no`` / ``payload_hash``
   fields — the OPS channel can never masquerade as an evidence/competition event.
5. **Emit-wiring (anti-orphan regression):** a directional ``standalone_run`` over a fixture tape
   produces >=1 durable RuntimeEvent readable ONLY through the OWNER-SCOPED
   ``GET /agents/instances/{id}/runtime-events`` — a non-owner gets 403/404 (the "empty drawer" and
   the public-leak bugs this task prevents).

The InMemory path always runs; the live-Postgres portion of RED #1 SKIPs without ``DATABASE_URL``
(it is never faked) — the WAL replay + dedup logic is exercised deterministically against the
in-memory store, whose ``append_runtime_events`` mirrors the DB ``ON CONFLICT`` dedup by design.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from httpx import ASGITransport

from veridex.api.router import create_app
from veridex.config import Settings
from veridex.deploy.instance import AgentInstance
from veridex.ingest.marketstate import replay_marketstates
from veridex.runtime.runtime_events import RuntimeEventType, runtime_event
from veridex.runtime.runtime_store import DurableRuntimeEventStore
from veridex.store import InMemoryStore, RuntimeEventRow
from veridex.strategies.momentum import momentum_agent
from veridex_agent.run import standalone_run

_FIXTURE = str(Path(__file__).parent / "fixtures" / "wd2_momentum_replay.json")


# --- offline Privy ES256 token helpers (mirror the deploy-auth / instance-ownership suites) ------

_APP_ID = "test-privy-app-id"
_DID_ALICE = "did:privy:ALICE"
_DID_BOB = "did:privy:BOB"


def _make_keypair() -> tuple[str, str]:
    priv = ec.generate_private_key(ec.SECP256R1())
    priv_pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub_pem = (
        priv.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return priv_pem, pub_pem


_PRIV_PEM, _PUB_PEM = _make_keypair()


def _sign(*, sub: str) -> str:
    now = int(time.time())
    claims = {"sub": sub, "aud": _APP_ID, "iss": "privy.io", "iat": now, "exp": now + 3600, "sid": "sess-1"}
    return jwt.encode(claims, _PRIV_PEM, algorithm="ES256")


def _bearer(did: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_sign(sub=did)}"}


def _privy_settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        app_env="development",
        auth_mode="privy",
        privy_app_id=_APP_ID,
        privy_verification_key=_PUB_PEM,
    )


def _transport(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _make_instance(*, instance_id: str, agent_id: str, operator_id: str | None) -> AgentInstance:
    """A minimal persisted instance so the owner-scoped route can resolve instance -> owner + agent."""
    return AgentInstance(
        instance_id=instance_id,
        template_id="sharp-momentum-v2",
        agent_id=agent_id,
        submitted_config={"strategy": "momentum-sharp"},
        effective_config={"strategy": "momentum-sharp"},
        config_hash="cfg-hash",
        policy_hash="pol-hash",
        source_mode="replay",
        execution_mode="paper",
        run_id="run-x",
        operator_id=operator_id,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


class _FlakyStore(InMemoryStore):
    """An InMemoryStore whose ``append_runtime_events`` fails the first ``fail_times`` calls.

    Stands in for a transient DB outage — the flusher must retry, the decision-loop ``enqueue`` must
    never block/raise, and recovery must drain everything once the store heals.
    """

    def __init__(self, *, fail_times: int) -> None:
        super().__init__()
        self._remaining_failures = fail_times
        self.append_calls = 0

    async def append_runtime_events(self, rows: list[RuntimeEventRow]) -> int:
        self.append_calls += 1
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise RuntimeError("simulated postgres outage")
        return await super().append_runtime_events(rows)


def _events(agent_id: str, n: int) -> list[Any]:
    return [
        runtime_event(RuntimeEventType.ACTION_EMITTED, agent_id=agent_id, run_id="r", i=i, action="WAIT")
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# RED 1 — SIGKILL-after-ack: WAL replay after a crash + DB dedup on re-replay
# ---------------------------------------------------------------------------


async def test_sigkill_after_ack_replays_wal_and_dedups(tmp_path: Path) -> None:
    store = InMemoryStore()
    n = 12

    # A "writer process": every enqueue returns (acked to the OS), but NOTHING is flushed to the DB.
    writer = DurableRuntimeEventStore(store=store, wal_dir=tmp_path)
    for ev in _events("agent-k", n):
        writer.enqueue(ev)
    # SIGKILL: drop the writer WITHOUT flushing. The WAL on disk holds all N acked events; the
    # durable cursor never advanced (no commit happened).
    assert writer.pending == n
    del writer

    # "Restart": a fresh store over the SAME WAL replays the un-committed tail into the DB.
    restarted = DurableRuntimeEventStore(store=store, wal_dir=tmp_path)
    restarted.recover()
    await restarted.drain()
    rows = await store.list_runtime_events("agent-k")
    assert len(rows) == n, f"expected all {n} acked events replayed after crash, got {len(rows)}"

    # Re-replay the SAME WAL a second time -> DB dedup (ON CONFLICT DO NOTHING) keeps it at N.
    replayer = DurableRuntimeEventStore(store=store, wal_dir=tmp_path)
    await replayer.replay_all()
    await replayer.drain()
    rows_after = await store.list_runtime_events("agent-k")
    assert len(rows_after) == n, f"re-replay must dedup at the DB, got {len(rows_after)} != {n}"
    assert len({r.event_uuid for r in rows_after}) == n


@pytest.mark.skipif(
    not (os.getenv("DATABASE_URL")),
    reason="live-Postgres WAL replay/dedup: set DATABASE_URL (never faked)",
)
async def test_sigkill_after_ack_replays_wal_and_dedups_postgres(tmp_path: Path) -> None:
    import psycopg

    from veridex.store import PostgresStore

    dsn = os.environ["DATABASE_URL"]
    store = PostgresStore(dsn=dsn)
    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        await store.init_db(conn)

    agent = f"pg-agent-{int(time.time() * 1000)}"
    n = 8
    writer = DurableRuntimeEventStore(store=store, wal_dir=tmp_path)
    for ev in _events(agent, n):
        writer.enqueue(ev)
    del writer

    restarted = DurableRuntimeEventStore(store=store, wal_dir=tmp_path)
    restarted.recover()
    await restarted.drain()
    assert len(await store.list_runtime_events(agent)) == n

    replayer = DurableRuntimeEventStore(store=store, wal_dir=tmp_path)
    await replayer.replay_all()
    await replayer.drain()
    assert len(await store.list_runtime_events(agent)) == n


# ---------------------------------------------------------------------------
# RED 2 — reconnect with a `since` cursor: no loss, no duplication
# ---------------------------------------------------------------------------


async def test_reconnect_since_cursor_no_loss_no_dup(tmp_path: Path) -> None:
    store = InMemoryStore()
    dstore = DurableRuntimeEventStore(store=store, wal_dir=tmp_path)
    for ev in _events("agent-c", 10):
        dstore.enqueue(ev)
    await dstore.drain()

    first = await store.list_runtime_events("agent-c", since=0)
    assert len(first) == 10
    ids = [r.id for r in first]
    assert ids == sorted(ids) and len(set(ids)) == 10  # strictly increasing unique cursor

    # A client that has consumed through the 4th row reconnects with that id as `since`.
    cursor = ids[3]
    rest = await store.list_runtime_events("agent-c", since=cursor)
    assert [r.id for r in rest] == ids[4:]  # exactly the un-seen tail — no gap, no repeat
    assert cursor not in {r.id for r in rest}


# ---------------------------------------------------------------------------
# RED 3 — DB outage: flusher retries, enqueue never blocks, `degraded` surfaces, recovery drains
# ---------------------------------------------------------------------------


async def test_db_outage_degrades_without_blocking_then_recovers(tmp_path: Path) -> None:
    store = _FlakyStore(fail_times=3)
    dstore = DurableRuntimeEventStore(store=store, wal_dir=tmp_path, max_backoff_s=0.0)

    # The decision loop keeps enqueuing while the DB is down: every call returns, nothing raises.
    for ev in _events("agent-o", 5):
        dstore.enqueue(ev)  # must NOT block or raise even though the store is failing
    assert dstore.pending == 5  # nothing dropped — already durable in the WAL

    # A flush attempt during the outage fails -> degraded surfaces, nothing committed yet.
    committed = await dstore.flush_once()
    assert committed == 0
    assert dstore.degraded is True
    assert dstore.flush_failures >= 1
    assert len(await store.list_runtime_events("agent-o")) == 0

    # Recovery: the flusher retries with backoff and drains the WAL + queue once the store heals.
    await dstore.drain()
    assert store.append_calls >= 2  # retried, not given up after the first failure
    assert len(await store.list_runtime_events("agent-o")) == 5
    assert dstore.degraded is False  # visibly recovered
    assert dstore.pending == 0


# ---------------------------------------------------------------------------
# RED 4 — channel purity (SEC-003): no sequence_no / payload_hash anywhere
# ---------------------------------------------------------------------------


async def test_channel_purity_no_evidence_fields(tmp_path: Path) -> None:
    store = InMemoryStore()
    dstore = DurableRuntimeEventStore(store=store, wal_dir=tmp_path)
    dstore.enqueue(runtime_event(RuntimeEventType.RUN_STARTED, agent_id="agent-p", run_id="r"))
    await dstore.drain()

    rows = await store.list_runtime_events("agent-p")
    assert len(rows) == 1
    row = rows[0]
    # The persisted OPS row carries none of the three evidence/competition-event fields.
    for forbidden in ("sequence_no", "payload_hash", "evidence"):
        assert not hasattr(row, forbidden)
        assert forbidden not in row.payload
    assert row.channel == "OPS"


# ---------------------------------------------------------------------------
# RED 5 — emit-wiring (anti-orphan): standalone_run -> durable WAL -> owner-scoped API
# ---------------------------------------------------------------------------


async def test_standalone_run_emits_durable_events_readable_owner_scoped(tmp_path: Path) -> None:
    store = InMemoryStore()
    durable = DurableRuntimeEventStore(store=store, wal_dir=tmp_path)

    # A REAL directional run over a fixture tape, threading the durable sink (no network, no anchor).
    ticks = replay_marketstates(_FIXTURE)
    agent = momentum_agent("studio-agent")
    await standalone_run(
        ticks,
        agent,
        source_mode="replay",
        anchor_fn=None,
        runtime_event_sink=durable.sink(),
    )
    await durable.drain()

    produced = await store.list_runtime_events("studio-agent")
    assert len(produced) >= 1, "the sink is orphaned — standalone_run wrote NO durable RuntimeEvents"

    # The owner-scoped route resolves instance -> owner server-side and reads the durable feed.
    await store.persist_agent_instance(
        _make_instance(instance_id="inst_studio", agent_id="studio-agent", operator_id=_DID_ALICE)
    )
    app = create_app(store=store, settings=_privy_settings())
    async with _transport(app) as client:
        owner = await client.get("/agents/instances/inst_studio/runtime-events", headers=_bearer(_DID_ALICE))
        assert owner.status_code == 200, owner.text
        body = owner.json()
        assert len(body["events"]) >= 1
        ev0 = body["events"][0]
        assert ev0["channel"] == "OPS"
        assert "sequence_no" not in ev0 and "payload_hash" not in ev0  # channel purity over the wire

        # A non-owner is refused (fail-closed) — never reads another operator's ops telemetry.
        other = await client.get("/agents/instances/inst_studio/runtime-events", headers=_bearer(_DID_BOB))
        assert other.status_code in (403, 404), other.text
        assert "studio-agent" not in other.text  # no leak of the agent identity
