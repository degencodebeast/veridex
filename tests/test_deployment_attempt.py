"""I-3 — Durable DeploymentAttempt + idempotency (the deploy-saga backbone).

A durable :class:`~veridex.deploy.attempt.DeploymentAttempt` row is persisted BEFORE any external
side effect (the AgentInstance write); the same idempotency key returns the SAME instance (never a
duplicate); a crash between "attempt persisted" and "instance created" reconciles on retry to ONE
instance via the recorded state — never a blind re-execute. Fail-closed: an unknown/unexpected
attempt state NEVER auto-retries a side effect.

All offline: an injected fake stream + fetch_updates + ``anchor_fn=None`` (ZERO network); the
default ``dev`` auth mode yields a fixed ``did:privy:dev`` principal (no bearer token needed).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from veridex.api.deploy import DeployDeps
from veridex.api.router import create_app
from veridex.deploy.attempt import AttemptStatus, DeploymentAttempt, DuplicateAttemptError
from veridex.deploy.preflight import DeployConfig
from veridex.ingest.feed_health import FeedHealthReport
from veridex.ingest.marketstate import MarketState
from veridex.store import InMemoryStore

# --- offline live fixtures (mirror the deploy-endpoint live launch path) ----------------------

_LIVE_KEY = "OU|FT|2.5"
_DEV_DID = "did:privy:dev"


def _live_market(over_bps: int) -> dict[str, Any]:
    return {"stable_prob_bps": {"over": over_bps}, "stable_price": {"over": 1.6, "under": 2.4}, "suspended": False}


def _live_ms(over_bps: int, *, tick_seq: int, ts: int) -> MarketState:
    return MarketState(
        fixture_id=1, tick_seq=tick_seq, ts=ts, phase=0, markets={_LIVE_KEY: _live_market(over_bps)}, scores={}
    )


async def _never_ending_stream(_config: DeployConfig) -> AsyncIterator[MarketState]:
    """Yield one pre-kickoff tick, then block forever — the run never seals on its own."""
    yield _live_ms(5000, tick_seq=0, ts=1000)
    await asyncio.Event().wait()  # blocks until the task is cancelled in cleanup
    yield _live_ms(5200, tick_seq=1, ts=1100)  # unreachable


async def _close(_fixture_id: int) -> list[dict[str, Any]]:
    return [
        {
            "FixtureId": 1,
            "Ts": 1_200_000,
            "InRunning": 0,
            "SuperOddsType": "OU",
            "MarketPeriod": "FT",
            "MarketParameters": "2.5",
            "PriceNames": ["over", "under"],
            "Prices": [1600, 2400],
            "Pct": [66, 34],
        }
    ]


def _healthy_live_feed() -> FeedHealthReport:
    return FeedHealthReport(
        source_mode="live",
        txline_configured=True,
        connected=True,
        last_tick_ts=1000,
        ticks_seen=5,
        fixture_id=1,
        staleness_s=1,
        stale=False,
    )


_VALID: dict[str, Any] = {
    "template_id": "sharp-momentum-v2",
    "agent_id": "studio-agent",
    "strategy": "momentum-sharp",
    "source_mode": "live",
    "execution_mode": "paper",
    "market_allowlist": [_LIVE_KEY],
    "venue_allowlist": ["fake"],
    "min_edge_bps": 0,
    "max_stake": 0.0,
    "window_id": "w1",
    "fixture_id": 1,
    "end_rule": "pre_match",
    "alpha": 0.4,
    "z_threshold": 2.5,
    "ph_delta": 0.01,
    "ph_lambda": 0.15,
    "cooldown_ticks": 3,
    "warmup_ticks": 10,
    "min_movements": 8,
    "lookback": 64,
    "scale_floor": 0.02,
    "persistence_logit": 0.06,
}


def _deps() -> DeployDeps:
    return DeployDeps(
        feed_report=_healthy_live_feed(),
        market_resolved=True,
        stream_factory=_never_ending_stream,
        fetch_updates=_close,
        anchor_fn=None,  # offline: no on-chain anchor
    )


def _transport(app: Any, *, raise_app_exceptions: bool = True) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions),
        base_url="http://test",
    )


async def _drain(app: Any) -> None:
    tasks = list(getattr(app.state, "deploy_background_tasks", set()))
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# RED 1 — attempt-before-side-effect ordering: the attempt row is persisted BEFORE the instance
# ---------------------------------------------------------------------------


class _OrderSpyStore(InMemoryStore):
    """Records the ORDER of the durable writes so a test can assert attempt-before-instance."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    async def persist_deployment_attempt(self, attempt: DeploymentAttempt) -> None:
        self.calls.append("attempt")
        await super().persist_deployment_attempt(attempt)

    async def persist_agent_instance(self, instance: Any) -> None:
        self.calls.append("instance")
        await super().persist_agent_instance(instance)


