"""WD-4 / III-3 — judge-testable feed-health endpoint (REQ-053).

III-3 honesty: the reported state reflects the ACTIVE stream's real last-seen (an observable
:class:`~veridex.ingest.feed_health.LiveFeedStatus` the live runner records into) — NOT credential
or param presence. Offline (no active stream) the surface is honest: replay mode is
``recorded_replay``, and live mode with no connected stream is ``disconnected`` even when creds are
present.
"""

from __future__ import annotations

import time

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
    # Replay mode is NEVER shown as live — it is the recorded-replay feed state.
    assert body["feed_state"] == "recorded_replay"
    # Additive extension keeps A's throughput view present + consistent (ws_live == connected).
    assert body["ws_live"] is False
    assert body["events_per_min"] is None


def test_feed_health_creds_present_but_no_active_stream_is_disconnected() -> None:
    # III-3: credential PRESENCE does not mean the feed is live. With no ACTIVE stream, live mode is
    # honestly DISCONNECTED (the old surface dishonestly reported connected==configured&&live).
    settings = Settings(_env_file=None, JWT="x", TXLINE_X_API_TOKEN="y")
    client = TestClient(create_app(store=InMemoryStore(), settings=settings))
    body = client.get("/feed/health?source_mode=live").json()
    assert body["txline_configured"] is True
    assert body["source_mode"] == "live"
    assert body["connected"] is False
    assert body["ws_live"] is False
    assert body["feed_state"] == "disconnected"


def test_feed_health_reflects_active_stream_last_seen() -> None:
    # III-3 core: the endpoint derives its state from the ACTIVE stream's last-seen. A live runner
    # records into ``app.state.live_feed_status``; a fresh odds record there -> the endpoint is LIVE.
    settings = Settings(_env_file=None, JWT="x", TXLINE_X_API_TOKEN="y")
    app = create_app(store=InMemoryStore(), settings=settings)
    status = app.state.live_feed_status
    status.mark_connected()
    status.record_odds_record(ts=int(time.time()), fixture_id=99)

    body = TestClient(app).get("/feed/health?source_mode=live").json()
    assert body["feed_state"] == "live"
    assert body["connected"] is True
    assert body["ws_live"] is True
    assert body["fixture_id"] == 99
    assert body["last_tick_ts"] is not None
