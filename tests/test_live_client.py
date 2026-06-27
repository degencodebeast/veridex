"""B2 — Async live TxLINE SSE client tests (REQ-102 / AC-102).

TDD Iron Law: every test was written RED (ImportError / AttributeError — feature
missing) before the production module existed.

Behaviors under test
--------------------
- ``build_auth_headers``: emits ``Authorization: Bearer`` + ``X-Api-Token``.
- ``iter_sse_records``: drops heartbeats, blanks, ``event:`` lines, bad JSON;
  keeps valid ``data:`` dicts.
- ``marketstates_from_record_stream``: buffers per FixtureId; emits
  ``MarketState`` via B1 when buffer hits ``batch_size``; increments
  ``tick_seq`` per fixture; independent per-fixture state; correct market
  keys + de-vigged ``stable_prob_bps``.
- ``stream_marketstates``: yields ≥1 correct ``MarketState`` from a MOCK async
  client (zero real network); correct fixture_id propagated.
- Lazy httpx: ``import veridex.ingest.live_client`` must NOT pull httpx at
  module load (AST check on top-level imports).
- Import-audit: ``veridex/ingest/`` is LLM-SDK-free (CON-007).
- Creds-gated smoke: real devnet stream yields ≥1 ``MarketState`` (skipif).
"""

from __future__ import annotations

import ast
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from veridex.ingest.live_client import (
    build_auth_headers,
    iter_sse_records,
    marketstates_from_record_stream,
    stream_marketstates,
)

# ---------------------------------------------------------------------------
# Shared fixture factory — a minimal native TxLINE odds message
# ---------------------------------------------------------------------------

_BASE_TS_MS = 1_700_000_000_000


def _native_msg(
    fixture_id: int,
    *,
    ts_ms: int = _BASE_TS_MS,
    in_running: bool = False,
    pct: list[str] | None = None,
    prices: list[int] | None = None,
    names: list[str] | None = None,
) -> dict[str, Any]:
    """Minimal native TxLINE odds message for a single fixture."""
    return {
        "FixtureId": fixture_id,
        "Ts": ts_ms,
        "SuperOddsType": "1X2_PARTICIPANT_RESULT",
        "MarketPeriod": None,
        "MarketParameters": None,
        "InRunning": in_running,
        "PriceNames": names or ["home", "draw", "away"],
        "Prices": prices or [2000, 3000, 3500],
        "Pct": pct or ["45.000", "25.000", "30.000"],
    }


# ---------------------------------------------------------------------------
# B2-1: build_auth_headers — pure, testable without network
# ---------------------------------------------------------------------------


def test_build_auth_headers_includes_bearer_prefix() -> None:
    """Authorization header must start with 'Bearer '."""
    headers = build_auth_headers("myjwt", "mytoken")
    assert headers["Authorization"] == "Bearer myjwt"


def test_build_auth_headers_includes_api_token() -> None:
    """X-Api-Token header must equal the supplied token."""
    headers = build_auth_headers("myjwt", "mytoken")
    assert headers["X-Api-Token"] == "mytoken"


def test_build_auth_headers_returns_exactly_two_keys() -> None:
    """Only Authorization and X-Api-Token should be present."""
    headers = build_auth_headers("j", "t")
    assert set(headers.keys()) == {"Authorization", "X-Api-Token"}


# ---------------------------------------------------------------------------
# B2-2: iter_sse_records — pure, drop non-data lines
# ---------------------------------------------------------------------------


def test_iter_sse_records_drops_heartbeat() -> None:
    """Lines starting with ':' (SSE heartbeat / comment) are dropped."""
    lines = [": heartbeat", ": keep-alive"]
    assert list(iter_sse_records(lines)) == []


def test_iter_sse_records_drops_blank_lines() -> None:
    """Empty and whitespace-only lines are dropped."""
    lines = ["", "   ", "\t"]
    assert list(iter_sse_records(lines)) == []


def test_iter_sse_records_drops_event_prefix_lines() -> None:
    """Lines starting with 'event:' (SSE event type) are dropped."""
    lines = ["event: update", "event: odds"]
    assert list(iter_sse_records(lines)) == []