async def test_attempt_is_persisted_before_the_instance() -> None:
    store = _OrderSpyStore()
    app = create_app(store=store, deploy_deps=_deps())
    try:
        async with _transport(app) as client:
            resp = await client.post("/agents/deploy", json=_VALID, headers={"Idempotency-Key": "k-order"})
        assert resp.status_code == 200, resp.text
        # The DeploymentAttempt is written strictly BEFORE the AgentInstance (no side effect first).
        assert store.calls[:2] == ["attempt", "instance"]
        # And the attempt is a durable row keyed by (operator_id, idempotency_key), still PENDING.
        recorded = await store.get_deployment_attempt_by_key(_DEV_DID, "k-order")
        assert recorded is not None
        assert recorded.operator_id == _DEV_DID
        assert recorded.status is AttemptStatus.PENDING
    finally:
        await _drain(app)


# ---------------------------------------------------------------------------
# RED 2 — idempotency uniqueness: same key twice → SAME instance_id, no duplicate instance
# ---------------------------------------------------------------------------


async def test_same_key_returns_same_instance_and_no_duplicate() -> None:
    store = InMemoryStore()
    app = create_app(store=store, deploy_deps=_deps())
    try:
        async with _transport(app) as client:
            first = await client.post("/agents/deploy", json=_VALID, headers={"Idempotency-Key": "k-idem"})
            second = await client.post("/agents/deploy", json=_VALID, headers={"Idempotency-Key": "k-idem"})
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert first.json()["instance_id"] == second.json()["instance_id"]
        # Exactly one instance exists — the second call reconciled, it did not mint a duplicate.
        instances = await store.list_agent_instances()
        assert len(instances) == 1
    finally:
        await _drain(app)


# ---------------------------------------------------------------------------
# RED 3 — fingerprint conflict: same key + a DIFFERENT config_fingerprint → 409 (never reuse)
# ---------------------------------------------------------------------------


async def test_same_key_different_fingerprint_conflicts_409() -> None:
    store = InMemoryStore()
    app = create_app(store=store, deploy_deps=_deps())
    try:
        async with _transport(app) as client:
            first = await client.post("/agents/deploy", json=_VALID, headers={"Idempotency-Key": "k-fp"})
            assert first.status_code == 200, first.text
            # Same idempotency key, but a different config → a different config_fingerprint.
            variant = {**_VALID, "z_threshold": 2.6}
            second = await client.post("/agents/deploy", json=variant, headers={"Idempotency-Key": "k-fp"})
        assert second.status_code == 409, second.text
        # The conflict never silently reused or overwrote — still exactly one instance.
        assert len(await store.list_agent_instances()) == 1
    finally:
        await _drain(app)


# ---------------------------------------------------------------------------
# RED 4 — crash-between reconcile: attempt persisted, instance creation raises → a retry with the
# SAME key reconciles to ONE instance via the recorded attempt (never a blind re-execute)
# ---------------------------------------------------------------------------


class _FailOnceInstanceStore(InMemoryStore):
    """Raises on the FIRST ``persist_agent_instance`` (simulated crash), succeeds thereafter."""

    def __init__(self) -> None:
        super().__init__()
        self._fail_next = True

    async def persist_agent_instance(self, instance: Any) -> None:
        if self._fail_next:
            self._fail_next = False
            raise RuntimeError("simulated crash between attempt-persisted and instance-created")
        await super().persist_agent_instance(instance)


async def test_crash_between_attempt_and_instance_reconciles_to_one() -> None:
    store = _FailOnceInstanceStore()
    app = create_app(store=store, deploy_deps=_deps())
    try:
        async with _transport(app, raise_app_exceptions=False) as client:
            # First deploy crashes while creating the instance — no instance is created.
            crashed = await client.post("/agents/deploy", json=_VALID, headers={"Idempotency-Key": "k-crash"})
            assert crashed.status_code == 500, crashed.text
            assert len(await store.list_agent_instances()) == 0
            # But the durable attempt row survived the crash (the recovery anchor), still PENDING.
            recorded = await store.get_deployment_attempt_by_key(_DEV_DID, "k-crash")
            assert recorded is not None
            assert recorded.status is AttemptStatus.PENDING
            # The retry reconciles to ONE instance via the recorded attempt (no second side effect).
            retry = await client.post("/agents/deploy", json=_VALID, headers={"Idempotency-Key": "k-crash"})
            assert retry.status_code == 200, retry.text
        instances = await store.list_agent_instances()
        assert len(instances) == 1
        assert instances[0].instance_id == retry.json()["instance_id"]
    finally:
        await _drain(app)


# ---------------------------------------------------------------------------
# RED 5 — concurrent duplicate: two concurrent requests with the same key → EXACTLY ONE instance
# ---------------------------------------------------------------------------


