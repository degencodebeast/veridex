"""B2 — Async live TxLINE SSE client (REQ-102 / AC-102).

Async network shell: thin I/O wrapper around the TxLINE odds SSE endpoint.
Trust-path module: NO LLM SDK imports (CON-007).

``httpx`` is imported **lazily** inside :func:`stream_marketstates` only, so
``import veridex.ingest.live_client`` works without httpx installed or
imported at module load time (CON-010 — async-shell / sync-core split).

Pure functions
--------------
:func:`build_auth_headers`
    Composes TxLINE auth headers from ``(jwt, api_token)``.  Pure, testable.
:func:`iter_sse_records`
    Filters raw SSE lines to parsed ``data:`` dicts via
    :func:`~veridex.ingest.marketstate.parse_sse_line`.  Pure, testable with a
    list of strings — zero network dependency.
:func:`marketstates_from_record_stream`
    Buffers native records per ``FixtureId`` and emits
    :class:`~veridex.ingest.marketstate.MarketState` snapshots through the B1
    normaliser once each fixture's buffer reaches ``batch_size``.  Pure,
    testable with an in-memory iterable.

Async network shell
-------------------
:func:`stream_marketstates`
    Opens an ``httpx.AsyncClient`` streaming GET to
    ``{base_url}/odds/stream`` with TxLINE auth headers and yields normalised
    :class:`~veridex.ingest.marketstate.MarketState` objects.  Accepts an
    injected ``client`` for offline / mock testing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Iterator
from typing import Any

from veridex.ingest.marketstate import MarketState, parse_sse_line
from veridex.ingest.txline_normalize import marketstate_from_txline_odds

# ---------------------------------------------------------------------------
# Pure helpers — no network, no httpx, offline-testable
# ---------------------------------------------------------------------------


def build_auth_headers(jwt: str, token: str) -> dict[str, str]:
    """Compose TxLINE HTTP authentication headers.

    Args:
        jwt: IP-bound JWT bearer token.
        token: TxLINE API token value (``X-Api-Token``).

    Returns:
        Dict with ``Authorization: Bearer {jwt}`` and
        ``X-Api-Token: {token}`` entries.
    """
    return {"Authorization": f"Bearer {jwt}", "X-Api-Token": token}


def iter_sse_records(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    """Filter raw SSE text lines to parsed ``data:`` records.

    Passes each line through
    :func:`~veridex.ingest.marketstate.parse_sse_line`.  Lines that return
    ``None`` (heartbeats starting with ``:``, blank / whitespace lines,
    ``event:`` prefix lines, and malformed-JSON payloads) are silently
    discarded.

    Args:
        lines: Raw SSE text lines from an HTTP response body (e.g. produced
            by ``httpx.Response.aiter_lines()`` or a plain list for tests).

    Yields:
        Parsed ``dict`` records from well-formed ``data:`` SSE lines.
    """
    for line in lines:
        record = parse_sse_line(line)
        if record is not None:
            yield record


def marketstates_from_record_stream(
    records: Iterable[dict[str, Any]],
    *,
    batch_size: int = 1,
) -> Iterator[MarketState]:
    """Fold a stream of native TxLINE records into :class:`MarketState` snapshots.

    **Batching rule:** records are buffered independently per ``FixtureId``.
    When a fixture's buffer accumulates *batch_size* records, the entire
    buffer is flushed through
    :func:`~veridex.ingest.txline_normalize.marketstate_from_txline_odds` to
    produce one :class:`~veridex.ingest.marketstate.MarketState` snapshot; the
    buffer is then cleared and the ``tick_seq`` counter for that fixture is
    incremented by one.  Records for different fixtures interleave freely —
    each fixture's buffer and ``tick_seq`` evolve independently.

    Records missing a valid ``FixtureId`` are silently dropped.

    Args:
        records: Iterable of native TxLINE odds message dicts.  Each must
            carry a ``FixtureId`` key whose value is coercible to ``int``.
        batch_size: Number of records per fixture to accumulate before
            emitting one :class:`MarketState` snapshot.  Defaults to ``1``
            (emit on every record — no buffering beyond one message).

    Yields:
        Normalised :class:`~veridex.ingest.marketstate.MarketState` for each
        completed batch, one snapshot per fixture per batch boundary crossed.
    """
    fixture_buffers: dict[int, list[dict[str, Any]]] = {}
    tick_seqs: dict[int, int] = {}

    for record in records:
        try:
            fid = int(record["FixtureId"])
        except (KeyError, TypeError, ValueError):
            continue

        fixture_buffers.setdefault(fid, []).append(record)
        if len(fixture_buffers[fid]) >= batch_size:
            seq = tick_seqs.get(fid, 0)
            ms = marketstate_from_txline_odds(fixture_buffers[fid], tick_seq=seq)
            tick_seqs[fid] = seq + 1
            fixture_buffers[fid] = []
            yield ms


# ---------------------------------------------------------------------------
# Async network shell — httpx imported lazily (CON-010)
# ---------------------------------------------------------------------------


async def stream_marketstates(
    *,
    base_url: str | None = None,
    batch_size: int = 1,
    client: Any = None,
    creds: tuple[str, str] | None = None,
) -> AsyncIterator[MarketState]:
    """Stream normalised :class:`MarketState` snapshots from TxLINE odds SSE.

    Opens an async streaming GET to ``{base_url}/odds/stream`` with TxLINE
    authentication headers, pipes each received SSE line through
    :func:`iter_sse_records`, buffers native records per ``FixtureId``, and
    yields :class:`~veridex.ingest.marketstate.MarketState` snapshots once a
    fixture's buffer reaches *batch_size*.

    ``httpx`` is imported **lazily** inside this function so that
    ``import veridex.ingest.live_client`` works without the network library
    being present or imported at module load time (CON-010 async-shell /
    sync-core split).

    Args:
        base_url: TxLINE API base URL override (e.g.
            ``"https://txline-dev.txodds.com/api"``).  Defaults to
            :attr:`~veridex.config.Settings.txline_base_url`.
        batch_size: Records per fixture to accumulate before emitting one
            :class:`MarketState`.  Passed to
            :func:`marketstates_from_record_stream`.
        client: Optional injected async HTTP client for offline / mock tests.
            Must support ``client.stream(method, url, headers=...)`` as an
            async context manager whose ``__aenter__`` returns a response
            object with an ``aiter_lines()`` async iterator.  When ``None``
            (production path), an ``httpx.AsyncClient`` is created and closed
            automatically.
        creds: ``(jwt, api_token)`` tuple.  When ``None``, credentials are
            resolved from the environment via
            :func:`~veridex.config.require_txline`.

    Yields:
        :class:`~veridex.ingest.marketstate.MarketState` for each completed
        batch of records received from the live stream.

    Raises:
        ValueError: If ``creds`` is ``None`` and TxLINE credentials are
            absent from the environment.
    """
    from veridex.config import get_settings, require_txline

    settings = get_settings()
    jwt, token = creds if creds is not None else require_txline(settings)

    resolved_url = f"{base_url or settings.txline_base_url}/odds/stream"
    headers = build_auth_headers(jwt, token)

    # Lazy httpx import: keeps module load network-library-free (CON-010).
    _client: Any
    _own_client: bool
    if client is None:
        import httpx  # noqa: PLC0415

        # SSE is a long-lived idle-tolerant stream: keep a connect/write timeout but
        # DISABLE the read timeout (read=None), else httpx's default 5s read timeout
        # fires on any gap >5s between odds ticks (guaranteed pre-match, and common in a
        # live match's slow phases) → spurious disconnect/reconnect and lost FV data.
        _client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None))
        _own_client = True
    else:
        _client = client
        _own_client = False

    # Per-fixture buffer state for the async line-by-line loop.
    fixture_buffers: dict[int, list[dict[str, Any]]] = {}
    tick_seqs: dict[int, int] = {}

    try:
        async with _client.stream("GET", resolved_url, headers=headers) as resp:
            # Production only (real httpx client): surface the connect status so an operator
            # sees the stream opened (HTTP 200) vs an auth failure (401/403), instead of
            # inferring it from a silent "0 points received". Guarded by _own_client so
            # injected test clients (which may lack status_code) skip it.
            if _own_client:
                print(f"[fv] TxLINE stream connected: HTTP {resp.status_code}")
            async for line in resp.aiter_lines():
                record = parse_sse_line(line)
                if record is None:
                    continue
                try:
                    fid = int(record["FixtureId"])
                except (KeyError, TypeError, ValueError):
                    continue

                fixture_buffers.setdefault(fid, []).append(record)
                if len(fixture_buffers[fid]) >= batch_size:
                    seq = tick_seqs.get(fid, 0)
                    ms = marketstate_from_txline_odds(fixture_buffers[fid], tick_seq=seq)
                    tick_seqs[fid] = seq + 1
                    fixture_buffers[fid] = []
                    yield ms
    finally:
        if _own_client:
            await _client.aclose()
