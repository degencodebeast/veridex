"""EV-4 — backfill operator shell: scores unavailable must not block the odds-backed pack.

TDD Iron Law: written RED (the scores failure currently propagates out of
``_backfill_one`` and no pack gets written) before the non-fatal handling existed.

Behaviors under test (offline — a fake client injected through ``_backfill_one``,
no real network)
--------------------------------------------------------------------------------
- Odds fetch failure stays fatal: no odds, no pack (unchanged, not re-tested here —
  covered by the odds-required contract in ``veridex.ingest.backfill``).
- Scores fetch failure is NON-fatal: ``_backfill_one`` still builds a pack from odds
  alone, with an empty scores sibling, instead of raising and producing nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts.txline_live.backfill import _backfill_one
from veridex.ingest.marketstate import MarketState
from veridex.ingest.replay_pack import load_pack_marketstates

_REPO = Path(__file__).resolve().parents[1]
_SAMPLES = _REPO / "scripts" / "txline_live"
_BASE = "https://txline-dev.txodds.com/api"
_CREDS = ("jwt-1", "api-token-1")


def _load_real_odds() -> tuple[int, list[dict[str, Any]]]:
    odds = json.loads((_SAMPLES / "captured_odds.json").read_text())
    fixture_id = int(odds[0]["FixtureId"])
    return fixture_id, odds


class _FakeJSONResp:
    def __init__(self, payload: Any) -> None:
        self._payload = payload
        self.status_code = 200
        self.headers = {"content-type": "application/json"}
        self.content = json.dumps(payload).encode()

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


class _FakeClient:
    """Real captured odds on ``/odds/updates/``; ``/scores/updates/`` raises (scores down)."""

    def __init__(self, odds_payload: list[dict[str, Any]], *, scores_error: Exception) -> None:
        self._odds_payload = odds_payload
        self._scores_error = scores_error
        self.calls: list[str] = []

    async def get(self, url: str, headers: dict[str, str] | None = None, **kw: Any) -> _FakeJSONResp:
        self.calls.append(url)
        if "/scores/updates/" in url:
            raise self._scores_error
        return _FakeJSONResp(self._odds_payload)

    async def aclose(self) -> None:
        return None


async def test_backfill_one_builds_pack_from_odds_when_scores_unavailable(tmp_path: Path) -> None:
    fixture_id, odds = _load_real_odds()
    client = _FakeClient(odds, scores_error=RuntimeError("scores endpoint down"))
    packs_dir = tmp_path / "packs"

    await _backfill_one(fixture_id, _BASE, _CREDS, packs_dir, client=client)

    out_dir = packs_dir / str(fixture_id)
    states = load_pack_marketstates(out_dir, fixture_id, verify=True)
    assert states, "expected the pack to build from odds alone"
    assert all(isinstance(s, MarketState) for s in states)
    assert any(s.markets for s in states)

    sibling = out_dir / f"scores_{fixture_id}.json"
    assert json.loads(sibling.read_text()) == []  # scores unavailable -> empty sibling, not a crash