class _YieldingAttemptStore(InMemoryStore):
    """Yields control inside ``persist_deployment_attempt`` so two gathered requests genuinely
    interleave and both reach the UNIQUE claim — exercising the concurrent duplicate-insert path."""

    async def persist_deployment_attempt(self, attempt: DeploymentAttempt) -> None:
        await asyncio.sleep(0)  # force a scheduling point BEFORE the uniqueness check
        await super().persist_deployment_attempt(attempt)


async def test_concurrent_same_key_creates_exactly_one_instance() -> None:
    store = _YieldingAttemptStore()
    app = create_app(store=store, deploy_deps=_deps())
    try:
        async with _transport(app) as client:
            first, second = await asyncio.gather(
                client.post("/agents/deploy", json=_VALID, headers={"Idempotency-Key": "k-conc"}),
                client.post("/agents/deploy", json=_VALID, headers={"Idempotency-Key": "k-conc"}),
            )
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        # Both concurrent callers resolve to the SAME instance, and exactly one was created.
        assert first.json()["instance_id"] == second.json()["instance_id"]
        assert len(await store.list_agent_instances()) == 1
    finally:
        await _drain(app)


# ---------------------------------------------------------------------------
# RED 6 — write-once / forward-only status: a backward/illegal transition raises; forward succeeds
# ---------------------------------------------------------------------------


def _attempt(status: AttemptStatus) -> DeploymentAttempt:
    return DeploymentAttempt(
        attempt_id="a1",
        operator_id=_DEV_DID,
        idempotency_key="k6",
        config_fingerprint="fp",
        status=status,
        created_at="2026-01-01T00:00:00+00:00",
        instance_id="inst_a1",
        external_id=None,
    )


def test_status_is_write_once_and_forward_only() -> None:
    pending = _attempt(AttemptStatus.PENDING)

    # A forward transition succeeds and is FUNCTIONAL (the original is left unchanged — write-once).
    advanced = pending.transition(AttemptStatus.INSTANCE_CREATED)
    assert advanced.status is AttemptStatus.INSTANCE_CREATED
    assert pending.status is AttemptStatus.PENDING

    # A backward transition raises (never rewind the saga).
    with pytest.raises(ValueError):
        advanced.transition(AttemptStatus.PENDING)

    # Re-writing the SAME status is not forward progress → also raises (write-once).
    with pytest.raises(ValueError):
        advanced.transition(AttemptStatus.INSTANCE_CREATED)


def test_all_reserved_wallet_states_are_defined() -> None:
    # II-5b RESERVED states are DEFINED now (enum members only — no wallet flow is built in I-3).
    for name in (
        "WALLET_REQUESTED",
        "WALLET_CREATE_UNCERTAIN",
        "WALLET_CREATED",
        "WALLET_BOUND",
        "BINDING_PERSIST_FAILED",
        "BINDING_PERSISTED",
    ):
        assert hasattr(AttemptStatus, name)


# ---------------------------------------------------------------------------
# Postgres-gated — the UNIQUE(operator_id, idempotency_key) DDL enforces one claim per key
# (SKIPs without DATABASE_URL + psycopg; an acceptable residual — never faked).
# ---------------------------------------------------------------------------


def _psycopg_available() -> bool:
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(
    not (os.getenv("DATABASE_URL") and _psycopg_available()),
    reason="Postgres UNIQUE-constraint check: set DATABASE_URL and install psycopg",
)
async def test_postgres_unique_constraint_rejects_duplicate_key() -> None:
    import psycopg

    from veridex.store import PostgresStore

    dsn = os.environ["DATABASE_URL"]
    store = PostgresStore(dsn=dsn)
    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        await store.init_db(conn)

    op = "did:privy:PGOWNER"
    key = "pg-unique-key"

    def _row(attempt_id: str, fingerprint: str) -> DeploymentAttempt:
        return DeploymentAttempt(
            attempt_id=attempt_id,
            operator_id=op,
            idempotency_key=key,
            config_fingerprint=fingerprint,
            status=AttemptStatus.PENDING,
            created_at="2026-01-01T00:00:00+00:00",
            instance_id=f"inst_{attempt_id}",
            external_id=None,
        )

    await store.persist_deployment_attempt(_row("pg1", "fp-a"))
    # A second INSERT with the SAME (operator_id, idempotency_key) violates UNIQUE → DuplicateAttemptError.
    with pytest.raises(DuplicateAttemptError):
        await store.persist_deployment_attempt(_row("pg2", "fp-b"))
    # The winning row round-trips intact through get_deployment_attempt_by_key.
    got = await store.get_deployment_attempt_by_key(op, key)
    assert got is not None
    assert got.attempt_id == "pg1"
    assert got.config_fingerprint == "fp-a"
