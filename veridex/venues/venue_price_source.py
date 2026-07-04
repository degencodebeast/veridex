"""C-2 (S5) — the time-aligned venue price seam: the ONLY door venue data enters the VvV trust path.

Where S5's first cut injected a market-key-only ``Callable[[str], float | None]``, the trust path a
real Polymarket backfill demands is TIME-ALIGNED: a quote must be looked up for the exact decision
coordinate — the ``(fixture_id, market_key, side, ts)`` the agent is deciding on — so the price the
edge is estimated against is the one that was on offer *at that tick*, carrying the measured
``staleness_s`` of that alignment (never an interpolated or future price; CON-006).

Two boundaries stay load-bearing (proven in the agent/producer tests, not here):

  * **Venue data enters ONLY through this injected source (SEC-003).** The agent never reads a venue
    price out of the (evidence-sealed) ``market_state``; it calls the source with the decision
    coordinate and reads ``.venue_decimal_price``.
  * **The quote's NUMBERS never ride into evidence (CON-002).** ``venue_decimal_price`` / ``staleness_s``
    are venue numbers: a venue-driven change of ACTION (fire vs wait) legitimately changes evidence,
    but the numbers themselves never enter ``AgentAction.params`` / law / scoring / the sealed
    ``run_events``.

``None`` is the fail-safe: no quote at/under the caller's freshness bound ⇒ ``None`` ⇒ no edge to
estimate ⇒ the agent WAITs (there is nothing to price against). Staleness/freshness bounding is the
SOURCE's job (C-4); this module only fixes the shape of the seam both consumers share.
"""

from __future__ import annotations

import bisect
import hashlib
import json
from collections import defaultdict
from collections.abc import Callable, Sequence
from typing import TypeAlias

from pydantic import BaseModel

from veridex.venues.price_history import VenuePriceHistoryFrame


class TimedVenueQuote(BaseModel):
    """A venue DECIMAL price time-aligned to a decision tick, with the staleness of that alignment.

    Attributes:
        venue_decimal_price: The venue's DECIMAL odds (``1/q`` and thus always ``> 1.0``) on offer for
            the priced side at the decision tick. Consumers feed this straight into the raw EV math
            (``executable_edge_bps`` / ``vvv_signal``) — frames are already decimal (AC-014), so
            ``native_to_decimal`` is NOT re-applied at this seam.
        staleness_s: How stale (seconds) the aligned quote is relative to the decision ``ts`` — the
            gap between the decision and the quote actually used. Reported/attached at the report
            layer; the freshness BOUND that turns a too-stale quote into ``None`` lives in the source.
    """

    venue_decimal_price: float
    staleness_s: int


#: A time-aligned venue price source: ``(fixture_id, market_key, side, ts) -> TimedVenueQuote | None``.
#: ``None`` means "no quote available at/under the caller's freshness bound" — the fail-safe the agent
#: WAITs on. The venue's identity (which artifact it prices against) is pinned SEPARATELY via the
#: agent's ``venue_source_id`` (config-hash identity), never smuggled through the returned numbers.
#:
#: TIME UNIT CONTRACT (load-bearing): ``ts`` is unix **SECONDS** — the Polymarket-canonical scale the
#: backfilled :class:`~veridex.venues.price_history.VenuePriceHistoryFrame` timestamps live in. TxLINE
#: ``MarketState.ts`` is unix **MILLISECONDS**, so every call site that prices a TxLINE tick MUST convert
#: via :func:`txline_ts_to_venue_seconds` first. Feeding a raw ms ``ts`` to a seconds-indexed source makes
#: ``staleness_s = ts - frame.ts ≈ 1e12`` — always past any sane ``freshness_s`` bound — so EVERY lookup
#: fails to ``None`` (the Run-002-VvV 0%-coverage artifact). The contract is explicit here, NOT sniffed
#: from the magnitude of ``ts`` inside the source.
VenuePriceSource: TypeAlias = Callable[[int, str, str, int], TimedVenueQuote | None]


def txline_ts_to_venue_seconds(ts_ms: int) -> int:
    """Convert a TxLINE tick timestamp (unix MILLISECONDS) to the venue source's unix-SECONDS contract.

    ``MarketState.ts`` is unix milliseconds; a :data:`VenuePriceSource` is keyed by unix seconds
    (matching the Polymarket-canonical backfilled frames). Call this at EVERY site that prices a TxLINE
    tick against the source, so the resulting :attr:`TimedVenueQuote.staleness_s` comes out in seconds
    (comparable to ``freshness_s``) rather than the ≈1e12 a raw ms/seconds subtraction would produce.

    Args:
        ts_ms: A TxLINE ``MarketState.ts`` in unix milliseconds.

    Returns:
        The same instant floored to whole unix seconds (``ts_ms // 1000``).
    """
    return ts_ms // 1000


