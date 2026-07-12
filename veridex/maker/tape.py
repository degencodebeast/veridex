"""Real cp1 maker-tape builder: REAL ReplayPack FV + pinned-frame venue mid, both coordinate systems.

The maker tape is the per-tick, per-side ledger the maker proof card is built from. Its ONE
trust-critical job is to keep the TxLINE and venue coordinate systems distinct and honest:

  * The **fair value (FV)** is read from the REAL TxLINE coordinates the recorded pack actually
    carries — ``state.markets["1X2_PARTICIPANT_RESULT||"]["stable_prob_bps"][txline_side]`` (int bps,
    native prob ``= /1e4``). A :class:`~veridex.ingest.marketstate.MarketState` has NO venue key
    (``"1X2|home|full"`` does not exist on it); reading FV from a venue key would be a category error.
  * The **venue mid** is read from the backfilled Polymarket price frames, addressed by the venue
    ``market_ref``. The bridge from a TxLINE ``(market_key, side)`` to the venue side/ref is the
    canonical :func:`~veridex.venues.venue_price_source.txline_market_to_venue_ref` — ``part1→home``,
    ``part2→away``, ``draw→draw`` (:data:`_TXLINE_1X2_SIDE_TO_VENUE_SIDE`).

Every emitted row therefore carries BOTH coordinate systems (``txline_market_key`` + ``txline_side``
alongside ``venue_market_ref`` + ``venue_side``) so a downstream proof card can never blur venue vs
TxLINE identity. The venue mid is time-aligned with NO look-ahead — the most-recent frame at or before
the tick ``ts`` whose age is within ``freshness_s`` — and is ``None`` (never imputed) when no fresh
frame exists.
"""

from __future__ import annotations

import bisect
import json
from pathlib import Path
from typing import Any

from veridex.ingest.replay_pack import load_pack_marketstates
from veridex.maker.mapping import ResolvedMarketRecord, recompute_records_hash
from veridex.venues.venue_price_source import (
    _TXLINE_1X2_SIDE_TO_VENUE_SIDE,
    txline_market_to_venue_ref,
)

#: The REAL TxLINE market_key the FV is read under. A MarketState is keyed by THIS, never by the venue
#: ``"1X2|home|full"`` ref (which does not exist on a MarketState) — the load-bearing coordinate seam.
_TXLINE_1X2_FULL_MARKET_KEY = "1X2_PARTICIPANT_RESULT||"

#: The three TxLINE side tokens (``stable_prob_bps`` keys) the 1X2 full-match FV is read for, in order.
_TXLINE_1X2_SIDES = ("part1", "draw", "part2")


def _load_frame_index(frames_path: Path) -> tuple[list[int], list[float]]:
    """Load a venue frames file into parallel sorted ``(ts, native_price)`` lists for bisect alignment.

    Args:
        frames_path: Path to a ``{home,away,draw}.jsonl`` file — one JSON frame per line, each with a
            unix-seconds ``ts`` and a ``native_price`` (venue implied prob ``∈ [0, 1]``).

    Returns:
        A ``(ts_list, price_list)`` pair sorted ascending by ``ts`` (parallel indices).
    """
    frames: list[tuple[int, float]] = []
    for line in frames_path.read_text().splitlines():
        if not line:
            continue
        frame = json.loads(line)
        frames.append((int(frame["ts"]), float(frame["native_price"])))
    frames.sort(key=lambda pair: pair[0])
    return [ts for ts, _ in frames], [price for _, price in frames]


def _aligned_mid(
    ts_list: list[int], price_list: list[float], tick_ts: int, freshness_s: int
) -> tuple[float | None, int | None]:
    """Most-recent venue mid at or before ``tick_ts`` within ``freshness_s`` — NO look-ahead.

    Args:
        ts_list: Ascending frame timestamps (unix seconds).
        price_list: Frame ``native_price`` values, parallel to ``ts_list``.
        tick_ts: The decision tick timestamp (unix seconds) to align to.
        freshness_s: Staleness bound; a frame strictly older than this yields ``None``.

    Returns:
        A ``(mid, staleness_s)`` pair. ``mid`` is the aligned ``native_price`` and ``staleness_s`` the
        seconds gap ``tick_ts - frame_ts``; both are ``None`` when no frame exists at/before ``tick_ts``
        or the nearest such frame is too stale (the mid is NEVER imputed).
    """
    # bisect_right → first index strictly after tick_ts, so pos-1 is the most recent frame at/before
    # tick_ts. A later frame (frame_ts > tick_ts) is invisible — no look-ahead.
    pos = bisect.bisect_right(ts_list, tick_ts)
    if pos == 0:
        return None, None
    frame_ts = ts_list[pos - 1]
    staleness_s = tick_ts - frame_ts
    if staleness_s > freshness_s:
        return None, None
    return price_list[pos - 1], staleness_s


