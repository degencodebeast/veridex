"""Backtest-data prep: pull the FREE public pmxt raw order-book archive for our World Cup
1X2 markets, reconstruct a downsampled per-outcome L2 book time-series, and write local
files for the soccer market-making backtest. This is the execution/venue layer.

ADDITIVE ONLY. This module IMPORTS the sealed R3 live-recorder merge read-only and reuses it:

* Reconstruction REUSES :class:`veridex.live_recorder.ws_book_source.BookStateMaintainer` — the
  SAME book-state merge the live R3 lane runs. We map each pmxt archive row to the maintainer's
  :class:`~veridex.live_recorder.ws_book_source.VenueBookFrame` shape and drive it synchronously
  offline. So backtest reconstruction is byte-for-byte identical to the live path: seed only from
  a ``book`` event, ``BUY``→bid / ``SELL``→ask, ``size=="0"`` DELETES a level, and every
  ``price_change`` self-validates against the row's ``best_bid``/``best_ask`` checksum.

Trust discipline (mirrors the R3 lane):

* **Offline-safe import.** NO network library is imported at module scope. ``duckdb`` and the
  Gamma HTTP client are lazy-imported INSIDE the functions that use them, so importing this
  module (and the offline test-suite) touches no network and needs no credentials.
* **No credentials.** The pmxt raw archive and Polymarket Gamma are PUBLIC HTTPS — this module
  reads/logs no API key. The resolver is an INJECTABLE seam so tests use a FAKE resolver.
* **Honest gaps (fail-closed).** If a ``price_change`` arrives before any ``book`` seed for a
  token, or a computed best_bid/ask diverges from the row's checksum, we emit an EXPLICIT gap
  marker row and DISCARD state (force a fresh re-seed) — never a silent splice or guessed book.

Output format: **JSONL** (``.jsonl``), one reconstructed snapshot per line. JSONL is chosen over
Parquet deliberately — Parquet writing would add a ``pyarrow``/``pandas`` dependency that this
offline package does not otherwise need; JSONL uses only the stdlib and round-trips losslessly.
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from veridex.live_recorder.top_of_book import extract_top_of_book
from veridex.live_recorder.ws_book_source import (
    BookState,
    BookStateMaintainer,
    VenueBookFrame,
    _coerce_ms,
)

# Public pmxt raw archive: hourly Parquet, one UTC hour per file (no auth, public HTTPS).
ARCHIVE_URL_TEMPLATE = "https://r2v2.pmxt.dev/polymarket_orderbook_{hour}.parquet"
# Public Polymarket Gamma events endpoint (no auth) — resolves event_slug → 1X2 markets.
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"

_BOOK = "book"
_PRICE_CHANGE = "price_change"
_TICK_SIZE_CHANGE = "tick_size_change"
_LAST_TRADE_PRICE = "last_trade_price"
_DEFAULT_TICK_SIZE = 0.01


# --------------------------------------------------------------------------- resolution seam
@dataclass(frozen=True)
class ResolvedMarket:
    """One resolved 1X2 outcome: the condition_id, its outcome token, and a human label."""

    fixture_id: int
    condition_id: str
    asset_id: str
    outcome_label: str
    question: str


# A resolver maps ONE fixture dict → a list of per-outcome market descriptor dicts, each with
# keys ``condition_id``/``asset_id``/``outcome_label``/``question``. Injectable so tests pass a
# FAKE (no Gamma); the real :func:`gamma_resolver` lazy-imports urllib inside the function.
ResolverFn = Callable[[Mapping[str, Any]], list[dict[str, Any]]]


def resolve_markets(
    fixtures: Iterable[Mapping[str, Any]],
    *,
    resolver: ResolverFn | None = None,
) -> list[ResolvedMarket]:
    """Resolve every fixture's three 1X2 markets via the injectable ``resolver`` seam.

    ``resolver`` defaults to :func:`gamma_resolver` (real, lazy-imports urllib). Tests pass a
    fake to avoid any Gamma call.
    """
    resolve = resolver if resolver is not None else gamma_resolver
    resolved: list[ResolvedMarket] = []
    seen_labels: dict[int, set[str]] = {}
    for fixture in fixtures:
        fixture_id = int(fixture["fixture_id"])
        for descriptor in resolve(fixture):
            label = str(descriptor["outcome_label"])
            # The writer keys artifact files purely on {fixture_id}_{outcome_label}; two markets
            # colliding on one stem would SILENTLY overwrite six files. Fail loudly instead.
            if label in seen_labels.setdefault(fixture_id, set()):
                raise ValueError(
                    f"duplicate outcome_label {label!r} for fixture {fixture_id}: two resolved "
                    f"markets map to the same '{fixture_id}_{label}' artifact stem (would overwrite)"
                )
            seen_labels[fixture_id].add(label)
            resolved.append(
                ResolvedMarket(
                    fixture_id=fixture_id,
                    condition_id=str(descriptor["condition_id"]),
                    asset_id=str(descriptor["asset_id"]),
                    outcome_label=label,
                    question=str(descriptor.get("question", "")),
                )
            )
    return resolved


def gamma_resolver(fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Real Gamma resolver: GET ``/events?slug=...`` → the event's three 1X2 markets.

    Lazy-imports ``urllib`` INSIDE the function so module import stays network-free. PUBLIC — no
    auth. Operator note: Gamma field names should be verified against the live JSON on first run;
    this parses the documented ``markets[].conditionId`` / ``clobTokenIds`` / ``outcomes`` /
    ``question`` shape and takes the FIRST clob token as the outcome token per market.
    """
    import urllib.parse  # noqa: PLC0415 — lazy: keep module import network-free
    import urllib.request  # noqa: PLC0415 — lazy: keep module import network-free

    slug = str(fixture["event_slug"])
    query = urllib.parse.urlencode({"slug": slug})
    url = f"{GAMMA_EVENTS_URL}?{query}"
    # Gamma sits behind Cloudflare, which 403s the default Python-urllib UA — send a browser UA.
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (veridex-research)"})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — fixed https Gamma host
        events = json.loads(resp.read().decode("utf-8"))
    if not events:
        return []
    markets = events[0].get("markets", [])
    home = str(fixture.get("home_team", "")).lower()
    away = str(fixture.get("away_team", "")).lower()

    descriptors: list[dict[str, Any]] = []
    for market in markets:
        token_ids_raw = market.get("clobTokenIds")
        token_ids = json.loads(token_ids_raw) if isinstance(token_ids_raw, str) else (token_ids_raw or [])
        if not token_ids:
            continue
        question = str(market.get("question", ""))
        q_lower = question.lower()
        # Draw FIRST: the draw question ("Will {home} vs. {away} end in a draw?") contains the
        # home team name, so a home-name check ahead of this would mislabel draw as home_win.
        if "draw" in q_lower or "tie" in q_lower:
            label = "draw"
        elif home and home in q_lower:
            label = "home_win"
        elif away and away in q_lower:
            label = "away_win"
        else:
            label = _slug(question)
        descriptors.append(
            {
                "condition_id": str(market.get("conditionId", "")),
                "asset_id": str(token_ids[0]),
                "outcome_label": label,
                "question": question,
            }
        )
    return descriptors


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in text.strip().lower()).strip("_")[:40] or "outcome"


