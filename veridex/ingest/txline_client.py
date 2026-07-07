"""§3.5 typed TxLINE off-chain client: odds/scores updates+stream, closing reconstruction, and
honest per-method validation against the txoracle Solana root.

CON-040: ``/odds/snapshot/{fid}`` is EMPTY pre-match — this client uses ``/odds/updates`` (full
movement history) + ``/odds/stream`` (SSE) and reconstructs the closing line from the last
pre-``InRunning`` update. REQ-043 honesty guard: odds validation is ``validateOdds``, never
``validateStat``. Proofs exist only for SEALED per-batch records — verify by ``messageId``.

Trust-path ``ingest/`` module: ``httpx`` lazy-imported inside async functions (CON-010);
credentials come from typed config only (CON-041), never repo/logs/events.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote

from veridex.ingest.live_client import build_auth_headers
from veridex.ingest.marketstate import parse_sse_line

_VALIDATION_METHODS: dict[str, str] = {
    "odds": "validateOdds",
    "fixture": "validateFixture",
    "fixture_batch": "validateFixtureBatch",
    "score": "validateStat",
    "stat": "validateStat",
}


def odds_updates_url(base: str, fid: int) -> str:
    """``/odds/updates/{fid}`` — full movement history (NOT the empty pre-match snapshot)."""
    return f"{base}/odds/updates/{fid}"


def odds_snapshot_url(base: str, fid: int, as_of: int) -> str:
    """``/odds/snapshot/{fid}?asOf=`` — point-in-time snapshot.

    The BARE ``/odds/snapshot/{fid}`` is empty pre-match (CON-040); ``asOf`` (epoch seconds)
    pins the snapshot to a specific instant so it can carry pre-match data.
    """
    return f"{base}/odds/snapshot/{fid}?asOf={as_of}"


def fixtures_snapshot_url(base: str, competition_id: int, start_epoch_day: int) -> str:
    """``/fixtures/snapshot?competitionId=&startEpochDay=`` — the DOCUMENTED discovery path.

    Discovers fixtures for a competition from ``start_epoch_day`` onward. The bare ``/fixtures``
    path 404s; this parameterized snapshot is the documented way to enumerate fixtures.
    """
    return f"{base}/fixtures/snapshot?competitionId={competition_id}&startEpochDay={start_epoch_day}"


def odds_stream_url(base: str) -> str:
    """``/odds/stream`` — live SSE odds movement."""
    return f"{base}/odds/stream"


def scores_updates_url(base: str, fid: int) -> str:
    """``/scores/updates/{fid}`` — full score/stat movement history."""
    return f"{base}/scores/updates/{fid}"


def scores_stream_url(base: str) -> str:
    """``/scores/stream`` — live SSE score/stat movement."""
    return f"{base}/scores/stream"


def odds_validation_url(base: str, message_id: str, ts: int) -> str:
    """``/odds/validation?messageId=&ts=`` — proof for one SEALED odds record.

    BOTH ``messageId`` and ``ts`` are REQUIRED by the TxLINE API (omitting ``ts`` → HTTP 404);
    ``message_id`` is URL-encoded because it contains ``:`` and ``-`` (e.g.
    ``1830828776:00003:001421-10021-stab``).
    """
    return f"{base}/odds/validation?messageId={quote(message_id)}&ts={ts}"


def validation_method(evidence_kind: str) -> str:
    """Map an evidence kind to its honest validation method label (REQ-043).

    Raises:
        ValueError: For an unknown kind — never silently mislabel (e.g. odds as a stat).
    """
    try:
        return _VALIDATION_METHODS[evidence_kind]
    except KeyError:
        raise ValueError(f"unknown evidence_kind for validation: {evidence_kind!r}") from None


def reconstruct_closing(updates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Reconstruct the closing line from the last pre-``InRunning`` odds update (CON-040).

    Args:
        updates: Odds updates in chronological order (oldest → newest).

    Returns:
        The last update with a falsy ``InRunning`` (the closing line), or ``None`` if the match
        was already in-running for every update.
    """
    closing: dict[str, Any] | None = None
    for update in updates:
        if not update.get("InRunning"):
            closing = update
    return closing


