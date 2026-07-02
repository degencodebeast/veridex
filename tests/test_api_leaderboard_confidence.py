"""WD-7 — the leaderboard API exposes CLV confidence the frontend binds to."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from veridex.api.router import create_app
from veridex.store import InMemoryStore


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(store=InMemoryStore()))


def test_demo_run_rows_carry_confidence(client: TestClient) -> None:
    rows = client.post("/demo/run").json()["leaderboard"]
    assert rows
    for row in rows:
        assert "valid_count" in row and isinstance(row["valid_count"], int)
        assert row["clv_confidence"] in {"low", "medium", "high"}
        assert isinstance(row["low_sample"], bool)


def test_leaderboard_endpoint_carries_confidence(client: TestClient) -> None:
    client.post("/demo/run")
    rows = client.get("/leaderboard").json()["rows"]
    assert rows
    for row in rows:
        assert "clv_confidence" in row and "valid_count" in row and "low_sample" in row