# --------------------------------------------------------------------------- archive row → frame
def _parse_levels(raw: Any) -> list[dict[str, str]]:
    """Convert an archive book side (JSON string / list of ``[price, size]``) → level dicts.

    The maintainer's ``book_snapshot_from_json`` expects ``[{"price":..,"size":..}, ...]``.
    """
    if raw is None or raw == "":
        return []
    levels = json.loads(raw) if isinstance(raw, str) else raw
    return [{"price": lvl[0], "size": lvl[1]} for lvl in levels]


def archive_row_to_frame(row: Mapping[str, Any]) -> VenueBookFrame | None:
    """Map one pmxt archive row → a :class:`VenueBookFrame` the maintainer can apply.

    ``recv_ts`` is the pmxt ingest clock (``timestamp_received``, ms). The venue source
    ``timestamp`` is threaded into ``payload['timestamp']`` (the maintainer's stale/order check).
    Non-book/delta events (``last_trade_price`` etc.) → ``None`` (ignored). One ``price_change``
    row becomes a single-change frame in the maintainer's ``price_changes`` list shape.
    """
    event_type = row.get("event_type")
    recv_ts = int(row["timestamp_received"])
    src_ts = row.get("timestamp")
    asset_id = row.get("asset_id")
    market = row.get("market")

    if event_type == _BOOK:
        payload = {
            "event_type": _BOOK,
            "asset_id": asset_id,
            "market": market,
            "timestamp": src_ts,
            "bids": _parse_levels(row.get("bids")),
            "asks": _parse_levels(row.get("asks")),
        }
        return VenueBookFrame(recv_ts=recv_ts, event_type=_BOOK, token_id=asset_id, payload=payload)

    if event_type == _PRICE_CHANGE:
        change = {
            "asset_id": asset_id,
            "side": row.get("side"),
            "price": row.get("price"),
            "size": row.get("size"),
            "best_bid": row.get("best_bid"),
            "best_ask": row.get("best_ask"),
        }
        payload = {
            "event_type": _PRICE_CHANGE,
            "market": market,
            "timestamp": src_ts,
            "price_changes": [change],
        }
        return VenueBookFrame(recv_ts=recv_ts, event_type=_PRICE_CHANGE, token_id=None, payload=payload)

    if event_type == _TICK_SIZE_CHANGE:
        payload = {
            "event_type": _TICK_SIZE_CHANGE,
            "asset_id": asset_id,
            "market": market,
            "timestamp": src_ts,
            "new_tick_size": row.get("new_tick_size"),
        }
        return VenueBookFrame(recv_ts=recv_ts, event_type=_TICK_SIZE_CHANGE, token_id=asset_id, payload=payload)

    return None  # last_trade_price and any other event → ignored (not a book delta)


# --------------------------------------------------------------------------- reconstruction
def _frame_token(frame: VenueBookFrame) -> str | None:
    """The single token a mapped archive frame touches (delta frames are 1-change)."""
    if frame.event_type == _PRICE_CHANGE:
        changes = frame.payload.get("price_changes", [])
        return changes[0].get("asset_id") if changes else None
    return frame.token_id


def _levels_to_pairs(book: Mapping[float, float], *, reverse: bool, top_n: int) -> list[list[float]]:
    ordered = sorted(book.items(), key=lambda kv: kv[0], reverse=reverse)
    return [[price, size] for price, size in ordered[:top_n] if size > 0.0][:top_n]