async def _get_updates_with_cache_retry(
    client: Any, url: str, headers: dict[str, str], *, retries: int, retry_delay: float
) -> list[dict[str, Any]]:
    """GET ``url``, retrying through a cache-cold EMPTY 200 (5-minute TxLINE update cache).

    Content-type decides how the body is parsed: ``/odds/updates`` returns JSON, but
    ``/scores/updates`` returns ``text/event-stream`` (SSE) — parsed line-by-line through the
    SAME :func:`~veridex.ingest.marketstate.parse_sse_line` normalizer live capture uses, never a
    second hand-rolled SSE parser.

    A cold cache returns a quick empty body while it warms: an empty JSON body makes ``.json()``
    raise ``JSONDecodeError`` (a cryptic symptom of the real cause), and an empty/heartbeat-only
    SSE body parses to zero records. Either empty outcome retries a few times before raising a
    descriptive error; never let a bare ``JSONDecodeError`` escape.
    """
    resp = None
    for attempt in range(1, retries + 1):
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        data: list[dict[str, Any]]
        if "event-stream" in content_type:
            data = [rec for line in resp.text.splitlines() if (rec := parse_sse_line(line)) is not None]
        else:
            try:
                parsed: Any = resp.json()
            except ValueError:
                parsed = None
            if parsed is None:
                data = []
            elif isinstance(parsed, list):
                data = list(parsed)
            else:
                data = list(parsed.get("updates", []))
        if data:
            return data
        if attempt < retries:
            await asyncio.sleep(retry_delay)
    content = resp.content if resp is not None else b""
    content_type = resp.headers.get("content-type", "") if resp is not None else ""
    status = resp.status_code if resp is not None else "?"
    raise RuntimeError(
        f"{url}: HTTP {status} {content_type} returned {len(content)} bytes but no parseable "
        f"JSON after {retries} attempts (cache cold? outside retention?)"
    )


async def fetch_odds_updates(
    fid: int,
    *,
    base_url: str | None = None,
    creds: tuple[str, str] | None = None,
    client: Any = None,
    retries: int = 3,
    retry_delay: float = 2.0,
) -> list[dict[str, Any]]:
    """GET ``/odds/updates/{fid}`` with TxLINE auth; returns the list of native odds updates.

    Retries through a cache-cold empty 200 (see :func:`_get_updates_with_cache_retry`); the
    own-client path uses a generous timeout since a warm payload can be tens of MB.
    """
    from veridex.config import get_settings, require_txline

    settings = get_settings()
    jwt, token = creds if creds is not None else require_txline(settings)
    base = base_url or settings.txline_base_url
    headers = build_auth_headers(jwt, token)
    own = client is None
    if own:
        import httpx  # noqa: PLC0415

        client = httpx.AsyncClient(timeout=httpx.Timeout(120.0))
    try:
        url = odds_updates_url(base, fid)
        return await _get_updates_with_cache_retry(client, url, headers, retries=retries, retry_delay=retry_delay)
    finally:
        if own:
            await client.aclose()


async def fetch_scores_updates(
    fid: int,
    *,
    base_url: str | None = None,
    creds: tuple[str, str] | None = None,
    client: Any = None,
    retries: int = 3,
    retry_delay: float = 2.0,
) -> list[dict[str, Any]]:
    """GET ``/scores/updates/{fid}`` with TxLINE auth; returns the list of native score updates.

    Mirrors :func:`fetch_odds_updates` exactly — same auth headers, lazy ``httpx``, generous
    own-client timeout, and cache-cold-empty retry — for the backfill scores leg.
    """
    from veridex.config import get_settings, require_txline

    settings = get_settings()
    jwt, token = creds if creds is not None else require_txline(settings)
    base = base_url or settings.txline_base_url
    headers = build_auth_headers(jwt, token)
    own = client is None
    if own:
        import httpx  # noqa: PLC0415

        client = httpx.AsyncClient(timeout=httpx.Timeout(120.0))
    try:
        url = scores_updates_url(base, fid)
        return await _get_updates_with_cache_retry(client, url, headers, retries=retries, retry_delay=retry_delay)
    finally:
        if own:
            await client.aclose()


async def validate_odds(
    message_id: str,
    ts: int,
    *,
    base_url: str | None = None,
    creds: tuple[str, str] | None = None,
    client: Any = None,
) -> dict[str, Any]:
    """GET ``/odds/validation?messageId=&ts=`` — the sealed-record proof to verify vs the txoracle root.

    BOTH ``message_id`` and ``ts`` are REQUIRED (they are the ``MessageId``/``Ts`` fields from
    ``/odds/updates`` responses); omitting ``ts`` yields HTTP 404.

    Returns the ``{odds, summary, subTreeProof, mainTreeProof}`` payload. Proofs exist only for
    SEALED per-batch records — callers verify by ``messageId``, never the freshest tick.
    """
    from veridex.config import get_settings, require_txline

    settings = get_settings()
    jwt, token = creds if creds is not None else require_txline(settings)
    base = base_url or settings.txline_base_url
    headers = build_auth_headers(jwt, token)
    own = client is None
    if own:
        import httpx  # noqa: PLC0415

        client = httpx.AsyncClient()
    try:
        resp = await client.get(odds_validation_url(base, message_id, ts), headers=headers)
        resp.raise_for_status()
        return dict(resp.json())
    finally:
        if own:
            await client.aclose()
