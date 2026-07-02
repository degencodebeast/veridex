"""WD-4 — live-feed health signals + closing-line reconstruction (REQ-053 / CON-040).

Pure, dependency-light helpers the UI binds to in order to *show* that the TxLINE feed is real:
a feed-health report (source mode, connection, staleness) and the CON-040 closing-line
reconstruction (the closing line is the last pre-``InRunning`` update — the pre-match
``/odds/snapshot`` is empty, so the build reconstructs it from ``/odds/updates``).

Feed-health is read-only OPERATIONAL TELEMETRY: it is never scored, never part of
``evidence_hash``, never a proof check, and never a leaderboard input (same doctrine as
RuntimeEvent/runtime logs — telemetry stays outside the canonical sealed evidence).

TRUST PATH (CON-004): no LLM SDK imports. No network here — the async SSE shell lives in
``veridex.ingest.live_client``; this module shapes signals computed from it.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

#: Default seconds since the last tick before the feed is considered stale.
DEFAULT_STALE_AFTER_S: int = 30


class FeedHealthReport(BaseModel):
    """Health of the live/replay TxLINE feed (WD-4 — the UI's feed-health panel binds here).

    Attributes:
        source_mode: ``"live"`` or ``"replay"`` (the active ingestion mode).
        txline_configured: Whether TxLINE credentials are present (never the secrets themselves).
        connected: Whether the stream is currently connected (best-effort).
        last_tick_ts: Unix seconds of the most recent tick, or ``None`` when none seen yet.
        ticks_seen: Count of ticks ingested so far.
        fixture_id: The fixture currently being followed, or ``None``.
        staleness_s: Seconds since ``last_tick_ts`` (``None`` when no tick seen yet).
        stale: Whether ``staleness_s`` exceeds the staleness budget.
    """

    source_mode: str
    txline_configured: bool
    connected: bool
    last_tick_ts: int | None
    ticks_seen: int
    fixture_id: int | None
    staleness_s: int | None
    stale: bool


def feed_health(
    *,
    source_mode: str,
    txline_configured: bool,
    connected: bool,
    last_tick_ts: int | None,
    now_ts: int,
    ticks_seen: int,
    fixture_id: int | None = None,
    stale_after_s: int = DEFAULT_STALE_AFTER_S,
) -> FeedHealthReport:
    """Compute a :class:`FeedHealthReport` from raw feed signals.

    Args:
        source_mode: ``"live"`` or ``"replay"``.
        txline_configured: Whether TxLINE credentials are configured.
        connected: Whether the stream is connected.
        last_tick_ts: Unix seconds of the last tick, or ``None``.
        now_ts: Current Unix seconds (the staleness reference).
        ticks_seen: Number of ticks ingested.
        fixture_id: The fixture being followed, or ``None``.
        stale_after_s: Staleness budget in seconds.

    Returns:
        The populated :class:`FeedHealthReport` (``staleness_s``/``stale`` are ``None``/``False``
        until a first tick arrives).
    """
    staleness_s = (now_ts - last_tick_ts) if last_tick_ts is not None else None
    stale = staleness_s is not None and staleness_s > stale_after_s
    return FeedHealthReport(
        source_mode=source_mode,
        txline_configured=txline_configured,
        connected=connected,
        last_tick_ts=last_tick_ts,
        ticks_seen=ticks_seen,
        fixture_id=fixture_id,
        staleness_s=staleness_s,
        stale=stale,
    )


def reconstruct_closing_line(updates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Reconstruct the closing line as the last pre-``InRunning`` update (CON-040).

    The pre-match ``/odds/snapshot`` is empty, so the closing line is recovered from the full
    movement history (``/odds/updates``): the last update emitted before the market flips
    ``InRunning`` true.

    Args:
        updates: Ordered TxLINE odds updates (oldest first).

    Returns:
        The last update with ``InRunning`` falsy, or ``None`` when every update is already live.
    """
    pre_match = [u for u in updates if not bool(u.get("InRunning"))]
    return pre_match[-1] if pre_match else None