def _snapshot_row(
    token: str,
    frame: VenueBookFrame,
    state: BookState,
    meta: Mapping[str, Any] | None,
    *,
    top_n: int,
    ts_ms: int,
) -> dict[str, Any]:
    return {
        "ts_ms": ts_ms,
        "source_ts_ms": state.last_ts,
        "best_bid": state.best_bid,
        "best_ask": state.best_ask,
        "mid": state.mid,
        "bids": _levels_to_pairs(state.bids, reverse=True, top_n=top_n),
        "asks": _levels_to_pairs(state.asks, reverse=False, top_n=top_n),
        "condition_id": (meta or {}).get("condition_id", state.venue_market_ref),
        "asset_id": token,
        "fixture_id": (meta or {}).get("fixture_id"),
        "outcome_label": (meta or {}).get("outcome_label"),
        "gap": False,
        "gap_reason": None,
    }


def _gap_row(
    token: str,
    frame: VenueBookFrame,
    meta: Mapping[str, Any] | None,
    reason: str,
    *,
    ts_ms: int,
) -> dict[str, Any]:
    return {
        "ts_ms": ts_ms,
        "source_ts_ms": _coerce_ms(frame.payload.get("timestamp")),
        "best_bid": None,
        "best_ask": None,
        "mid": None,
        "bids": [],
        "asks": [],
        "condition_id": (meta or {}).get("condition_id", frame.payload.get("market")),
        "asset_id": token,
        "fixture_id": (meta or {}).get("fixture_id"),
        "outcome_label": (meta or {}).get("outcome_label"),
        "gap": True,
        "gap_reason": reason,
    }


