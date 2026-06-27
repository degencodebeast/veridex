"""Normalize live TxLINE-native odds messages -> our MarketState (T9, REQ-007 live close).

Demonstrates the field mapping discovered from live devnet capture. This is a SPIKE
demonstration (productionizing the normalizer into veridex/ingest with tests is Phase 1).
Run: .venv/bin/python scripts/txline_live/normalize_demo.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from veridex.ingest.marketstate import marketstate_from_sse  # noqa: E402


def _f(x):
    """Tolerant float: live data carries 'NA' for unpriced outcomes (a completeness finding)."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def market_key(msg: dict) -> str:
    """TxLINE-native -> stable market key: oddstype|period|params."""
    return "|".join(
        str(msg.get(k) or "") for k in ("SuperOddsType", "MarketPeriod", "MarketParameters")
    )


def normalize_fixture_messages(messages: list[dict]) -> dict:
    """Fold N TxLINE-native odds messages for ONE fixture into a normalized tick record
    matching our {fixture_id-less} contract: {ts, phase, markets, scores}.
    marketstate_from_sse adds fixture_id/tick_seq."""
    markets: dict[str, dict] = {}
    latest_ts = 0
    in_running = False
    for m in messages:
        key = market_key(m)
        names = m.get("PriceNames") or []
        prices = m.get("Prices") or []
        pct = m.get("Pct") or []
        markets[key] = {
            # de-vigged probability in bps (Pct is already de-margined; sums to ~100%)
            "stable_prob_bps": {n: round(_f(p) * 100) for n, p in zip(names, pct) if _f(p) is not None},
            "stable_price": {n: _f(pr) / 1000 for n, pr in zip(names, prices) if _f(pr) is not None},
            "suspended": False,  # pre-match (InRunning=False, GameState=null): not suspended
        }
        latest_ts = max(latest_ts, int(m.get("Ts", 0)) // 1000)  # ms -> s
        in_running = in_running or bool(m.get("InRunning"))
    return {
        "ts": latest_ts,
        "phase": 1 if in_running else 0,  # 0 = pre-match (no live phase yet)
        "markets": markets,
        "scores": {},  # scores arrive on the scores stream; empty pre-match
    }


def main() -> None:
    odds = json.load(open(Path(__file__).parent / "captured_odds.json"))
    by_fixture: dict[int, list[dict]] = {}
    for m in odds:
        by_fixture.setdefault(int(m["FixtureId"]), []).append(m)

    print(f"fixtures: {list(by_fixture)}")
    for fid, msgs in by_fixture.items():
        norm = normalize_fixture_messages(msgs)
        ms = marketstate_from_sse(norm, tick_seq=0, fixture_id=fid)
        print(f"\n== fixture {fid}: {len(msgs)} native msgs -> 1 MarketState ==")
        print(f"  type: {type(ms).__name__}  fixture_id={ms.fixture_id}  ts={ms.ts}  phase={ms.phase}")
        print(f"  markets: {len(ms.markets)} distinct keys")
        k0 = next(iter(ms.markets))
        print(f"  sample market key: {k0!r}")
        print(f"  sample market val: {json.dumps(ms.markets[k0])}")
        # prove de-vig on the normalized data
        probs = ms.markets[k0]["stable_prob_bps"]
        if len(probs) > 1:
            print(f"  de-vig check (bps sum, want ~10000): {sum(probs.values())}")
    print("\nOK: live TxLINE-native messages normalize into the SAME MarketState contract.")


if __name__ == "__main__":
    main()
