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

import enum
from typing import Any

from pydantic import BaseModel

#: Default seconds since the last tick before the feed is considered stale.
DEFAULT_STALE_AFTER_S: int = 30


class FeedState(str, enum.Enum):
    """The five connection-derived feed states (R-0a — consumed by III-3's health panel).

    A ``str`` enum so a state renders directly as its wire value in JSON/UI without a
    separate serializer. The five states are exhaustive and mutually exclusive for a
    single observation of the feed:

    * ``DISCONNECTED`` — no active connection (initial, or the stream dropped).
    * ``CONNECTING`` — a connection is being established / no frame has arrived yet.
    * ``LIVE`` — connected and at least one fresh odds record has been received.
    * ``HEARTBEAT_ONLY`` — connected and fresh, but ONLY heartbeats (no odds records):
      liveness is proven, yet there is no market data — so no pack can be minted.
    * ``STALE`` — connected, but the last frame is older than the staleness budget.
    """

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    LIVE = "live"
    HEARTBEAT_ONLY = "heartbeat_only"
    STALE = "stale"
    #: The active ingestion is a RECORDED REPLAY, not a live connection (III-3 honesty surface).
    #: A replay is NEVER labelled live/heartbeat/stale/disconnected — those describe a live socket;
    #: a replay is its own state so the ``/feed/health`` panel can never dress a replay up as live.
    RECORDED_REPLAY = "recorded_replay"


def derive_feed_state(
    *,
    connecting: bool,
    connected: bool,
    odds_records_seen: int,
    heartbeats_seen: int,
    last_frame_ts: int | None,
    now_ts: int,
    stale_after_s: int = DEFAULT_STALE_AFTER_S,
) -> FeedState:
    """Derive the current :class:`FeedState` from observed feed signals (pure).

    Precedence, for a connected feed: staleness dominates (a feed whose last frame is older than
    the budget is ``STALE`` regardless of what that frame was), then a fresh odds record makes the
    feed ``LIVE``, then a fresh heartbeat (with no odds) makes it ``HEARTBEAT_ONLY``; a connected
    feed that has produced no frame yet is still ``CONNECTING``.

    Args:
        connecting: A connection is being established (only consulted when not ``connected``).
        connected: The stream is currently connected.
        odds_records_seen: Count of odds records received.
        heartbeats_seen: Count of heartbeat frames received.
        last_frame_ts: Unix seconds of the last frame (odds or heartbeat), or ``None``.
        now_ts: Current Unix seconds (the staleness reference).
        stale_after_s: Staleness budget in seconds.

    Returns:
        The single :class:`FeedState` describing this observation.
    """
    if not connected:
        return FeedState.CONNECTING if connecting else FeedState.DISCONNECTED
    if last_frame_ts is not None and (now_ts - last_frame_ts) > stale_after_s:
        return FeedState.STALE
    if odds_records_seen > 0:
        return FeedState.LIVE
    if heartbeats_seen > 0:
        return FeedState.HEARTBEAT_ONLY
    return FeedState.CONNECTING


def derive_live_feed_state(
    *,
    source_mode: str,
    connected: bool,
    connecting: bool,
    last_odds_ts: int | None,
    last_heartbeat_ts: int | None,
    now_ts: int,
    stale_after_s: int = DEFAULT_STALE_AFTER_S,
) -> FeedState:
    """Derive the honest III-3 feed state from the ACTIVE stream's PER-CHANNEL last-seen (pure).

    This is the endpoint's derivation (as opposed to :func:`derive_feed_state`, which the offline
    capture chain uses with ``now_ts == last_frame_ts``). The difference that matters: here ``now``
    advances INDEPENDENTLY of the frames, so LIVE-vs-HEARTBEAT_ONLY is decided by the RECENCY of
    each channel — not by cumulative counts. A feed whose odds records went stale while its
    heartbeat stays fresh is honestly ``HEARTBEAT_ONLY`` (liveness proven, no market data), never a
    stale ``LIVE``.

    Precedence:

    * ``replay`` mode is ALWAYS :attr:`FeedState.RECORDED_REPLAY` — a recorded replay is never a
      live connection, whatever the last-seen values are (the core honesty invariant).
    * not connected → :attr:`FeedState.CONNECTING` if a connect is in flight, else
      :attr:`FeedState.DISCONNECTED`.
    * a FRESH odds record (within the budget) → :attr:`FeedState.LIVE`.
    * else a FRESH heartbeat (within the budget) → :attr:`FeedState.HEARTBEAT_ONLY`.
    * else, having seen a frame that is now beyond the budget → :attr:`FeedState.STALE`.
    * else (connected, no frame yet) → :attr:`FeedState.CONNECTING`.

    Args:
        source_mode: ``"live"`` or ``"replay"``. ``"replay"`` short-circuits to RECORDED_REPLAY.
        connected: Whether the live stream is currently connected.
        connecting: Whether a connection is being established (only consulted when not connected).
        last_odds_ts: Unix seconds of the last odds RECORD, or ``None``.
        last_heartbeat_ts: Unix seconds of the last HEARTBEAT, or ``None``.
        now_ts: Current Unix seconds (the recency reference).
        stale_after_s: Freshness budget in seconds.

    Returns:
        The single honest :class:`FeedState` for this observation.
    """
    if source_mode == "replay":
        return FeedState.RECORDED_REPLAY
    if not connected:
        return FeedState.CONNECTING if connecting else FeedState.DISCONNECTED

    def _fresh(ts: int | None) -> bool:
        return ts is not None and (now_ts - ts) <= stale_after_s

    if _fresh(last_odds_ts):
        return FeedState.LIVE
    if _fresh(last_heartbeat_ts):
        return FeedState.HEARTBEAT_ONLY
    if last_odds_ts is not None or last_heartbeat_ts is not None:
        return FeedState.STALE  # saw a frame, but it is now beyond the budget
    return FeedState.CONNECTING  # connected, nothing received yet


