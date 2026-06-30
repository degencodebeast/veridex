"""CON-041 — guest JWT + on-chain subscribe + token activate (no secrets in repo/logs).

TDD Iron Law: every test was written RED (ImportError / AttributeError — feature
missing) before the production module existed.

Behaviors under test
--------------------
- ``guest_start_url`` / ``activate_url``: pure URL builders.
- ``guest_start``: POSTs ``/auth/guest/start`` (MOCK client) → guest JWT.
- ``activate_token``: POSTs ``/api/token/activate`` with Bearer JWT + subscribe sig
  → API token; the JWT is sent only as a header, never logged.
- ``acquire_live_credentials``: end-to-end → ``(jwt, api_token)`` with a MOCK
  auth client and a patched on-chain subscribe (zero real network / no real tx).
- ``require_txline_subscribe`` (config): returns ``(keypair_path, program_id)`` or
  raises when either is unset — secrets come from typed config only.
- Lazy imports: ``import veridex.ingest.txline_auth`` must NOT pull httpx/solders.
- Import-audit: ``veridex/ingest/`` stays LLM-SDK-free (CON-007).
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from veridex.ingest.txline_auth import (
    acquire_live_credentials,
    activate_token,
    activate_url,
    guest_start,
    guest_start_url,
)


def test_url_builders_are_pure() -> None:
    assert guest_start_url("https://txline-dev.txodds.com") == "https://txline-dev.txodds.com/auth/guest/start"
    assert activate_url("https://txline-dev.txodds.com") == "https://txline-dev.txodds.com/api/token/activate"


class _FakeResp:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._p = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._p


class _FakeClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._p = payload
        self.calls: list[str] = []
        self.kwargs: list[dict[str, Any]] = []

    async def post(self, url: str, **kw: Any) -> _FakeResp:
        self.calls.append(url)
        self.kwargs.append(kw)
        return _FakeResp(self._p)

    async def aclose(self) -> None:
        return None


async def test_guest_start_returns_jwt() -> None:
    client = _FakeClient({"jwt": "guest-jwt-123"})
    jwt = await guest_start(base_url="https://txline-dev.txodds.com", client=client)
    assert jwt == "guest-jwt-123"
    assert client.calls == ["https://txline-dev.txodds.com/auth/guest/start"]


async def test_activate_token_sends_bearer_and_sig() -> None:
    client = _FakeClient({"apiToken": "api-token-456"})
    token = await activate_token("guest-jwt-123", "sig-abc", base_url="https://txline-dev.txodds.com", client=client)
    assert token == "api-token-456"
    assert client.calls == ["https://txline-dev.txodds.com/api/token/activate"]
    # JWT travels only as the Authorization header; subscribe sig in the body.
    kw = client.kwargs[0]
    assert kw["headers"]["Authorization"] == "Bearer guest-jwt-123"
    assert kw["json"]["subscribeSignature"] == "sig-abc"


async def test_acquire_live_credentials_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    # One fake auth client services both POSTs; the second payload wins per call
    # order, so use a client that returns whatever the URL implies.
    class _DualClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def post(self, url: str, **kw: Any) -> _FakeResp:
            self.calls.append(url)
            if url.endswith("/auth/guest/start"):
                return _FakeResp({"jwt": "guest-jwt-123"})
            return _FakeResp({"apiToken": "api-token-456"})

        async def aclose(self) -> None:
            return None

    async def _fake_subscribe(jwt: str, **kw: Any) -> str:
        assert jwt == "guest-jwt-123"  # the activated flow threads the guest JWT
        return "sig-abc"

    monkeypatch.setattr("veridex.ingest.txline_auth.on_chain_subscribe", _fake_subscribe)
    client = _DualClient()
    jwt, api_token = await acquire_live_credentials(base_url="https://txline-dev.txodds.com", auth_client=client)
    assert (jwt, api_token) == ("guest-jwt-123", "api-token-456")
    assert client.calls == [
        "https://txline-dev.txodds.com/auth/guest/start",
        "https://txline-dev.txodds.com/api/token/activate",
    ]


def test_require_txline_subscribe_returns_creds_from_config() -> None:
    from veridex.config import Settings, require_txline_subscribe

    settings = Settings(
        _env_file=None,
        SOLANA_KEYPAIR_PATH="/tmp/kp.json",
        TXLINE_SUBSCRIBE_PROGRAM_ID="Prog1111111111111111111111111111111111111111",
    )
    assert require_txline_subscribe(settings) == (
        "/tmp/kp.json",
        "Prog1111111111111111111111111111111111111111",
    )


def test_require_txline_subscribe_raises_when_unset() -> None:
    from veridex.config import Settings, require_txline_subscribe

    settings = Settings(_env_file=None)
    with pytest.raises(ValueError, match="subscribe creds missing"):
        require_txline_subscribe(settings)


def test_subscribe_program_id_defaults_none() -> None:
    from veridex.config import Settings

    assert Settings(_env_file=None).txline_subscribe_program_id is None


def test_import_does_not_pull_httpx_or_solders() -> None:
    # Static guard: no top-level httpx/solders/solana import in the module source.
    src = Path("veridex/ingest/txline_auth.py").read_text()
    tree = ast.parse(src)
    top_level_imports: set[str] = set()
    for node in tree.body:  # only module-level statements
        if isinstance(node, ast.Import):
            top_level_imports.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level_imports.add(node.module.split(".")[0])
    assert "httpx" not in top_level_imports
    assert "solders" not in top_level_imports
    assert "solana" not in top_level_imports


def test_ingest_package_is_llm_free() -> None:
    from veridex.verifier.import_audit import assert_no_llm_imports

    assert_no_llm_imports(Path("veridex/ingest"))