def test_iter_sse_records_drops_bad_json() -> None:
    """'data:' lines whose payload is not valid JSON are silently dropped."""
    lines = ["data: not-json", "data: {broken"]
    assert list(iter_sse_records(lines)) == []


def test_iter_sse_records_drops_non_dict_json() -> None:
    """'data:' lines whose JSON value is not a dict (e.g. a list) are dropped."""
    lines = ["data: [1, 2, 3]", "data: 42"]
    assert list(iter_sse_records(lines)) == []


def test_iter_sse_records_keeps_valid_data_dict() -> None:
    """A well-formed 'data:' line with a JSON dict is yielded."""
    payload = {"FixtureId": 99, "Ts": 1000}
    lines = [f"data: {json.dumps(payload)}"]
    records = list(iter_sse_records(lines))
    assert len(records) == 1
    assert records[0]["FixtureId"] == 99


def test_iter_sse_records_mixed_lines_keeps_only_valid() -> None:
    """Heartbeats, blanks, and valid data: lines in a batch — only data dicts pass."""
    msg = _native_msg(100)
    lines = [
        ": heartbeat",
        "",
        "event: update",
        f"data: {json.dumps(msg)}",
        "data: not-json",
        f"data: {json.dumps(_native_msg(200))}",
    ]
    records = list(iter_sse_records(lines))
    assert len(records) == 2
    assert {r["FixtureId"] for r in records} == {100, 200}


# ---------------------------------------------------------------------------
# B2-3: marketstates_from_record_stream — pure, offline
# ---------------------------------------------------------------------------


def test_marketstates_batch_size_1_one_record_one_state() -> None:
    """With batch_size=1 every record immediately yields one MarketState."""
    states = list(marketstates_from_record_stream([_native_msg(100)], batch_size=1))
    assert len(states) == 1
    assert states[0].fixture_id == 100


def test_marketstates_batch_size_2_one_record_zero_states() -> None:
    """With batch_size=2, a single record does NOT yet flush — buffer not full."""
    states = list(marketstates_from_record_stream([_native_msg(100)], batch_size=2))
    assert states == []


def test_marketstates_batch_size_2_two_records_one_state() -> None:
    """Two records for the same fixture with batch_size=2 flushes exactly once."""
    msgs = [_native_msg(100), _native_msg(100)]
    states = list(marketstates_from_record_stream(msgs, batch_size=2))
    assert len(states) == 1
    assert states[0].fixture_id == 100


def test_marketstates_two_fixtures_are_independent() -> None:
    """Buffers are per-FixtureId; one fixture reaching batch_size=1 doesn't affect another."""
    msgs = [
        _native_msg(100),  # → flushes immediately (batch_size=1)
        _native_msg(200),  # → flushes immediately
    ]
    states = list(marketstates_from_record_stream(msgs, batch_size=1))
    assert len(states) == 2
    assert {ms.fixture_id for ms in states} == {100, 200}


def test_marketstates_fixture_split_with_incomplete_batch() -> None:
    """Fixture 200 with only 1 record never flushes when batch_size=2."""
    msgs = [_native_msg(100), _native_msg(200), _native_msg(100)]
    states = list(marketstates_from_record_stream(msgs, batch_size=2))
    # fixture 100 → 2 records → 1 flush; fixture 200 → 1 record → 0 flushes
    assert len(states) == 1
    assert states[0].fixture_id == 100


def test_marketstates_tick_seq_increments_per_fixture() -> None:
    """tick_seq counts flushes per fixture, independently of other fixtures."""
    # 4 records for fixture 100 with batch_size=2 → 2 MarketStates (seq 0, 1)
    msgs = [_native_msg(100)] * 4
    states = list(marketstates_from_record_stream(msgs, batch_size=2))
    assert len(states) == 2
    assert states[0].tick_seq == 0
    assert states[1].tick_seq == 1


