"""Production TxLINE odds normalizer (B1 — REQ-101 / AC-101).

Folds N TxLINE-native odds SSE messages for ONE fixture into a `MarketState`.
Trust-path module: NO LLM SDK imports (CON-007).
"""
from __future__ import annotations

from typing import Any

from veridex.ingest.marketstate import MarketState


def market_key(message: dict[str, Any]) -> str:
    """Compose a stable market key from a native TxLINE odds message.

    Format: ``{SuperOddsType}|{MarketPeriod or ''}|{MarketParameters or ''}``.
    Null / missing segments collapse to an empty string so the key is always
    two-pipe separated and comparable across ticks.
    """
    return "|".join(
        str(message.get(k) or "")
        for k in ("SuperOddsType", "MarketPeriod", "MarketParameters")
    )


def group_by_fixture(
    messages: list[dict[str, Any]],
) -> dict[int, list[dict[str, Any]]]:
    """Bucket native messages by ``FixtureId``, preserving insertion order."""
    result: dict[int, list[dict[str, Any]]] = {}
    for msg in messages:
        fid = int(msg["FixtureId"])
        result.setdefault(fid, []).append(msg)
    return result


def _try_float(value: Any) -> float | None:
    """Return ``float(value)`` or ``None`` for 'NA' / non-numeric sentinels."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def marketstate_from_txline_odds(
    messages: list[dict[str, Any]],
    *,
    tick_seq: int = 0,
) -> MarketState:
    """Fold N native odds messages for ONE fixture into a :class:`MarketState`.

    Parameters
    ----------
    messages:
        One or more TxLINE-native odds SSE messages for a *single* fixture.
    tick_seq:
        Monotonic counter passed through to ``MarketState.tick_seq``.

    Raises
    ------
    ValueError
        If *messages* span more than one ``FixtureId`` (single fixture
        contract — see REQ-101).
    """
    if not messages:
        raise ValueError(
            "marketstate_from_txline_odds requires at least one message"
        )

    fixture_ids = {int(m["FixtureId"]) for m in messages}
    if len(fixture_ids) > 1:
        raise ValueError(
            f"single fixture expected but received messages for "
            f"{len(fixture_ids)} fixtures: {sorted(fixture_ids)}"
        )

    fixture_id = next(iter(fixture_ids))

    # ts: max Ts in ms → seconds
    latest_ts: int = max(int(m["Ts"]) for m in messages) // 1000

    # phase: 1 (in-running) if ANY message is live, else 0 (pre-match)
    in_running: bool = any(bool(m.get("InRunning")) for m in messages)

    markets: dict[str, dict[str, Any]] = {}
    for msg in messages:
        key = market_key(msg)
        names: list[str] = list(msg.get("PriceNames") or [])
        prices: list[Any] = list(msg.get("Prices") or [])
        pcts: list[Any] = list(msg.get("Pct") or [])

        # de-vigged probability in basis-points (Pct is already de-margined)
        stable_prob_bps: dict[str, int] = {}
        for name, pct in zip(names, pcts):
            val = _try_float(pct)
            if val is not None:
                stable_prob_bps[name] = round(val * 100)

        # decimal odds (Prices are decimal × 1000); prices are retained on
        # suspended markets as last-known odds even when stable_prob_bps is empty.
        stable_price: dict[str, float] = {}
        for name, price in zip(names, prices):
            val = _try_float(price)
            if val is not None:
                stable_price[name] = val / 1000

        # suspended iff no priced (non-NA) prob outcomes remain
        suspended: bool = len(stable_prob_bps) == 0

        markets[key] = {
            "stable_prob_bps": stable_prob_bps,
            "stable_price": stable_price,
            "suspended": suspended,
        }

    return MarketState(
        fixture_id=fixture_id,
        tick_seq=tick_seq,
        ts=latest_ts,
        phase=1 if in_running else 0,
        markets=markets,
        scores={},
    )