#: The REAL TxLINE market_key for the 1X2 FULL-match market: ``{SuperOddsType}|{MarketPeriod}|{MarketParameters}``
#: with an EMPTY period/params (full match) collapses to ``"1X2_PARTICIPANT_RESULT||"``. The 1X2 HALF-match
#: market is ``"1X2_PARTICIPANT_RESULT|half=1|"`` (a different key) and has NO C/P1 frames.
_TXLINE_1X2_FULL_MARKET_KEY = "1X2_PARTICIPANT_RESULT||"

#: The REAL TxLINE 1X2 side tokens (``PriceNames`` in the raw feed, ``stable_prob_bps`` keys after
#: normalization) → the C-3 frame ``market_ref``'s side token. ``part1`` is the HOME participant and
#: ``part2`` the AWAY participant (listed first/second in TxLINE), verified against ``packs/18179763``
#: both structurally and numerically (part1 ≈ venue home implied prob, part2 ≈ venue away). ``draw``
#: is the draw. Any other token (e.g. a raw ``home``/``away``) is unmappable → no frame → ``None``.
_TXLINE_1X2_SIDE_TO_VENUE_SIDE = {"part1": "home", "part2": "away", "draw": "draw"}


def txline_market_to_venue_ref(market_key: str, side: str) -> str | None:
    """Bridge a TxLINE ``(market_key, side)`` decision coordinate to a C-3 frame ``market_ref``, or ``None``.

    The C-3 :class:`~veridex.venues.price_history.VenuePriceHistoryFrame`s are keyed by a Polymarket-side
    ``market_ref`` that BAKES the side into the ref (``"1X2|home|full"``). The REAL TxLINE marketstate
    instead keys the 1X2 FULL-match market as ``"1X2_PARTICIPANT_RESULT||"`` with the side as a SEPARATE
    dimension (``part1`` / ``draw`` / ``part2`` in ``stable_prob_bps``). This function is the identity
    bridge between those two namespaces: given the TxLINE key + side the agent is deciding on, it returns
    the frame ``market_ref`` to look the quote up under — the string the source's index is keyed by.

    Only the 1X2 FULL-match market is in venue scope for C/P1: Polymarket frames exist for that market
    and all three sides ONLY. Every other TxLINE market — 1X2 HALF-match (``"...|half=1|"``),
    Asian-handicap, over/under — has NO frame and returns ``None`` so its decisions SKIP the venue lookup
    (out of venue scope, distinct from "in scope but no quote near this tick"). An unrecognized side token
    on the covered market also returns ``None`` (never guess a wrong ref).

    Args:
        market_key: The TxLINE ``MarketState.markets`` key (e.g. ``"1X2_PARTICIPANT_RESULT||"``).
        side: The TxLINE side token (a ``stable_prob_bps`` key, e.g. ``"part1"``).

    Returns:
        The C-3 frame ``market_ref`` (e.g. ``"1X2|home|full"``) to query the source with, or ``None``
        when the coordinate is out of the C/P1 venue scope (no backfilled frame exists for it).
    """
    if market_key != _TXLINE_1X2_FULL_MARKET_KEY:
        return None
    venue_side = _TXLINE_1X2_SIDE_TO_VENUE_SIDE.get(side)
    if venue_side is None:
        return None
    return f"1X2|{venue_side}|full"