def test_marketstates_tick_seq_independent_across_fixtures() -> None:
    """tick_seq for fixture 200 starts at 0 regardless of fixture 100's counter."""
    msgs = [
        _native_msg(100),  # → seq 0 for fixture 100
        _native_msg(100),  # → seq 1 for fixture 100
        _native_msg(200),  # → seq 0 for fixture 200 (independent counter)
    ]
    states = list(marketstates_from_record_stream(msgs, batch_size=1))
    # states in order: (100,seq=0), (100,seq=1), (200,seq=0)
    states_100 = [ms for ms in states if ms.fixture_id == 100]
    states_200 = [ms for ms in states if ms.fixture_id == 200]
    # fixture 100: first snapshot starts at 0, second at 1
    assert states_100[0].tick_seq == 0
    assert states_100[1].tick_seq == 1
    # fixture 200's counter is independent — starts at 0
    assert states_200[0].tick_seq == 0


def test_marketstates_market_keys_present() -> None:
    """B1 normalizer is called; expected market key exists in the result."""
    states = list(marketstates_from_record_stream([_native_msg(100)], batch_size=1))
    assert "1X2_PARTICIPANT_RESULT||" in states[0].markets


def test_marketstates_devig_stable_prob_bps_sum_to_10000() -> None:
    """De-vigged stable_prob_bps sums to 10000 (Pct already de-margined via B1)."""
    states = list(marketstates_from_record_stream([_native_msg(100)], batch_size=1))
    probs = states[0].markets["1X2_PARTICIPANT_RESULT||"]["stable_prob_bps"]
    assert sum(probs.values()) == 10000


def test_marketstates_scores_empty() -> None:
    """Scores stream is out of scope for B2; scores dict must be empty."""
    states = list(marketstates_from_record_stream([_native_msg(100)], batch_size=1))
    assert states[0].scores == {}


# ---------------------------------------------------------------------------
# B2-4: stream_marketstates with a MOCK async client (zero real network)
# ---------------------------------------------------------------------------


