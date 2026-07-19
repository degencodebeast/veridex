"""I-5 — generic ``/healthz`` liveness + CORS boundary on the demo API surface (TDD).

These two additions are the ONLY router.py changes I-5 makes (no competition-write endpoint is
touched). CORS origins are read from the ``CORS_ORIGINS`` env (comma-separated); an origin that is
NOT configured must NOT be echoed an ``access-control-allow-origin`` — same-origin still works, but
a foreign origin is denied (fail-closed default when the env is unset ⇒ empty allow-list).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from veridex.api.router import create_app
from veridex.store import InMemoryStore


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A TestClient over a fresh in-process app with ONE configured CORS origin."""
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.test")
    app = create_app(store=InMemoryStore())
    return TestClient(app)


def test_healthz_returns_200(client: TestClient) -> None:
    """GET /healthz is a generic liveness probe → 200 (never gated on auth/DB)."""
    resp = client.get("/healthz")
    assert resp.status_code == 200


def test_cors_preflight_allows_configured_origin(client: TestClient) -> None:
    """A preflight from the configured origin is echoed the allow-origin header."""
    resp = client.options(
        "/healthz",
        headers={
            "Origin": "https://app.example.test",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.headers.get("access-control-allow-origin") == "https://app.example.test"


def test_cors_preflight_rejects_foreign_origin(client: TestClient) -> None:
    """A preflight from a NON-configured origin is NOT granted the allow-origin header."""
    resp = client.options(
        "/healthz",
        headers={
            "Origin": "https://evil.example.test",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.headers.get("access-control-allow-origin") != "https://evil.example.test"
