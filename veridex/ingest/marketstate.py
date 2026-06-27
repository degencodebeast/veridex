"""TxLINE ingest → `MarketState(≤t)`. Test-driven (T2).

T2a: tiny local JSON fixture → MarketState (replay driver — NOT a full historical client).
T2b: live SSE smoke → the SAME MarketState shape (a smoke gate — KILL-5 if it can't share quickly).
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict


class MarketState(BaseModel):
    """Immutable snapshot of TxLINE data up to tick `t` an agent may see (no future rows).

    Top-level immutable (frozen); construct fresh per tick. (Nested dict deep-freeze is a
    Phase-1 concern; Phase-0 relies on fresh construction + this top-level guard.)
    """

    model_config = ConfigDict(frozen=True)

    fixture_id: int
    tick_seq: int
    ts: int
    phase: int
    markets: dict[str, dict[str, Any]]  # market_key -> {stable_prob_bps, stable_price, suspended}
    scores: dict[str, int]  # stat_key (str) -> value


def _marketstate_from_record(record: dict[str, Any], *, tick_seq: int, fixture_id: int) -> MarketState:
    """Shared adapter: one normalized tick record -> MarketState (used by replay AND live SSE)."""
    scores = {str(k): int(v) for k, v in record.get("scores", {}).items()}
    return MarketState(
        fixture_id=int(fixture_id),
        tick_seq=int(tick_seq),
        ts=int(record["ts"]),
        phase=int(record["phase"]),
        markets=dict(record.get("markets", {})),
        scores=scores,
    )


def marketstate_from_fixture(record: dict[str, Any], *, tick_seq: int, fixture_id: int) -> MarketState:
    """T2a: one replay fixture tick → MarketState."""
    return _marketstate_from_record(record, tick_seq=tick_seq, fixture_id=fixture_id)


def replay_marketstates(fixture_path: str) -> list[MarketState]:
    """T2a: a local fixture file → ordered, deterministic list of MarketState."""
    with open(fixture_path) as f:
        data = json.load(f)
    fixture_id = data["fixture_id"]
    return [
        _marketstate_from_record(tick, tick_seq=index, fixture_id=fixture_id)
        for index, tick in enumerate(data["ticks"])
    ]


def parse_sse_line(line: str) -> dict[str, Any] | None:
    """T2b: parse one SSE line. Returns the data record, or None for heartbeats/blank lines."""
    if line is None:
        return None
    stripped = line.strip()
    if not stripped or stripped.startswith(":") or stripped.startswith("event:"):
        return None
    if not stripped.startswith("data:"):
        return None
    payload = stripped[len("data:") :].strip()
    try:
        record = json.loads(payload)
    except (ValueError, TypeError):
        return None
    return record if isinstance(record, dict) else None


def marketstate_from_sse(event: dict[str, Any], *, tick_seq: int, fixture_id: int) -> MarketState:
    """T2b: one live SSE data record → the SAME MarketState shape as replay."""
    return _marketstate_from_record(event, tick_seq=tick_seq, fixture_id=fixture_id)
