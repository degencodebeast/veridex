"""T8 — the windowed LIVE RUNNER: stream ticks in real time, seal one window (REQ-2D-103/104).

This is the ASYNC SHELL that ties the whole live loop together. It streams live TxLINE ticks into
``CompetitionRun.feed()`` AS THEY ARRIVE (never buffered-then-replayed — that would be replay-as-live,
REQ-2D-101), detects window end per the :class:`~veridex.runtime.window.RunWindow` end rule, and for a
``pre_match`` window fetches + reconstructs the CON-040 closing line and SEALS it via ``feed_closing``
BEFORE ``finalize`` — so the verifier recomputes TRUE CLV from the authoritative close, not from the
last stream tick (which a gap could have made stale).

Trust boundary (CON-2D-102 — async shell / sync core): all concurrency lives in
``CompetitionRun.feed`` (the sync deterministic seal is untouched here). ``httpx`` is reached ONLY
through the injected/real stream + ``fetch_updates`` seams (both lazy), and NO LLM SDK is imported —
so ``import veridex.runtime.live_runner`` is offline-safe and every test drives it with an injected
async-iterator ``stream`` + injected ``fetch_updates`` (ZERO network).

Honesty doctrine — the mode label NEVER lies (REQ-2D-104):
  * ``pre_match`` + a successful CON-040 close  → the close enters SEALED evidence via ``feed_closing``
    and the window finalizes as ``pre_match`` → rows carry TRUE ``clv_bps``.
  * ANY degrade (fetch raised / no pre-InRunning close) → NO fabricated close, NO ``feed_closing``.
    The run finalizes on an effective ``manual_stop`` window (window CLV), so rows carry
    ``window_clv_bps`` (never true ``clv_bps``), plus a NON-SEALED ops marker
    ``closing_source: "stream_observed_fallback"``. Stream-observed CLV is NEVER presented as true CLV.

The proof card / anchor happen ONLY AFTER ``finalize`` (DEC-2D-3). The composition mirrors
``veridex_agent.run.standalone_run`` but is LIFTED here (not imported) to avoid a package cycle.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from veridex.chain.anchor import run_manifest_hash
from veridex.checks.build import (
    build_check_results,
    build_performance_metrics,
    check_results_to_proof_block,
)
from veridex.ingest.feed_health import LiveFeedStatus
from veridex.ingest.marketstate import MarketState
from veridex.ingest.txline_client import reconstruct_closing
from veridex.ingest.txline_normalize import market_key, marketstate_from_txline_odds
from veridex.runtime.competition import DEFAULT_CLUSTER, SCHEMA_VERSIONS
from veridex.runtime.orchestrator import Agent, CompetitionRun, RunResult
from veridex.runtime.window import RunWindow
from veridex.scoring import score_run
from veridex.verifier.proof_card import proof_card_from_run_result
from veridex.verifier.recompute import manifest_from_run, recompute_score_root

if TYPE_CHECKING:  # avoid a runtime import cycle (the store lazily imports RunResult)
    from veridex.store import Store

#: The non-sealed ops annotation value used when no authoritative CON-040 close could be sealed and
#: the run's last fed stream tick became the de-facto close. It labels the run's CLV as WINDOW CLV
#: (``window_clv_bps``), NEVER true closing CLV — and it lives on the OPS bundle, never in evidence.
CLOSING_SOURCE_FALLBACK = "stream_observed_fallback"

#: Sentinel: the stream is exhausted (a normal, finite end of the window).
_STREAM_DONE: Any = object()
#: Sentinel: ``stop_event`` won the next-tick race (manual_stop ended on an IDLE stream).
_STOPPED: Any = object()


@contextlib.asynccontextmanager
async def _aclosing_if_possible(
    stream: AsyncIterator[MarketState],
) -> AsyncIterator[AsyncIterator[MarketState]]:
    """Yield ``stream``, ``aclose()``-ing it on exit IF it supports it (robustness fold).

    The real :func:`~veridex.ingest.live_client.stream_marketstates` owns an ``httpx`` client that
    must be closed so it does not linger to GC across a long-running (hours) window; on ANY exit
    (break at kickoff/duration/stop, normal exhaustion, or a mid-stream error) this closes it. An
    injected test iterator that does not implement ``aclose`` is left untouched (guarded).
    """
    try:
        yield stream
    finally:
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()


async def _next_or_done(stream: AsyncIterator[MarketState]) -> Any:
    """Return the next tick, or the ``_STREAM_DONE`` sentinel when the stream is exhausted.

    ``StopAsyncIteration`` is converted to a sentinel so a raced ``anext`` future never has to carry
    it as a result/exception; any OTHER exception propagates unchanged (the interrupt-degrade path).
    """
    try:
        return await anext(stream)
    except StopAsyncIteration:
        return _STREAM_DONE


async def _race_next_tick(stream: AsyncIterator[MarketState], stop_event: asyncio.Event) -> Any:
    """Await the next tick, but return ``_STOPPED`` if ``stop_event`` fires first (idle-stop fold).

    A plain ``anext`` blocks until the next tick — so on an IDLE stream (halftime, no line moves) a
    SET ``stop_event`` would not be honored until some later tick arrived, and if none ever did the
    run would hang forever. Racing the next-tick future against ``stop_event.wait()`` ends the window
    promptly on an idle stream. ``stop_event`` is prioritized when both are ready (that just-arrived
    tick is post-stop and is dropped), and the losing future is cancelled + settled so nothing leaks.
    """
    tick_task = asyncio.ensure_future(_next_or_done(stream))
    stop_task = asyncio.ensure_future(stop_event.wait())
    try:
        await asyncio.wait({tick_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if stop_task.done() and not stop_task.cancelled():
            return _STOPPED  # stop wins even alongside a ready tick — the tick is not fed.
        return tick_task.result()  # a real stream error re-raises here (interrupt-degrade path).
    finally:
        for task in (tick_task, stop_task):
            if not task.done():
                task.cancel()
            with contextlib.suppress(BaseException):  # settle cancelled/errored tasks (cleanup only)
                await task


@dataclass(frozen=True)
class LiveRunResult:
    """The bundle returned by :func:`run_live_window` (Tasks 9/20/21 consume this).

    The sealed :class:`~veridex.runtime.orchestrator.RunResult` is the trust artifact; the proof
    card / manifest / anchor are the AFTER-seal composition (DEC-2D-3). ``ops`` carries NON-SEALED
    operational annotations (e.g. the ``closing_source`` fallback marker) that must never appear in
    the sealed evidence.

    Attributes:
        run: The sealed run (evidence hash + events + windowed score rows).
        scores: The ranked per-agent metric stack (``score_run`` output).
        proof_card: The judge-visible proof card, built ONLY after ``finalize``.
        manifest_hash: SHA-256 of the run manifest (the exact anchored Memo payload).
        anchor_status: ``"anchored"`` or ``"not_anchored"``.
        signature: The anchor tx signature when anchored, else ``None``.
        ops: Non-sealed operational annotations (``closing_source``, ``closing_incomplete_markets``,
            ``closing_error``). NEVER part of the sealed evidence.
    """

    run: RunResult
    scores: list[dict[str, Any]]
    proof_card: dict[str, Any]
    manifest_hash: str
    anchor_status: str
    signature: str | None
    ops: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Pure helpers — offline, no network, no LLM SDK.
# ---------------------------------------------------------------------------


def _is_in_running(state: MarketState) -> bool:
    """True when the fixture has gone IN-RUNNING (kickoff) at this tick.

    The TxLINE normalizer (:func:`~veridex.ingest.txline_normalize.marketstate_from_txline_odds`)
    marks a snapshot ``phase == 1`` iff any folded message is ``InRunning`` — so "the fixture is now
    live" is exactly ``phase == 1``. This is the SINGLE field the pre_match end rule keys on.
    """
    return state.phase == 1


def _allowed(market: str, allowlist: list[str]) -> bool:
    """True when ``market`` matches an allowlist PREFIX (e.g. ``"OU|FT|2.5"`` under ``["OU"]``)."""
    return any(market.startswith(prefix) for prefix in allowlist)


def _filter_markets(state: MarketState, allowlist: list[str]) -> MarketState:
    """Restrict a snapshot's ``markets`` to allowlisted keys (returns the same object if unchanged).

    ``MarketState`` is frozen, so a filtered snapshot is produced via ``model_copy``. When every
    market already passes, the original object is returned unchanged (no needless copy — keeps the
    fed snapshot byte-identical to the stream tick on the common path).
    """
    filtered = {key: value for key, value in state.markets.items() if _allowed(key, allowlist)}
    if len(filtered) == len(state.markets):
        return state
    return state.model_copy(update={"markets": filtered})


def _reconstruct_closing_state(
    updates: list[dict[str, Any]], *, allowlist: list[str], tick_seq: int
) -> MarketState | None:
    """Build the authoritative CON-040 closing snapshot from ``/odds/updates``, or ``None``.

    CON-040: the close is the last pre-``InRunning`` odds movement (the empty pre-match snapshot is
    NOT used). :func:`~veridex.ingest.txline_client.reconstruct_closing` gates existence — if EVERY
    update was already in-running it returns ``None`` (no pre-kickoff close exists; the caller must
    degrade honestly, NOT fabricate one).

    T7-review completeness contract: a single ``reconstruct_closing`` update carries only ONE market,
    but the closing snapshot must carry EVERY market scored during the window — otherwise a scored
    market's closing falls back to its last-seen entry tick and silently yields CLV 0. So this folds
    the last pre-``InRunning`` update PER market_key into one snapshot (still CON-040 per market),
    then filters it to the allowlist. Coverage vs the scored set is checked by the caller.

    Returns:
        The allowlist-filtered closing :class:`MarketState`, or ``None`` when no pre-kickoff close
        exists (degrade path — never a fabricated close).
    """
    if reconstruct_closing(updates) is None:
        return None  # no pre-InRunning update at all — degrade, never fabricate.

    # Last pre-InRunning update per market (later updates overwrite earlier — CON-040 per market).
    per_market: dict[str, dict[str, Any]] = {}
    for update in updates:
        if update.get("InRunning"):
            continue
        per_market[market_key(update)] = update
    if not per_market:
        return None

    closing = marketstate_from_txline_odds(list(per_market.values()), tick_seq=tick_seq)
    return _filter_markets(closing, allowlist)


# ---------------------------------------------------------------------------
# The live runner — async driver over the incremental core.
# ---------------------------------------------------------------------------


async def run_live_window(
    window: RunWindow,
    agents: list[Agent],
    *,
    stream: AsyncIterator[MarketState] | None = None,
    fetch_updates: Callable[[int], Awaitable[list[dict[str, Any]]]] | None = None,
    event_sink: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    store: Store | None = None,
    anchor_fn: Callable[[str], Awaitable[str]] | None = None,
    stop_event: asyncio.Event | None = None,
    run_id: str | None = None,
    feed_status: LiveFeedStatus | None = None,
    clock: Callable[[], int] | None = None,
) -> LiveRunResult:
    """Drive one windowed LIVE run: stream → feed (real time) → seal close → finalize → proof.

    Args:
        window: The live coverage window + end rule (``pre_match`` / ``fixed_duration`` /
            ``manual_stop``). Its ``fixture_id`` filters ticks and its ``market_allowlist`` (prefix
            match) restricts scored markets.
        agents: Participating agents (≥1; typically ≥1 LLM + the deterministic baseline).
        stream: Injected async iterator of :class:`MarketState` (tests). ``None`` → the real
            :func:`~veridex.ingest.live_client.stream_marketstates` (lazy httpx).
        fetch_updates: Injected ``async (fixture_id) -> list[update]`` for the CON-040 close (tests).
            ``None`` → the real :func:`~veridex.ingest.txline_client.fetch_odds_updates` (lazy httpx).
        event_sink: Optional async observer forwarded to ``CompetitionRun`` (live projection of the
            sealed events; never feeds back into the seal).
        store: Optional async store; when given the run is persisted at ``finalize`` time.
        anchor_fn: Injectable ``async (manifest_hash) -> signature``; ``None`` skips anchoring
            (``anchor_status="not_anchored"``). Anchoring happens ONLY after ``finalize`` (DEC-2D-3).
        stop_event: For the ``manual_stop`` end rule — the loop ends when it is set. Checked between
            ticks AND raced against the next-tick await, so a stop is honored promptly even on an
            IDLE stream (no line moves) rather than blocking until the next tick.
        run_id: Optional pre-known run id (T21 deploy) so a launcher can return the run id BEFORE the
            window seals; ``None`` → a fresh id is minted at construction (the arena default).
        feed_status: Optional observable :class:`~veridex.ingest.feed_health.LiveFeedStatus` (III-3).
            When given, the runner records the last-seen wall time of each fed odds RECORD into it and
            marks connect/disconnect around the stream, so ``/feed/health`` derives its honest feed
            state from THIS active stream (not credential presence). Read-only telemetry — it is
            never scored, never in the sealed evidence.
        clock: Injectable ``() -> int`` wall-seconds source for the ``feed_status`` last-seen stamps
            (test seam); ``None`` → ``int(time.time())``. Only consulted when ``feed_status`` is set.

    Returns:
        A :class:`LiveRunResult` bundle (the sealed run + the after-seal proof composition + ops).

    Note:
        Resilience over data-loss: if ``stream`` raises mid-iteration (e.g. an httpx blip over a
        multi-hour window) AFTER at least one tick was fed, the partial run is finalized as a DEGRADE
        (``window_clv_bps``, never true CLV — the window was cut short) with a non-sealed
        ``ops["stream_interrupted"]`` marker, and the bundle is RETURNED so the sealed evidence is
        preserved and the interruption is explicit. The caller owns reconnect/restart (open a NEW
        window) to continue past an interruption. If ZERO ticks were fed, the stream error re-raises
        (there is nothing to seal).

    Raises:
        Exception: Re-raises a mid-stream error from ``stream`` when NO ticks were fed yet.
    """
    run = CompetitionRun(agents, source_mode="live", event_sink=event_sink, run_id=run_id)

    if stream is None:
        from veridex.ingest.live_client import stream_marketstates  # noqa: PLC0415  (lazy httpx)

        stream = stream_marketstates()

    started_ts: int | None = None  # first fed tick's ts — the window start we track during the loop.
    last_tick_seq = -1
    fed_any = False
    seen_markets: set[str] = set()
    stream_interrupted: BaseException | None = None
    # A stop_event only governs the manual_stop end rule; ``None`` elsewhere disables the race path.
    stop = stop_event if window.end_rule == "manual_stop" else None
    # III-3 last-seen hook: stamp fed odds records into the observable feed status (wall time), so
    # /feed/health derives its honest state from THIS active stream. No-op when feed_status is None.
    # We start CONNECTING (dialing / awaiting the first frame — the socket is not confirmed open yet),
    # flip to connected on the first received frame, and mark disconnected in the finally below.
    _now: Callable[[], int] = clock if clock is not None else (lambda: int(time.time()))
    if feed_status is not None:
        feed_status.mark_connecting()

    try:
        async with _aclosing_if_possible(stream) as guarded_stream:
            try:
                while True:
                    # (a) Obtain the next tick. For manual_stop, RACE the next-tick against stop_event
                    # so an IDLE stream (no line moves) still honors a SET stop promptly, not hanging.
                    if stop is not None:
                        outcome = await _race_next_tick(guarded_stream, stop)
                        if outcome is _STOPPED:
                            break
                        tick = outcome
                    else:
                        tick = await _next_or_done(guarded_stream)
                    if tick is _STREAM_DONE:
                        break  # a finite stream reached its end — a normal window close.

                    # A real frame arrived from the socket -> honestly connected (idempotent). Even a
                    # foreign-fixture frame proves the socket is live; with no fed odds/heartbeat yet
                    # the derive still reads CONNECTING (connected, no frames) until real data lands.
                    if feed_status is not None:
                        feed_status.mark_connected()

                    # (b) Between-ticks stop check: a stop set by the time this tick arrived means the
                    # tick is post-stop and must NOT be fed. This keeps the "ends between ticks" rule
                    # deterministic even when a tick and the stop become ready in the same loop turn.
                    if stop is not None and stop.is_set():
                        break

                    # Fixture filter: a tick for another fixture is dropped whole (never fed).
                    if tick.fixture_id != window.fixture_id:
                        continue

                    # pre_match end: the first IN-RUNNING (kickoff) tick TERMINATES the window and is
                    # NOT fed — it is post-kickoff; the line agents are scored against comes from the
                    # reconstructed close, not this tick. All prior pre-kickoff ticks were already fed.
                    if window.end_rule == "pre_match" and _is_in_running(tick):
                        break

                    # fixed_duration end: a tick past started_ts + duration_s terminates and is NOT
                    # fed. We track started_ts from the first FED tick ourselves (finalize stamps
                    # window.started_ts, but that is not visible during the loop).
                    if (
                        window.end_rule == "fixed_duration"
                        and started_ts is not None
                        and window.duration_s is not None
                        and tick.ts > started_ts + window.duration_s
                    ):
                        break

                    # Restrict to allowlisted markets, then feed in REAL TIME (concurrency in feed()).
                    filtered = _filter_markets(tick, window.market_allowlist)
                    await run.feed(filtered)
                    fed_any = True
                    if feed_status is not None:
                        feed_status.record_odds_record(_now(), fixture_id=filtered.fixture_id)
                    if started_ts is None:
                        started_ts = filtered.ts
                    last_tick_seq = filtered.tick_seq
                    seen_markets.update(filtered.markets)
            except Exception as exc:  # noqa: BLE001 — a mid-stream error (e.g. an httpx blip / hours).
                # Honesty over data-loss: if ANY ticks were fed, finalize the PARTIAL run as a DEGRADE
                # below (window_clv, NEVER true CLV — the window was cut short) so hours of sealed work
                # are not vaporized and the interruption is EXPLICIT. ZERO ticks -> nothing to seal.
                if not fed_any:
                    raise
                stream_interrupted = exc
    finally:
        # The stream context has exited by ANY path — normal exhaustion, a manual/idle stop, a mid-
        # stream Exception, OR a BaseException (asyncio.CancelledError from a cancelled live task, a
        # KeyboardInterrupt). The live socket is no longer open, so honestly mark the feed
        # disconnected for /feed/health. A finally (not a post-``async with`` statement) is REQUIRED:
        # a pre-first-tick crash re-raises out of the loop, and CancelledError is a BaseException the
        # ``except Exception`` never catches — either would otherwise skip the mark and leave the feed
        # stuck CONNECTING (pre-tick) or FALSELY-FRESH LIVE (post-tick) until staleness.
        if feed_status is not None:
            feed_status.mark_disconnected()

    ops: dict[str, Any] = {}
    effective_window = window

    if stream_interrupted is not None:
        # A truncated window is NOT a complete authoritative close: degrade to WINDOW CLV and record
        # the cause as a NON-sealed ops marker (never in evidence). The caller owns reconnect/restart
        # (open a NEW window) — run_live_window degrades-and-returns rather than crashing, so the
        # partial sealed evidence is preserved honestly. Only a pre_match window needs the rename;
        # fixed_duration/manual_stop already yield window_clv_bps.
        ops["stream_interrupted"] = f"{type(stream_interrupted).__name__}: {stream_interrupted}"
        if window.end_rule == "pre_match":
            effective_window = window.model_copy(update={"end_rule": "manual_stop"})

    # pre_match closing (REQ-2D-104, honesty-critical): fetch + reconstruct the CON-040 close and
    # SEAL it via feed_closing BEFORE finalize, so the verifier recomputes TRUE CLV from it. Skipped
    # entirely on an interrupted run (a cut-short window can never be a complete authoritative close).
    elif window.end_rule == "pre_match" and fed_any:
        if fetch_updates is None:
            from veridex.ingest.txline_client import fetch_odds_updates  # noqa: PLC0415  (lazy httpx)

            fetch_updates = fetch_odds_updates

        closing_state: MarketState | None = None
        try:
            updates = await fetch_updates(window.fixture_id)
            closing_state = _reconstruct_closing_state(
                updates, allowlist=window.market_allowlist, tick_seq=last_tick_seq + 1
            )
        except Exception as exc:  # fetch failed — degrade honestly, NEVER fabricate a close.
            ops["closing_source"] = CLOSING_SOURCE_FALLBACK
            ops["closing_error"] = f"{type(exc).__name__}: {exc}"
            closing_state = None

        if closing_state is not None:
            # Seal the (possibly partial) authoritative close first, so covered markets score against
            # real CON-040 data even if the close turns out incomplete.
            await run.feed_closing(closing_state)

            # Completeness gate (REQ-2D-104): true clv_bps requires a COMPLETE authoritative close
            # covering EVERY scored market. ``seen_markets`` (every allowlisted market fed during the
            # window) is the conservative coverage requirement — a SUPERSET of the actually-scored
            # set, so a close that covers every seen market can NEVER leave a scored market on a
            # stream fallback. Any gap means a scored market would close against its last STREAM tick.
            missing = seen_markets - set(closing_state.markets)
            if missing:
                # An INCOMPLETE close is a DEGRADE — a third trigger alongside fetch-fail / reconstruct
                # None. Labeling this run true clv_bps would present stream-observed CLV as true
                # closing CLV for the missing markets (a per-row honesty lie). Because finalize labels
                # every row uniformly by end_rule, the whole run degrades to WINDOW CLV
                # (window_clv_bps) plus an honest, NON-sealed ops marker naming the uncovered markets.
                ops["closing_source"] = CLOSING_SOURCE_FALLBACK
                ops["closing_incomplete_markets"] = sorted(missing)
                effective_window = window.model_copy(update={"end_rule": "manual_stop"})
            # else: a COMPLETE close -> effective_window stays pre_match -> rows carry TRUE clv_bps.
        else:
            # No authoritative close (fetch failed OR no pre-InRunning update). NO fabricated close,
            # NO feed_closing: the run's last fed tick is the de-facto close via _closing_snapshots.
            # Finalize on an effective manual_stop window so the value is labeled WINDOW CLV
            # (window_clv_bps) — stream-observed CLV is NEVER presented as true clv_bps.
            ops.setdefault("closing_source", CLOSING_SOURCE_FALLBACK)
            effective_window = window.model_copy(update={"end_rule": "manual_stop"})

    # --- finalize (the SYNC seal) -----------------------------------------------------------------
    result = await run.finalize(store=store, window=effective_window)

    # Keep the caller's window honest when we finalized on a copy: propagate the evidence-derived
    # started_ts back so a caller inspecting the original window sees the real coverage start.
    if effective_window is not window and window.started_ts is None:
        window.started_ts = effective_window.started_ts

    # --- proof card / anchor — ONLY AFTER finalize (DEC-2D-3), mirroring standalone_run -----------
    scores = score_run(result)
    manifest = manifest_from_run(
        result,
        fixture_or_window_id=window.window_id,  # the windowed identity (DEC-2D)
        score_root=recompute_score_root(scores),
        schema_versions=dict(SCHEMA_VERSIONS),
    )
    manifest_hash = run_manifest_hash(manifest)

    if anchor_fn is None:
        anchor_status = "not_anchored"
        signature: str | None = None
    else:
        signature = await anchor_fn(manifest_hash)
        anchor_status = "anchored"

    anchor_block = {"status": anchor_status, "signature": signature, "cluster": DEFAULT_CLUSTER}
    checks = check_results_to_proof_block(
        build_check_results(
            scores=scores,
            run=result,
            manifest=manifest,
            manifest_hash=manifest_hash,
            anchor=anchor_block,
            source_mode=result.source_mode,
        )
    )
    proof_card = proof_card_from_run_result(
        result,
        checks=checks,
        metrics=build_performance_metrics(scores),
        anchor=anchor_block,
        schema_versions=dict(SCHEMA_VERSIONS),
    )

    return LiveRunResult(
        run=result,
        scores=scores,
        proof_card=proof_card,
        manifest_hash=manifest_hash,
        anchor_status=anchor_status,
        signature=signature,
        ops=ops,
    )