class _MockResponse:
    """Fake httpx Response with ``aiter_lines()`` over canned lines."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def aiter_lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            yield line


class _MockStreamCtx:
    """Async context manager wrapping _MockResponse (mimics httpx stream ctx)."""

    def __init__(self, lines: list[str]) -> None:
        self._response = _MockResponse(lines)

    async def __aenter__(self) -> _MockResponse:
        return self._response

    async def __aexit__(self, *_: object) -> None:
        pass


class _MockClient:
    """Injected async HTTP client for offline tests.

    The ``stream()`` method returns a ``_MockStreamCtx`` that yields the
    pre-loaded canned SSE lines via ``aiter_lines()``.
    """

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def stream(self, method: str, url: str, headers: dict[str, str]) -> _MockStreamCtx:  # noqa: ARG002
        return _MockStreamCtx(self._lines)


def _make_canned_lines(*msgs: dict[str, Any]) -> list[str]:
    """Encode native msgs as SSE ``data:`` lines plus a trailing heartbeat."""
    lines = [f"data: {json.dumps(m)}" for m in msgs]
    lines.append(": heartbeat")  # should be dropped by iter_sse_records
    return lines


async def test_stream_marketstates_mock_yields_at_least_one_marketstate() -> None:
    """stream_marketstates must yield ≥1 MarketState from mock canned lines."""
    msg = _native_msg(111)
    mock = _MockClient(_make_canned_lines(msg))
    states: list[Any] = []
    async for ms in stream_marketstates(
        base_url="https://test.example.com/api",
        client=mock,
        creds=("jwt-test", "token-test"),
    ):
        states.append(ms)

    assert len(states) >= 1


async def test_stream_marketstates_mock_correct_fixture_id() -> None:
    """Yielded MarketState carries the fixture_id from the canned SSE message."""
    msg = _native_msg(42)
    mock = _MockClient(_make_canned_lines(msg))
    states: list[Any] = []
    async for ms in stream_marketstates(
        base_url="https://test.example.com/api",
        client=mock,
        creds=("jwt-test", "token-test"),
    ):
        states.append(ms)

    assert states[0].fixture_id == 42


async def test_stream_marketstates_mock_multiple_fixtures() -> None:
    """Two distinct fixtures in the canned stream both yield MarketStates."""
    lines = _make_canned_lines(_native_msg(10), _native_msg(20))
    mock = _MockClient(lines)
    fixture_ids: set[int] = set()
    async for ms in stream_marketstates(
        base_url="https://test.example.com/api",
        client=mock,
        creds=("jwt-test", "token-test"),
    ):
        fixture_ids.add(ms.fixture_id)

    assert fixture_ids == {10, 20}


async def test_stream_marketstates_mock_heartbeats_silently_dropped() -> None:
    """Heartbeat-only stream produces zero MarketStates (no crash)."""
    lines = [": heartbeat", "", "event: info", ": keep-alive"]
    mock = _MockClient(lines)
    states: list[Any] = []
    async for ms in stream_marketstates(
        base_url="https://test.example.com/api",
        client=mock,
        creds=("jwt-test", "token-test"),
    ):
        states.append(ms)

    assert states == []


async def test_stream_marketstates_mock_market_key_correct() -> None:
    """Yielded MarketState has the correct B1-normalised market key."""
    msg = _native_msg(99)
    mock = _MockClient(_make_canned_lines(msg))
    states: list[Any] = []
    async for ms in stream_marketstates(
        base_url="https://test.example.com/api",
        client=mock,
        creds=("jwt-test", "token-test"),
    ):
        states.append(ms)

    assert "1X2_PARTICIPANT_RESULT||" in states[0].markets


# ---------------------------------------------------------------------------
# B2-5: lazy-import guard — httpx must NOT appear at module top level
# ---------------------------------------------------------------------------

_LIVE_CLIENT_PATH = Path(__file__).parent.parent / "veridex" / "ingest" / "live_client.py"


def test_httpx_not_imported_at_module_top_level() -> None:
    """httpx must be imported lazily (inside a function), not at module load time.

    AST-walks the top-level body of live_client.py and asserts that no
    ``import httpx`` or ``from httpx import ...`` statement appears there.
    """
    src = _LIVE_CLIENT_PATH.read_text()
    tree = ast.parse(src, filename=str(_LIVE_CLIENT_PATH))

    for node in tree.body:  # only direct children of the module body
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "httpx", "httpx must not be imported at module top level"
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            assert node.module != "httpx", "httpx must not be imported at module top level via 'from'"


# ---------------------------------------------------------------------------
# B2-6: import-audit — veridex/ingest/ must be LLM-SDK-free (CON-007)
# ---------------------------------------------------------------------------


def test_ingest_import_audit_clean() -> None:
    """No LLM SDK imports (agno, anthropic, openai, etc.) in the ingest package."""
    import veridex.ingest as ingest_pkg
    from veridex.verifier.import_audit import assert_no_llm_imports

    ingest_dir = Path(ingest_pkg.__file__).parent
    assert_no_llm_imports(ingest_dir)


# ---------------------------------------------------------------------------
# B2-7: creds-gated live smoke — real devnet, ≥1 MarketState (default SKIP)
# ---------------------------------------------------------------------------

_HAS_CREDS = bool(os.environ.get("JWT") and os.environ.get("TXLINE_X_API_TOKEN"))


@pytest.mark.skipif(not _HAS_CREDS, reason="No TxLINE creds — set JWT + TXLINE_X_API_TOKEN in veridex/.env")
async def test_live_stream_smoke() -> None:
    """Creds-gated: the real devnet odds stream yields ≥1 MarketState.

    Requires ``JWT`` and ``TXLINE_X_API_TOKEN`` in the environment or
    ``veridex/.env``. Not part of the default offline suite.
    """
    import asyncio

    states: list[Any] = []

    async def _collect() -> None:
        async for ms in stream_marketstates():
            states.append(ms)
            break  # one is enough for the smoke gate

    await asyncio.wait_for(_collect(), timeout=30.0)
    assert len(states) >= 1, "Live devnet stream yielded no MarketState within 30 s"