class LiveFeedStatus:
    """Observable last-seen state of the ACTIVE stream — the place the live runner records into and
    the ``/feed/health`` endpoint reads from (III-3).

    Read-only OPERATIONAL TELEMETRY (same doctrine as :class:`FeedHealthReport`): never scored,
    never in ``evidence_hash``, never a proof check or leaderboard input. It is a small MUTABLE
    holder — the live runner is a single-event-loop async shell, so a plain object (no lock) is
    sufficient; the endpoint reads a coherent-enough snapshot for a health panel.

    Records BOTH channels independently: the last odds-RECORD wall time (``last_odds_ts``) and the
    last HEARTBEAT wall time (``last_heartbeat_ts``), plus the connection lifecycle. The runner's
    stream yields odds records, so it drives :meth:`record_odds_record`; the heartbeat channel is
    exposed (:meth:`record_heartbeat`) for the SSE reader that observes raw heartbeat frames.
    """

    def __init__(self) -> None:
        self.connecting: bool = False
        self.connected: bool = False
        self.last_odds_ts: int | None = None
        self.last_heartbeat_ts: int | None = None
        self.odds_records_seen: int = 0
        self.heartbeats_seen: int = 0
        self.fixture_id: int | None = None

    def mark_connecting(self) -> None:
        """A connection is being established (pre-first-frame)."""
        self.connecting = True
        self.connected = False

    def mark_connected(self) -> None:
        """The live stream is up."""
        self.connected = True
        self.connecting = False

    def mark_disconnected(self) -> None:
        """The live stream dropped / ended — honestly no longer live."""
        self.connected = False
        self.connecting = False

    def record_odds_record(self, ts: int, fixture_id: int | None = None) -> None:
        """Record the receipt (wall) time of one odds RECORD (and the fixture it followed)."""
        self.last_odds_ts = ts
        self.odds_records_seen += 1
        if fixture_id is not None:
            self.fixture_id = fixture_id

    def record_heartbeat(self, ts: int) -> None:
        """Record the receipt (wall) time of one HEARTBEAT frame (liveness, no market data)."""
        self.last_heartbeat_ts = ts
        self.heartbeats_seen += 1

    def last_frame_ts(self) -> int | None:
        """The most recent of the two channels' last-seen times (the staleness reference)."""
        seen = [ts for ts in (self.last_odds_ts, self.last_heartbeat_ts) if ts is not None]
        return max(seen) if seen else None

    def feed_state(
        self, *, source_mode: str, now_ts: int, stale_after_s: int = DEFAULT_STALE_AFTER_S
    ) -> FeedState:
        """Derive the honest :class:`FeedState` from this status via :func:`derive_live_feed_state`."""
        return derive_live_feed_state(
            source_mode=source_mode,
            connected=self.connected,
            connecting=self.connecting,
            last_odds_ts=self.last_odds_ts,
            last_heartbeat_ts=self.last_heartbeat_ts,
            now_ts=now_ts,
            stale_after_s=stale_after_s,
        )

    def report(
        self,
        *,
        source_mode: str,
        txline_configured: bool,
        now_ts: int,
        stale_after_s: int = DEFAULT_STALE_AFTER_S,
    ) -> FeedHealthReport:
        """Project the WD-4 :class:`FeedHealthReport` from this status (staleness view).

        ``connected`` / ``last_tick_ts`` / ``ticks_seen`` / ``fixture_id`` come from the ACTIVE
        stream's observed state — never from credential presence.
        """
        return feed_health(
            source_mode=source_mode,
            txline_configured=txline_configured,
            connected=self.connected,
            last_tick_ts=self.last_frame_ts(),
            now_ts=now_ts,
            ticks_seen=self.odds_records_seen,
            fixture_id=self.fixture_id,
            stale_after_s=stale_after_s,
        )


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