def reconstruct_book_series(
    rows: Iterable[Mapping[str, Any]],
    *,
    interval_s: float = 1.0,
    top_n: int = 10,
    meta_by_asset: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Reconstruct a downsampled per-token L2 book series from ordered pmxt archive rows.

    Drives the SEALED :class:`BookStateMaintainer` merge (identical to the live R3 path). Emits
    ONE snapshot per (token, ``interval_s`` bucket) — the LAST state in the bucket, top-``top_n``
    levels per side — plus an explicit gap-marker row wherever a ``price_change`` arrived before a
    ``book`` seed or the maintainer discarded state (checksum/stale). ``meta_by_asset`` optionally
    supplies ``condition_id``/``fixture_id``/``outcome_label`` per token.

    Rows MUST already be ordered by ``timestamp_received`` (the archive query's ``ORDER BY``).
    """
    interval_ms = max(1, int(round(interval_s * 1000)))
    maintainer = BookStateMaintainer()
    out: dict[str, list[dict[str, Any]]] = {}
    pending: dict[str, tuple[int, dict[str, Any]]] = {}  # token → (bucket_id, row)

    def flush(token: str) -> None:
        prior = pending.pop(token, None)
        if prior is not None:
            out.setdefault(token, []).append(prior[1])

    for raw_row in _sorted_by_recv_ts(rows):
        frame = archive_row_to_frame(raw_row)
        if frame is None:
            continue
        token = _frame_token(frame)
        if token is None:
            maintainer.apply_frame(frame)
            continue

        meta = None if meta_by_asset is None else meta_by_asset.get(token)
        bucket_id = frame.recv_ts // interval_ms
        bucket_ts = bucket_id * interval_ms

        pre = maintainer.state(token)
        pre_seeded = pre is not None and pre.seeded
        is_delta = frame.event_type == _PRICE_CHANGE

        maintainer.apply_frame(frame)

        post = maintainer.state(token)
        post_seeded = post is not None and post.seeded

        gap_reason: str | None = None
        if is_delta and not pre_seeded:
            gap_reason = "delta_before_seed"  # delta with no prior book → honest gap, never guess
        elif pre_seeded and not post_seeded:
            gap_reason = "checksum_or_stale_reseed"  # maintainer discarded state → honest gap

        if gap_reason is not None:
            flush(token)  # emit any pending snapshot before the break, then the gap marker
            out.setdefault(token, []).append(_gap_row(token, frame, meta, gap_reason, ts_ms=bucket_ts))
            continue

        if not post_seeded:
            continue  # e.g. a tick_size_change on an unseeded token → nothing to snapshot yet

        assert post is not None
        prev = pending.get(token)
        if prev is not None and prev[0] != bucket_id:
            flush(token)  # bucket boundary crossed → emit the previous bucket's last state
        pending[token] = (bucket_id, _snapshot_row(token, frame, post, meta, top_n=top_n, ts_ms=bucket_ts))

    for token in list(pending.keys()):
        flush(token)
    return out


def series_summary(series: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, dict[str, Any]]:
    """Per-token summary: row count, recv-time span, and #gaps (for the CLI print)."""
    summary: dict[str, dict[str, Any]] = {}
    for token, token_rows in series.items():
        ts_values = [r["ts_ms"] for r in token_rows]
        gaps = sum(1 for r in token_rows if r.get("gap"))
        summary[token] = {
            "rows": len(token_rows),
            "ts_min": min(ts_values) if ts_values else None,
            "ts_max": max(ts_values) if ts_values else None,
            "gaps": gaps,
        }
    return summary


# ===================================================================== OPTION A — direct series
# Four honest, per-artifact series built straight from the archive columns (no ladder merge):
# the DIRECT top-of-book (primary), periodic full-depth ``book`` snapshots, ``last_trade_price``
# prints, and the demoted continuous-merge diagnostic (opt-in). The direct top-of-book routes
# EVERY row through the shared :func:`~veridex.live_recorder.top_of_book.extract_top_of_book` so a
# future R3 increment can adopt the same normalization on the live WS path.


def _meta_fields(meta: Mapping[str, Any] | None, token: str, *, market: Any = None) -> dict[str, Any]:
    """The shared per-row identity fields (condition/asset/fixture/outcome) — never honesty labels."""
    meta = meta or {}
    return {
        "condition_id": meta.get("condition_id", market),
        "asset_id": token,
        "fixture_id": meta.get("fixture_id"),
        "outcome_label": meta.get("outcome_label"),
    }


def _recv_ts_or_none(row: Mapping[str, Any]) -> int | None:
    """The row's ``timestamp_received`` as int, or ``None`` for a NULL / non-integer value.

    A single NULL ``timestamp_received`` must NOT crash the whole pull (fail-closed but all-or-
    nothing); the caller skips-and-drops that row instead.
    """
    raw = row.get("timestamp_received")
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _sorted_by_recv_ts(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Stable-sort rows by ``timestamp_received`` so last-in-bucket == latest-by-receipt.

    Global ordering is otherwise only ASSUMED (per-file ``ORDER BY`` + chronological hour URLs);
    any boundary spillover would silently corrupt last-wins buckets. NULL-ts rows sort to the end
    (they are skipped by the builders regardless).
    """
    return sorted(rows, key=lambda r: (_recv_ts_or_none(r) is None, _recv_ts_or_none(r) or 0))


def build_top_of_book_series(
    rows: Iterable[Mapping[str, Any]],
    *,
    interval_ms: int,
    meta_by_asset: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Build the PRIMARY direct top-of-book series from ``price_change`` rows' own best_bid/ask.

    EVENT-BUCKET downsample (no ladder reconstruction, no carry-forward): each row's
    ``bucket_id = ts_recv // interval_ms`` and, within a bucket ``[bucket_start, bucket_end)``, the
    LAST row wins (rows arrive ordered by ``timestamp_received``, so a later row overwrites). An
    EMPTY bucket is simply never created → OMITTED, never surfaced as a stale-as-fresh copy of a
    prior bucket. Every row is normalized through :func:`extract_top_of_book` (``ok``/``gap``/
    ``excluded`` with a reason; missing best → gap, crossed → excluded, both disclosed).
    """
    step = max(1, int(interval_ms))
    per_token: dict[str, dict[int, dict[str, Any]]] = {}
    for row in _sorted_by_recv_ts(rows):
        if row.get("event_type") != _PRICE_CHANGE:
            continue
        token = row.get("asset_id")
        if token is None:
            continue
        ts_recv = _recv_ts_or_none(row)
        if ts_recv is None:  # NULL/invalid timestamp_received → skip this row, never crash the pull
            continue
        bucket_id = ts_recv // step
        tob = extract_top_of_book(row.get("best_bid"), row.get("best_ask"))
        built = {
            "recv_ts_ms": ts_recv,  # TRUE receipt of this row (consistent with depth/trades)
            "bucket_ts_ms": bucket_id * step,  # bucket grid START (floor); assignment never looks ahead
            "source_ts_ms": _coerce_ms(row.get("timestamp")),
            "best_bid": tob.bid,
            "best_ask": tob.ask,
            "mid": tob.mid,
            "spread": tob.spread,
            "status": tob.status,
            "status_reason": tob.reason,
            **_meta_fields(None if meta_by_asset is None else meta_by_asset.get(token), token, market=row.get("market")),
        }
        per_token.setdefault(token, {})[bucket_id] = built  # LAST-in-bucket wins; empty → absent
    return {token: [buckets[b] for b in sorted(buckets)] for token, buckets in per_token.items()}


def build_depth_series(
    rows: Iterable[Mapping[str, Any]],
    *,
    top_n: int,
    meta_by_asset: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Build the periodic full-depth series from ``book`` snapshot rows (the queue/slippage ref).

    One row per ``book`` event (NOT downsampled — book snapshots are already periodic): the top-N
    ladder per side, derived best_bid/best_ask/mid, and the current ``tick_size`` (tracked from
    ``tick_size_change`` rows, default ``0.01``).
    """
    tick_by_token: dict[str, float] = {}
    per_token: dict[str, list[dict[str, Any]]] = {}
    for row in _sorted_by_recv_ts(rows):
        event_type = row.get("event_type")
        token = row.get("asset_id")
        if event_type == _TICK_SIZE_CHANGE and token is not None:
            # Route through the finite-guard: a malformed/non-finite tick must not crash the pull
            # or put NaN on depth rows (same all-or-nothing class as the NULL-ts fix).
            new_tick = _to_float_or_none(row.get("new_tick_size"))
            if new_tick is not None:
                tick_by_token[token] = new_tick
            continue
        if event_type != _BOOK or token is None:
            continue
        ts_recv = _recv_ts_or_none(row)
        if ts_recv is None:  # NULL/invalid timestamp_received → skip this book row, never crash
            continue
        bid_levels, dropped_bids = _finite_levels(_levels_from_raw(row.get("bids")))
        ask_levels, dropped_asks = _finite_levels(_levels_from_raw(row.get("asks")))
        bids = sorted((lvl for lvl in bid_levels if lvl[1] > 0.0), key=lambda lvl: lvl[0], reverse=True)[:top_n]
        asks = sorted((lvl for lvl in ask_levels if lvl[1] > 0.0), key=lambda lvl: lvl[0])[:top_n]
        best_bid = bids[0][0] if bids else None
        best_ask = asks[0][0] if asks else None
        # A crossed/locked book (best_bid >= best_ask) gets NO mid — the primary top-of-book
        # EXCLUDES crossed moments, so depth must not fabricate a mid the primary refuses to price.
        crossed = best_bid is not None and best_ask is not None and best_bid >= best_ask
        mid = (best_bid + best_ask) / 2.0 if (best_bid is not None and best_ask is not None and not crossed) else None
        per_token.setdefault(token, []).append(
            {
                "recv_ts_ms": ts_recv,
                "source_ts_ms": _coerce_ms(row.get("timestamp")),
                "bids": bids,
                "asks": asks,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "mid": mid,
                "crossed": crossed,
                "tick_size": tick_by_token.get(token, _DEFAULT_TICK_SIZE),
                "dropped_levels": dropped_bids + dropped_asks,
                **_meta_fields(None if meta_by_asset is None else meta_by_asset.get(token), token, market=row.get("market")),
            }
        )
    return per_token


def build_trades_series(
    rows: Iterable[Mapping[str, Any]],
    *,
    meta_by_asset: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Build the trade-print series from ``last_trade_price`` rows (venue prints, NOT our fills).

    Per row: recv/source ms, ``price``, ``size``, ``side`` (only if the row carries one), and the
    ``transaction_hash`` anchor. All other event types are ignored.
    """
    per_token: dict[str, list[dict[str, Any]]] = {}
    for row in _sorted_by_recv_ts(rows):
        if row.get("event_type") != _LAST_TRADE_PRICE:
            continue
        token = row.get("asset_id")
        if token is None:
            continue
        ts_recv = _recv_ts_or_none(row)
        if ts_recv is None:  # NULL/invalid timestamp_received → skip this trade, never crash
            continue
        trade: dict[str, Any] = {
            "recv_ts_ms": ts_recv,
            "source_ts_ms": _coerce_ms(row.get("timestamp")),
            "price": _to_float_or_none(row.get("price")),
            "size": _to_float_or_none(row.get("size")),
        }
        side = row.get("side")
        if side is not None and side != "":
            trade["side"] = side
        trade["transaction_hash"] = row.get("transaction_hash")
        trade.update(_meta_fields(None if meta_by_asset is None else meta_by_asset.get(token), token, market=row.get("market")))
        per_token.setdefault(token, []).append(trade)
    return per_token


def _levels_from_raw(raw: Any) -> list[Sequence[Any]]:
    """A book side (JSON string / list of ``[price, size]``) → a list of ``[price, size]`` pairs."""
    if raw is None or raw == "":
        return []
    levels = json.loads(raw) if isinstance(raw, str) else raw
    return list(levels)


def _to_float_or_none(raw: Any) -> float | None:
    """Parse ``raw`` → float, or ``None`` for absent / non-numeric / NON-FINITE (NaN/Inf) input.

    A non-finite value survives ``float()`` (``float("nan")`` succeeds), so it must be gated
    explicitly — otherwise NaN/Infinity leaks into the JSONL artifacts (invalid strict JSON and
    undefined ``sorted()`` ordering). Mirrors the extractor's ``math.isfinite`` gate.
    """
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _finite_levels(raw_levels: Iterable[Sequence[Any]]) -> tuple[list[list[float]], int]:
    """Parse book ladder levels, DROPPING any level with a non-finite/non-numeric price or size.

    Returns ``(finite_levels, dropped_count)`` — the dropped count is surfaced on the depth row so
    a poisoned level is COUNTED, never silently emitted (and never fed into ``sorted()``).
    """
    finite: list[list[float]] = []
    dropped = 0
    for lvl in raw_levels:
        price = _to_float_or_none(lvl[0])
        size = _to_float_or_none(lvl[1])
        if price is None or size is None:
            dropped += 1
            continue
        finite.append([price, size])
    return finite, dropped


def summarize_outcome(
    top_rows: Sequence[Mapping[str, Any]],
    depth_rows: Sequence[Mapping[str, Any]],
    trades_rows: Sequence[Mapping[str, Any]],
    *,
    total_price_change: int,
    interval_ms: int,
    window_start_ms: int | None = None,
    window_end_ms: int | None = None,
) -> dict[str, Any]:
    """Per-outcome honest counts + coverage.

    Two coverage numbers are reported so a near-dead market cannot masquerade as full coverage:

    * ``span_coverage`` = ``ok`` buckets / buckets spanned by the OBSERVED top-of-book rows
      (``span_buckets`` = last_bucket − first_bucket + 1). This is 100% for a single lonely quote.
    * ``window_coverage`` = ``ok`` buckets in-window / buckets of the REQUESTED pull window
      (``window_buckets`` from ``window_start_ms``/``window_end_ms``). ``None`` when no window is
      supplied. THIS is the honest denominator: one quote 2h into a 3h window reads far below 1.0.

    ``interval_ms`` and the window bounds are emitted so the artifact set is self-describing
    (bucket edges recomputable). ``crossed`` equals the ``excluded`` count (a crossed book is the
    only exclusion path); ``crossed_depth`` counts crossed ``book`` snapshots in ``depth.jsonl``.
    """
    step = max(1, int(interval_ms))
    gap = sum(1 for r in top_rows if r.get("status") == "gap")
    excluded = sum(1 for r in top_rows if r.get("status") == "excluded")
    ok_rows = [r for r in top_rows if r.get("status") == "ok"]
    if top_rows:
        bucket_ids = [int(r["bucket_ts_ms"]) // step for r in top_rows]
        span_buckets = max(bucket_ids) - min(bucket_ids) + 1
    else:
        span_buckets = 0
    ok_buckets = len({int(r["bucket_ts_ms"]) // step for r in ok_rows})
    span_coverage = (ok_buckets / span_buckets) if span_buckets else 0.0
    crossed_depth = sum(1 for r in depth_rows if r.get("crossed"))

    window_buckets: int | None = None
    window_coverage: float | None = None
    if window_start_ms is not None and window_end_ms is not None:
        window_buckets = max(0, (int(window_end_ms) // step) - (int(window_start_ms) // step) + 1)
        ok_in_window = len(
            {
                int(r["bucket_ts_ms"]) // step
                for r in ok_rows
                if int(window_start_ms) <= int(r["recv_ts_ms"]) <= int(window_end_ms)
            }
        )
        window_coverage = (ok_in_window / window_buckets) if window_buckets else 0.0

    return {
        "price_change_rows": int(total_price_change),
        "gap": gap,
        "excluded": excluded,
        "crossed": excluded,
        "book_snapshots": len(depth_rows),
        "crossed_depth": crossed_depth,
        "trades": len(trades_rows),
        "ok_buckets": ok_buckets,
        "interval_ms": step,
        "span_buckets": span_buckets,
        "span_coverage": span_coverage,
        "window_start_ms": window_start_ms,
        "window_end_ms": window_end_ms,
        "window_buckets": window_buckets,
        "window_coverage": window_coverage,
    }


# Honesty labels are SIDECAR metadata (never a JSONL data row). Ceilings pinned per the spec.
_HONESTY_LABELS = {
    "top_of_book": (
        "Direct venue best_bid/best_ask top quote (mid/spread), pmxt third-party RESEARCH-GRADE "
        "evidence — NOT cryptographic R3/Veridex proof; NOT queue position, fill probability, or "
        "maker PnL. recv_ts_ms is the TRUE pmxt-RECORDER-CLOCK receipt of the winning row "
        "(seconds-scale alignment, not sub-2s single-clock lead); bucket_ts_ms is the bucket grid "
        "START (floor). Join downstream on recv_ts_ms (recv_ts_ms <= decision_ts), NEVER on "
        "bucket_ts_ms — bucket_ts_ms is a lower bound and would admit a quote up to interval_ms-1 "
        "early (optimistic look-ahead). EVENT-BUCKET downsample, empty buckets omitted (no "
        "stale-as-fresh)."
    ),
    "depth": (
        "Full-depth / queue reference from periodic `book` snapshots ONLY, timestamp-bounded. "
        "recv_ts_ms is the pmxt recorder clock. Between-snapshot depth is NOT reconstructed here."
    ),
    "trades": (
        "`last_trade_price` venue PRINTS / anchors (price, size, transaction_hash) — NOT our "
        "fills. recv_ts_ms is the pmxt recorder clock."
    ),
    "merge_diagnostic": (
        "OPT-IN, FRAGILE diagnostic: the continuous-ladder BookStateMaintainer merge. High gap "
        "rate under this feed; DO NOT treat as the primary backtest book (selection bias)."
    ),
}


def build_meta(fixture_id: int, outcome_label: str, *, emit_merge_diagnostic: bool) -> dict[str, Any]:
    """The sidecar honesty-label metadata for one fixture×outcome (keyed per artifact)."""
    artifacts = ["top_of_book", "depth", "trades"]
    if emit_merge_diagnostic:
        artifacts.append("merge_diagnostic")
    return {
        "fixture_id": fixture_id,
        "outcome_label": outcome_label,
        "timestamp_received": "pmxt recorder clock (seconds-scale); NOT Veridex single-clock arrival",
        "artifacts": {name: _HONESTY_LABELS[name] for name in artifacts},
    }


_README_TEXT = """# pmxt WC 1X2 archive pull — Option A artifacts

Per fixture×outcome, up to four JSONL artifacts plus sidecar metadata:

- `{fixture}_{outcome}.top_of_book.jsonl` — PRIMARY. Direct venue `best_bid`/`best_ask` top
  quote (mid/spread), EVENT-BUCKET downsampled by `timestamp_received` (empty buckets omitted, no
  carry-forward). Status: `ok` | `gap` (missing/non-numeric/non-finite best) | `excluded` (crossed).
  `recv_ts_ms` is the TRUE receipt of the winning row; `bucket_ts_ms` is the bucket grid START.
  Downstream no-look-ahead joins MUST filter on `recv_ts_ms` (`recv_ts_ms <= decision_ts`), never
  on `bucket_ts_ms` (a lower bound that admits a quote up to `interval_ms-1` early).
- `{fixture}_{outcome}.depth.jsonl` — periodic full-depth `book` snapshots (queue/slippage ref).
  Non-finite ladder levels are dropped and counted (`dropped_levels`); a crossed snapshot carries
  `mid=null` + `crossed=true` (never a fabricated mid, consistent with the primary's exclusion).
- `{fixture}_{outcome}.trades.jsonl` — `last_trade_price` venue prints/anchors (not our fills).
- `{fixture}_{outcome}.summary.json` — honest counts + `interval_ms`, observed `span_coverage`,
  and requested-window `window_coverage` (a market silent for the window is NOT reported 100%).
- `{fixture}_{outcome}.merge_diagnostic.jsonl` — OPT-IN (`--emit-merge-diagnostic`), FRAGILE
  continuous-ladder merge; diagnostic only.
- `{fixture}_{outcome}.meta.json` — sidecar honesty labels (never a JSONL data row).

Honesty ceiling: research-grade third-party evidence, NOT cryptographic R3 proof;
`timestamp_received` is the pmxt recorder clock (seconds-scale), not a sub-2s single-clock lead;
top-of-book is quote/spread/mid, not queue/fill/PnL. JSONL rows are PURE data (labels live in
the sidecar `.meta.json`, never in a data row).
"""


def write_outcome_artifacts(
    *,
    out_dir: Path,
    fixture_id: int,
    outcome_label: str,
    top_rows: Sequence[Mapping[str, Any]],
    depth_rows: Sequence[Mapping[str, Any]],
    trades_rows: Sequence[Mapping[str, Any]],
    merge_rows: Sequence[Mapping[str, Any]] | None,
    summary: Mapping[str, Any],
    emit_merge_diagnostic: bool,
) -> dict[str, Path]:
    """Write the four (or three) JSONL artifacts + `.summary.json` + `.meta.json` for one outcome.

    The merge diagnostic is written ONLY when ``emit_merge_diagnostic``. Returns the written
    artifact→path map. JSONL rows stay PURE — honesty labels go only into the `.meta.json` sidecar.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{fixture_id}_{outcome_label}"
    written: dict[str, Path] = {}

    written["top_of_book"] = _write_jsonl(top_rows, out_dir / f"{stem}.top_of_book.jsonl")
    written["depth"] = _write_jsonl(depth_rows, out_dir / f"{stem}.depth.jsonl")
    written["trades"] = _write_jsonl(trades_rows, out_dir / f"{stem}.trades.jsonl")
    if emit_merge_diagnostic:
        written["merge_diagnostic"] = _write_jsonl(merge_rows or [], out_dir / f"{stem}.merge_diagnostic.jsonl")

    summary_path = out_dir / f"{stem}.summary.json"
    summary_path.write_text(json.dumps(dict(summary), indent=2, allow_nan=False) + "\n", encoding="utf-8")
    written["summary"] = summary_path

    meta_path = out_dir / f"{stem}.meta.json"
    meta_path.write_text(
        json.dumps(
            build_meta(fixture_id, outcome_label, emit_merge_diagnostic=emit_merge_diagnostic),
            indent=2,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )
    written["meta"] = meta_path
    return written


def _write_jsonl(rows: Sequence[Mapping[str, Any]], path: Path) -> Path:
    # allow_nan=False: a non-finite value (NaN/Infinity) is a boundary error — fail LOUDLY here
    # rather than write non-standard JSON tokens that strict readers reject or NaN-poison silently.
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, allow_nan=False) + "\n")
    return path


def write_readme(out_dir: Path) -> Path:
    """Write the per-directory README documenting the artifacts + honesty ceiling (once)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "README.md"
    path.write_text(_README_TEXT, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- output
def write_series_jsonl(
    token_rows: Sequence[Mapping[str, Any]],
    *,
    out_dir: Path,
    fixture_id: int,
    outcome_label: str,
) -> Path:
    """Write one token's reconstructed rows to ``{out_dir}/{fixture_id}_{outcome_label}.jsonl``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{fixture_id}_{outcome_label}.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for row in token_rows:
            handle.write(json.dumps(row, allow_nan=False) + "\n")
    return path


# --------------------------------------------------------------------------- archive pull (live)
def _hour_urls(kickoff_ts: int, *, pre_hours: int, post_hours: int) -> list[str]:
    """The hourly archive file URLs spanning ``[kickoff - pre, kickoff + post]`` UTC hours."""
    import datetime as _dt  # noqa: PLC0415 — stdlib; kept local for symmetry with lazy imports

    start = _dt.datetime.fromtimestamp(kickoff_ts, tz=_dt.UTC) - _dt.timedelta(hours=pre_hours)
    urls: list[str] = []
    for offset in range(pre_hours + post_hours + 1):
        hour = (start + _dt.timedelta(hours=offset)).strftime("%Y-%m-%dT%H")
        urls.append(ARCHIVE_URL_TEMPLATE.format(hour=hour))
    return urls


def fetch_archive_rows(
    urls: Sequence[str],
    condition_ids: Sequence[str],
) -> list[dict[str, Any]]:
    """Pull archive rows for ``condition_ids`` from the hourly files (LIVE; lazy-imports duckdb).

    Uses DuckDB httpfs with a ``market IN (...)`` predicate so only relevant row-groups are
    fetched. The ``market`` column is ``fixed_size_binary[66]`` → decoded from bytes to ascii;
    timestamps are CAST to BIGINT epoch-ms in SQL (native timestamp→Python needs pytz, absent).
    This is the operator's LIVE step and is never exercised by the offline test-suite.
    """
    import duckdb  # noqa: PLC0415 — lazy optional network client

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    in_list = ", ".join("?" for _ in condition_ids)
    rows: list[dict[str, Any]] = []
    for url in urls:
        sql = (
            "SELECT epoch_ms(timestamp_received) AS timestamp_received, "
            "epoch_ms(timestamp) AS timestamp, market, event_type, asset_id, "
            "bids, asks, price, size, side, best_bid, best_ask, new_tick_size, transaction_hash "
            f"FROM read_parquet('{url}') WHERE market IN ({in_list}) ORDER BY timestamp_received"
        )
        try:
            cursor = con.execute(sql, list(condition_ids))
        except Exception as exc:  # noqa: BLE001 — a missing hour file must not abort the whole pull
            # Distinguish a genuinely missing hour file (tolerate) from a real query/schema error
            # (e.g. a BinderException) — and ALWAYS surface the message, not just the type.
            msg = str(exc)
            missing_file = any(tok in msg for tok in ("404", "No files found", "HTTP", "Could not establish", "IO Error"))
            kind = "missing hour file (skipped)" if missing_file else "QUERY ERROR — investigate (skipped)"
            print(f"  skip {url}: {kind}: {type(exc).__name__}: {msg}")
            continue
        columns = [d[0] for d in cursor.description]
        for record in cursor.fetchall():
            row = dict(zip(columns, record, strict=True))
            market = row.get("market")
            if isinstance(market, (bytes, bytearray)):
                row["market"] = bytes(market).decode("ascii")
            rows.append(row)
    return rows


# --------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser. ``--help`` works with ZERO network/credentials (all imports lazy)."""
    parser = argparse.ArgumentParser(
        prog="pmxt_archive_pull",
        description="Pull the FREE public pmxt raw order-book archive for WC 1X2 markets and "
        "reconstruct a downsampled per-outcome L2 book series for backtesting.",
    )
    parser.add_argument("--fixtures", required=True, help="path to fixtures.json (fixture_id/event_slug/teams)")
    parser.add_argument("--fixture-id", type=int, default=None, dest="fixture_id", help="restrict to one fixture_id")
    parser.add_argument("--interval", type=float, default=1.0, help="downsample bucket seconds (default 1.0)")
    parser.add_argument("--top-n", type=int, default=10, dest="top_n", help="levels kept per side (default 10)")
    parser.add_argument("--pre-hours", type=int, default=1, dest="pre_hours", help="hours before kickoff (default 1)")
    parser.add_argument("--post-hours", type=int, default=2, dest="post_hours", help="hours after kickoff (default 2)")
    parser.add_argument("--out", default=".omc/research/pmxt-wc-books", help="output directory for per-outcome jsonl")
    parser.add_argument(
        "--emit-merge-diagnostic",
        action="store_true",
        dest="emit_merge_diagnostic",
        help="also emit the FRAGILE opt-in continuous-ladder merge diagnostic (default off)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Operator LIVE entrypoint: resolve markets, pull the archive, reconstruct, and write files."""
    args = build_parser().parse_args(argv)

    fixtures = json.loads(Path(args.fixtures).read_text())
    if args.fixture_id is not None:
        fixtures = [f for f in fixtures if int(f["fixture_id"]) == args.fixture_id]
    if not fixtures:
        print("no fixtures selected")
        return 1

    out_dir = Path(args.out)
    interval_ms = max(1, int(round(args.interval * 1000)))
    resolved = resolve_markets(fixtures)  # real Gamma resolver (lazy urllib)
    by_fixture: dict[int, list[ResolvedMarket]] = {}
    for market in resolved:
        by_fixture.setdefault(market.fixture_id, []).append(market)

    write_readme(out_dir)  # document the artifacts + honesty ceiling once, up front

    for fixture in fixtures:
        fixture_id = int(fixture["fixture_id"])
        markets = by_fixture.get(fixture_id, [])
        if not markets:
            print(f"fixture {fixture_id}: no markets resolved — skipping")
            continue
        condition_ids = sorted({m.condition_id for m in markets})
        meta_by_asset = {
            m.asset_id: {"condition_id": m.condition_id, "fixture_id": m.fixture_id, "outcome_label": m.outcome_label}
            for m in markets
        }
        urls = _hour_urls(int(fixture["kickoff_ts"]), pre_hours=args.pre_hours, post_hours=args.post_hours)
        print(f"fixture {fixture_id}: {len(condition_ids)} markets, {len(urls)} hourly files")
        rows = fetch_archive_rows(urls, condition_ids)
        # Requested pull window in recorder-clock ms — the HONEST coverage denominator (a market
        # silent for the whole window must not read as 100% off its own tiny observed span).
        kickoff_ms = int(fixture["kickoff_ts"]) * 1000
        window_start_ms = kickoff_ms - args.pre_hours * 3_600_000
        window_end_ms = kickoff_ms + args.post_hours * 3_600_000

        top_series = build_top_of_book_series(rows, interval_ms=interval_ms, meta_by_asset=meta_by_asset)
        depth_series = build_depth_series(rows, top_n=args.top_n, meta_by_asset=meta_by_asset)
        trades_series = build_trades_series(rows, meta_by_asset=meta_by_asset)
        merge_series = (
            reconstruct_book_series(rows, interval_s=args.interval, top_n=args.top_n, meta_by_asset=meta_by_asset)
            if args.emit_merge_diagnostic
            else {}
        )
        pc_by_token: dict[str, int] = {}
        for row in rows:
            if row.get("event_type") == _PRICE_CHANGE and row.get("asset_id") is not None:
                pc_by_token[row["asset_id"]] = pc_by_token.get(row["asset_id"], 0) + 1

        for market in markets:
            token = market.asset_id
            top_rows = top_series.get(token, [])
            depth_rows = depth_series.get(token, [])
            trades_rows = trades_series.get(token, [])
            summary = summarize_outcome(
                top_rows, depth_rows, trades_rows,
                total_price_change=pc_by_token.get(token, 0), interval_ms=interval_ms,
                window_start_ms=window_start_ms, window_end_ms=window_end_ms,
            )
            write_outcome_artifacts(
                out_dir=out_dir, fixture_id=fixture_id, outcome_label=market.outcome_label,
                top_rows=top_rows, depth_rows=depth_rows, trades_rows=trades_rows,
                merge_rows=merge_series.get(token, []), summary=summary,
                emit_merge_diagnostic=args.emit_merge_diagnostic,
            )
            wb = summary["window_buckets"] if summary["window_buckets"] is not None else summary["span_buckets"]
            cov = summary["window_coverage"] if summary["window_coverage"] is not None else summary["span_coverage"]
            print(
                f"  {market.outcome_label} [{token[:12]}...]: "
                f"price_change={summary['price_change_rows']} top_ok_buckets={summary['ok_buckets']}/"
                f"{wb} coverage={cov:.1%} "
                f"gap={summary['gap']} excluded/crossed={summary['excluded']} "
                f"book={summary['book_snapshots']}/crossed={summary['crossed_depth']} trades={summary['trades']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