def build_cp1_maker_tape(
    records: list[ResolvedMarketRecord],
    *,
    pack_root: Path,
    cp1_frames_root: Path,
    freshness_s: int = 120,
) -> list[dict[str, Any]]:
    """Build the cp1 maker tape from REAL ReplayPacks (TxLINE FV) + pinned venue frame bytes (venue mid).

    For each fixture in ``records`` (the canonical cp1 universe — 18 fixtures), replays the committed
    ReplayPack through the SAME normalizer live TxLINE uses and, for every tick carrying the 1X2
    full-match market and each TxLINE side (``part1``/``draw``/``part2``), emits a row carrying BOTH
    coordinate systems: the FV read from the REAL TxLINE coordinate and the venue mid read from the
    backfilled frames addressed by the bridged venue ``market_ref``.

    Args:
        records: Resolved (fixture, venue-side) → market mapping records (the canonical cp1 universe).
        pack_root: Directory holding one ReplayPack per fixture at ``pack_root/<fixture_id>/``.
        cp1_frames_root: Root of the committed venue frames (``cp1_frames_root/<fixture_id>/<side>.jsonl``).
        freshness_s: Venue-mid staleness bound (seconds); a mid older than this becomes ``None``.

    Returns:
        The maker-tape rows. Each row is a dict with ``fixture_id``, ``tick_seq``, ``ts``,
        ``txline_market_key``, ``txline_side``, ``venue_market_ref``, ``venue_side``, ``fv``, ``mid``,
        ``staleness_s``, ``mapping_content_hash``, ``source_artifact_content_hash``. Rows with no fresh
        venue mid carry ``mid=None`` / ``staleness_s=None`` (never imputed).
    """
    # The mapping content hash pins WHICH resolved-market artifact these rows priced against; recomputed
    # once over the same byte serialization the mapping builder hashed (records-only, sorted).
    mapping_content_hash = recompute_records_hash([r.model_dump() for r in records])

    # Index records by (fixture_id, venue_side) — the coordinate the TxLINE→venue bridge resolves to.
    record_by_venue_coord: dict[tuple[int, str], ResolvedMarketRecord] = {
        (record.fixture_id, record.side): record for record in records
    }
    fixture_ids = sorted({record.fixture_id for record in records})

    frame_index_cache: dict[Path, tuple[list[int], list[float]]] = {}

    tape: list[dict[str, Any]] = []
    for fixture_id in fixture_ids:
        states = load_pack_marketstates(pack_root / str(fixture_id), fixture_id, verify=True)
        for state in states:
            market = state.markets.get(_TXLINE_1X2_FULL_MARKET_KEY)
            if market is None:
                continue  # this tick has no 1X2 full-match market — skip all its sides
            stable_prob_bps = market.get("stable_prob_bps")
            if not stable_prob_bps:
                continue
            for txline_side in _TXLINE_1X2_SIDES:
                bps = stable_prob_bps.get(txline_side)
                if bps is None:
                    continue  # side absent on this tick — skip (never impute FV)
                venue_market_ref = txline_market_to_venue_ref(_TXLINE_1X2_FULL_MARKET_KEY, txline_side)
                venue_side = _TXLINE_1X2_SIDE_TO_VENUE_SIDE.get(txline_side)
                if venue_market_ref is None or venue_side is None:
                    continue  # out of venue scope — no bridge
                record = record_by_venue_coord.get((fixture_id, venue_side))
                if record is None:
                    continue  # no resolved venue market for this coordinate — cannot address frames

                # Resolve the record's own frames file under the provided frames root (record path is
                # repo-relative: ``.../frames/<fixture_id>/<side>.jsonl`` — keep its last two segments).
                rel = Path(record.source_frames_file)
                frames_path = cp1_frames_root / rel.parent.name / rel.name
                frame_index = frame_index_cache.get(frames_path)
                if frame_index is None:
                    frame_index = _load_frame_index(frames_path)
                    frame_index_cache[frames_path] = frame_index
                ts_list, price_list = frame_index
                mid, staleness_s = _aligned_mid(ts_list, price_list, state.ts, freshness_s)

                tape.append(
                    {
                        "fixture_id": fixture_id,
                        "tick_seq": state.tick_seq,
                        "ts": state.ts,
                        "txline_market_key": _TXLINE_1X2_FULL_MARKET_KEY,
                        "txline_side": txline_side,
                        "venue_market_ref": venue_market_ref,
                        "venue_side": venue_side,
                        "fv": bps / 1e4,
                        "mid": mid,
                        "staleness_s": staleness_s,
                        "mapping_content_hash": mapping_content_hash,
                        "source_artifact_content_hash": record.source_artifact_content_hash,
                    }
                )
    return tape
