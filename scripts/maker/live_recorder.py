"""Operator read-only LIVE-RECORDER CLI (MM-R3, milestone E9).

A THIN operator entrypoint that wires the already-built, already-trust-verified E1-E8
live-recorder components into one command. It streams TxLINE fair value, polls the PUBLIC
Polymarket ``/book`` for full depth, aligns with NO look-ahead, and RECORDS sealed,
replayable, append-only evidence to ``--out/<session_ts>/{records.jsonl, meta.json}``.

It sends **NO orders**. It is read-only: it constructs no order-placing, funded venue
client and references no order/venue-write symbol. The default decision policy ABSTAINS
(``no_quote``) every poll, so every recorded ``DecisionEvent`` is honestly labeled and can
never be mistaken for a live order; a strategy ``decide_fn`` is a pluggable seam on
:func:`~veridex.live_recorder.runner.run_live_recorder`.

Trust discipline (mirrors ``scripts/maker/live_monitor.py`` and the E4 sources module):

* **Fail-closed secrets (AC-015 / SEC-007).** :func:`main` resolves both required TxLINE
  credentials via :func:`~veridex.live_recorder.sources.require_live_creds` BEFORE any I/O;
  a missing credential exits immediately. No secret VALUE is ever logged — any guard
  message is scrubbed via :func:`~veridex.live_recorder.sources._scrub`, and artifacts
  carry only the boolean :func:`~veridex.live_recorder.sources.configured` flag.
* **Offline-safe import.** No network library is imported at module scope; ``httpx`` is
  lazy inside the default book source, and the TxLINE stream client is imported lazily
  inside the default FV source's ``stream``.
* **No ``veridex.maker`` / ``veridex.scoring`` import.** The default FV source wraps
  :func:`~veridex.ingest.live_client.stream_marketstates` directly (rather than reusing the
  live-monitor's source, which pulls ``veridex.maker``), so this lane stays maker-free.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

from veridex.ingest.marketstate import MarketState
from veridex.live_recorder.contracts import FillAssumptionConfig, LiveRecorderSessionMeta
from veridex.live_recorder.recorder import LiveRecorder
from veridex.live_recorder.runner import Decision, RecorderMarket, run_live_recorder
from veridex.live_recorder.sources import (
    _DefaultBookDepthSource,
    _scrub,
    configured,
    require_live_creds,
)

__all__ = ["build_parser", "main"]

#: Recorded tool version stamped onto the session meta (provenance, never a secret).
_TOOL_VERSION = "live_recorder-r3"

#: Public Polymarket CLOB base URL (mirrors ``scripts/maker/live_monitor.py::_CLOB_URL``).
_CLOB_URL = "https://clob.polymarket.com"

#: Stable identifier of the DEFAULT observe-only recording policy (recorded, never a secret).
_POLICY_HASH = hashlib.sha256(b"live_recorder:observe-only:no-orders:r3").hexdigest()

#: (txline_side, venue_side, venue_market_ref) for the three 1X2 sides — mirrors
#: ``scripts/maker/live_monitor.py::_SIDE_SPECS`` (part1->home, draw->draw, part2->away).
_SIDE_SPECS: tuple[tuple[str, str, str], ...] = (
    ("part1", "home", "1X2|home|full"),
    ("draw", "draw", "1X2|draw|full"),
    ("part2", "away", "1X2|away|full"),
)


class _DefaultLiveFvSource:
    """Live TxLINE FV source for the recorder lane — yields :class:`MarketState`s in arrival order.

    Mirrors ``scripts/maker/live_monitor.py::_DefaultFvSource`` (reconnect/backoff 1s->60s,
    scrubbed diagnostics) but wraps :func:`~veridex.ingest.live_client.stream_marketstates`
    DIRECTLY so this module imports nothing from ``veridex.maker``. Creds are held privately
    and never logged; ``httpx`` is imported lazily inside ``stream_marketstates``, so
    constructing this source touches no network.
    """

    def __init__(self, *, creds: tuple[str, str], base_url: str | None = None) -> None:
        self._creds = creds
        self._base_url = base_url

    async def stream(self) -> AsyncIterator[MarketState]:
        from veridex.ingest.live_client import stream_marketstates

        backoff = 1.0
        while True:
            try:
                async for state in stream_marketstates(base_url=self._base_url, creds=self._creds):
                    backoff = 1.0
                    yield state
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — reconnect on any stream error; never leak creds
                print(f"  FV stream disconnected: {_scrub(f'{type(exc).__name__}: {exc}', *self._creds)}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


def _observe_only_decision(_aligned: Any, _snapshot: Any, _config: Any) -> Decision:
    """The DEFAULT read-only recording policy: abstain (``no_quote``) every poll.

    The thin operator CLI records evidence only and never fabricates a trading intent. A
    strategy ``decide_fn`` is a pluggable seam on
    :func:`~veridex.live_recorder.runner.run_live_recorder`; this default abstains so every
    recorded ``DecisionEvent`` is honestly labeled and can never be read as a live order.
    """
    return Decision(
        intent_kind="no_quote",
        reason_code="observe_only",
        # A policy-abstain reason — NOT a fabricated market condition against a deep book.
        no_quote_reason="observe_only",
    )


async def _match_markets(fixtures: list[dict[str, Any]]) -> list[RecorderMarket]:
    """Resolve each fixture's three 1X2 sides to venue tokens; skip (honestly) any unavailable side.

    Mirrors ``scripts/maker/live_monitor.py::match_markets`` but yields
    :class:`~veridex.live_recorder.runner.RecorderMarket` (so this module imports nothing
    from ``veridex.maker``). The resolver is imported lazily so importing this module touches
    no network. A :class:`MarketUnavailable` (or an unmappable side) is logged and SKIPPED —
    never fabricated — so a partial event reads as a partial event.
    """
    from veridex.venues.polymarket_resolver import (
        MarketUnavailable,
        resolve_market,
        side_to_token,
    )

    matched: list[RecorderMarket] = []
    for fixture in fixtures:
        fixture_id = int(fixture["fixture_id"])
        slug = str(fixture["event_slug"])
        home_team = fixture.get("home_team")
        away_team = fixture.get("away_team")
        for txline_side, venue_side, market_ref in _SIDE_SPECS:
            try:
                resolved = await resolve_market(
                    market_ref, slug, home_team=home_team, away_team=away_team
                )
                token_id = side_to_token(resolved, venue_side)
            except MarketUnavailable as exc:
                print(f"fixture {fixture_id} side {venue_side}: UNRESOLVED ({exc}) — skipped, not fabricated")
                continue
            except ValueError as exc:
                print(f"fixture {fixture_id} side {venue_side}: unmappable side ({exc}) — skipped")
                continue
            matched.append(RecorderMarket(fixture_id, txline_side, market_ref, token_id))
    return matched


def build_parser() -> argparse.ArgumentParser:
    """Build the operator argument parser (constructs NO live source — offline-safe, ``--help`` works)."""
    parser = argparse.ArgumentParser(
        prog="live_recorder",
        description="Read-only LIVE recorder — records sealed, replayable evidence (sends NO orders).",
    )
    parser.add_argument("--fixtures", required=True, help="path to fixtures.json (fixture_id/event_slug/teams)")
    parser.add_argument("--poll-interval-ms", type=int, default=5_000, dest="poll_interval_ms")
    parser.add_argument("--minutes", type=float, default=30.0)
    parser.add_argument("--out", default=".omc/research/live-recorder", help="session output root")
    parser.add_argument(
        "--base-url",
        type=str,
        default=None,
        dest="base_url",
        help="override TxLINE base URL (e.g. https://txline.txodds.com/api for mainnet); default uses config",
    )
    return parser


def _load_fixtures(path: str) -> list[dict[str, Any]]:
    """Load the operator fixtures list; a non-list / empty file fails closed with a clear error."""
    fixtures = json.loads(Path(path).read_text())
    if not isinstance(fixtures, list) or not fixtures:
        raise SystemExit(f"fixtures file {path} must be a non-empty JSON list")
    return fixtures


async def _run_cli(
    args: argparse.Namespace,
    creds: tuple[str, str],
    env: Mapping[str, str],
    fixtures: list[dict[str, Any]],
) -> None:
    """Match markets (live Gamma), run the recorder session, seal the artifacts — send NO orders."""
    matched = await _match_markets(fixtures)
    if not matched:
        raise SystemExit("no markets resolved — nothing to record (all sides UNRESOLVED)")

    session_ts = int(time.time())
    session_dir = Path(args.out) / str(session_ts)
    # Pinned counterfactual fill-cost assumptions (recorded, never a fill). The Rose 4x fee
    # stress is the R4-gate variant FillAssumptionConfig(fee_stress_multiplier=4, ...).
    config = FillAssumptionConfig(
        taker_fee_bps=0.0,
        fee_stress_multiplier=1.0,
        spread_assumption=0.0,
        slippage_assumption=0.0,
    )
    start_meta = LiveRecorderSessionMeta(
        session_ts=session_ts,
        endpoints={"txline": args.base_url or "config-default", "venue": _CLOB_URL},
        tool_version=_TOOL_VERSION,
        config_hash=config.config_hash(),
        source_provenance={
            "fv": "txline",
            "book": "polymarket_clob_public",
            # Boolean-only telemetry (AC-015 / SEC-007) — the flag, NEVER a secret value.
            "txline_configured": str(configured(env)).lower(),
        },
        fixture_ids=tuple(sorted({m.fixture_id for m in matched})),
    )

    recorder = LiveRecorder(session_dir, start_meta)
    try:
        result = await run_live_recorder(
            matched=matched,
            fv_source=_DefaultLiveFvSource(creds=creds, base_url=args.base_url),
            book_source=_DefaultBookDepthSource(),
            trade_source=None,
            recorder=recorder,
            decide_fn=_observe_only_decision,
            config=config,
            policy_hash=_POLICY_HASH,
            now_fn=lambda: int(time.time() * 1000.0),
            sleep_fn=asyncio.sleep,
            poll_interval_ms=args.poll_interval_ms,
            minutes=args.minutes,
        )
    finally:
        recorder.close()

    print(f"session: {session_dir}")
    print(f"records: {session_dir / 'records.jsonl'}  meta: {session_dir / 'meta.json'}")
    print(
        f"polls: {result.polls}  events: {result.events_recorded}  "
        f"gaps: {result.gaps}  fv points: {result.fv_points}"
    )


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    """Parse ``argv`` and run the operator recorder session (fail-closed on missing creds).

    ``env`` defaults to ``os.environ`` and is injectable so the guard is drivable offline.
    Both required TxLINE credentials are resolved BEFORE any I/O; a missing credential exits
    immediately and no secret VALUE is ever echoed.
    """
    env = os.environ if env is None else env
    args = build_parser().parse_args(argv)

    # FAIL CLOSED (AC-015 / SEC-007) — require both creds BEFORE any I/O; never leak a secret.
    try:
        creds = require_live_creds(env)
    except ValueError as exc:
        raise SystemExit(
            _scrub(str(exc), env.get("JWT", ""), env.get("TXLINE_X_API_TOKEN", ""))
        ) from None

    fixtures = _load_fixtures(args.fixtures)
    asyncio.run(_run_cli(args, creds, env, fixtures))
    return 0


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(main())
