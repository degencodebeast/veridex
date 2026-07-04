"""C/P1 1X2 prices-history backfill helpers (REQ-003) — thin wrapper over M0's contract.

NO new frame/Pack model (Codex M2): this module REUSES
:class:`veridex.venues.price_history.VenuePriceHistoryFrame`,
:class:`~veridex.venues.price_history.VenuePriceHistoryPack`,
:func:`~veridex.venues.price_history.fetch_price_history`, and
:func:`~veridex.venues.price_history.compute_price_history_hash` verbatim. It only adds the two
pieces M0 left to the caller: a JSONL frames-file writer and a per-fixture/side convenience that
stitches fetch -> write -> hash -> Pack into one ``(frames, Pack)`` result, plus a live CLOB
``/prices-history`` client that carries the mandatory ``interval`` time component.

CON-010 (offline-safe import): ``httpx`` is imported lazily inside
:meth:`PolymarketPricesHistoryClient.get_prices_history`, so importing this module touches no
network and needs no credentials. Tests inject a fake client and never hit the wire.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from veridex.venues.polymarket_resolver import ResolvedMarket
from veridex.venues.price_history import (
    PriceHistoryClient,
    VenuePriceHistoryFrame,
    VenuePriceHistoryPack,
    compute_price_history_hash,
    fetch_price_history,
)

CLOB_BASE_URL = "https://clob.polymarket.com"

# The C-1 probe learned live (fail-closed) that CLOB ``/prices-history`` 400s on a bare
# ``?market=<tok>`` with "the time component is mandatory, please use 'startTs' and 'endTs' or
# 'interval'". ``interval=max`` supplies that mandatory time component and pulls the full
# available price path (we filter to the decision window downstream, never here).
PRICES_HISTORY_INTERVAL = "max"

# CLOB ``fidelity`` is the point spacing in MINUTES (1 -> ~1 point/minute). Pinned to 1 = the
# finest resolution, matching the probe and the minute-scale freshness buckets; the frames'
# ``fidelity_s`` field records the same spacing in SECONDS (:data:`FIDELITY_S`).
PRICES_HISTORY_FIDELITY_MINUTES = 1
FIDELITY_S = PRICES_HISTORY_FIDELITY_MINUTES * 60


def _prices_history_params(token_id: str) -> dict[str, Any]:
    """Build the CLOB ``/prices-history`` query params (pure, so the time component is testable).

    Mirrors the C-1 probe's proven params: the mandatory ``interval`` time component plus the
    pinned :data:`PRICES_HISTORY_FIDELITY_MINUTES` resolution. A bare ``{"market": token_id}``
    (no time component) 400s, so this is the load-bearing integration detail for the real backfill.
    """
    return {
        "market": token_id,
        "interval": PRICES_HISTORY_INTERVAL,
        "fidelity": PRICES_HISTORY_FIDELITY_MINUTES,
    }


def _write_frames_jsonl(path: Path, frames: list[VenuePriceHistoryFrame]) -> None:
    """Write *frames* as JSONL (one ``model_dump_json`` line per frame) to *path*.

    JSONL is the append-friendly, line-per-point shape the ``compute_price_history_hash`` scheme
    hashes as opaque bytes; each line round-trips back to a :class:`VenuePriceHistoryFrame`
    (which re-checks AC-014 on load). Parent directories are created so the operator can write
    straight into ``.../frames/<fixture>/<side>.jsonl``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for frame in frames:
            handle.write(frame.model_dump_json())
            handle.write("\n")


async def build_price_history_pack(
    resolved: ResolvedMarket,
    side: str,
    *,
    fixture_id: int,
    market_ref: str,
    fidelity_s: int,
    client: PriceHistoryClient,
    pack_dir: Path,
    frames_file: str,
) -> tuple[list[VenuePriceHistoryFrame], VenuePriceHistoryPack]:
    """Backfill one fixture/side into frames + a sibling :class:`VenuePriceHistoryPack`.

    Stitches the existing M0 pieces — :func:`fetch_price_history` (side->token + AC-014
    native->decimal), :func:`_write_frames_jsonl`, and :func:`compute_price_history_hash` — so the
    operator shell doesn't re-wire them per side. The Pack's ``artifact_content_hash`` is computed
    over the bytes ACTUALLY written (hash-of-file, not hash-of-objects), and the Pack carries NO
    ``evidence_hash`` (AC-015): it is a sibling integrity manifest, never sealed evidence.
    """
    frames = await fetch_price_history(
        resolved,
        side,
        fixture_id=fixture_id,
        market_ref=market_ref,
        fidelity_s=fidelity_s,
        client=client,
    )
    _write_frames_jsonl(pack_dir / frames_file, frames)
    content_hash = compute_price_history_hash(pack_dir, frames_file)
    pack = VenuePriceHistoryPack(
        fixture_id=fixture_id,
        frames_file=frames_file,
        artifact_content_hash=content_hash,
    )
    return frames, pack


class PolymarketPricesHistoryClient:
    """Live CLOB ``/prices-history`` client (implements :class:`PriceHistoryClient`).

    Sends the :func:`_prices_history_params` query (with the mandatory ``interval`` time
    component) so the real backfill pulls the full price path instead of 400ing as a bare
    ``?market=<tok>`` did in the C-1 probe. ``httpx`` is imported lazily (offline-safe import);
    the returned CLOB shape is ``{"history": [{"t": <unix seconds>, "p": <native price>}, ...]}``,
    unwrapped to the point list :func:`fetch_price_history` consumes.
    """

    def __init__(self, *, base_url: str = CLOB_BASE_URL, timeout_s: float = 20.0) -> None:
        self._base_url = base_url
        self._timeout_s = timeout_s

    async def get_prices_history(self, token_id: str) -> list[dict[str, Any]]:
        import httpx

        async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout_s) as http:
            response = await http.get("/prices-history", params=_prices_history_params(token_id))
            response.raise_for_status()
            data = response.json()
        history = data.get("history", data) if isinstance(data, dict) else data
        return list(history) if isinstance(history, list) else []