def _compute_venue_source_id(
    *,
    price_history_artifact_hashes: Sequence[str],
    coverage_artifact_hash: str,
    freshness_s: int,
    haircut_ladder_bps: Sequence[int],
    source_config_version: str,
) -> str:
    """sha256 over the five reproducibility inputs, canonically serialized so it's deterministic.

    The price-history artifact hashes are SORTED (order of the backfill packs is immaterial to the
    source's identity), but the haircut ladder is kept ORDERED — ``[0, 100, 200, 300]`` and
    ``[0, 200]`` are genuinely different report configurations and MUST hash differently. Any of the
    five inputs changing ⇒ a different id, so the agent's ``config_hash`` provably pins exactly which
    artifact(s)/coverage/bound/ladder/config it priced against (spec §venue_source_id, CON-005).
    """
    payload = {
        "price_history_artifact_hashes": sorted(price_history_artifact_hashes),
        "coverage_artifact_hash": coverage_artifact_hash,
        "freshness_s": freshness_s,
        "haircut_ladder_bps": list(haircut_ladder_bps),
        "source_config_version": source_config_version,
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def build_backfilled_venue_source(
    frames: Sequence[VenuePriceHistoryFrame],
    *,
    price_history_artifact_hashes: Sequence[str],
    coverage_artifact_hash: str,
    freshness_s: int,
    haircut_ladder_bps: Sequence[int],
    source_config_version: str = "cp1-v1",
) -> tuple[VenuePriceSource, str]:
    """Build a bounded-staleness, time-indexed :data:`VenuePriceSource` over backfilled frames (C-4).

    This is the time-alignment C-2's seam only fixed the SHAPE of: on a decision coordinate the source
    returns the LATEST frame at/before ``ts`` whose age ``ts - frame.ts <= freshness_s``, carrying the
    real ``staleness_s`` of that alignment. It NEVER interpolates, NEVER looks ahead (a frame at
    ``ts' > ts`` is invisible), and a missing/too-stale quote is ``None`` — the fail-safe the agent
    WAITs on (CON-006). The returned price is the RAW ``venue_decimal_price``; the ``haircut_ladder_bps``
    is carried for identity/reporting ONLY (C-5) and never alters the quote (CON-007, REQ-005).

    Args:
        frames: Backfilled price-history frames (already decimal via AC-014; ``native_to_decimal`` is
            NOT re-applied at this seam). Frames are indexed by ``(fixture_id, market_ref)``.
        price_history_artifact_hashes: The C-3 ``VenuePriceHistoryPack.artifact_content_hash``es the
            frames came from (a frame carries no hash of its own). Order-independent in the identity.
        coverage_artifact_hash: The C-1 coverage-artifact hash.
        freshness_s: The staleness bound in seconds; a quote strictly older than this is ``None``.
        haircut_ladder_bps: Round-trip haircut ladder (bps) — report-only; pinned in the identity but
            NEVER applied to the returned quote.
        source_config_version: Source config version tag, part of the identity.

    Returns:
        A ``(source, venue_source_id)`` pair. ``venue_source_id`` is the sha256 reproducibility hash
        over the five identity inputs; the ``source`` is keyed by ``(fixture_id, market_key, side, ts)``.
    """
    # Index frames by (fixture_id, market_ref) → sorted [(ts, decimal_price), ...]. The WC 1X2
    # market_ref ("1X2|home|full", per polymarket_resolver) already embeds the side, and the VvV
    # agent passes that SAME string as market_key — so the lookup keys on market_key directly and the
    # side arg is a redundant confirmation of what the ref already names.
    index: dict[tuple[int, str], list[tuple[int, float]]] = defaultdict(list)
    for frame in frames:
        index[(frame.fixture_id, frame.market_ref)].append((frame.ts, frame.venue_decimal_price))
    for pairs in index.values():
        pairs.sort(key=lambda pair: pair[0])
    # Parallel ts-only lists for a bisect over the "most recent at or before ts" boundary.
    ts_index: dict[tuple[int, str], list[int]] = {
        key: [ts for ts, _ in pairs] for key, pairs in index.items()
    }

    def source(fixture_id: int, market_key: str, side: str, ts: int) -> TimedVenueQuote | None:
        # ``ts`` is unix SECONDS (the frame scale) — callers convert TxLINE ms ts via
        # ``txline_ts_to_venue_seconds`` so ``staleness_s`` below is a seconds gap, not ≈1e12.
        key = (fixture_id, market_key)
        pairs = index.get(key)
        if not pairs:
            return None  # no frames for this (fixture, market) — fail-safe None
        # bisect_right gives the first index whose ts is STRICTLY greater than the decision ts, so
        # pos-1 is the most recent frame AT OR BEFORE ts — a later frame (ts' > ts) is never seen.
        pos = bisect.bisect_right(ts_index[key], ts)
        if pos == 0:
            return None  # no quote at/before ts — no look-ahead to a future frame
        frame_ts, decimal_price = pairs[pos - 1]
        staleness_s = ts - frame_ts
        if staleness_s > freshness_s:
            return None  # too stale — never interpolate to fill the gap
        return TimedVenueQuote(venue_decimal_price=decimal_price, staleness_s=staleness_s)

    venue_source_id = _compute_venue_source_id(
        price_history_artifact_hashes=price_history_artifact_hashes,
        coverage_artifact_hash=coverage_artifact_hash,
        freshness_s=freshness_s,
        haircut_ladder_bps=haircut_ladder_bps,
        source_config_version=source_config_version,
    )
    return source, venue_source_id
