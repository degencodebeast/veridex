"""Live TxLINE devnet SSE capture + ingest smoke (T9).

Reads JWT + X-Api-Token from veridex/.env, streams real odds/scores records, saves raw,
and feeds them through veridex.ingest.marketstate to see what normalization REQ-007 needs.
Run: .venv/bin/python scripts/txline_live/capture.py
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from veridex.ingest.marketstate import parse_sse_line  # noqa: E402

BASE = "https://txline-dev.txodds.com/api"


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in (ROOT / "veridex" / ".env").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def stream(path: str, headers: dict[str, str], *, max_records: int, max_seconds: float) -> list[dict]:
    url = f"{BASE}{path}"
    req = urllib.request.Request(url, headers={**headers, "Accept": "text/event-stream"})
    out: list[dict] = []
    deadline = time.time() + max_seconds
    try:
        with urllib.request.urlopen(req, timeout=max_seconds) as resp:
            print(f"  {path} -> HTTP {resp.status}")
            for raw in resp:
                if time.time() > deadline or len(out) >= max_records:
                    break
                rec = parse_sse_line(raw.decode("utf-8", "replace"))
                if rec is not None:
                    out.append(rec)
    except Exception as e:  # noqa: BLE001
        print(f"  {path} stream ended: {type(e).__name__}: {e}")
    return out


def main() -> None:
    env = load_env()
    jwt = env.get("JWT", "")
    api = env.get("TXLINE_X_API_TOKEN", "")
    print(f"creds: JWT len={len(jwt)} | X-Api-Token len={len(api)} prefix={api[:13]!r}")
    headers = {"Authorization": f"Bearer {jwt}", "X-Api-Token": api}

    print("== odds stream ==")
    odds = stream("/odds/stream", headers, max_records=40, max_seconds=45)
    print(f"  captured {len(odds)} odds records")
    print("== scores stream ==")
    scores = stream("/scores/stream", headers, max_records=15, max_seconds=25)
    print(f"  captured {len(scores)} scores records")

    outdir = Path(__file__).parent
    (outdir / "captured_odds.json").write_text(json.dumps(odds, indent=1))
    (outdir / "captured_scores.json").write_text(json.dumps(scores, indent=1))

    if odds:
        print("\n== first odds record: top-level keys ==")
        print(sorted(odds[0].keys()))
        print("== first odds record (pretty, truncated) ==")
        print(json.dumps(odds[0], indent=1)[:1200])
    if scores:
        print("\n== first scores record: top-level keys ==")
        print(sorted(scores[0].keys()))
        print(json.dumps(scores[0], indent=1)[:600])


if __name__ == "__main__":
    main()
