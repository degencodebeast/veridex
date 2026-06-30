"""WD-4 — judge-testable feed-health endpoint (REQ-053)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from veridex.api.router import create_app
from veridex.config import Settings
from veridex.store import InMemoryStore


@pytest.fixture
def client() -> TestClient:
    # No creds → offline-honest report (no secrets in repo/tests, COM-001).
    return TestClient(create_app(store=InMemoryStore(), settings=Settings(_env_file=None)))


def test_feed_health_offline_is_honest(client: TestClient) -> None:
    body = client.get("/feed/health").json()
    assert body["txline_configured"] is False
    assert body["connected"] is False
    assert body["source_mode"] == "replay"
    assert body["stale"] is False
    assert body["last_tick_ts"] is None
    # Additive extension keeps A's throughput view present + consistent (ws_live == connected).
    assert body["ws_live"] is False
    assert body["events_per_min"] is None


def test_feed_health_reports_configured_when_creds_present() -> None:
    settings = Settings(_env_file=None, JWT="x", TXLINE_X_API_TOKEN="y")
    client = TestClient(create_app(store=InMemoryStore(), settings=settings))
    body = client.get("/feed/health?source_mode=live").json()
    assert body["txline_configured"] is True
    assert body["source_mode"] == "live"
    # Live + configured → connected, and the throughput view agrees.
    assert body["connected"] is True
    assert body["ws_live"] is True
