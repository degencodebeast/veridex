"""§3.5 TxLINE wiring — updates (not snapshot), closing reconstruction, honest validation labels.

TDD Iron Law: every test was written RED (ImportError / AttributeError — feature
missing) before the production module existed.

Behaviors under test
--------------------
- ``odds_updates_url`` / ``scores_updates_url``: ``/updates/{fid}`` — NOT the empty
  pre-match ``/snapshot`` (CON-040).
- ``odds_stream_url`` / ``scores_stream_url``: the live SSE endpoints.
- ``odds_validation_url``: keys on ``messageId`` (proofs exist only for sealed records).
- ``validation_method``: honest per-kind labels; raises on unknown (REQ-043).
- ``reconstruct_closing``: last pre-``InRunning`` update, or ``None``.
- ``fetch_odds_updates`` / ``validate_odds``: MOCK client (zero real network); list and
  dict-wrapped payloads both yield a list of updates.
- Lazy httpx: ``import veridex.ingest.txline_client`` must NOT pull httpx.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from veridex.ingest.txline_client import (
    fetch_odds_updates,
    fetch_scores_updates,
    fixtures_snapshot_url,
    odds_snapshot_url,
    odds_stream_url,
    odds_updates_url,
    odds_validation_url,
    reconstruct_closing,
    scores_stream_url,
    scores_updates_url,
    validate_odds,
    validation_method,
)

_BASE = "https://txline-dev.txodds.com/api"
_CREDS = ("jwt-1", "api-token-1")


def test_uses_updates_not_snapshot() -> None:
    url = odds_updates_url(_BASE, 999)
    assert url == "https://txline-dev.txodds.com/api/odds/updates/999"
    assert "snapshot" not in url  # CON-040


def test_scores_and_stream_urls() -> None:
    assert scores_updates_url(_BASE, 7) == "https://txline-dev.txodds.com/api/scores/updates/7"
    assert odds_stream_url(_BASE) == "https://txline-dev.txodds.com/api/odds/stream"
    assert scores_stream_url(_BASE) == "https://txline-dev.txodds.com/api/scores/stream"


def test_validation_url_keys_on_message_id() -> None:
    assert odds_validation_url(_BASE, "m-7") == "https://txline-dev.txodds.com/api/odds/validation?messageId=m-7"


def test_fixtures_snapshot_url_is_documented_discovery_path() -> None:
    # The DOCUMENTED discovery path (the bare `/api/fixtures` 404s).
    url = fixtures_snapshot_url(_BASE, 72, 20213)
    assert url == "https://txline-dev.txodds.com/api/fixtures/snapshot?competitionId=72&startEpochDay=20213"


def test_odds_snapshot_url_requires_as_of() -> None:
    # The bare snapshot is empty pre-match — asOf pins a point-in-time (CON-040).
    url = odds_snapshot_url(_BASE, 123, 1782518400)
    assert url == "https://txline-dev.txodds.com/api/odds/snapshot/123?asOf=1782518400"


def test_validation_labels_are_honest() -> None:
    assert validation_method("odds") == "validateOdds"
    assert validation_method("fixture") == "validateFixture"
    assert validation_method("fixture_batch") == "validateFixtureBatch"
    assert validation_method("stat") == "validateStat"
    assert validation_method("score") == "validateStat"
    with pytest.raises(ValueError):
        validation_method("bogus")  # never silently mislabel


def test_reconstruct_closing_picks_last_pre_inrunning() -> None:
    updates = [
        {"MessageId": "a", "InRunning": False, "Prices": [2000]},
        {"MessageId": "b", "InRunning": False, "Prices": [2100]},  # <- closing (last pre-InRunning)
        {"MessageId": "c", "InRunning": True, "Prices": [2500]},
    ]
    closing = reconstruct_closing(updates)
    assert closing is not None and closing["MessageId"] == "b"


def test_reconstruct_closing_none_when_all_in_running() -> None:
    assert reconstruct_closing([{"MessageId": "c", "InRunning": True}]) is None


def test_reconstruct_closing_none_on_empty() -> None:
    assert reconstruct_closing([]) is None


# ---------------------------------------------------------------------------
# Async shell — MOCK client only (zero real network)
# ---------------------------------------------------------------------------


class _FakeResp:
    """``payload=None`` simulates a cache-cold EMPTY 200 (body-less JSON body)."""

    def __init__(self, payload: Any) -> None:
        self._p = payload
        self.status_code = 200
        self.headers = {"content-type": "application/json"}
        body = b"" if payload is None else json.dumps(payload).encode()
        self.content = body
        self.text = body.decode()

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        if self._p is None:
            return json.loads("")  # raises JSONDecodeError, matching a real empty-body 200
        return self._p


class _FakeClient:
    """``sequence`` returns a different payload per successive ``.get`` call (last one sticks)."""

    def __init__(self, payload: Any = None, *, sequence: list[Any] | None = None) -> None:
        self._sequence = sequence if sequence is not None else [payload]
        self.calls: list[str] = []
        self.headers: list[dict[str, str]] = []

    async def get(self, url: str, headers: dict[str, str] | None = None, **kw: Any) -> _FakeResp:
        self.calls.append(url)
        self.headers.append(headers or {})
        idx = min(len(self.calls) - 1, len(self._sequence) - 1)
        return _FakeResp(self._sequence[idx])

    async def aclose(self) -> None:
        return None


async def test_fetch_odds_updates_list_payload() -> None:
    client = _FakeClient([{"MessageId": "a"}, {"MessageId": "b"}])
    updates = await fetch_odds_updates(123, base_url=_BASE, creds=_CREDS, client=client)
    assert [u["MessageId"] for u in updates] == ["a", "b"]
    assert client.calls == ["https://txline-dev.txodds.com/api/odds/updates/123"]
    # TxLINE auth headers threaded through (CON-041).
    assert client.headers[0]["Authorization"] == "Bearer jwt-1"
    assert client.headers[0]["X-Api-Token"] == "api-token-1"


async def test_fetch_odds_updates_dict_wrapped_payload() -> None:
    client = _FakeClient({"updates": [{"MessageId": "x"}]})
    updates = await fetch_odds_updates(1, base_url=_BASE, creds=_CREDS, client=client)
    assert [u["MessageId"] for u in updates] == ["x"]


async def test_fetch_scores_updates_list_payload() -> None:
    client = _FakeClient([{"Ts": 1}, {"Ts": 2}])
    updates = await fetch_scores_updates(456, base_url=_BASE, creds=_CREDS, client=client)
    assert [u["Ts"] for u in updates] == [1, 2]
    assert client.calls == ["https://txline-dev.txodds.com/api/scores/updates/456"]
    # TxLINE auth headers threaded through, mirroring fetch_odds_updates (CON-041).
    assert client.headers[0]["Authorization"] == "Bearer jwt-1"
    assert client.headers[0]["X-Api-Token"] == "api-token-1"


async def test_fetch_scores_updates_dict_wrapped_payload() -> None:
    client = _FakeClient({"updates": [{"Ts": 9}]})
    updates = await fetch_scores_updates(1, base_url=_BASE, creds=_CREDS, client=client)
    assert [u["Ts"] for u in updates] == [9]


# ---------------------------------------------------------------------------
# Cache-cold empty-body retry: the endpoint has a 5-minute cache; a cold cache
# returns a quick EMPTY 200 while it warms. Must retry, never leak a bare
# JSONDecodeError.
# ---------------------------------------------------------------------------


async def test_fetch_odds_updates_retries_through_cache_cold_empty() -> None:
    client = _FakeClient(sequence=[None, [{"MessageId": "a"}, {"MessageId": "b"}]])
    updates = await fetch_odds_updates(123, base_url=_BASE, creds=_CREDS, client=client, retry_delay=0.0)
    assert [u["MessageId"] for u in updates] == ["a", "b"]
    assert len(client.calls) == 2  # first call cache-cold empty, second call warm


async def test_fetch_odds_updates_raises_descriptive_on_persistent_empty() -> None:
    client = _FakeClient(None)  # always empty — cache never warms within retry budget
    with pytest.raises(RuntimeError) as exc_info:
        await fetch_odds_updates(999, base_url=_BASE, creds=_CREDS, client=client, retry_delay=0.0)
    message = str(exc_info.value)
    assert "999" in message
    assert "bytes" in message


async def test_fetch_scores_updates_retries_through_cache_cold_empty() -> None:
    client = _FakeClient(sequence=[None, [{"Ts": 1}, {"Ts": 2}]])
    updates = await fetch_scores_updates(456, base_url=_BASE, creds=_CREDS, client=client, retry_delay=0.0)
    assert [u["Ts"] for u in updates] == [1, 2]
    assert len(client.calls) == 2


async def test_validate_odds_keys_on_message_id() -> None:
    payload = {"odds": {}, "summary": {}, "subTreeProof": [], "mainTreeProof": []}
    client = _FakeClient(payload)
    result = await validate_odds("m-7", base_url=_BASE, creds=_CREDS, client=client)
    assert result == payload
    assert client.calls == ["https://txline-dev.txodds.com/api/odds/validation?messageId=m-7"]


def test_import_does_not_pull_httpx() -> None:
    src = Path("veridex/ingest/txline_client.py").read_text()
    tree = ast.parse(src)
    top_level_imports: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level_imports.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level_imports.add(node.module.split(".")[0])
    assert "httpx" not in top_level_imports
