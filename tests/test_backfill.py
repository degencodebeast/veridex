"""EV-2 — historical /odds/updates + /scores/updates -> verified ReplayPack.

TDD Iron Law: written RED (ImportError — ``veridex.ingest.backfill`` absent) before the
production module existed.

Behaviors under test (against the REAL captured sample payloads, offline)
------------------------------------------------------------------------
- ``build_pack_from_fixture`` is a PURE (no-network) transform: real captured native odds
  messages -> a self-describing, content-hashed :class:`ReplayPack`.
- The produced pack loads via ``load_pack_marketstates(pack_dir, fid, verify=True)`` into a
  non-empty list of :class:`MarketState` whose ``.markets`` are populated (the SAME normalizer
  live TxLINE uses).
- Scores ride ALONGSIDE the pack as a NON-EVIDENCE sibling file, never hashed into the sealed
  evidence path (``content_hash`` stays over the manifest legs only).
- Importing ``veridex.ingest.backfill`` pulls no network library (async-shell / sync-core split).
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from veridex.ingest.backfill import build_pack_from_fixture
from veridex.ingest.marketstate import MarketState
from veridex.ingest.replay_pack import load_pack_marketstates, verify_content_hash

_REPO = Path(__file__).resolve().parents[1]
_SAMPLES = _REPO / "scripts" / "txline_live"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _load_samples() -> tuple[int, list[dict], list[dict]]:
    odds = json.loads((_SAMPLES / "captured_odds.json").read_text())
    scores = json.loads((_SAMPLES / "captured_scores.json").read_text())
    fixture_id = int(odds[0]["FixtureId"])  # the fixture the first captured message belongs to
    return fixture_id, odds, scores


def test_build_pack_from_real_sample_loads_verified_nonempty(tmp_path: Path) -> None:
    fixture_id, odds, scores = _load_samples()
    out_dir = tmp_path / "pack"

    pack = build_pack_from_fixture(fixture_id, odds, scores, out_dir)

    # content_hash is a real sha256 hex digest.
    assert _HEX64.match(pack.content_hash)
    # The pack describes exactly this fixture.
    assert [f["fixture_id"] for f in pack.fixtures] == [fixture_id]

    # Loads through the SAME normalizer live uses, verified, non-empty, with populated markets.
    states = load_pack_marketstates(out_dir, fixture_id, verify=True)
    assert states, "expected non-empty marketstates from the real captured sample"
    assert all(isinstance(s, MarketState) for s in states)
    assert all(s.fixture_id == fixture_id for s in states)
    assert any(s.markets for s in states), "expected at least one populated .markets snapshot"


def test_scores_ride_as_non_evidence_sibling_not_in_hash(tmp_path: Path) -> None:
    fixture_id, odds, scores = _load_samples()
    out_dir = tmp_path / "pack"

    build_pack_from_fixture(fixture_id, odds, scores, out_dir)

    # A scores sibling file exists next to the pack...
    sibling = out_dir / f"scores_{fixture_id}.json"
    assert sibling.exists()
    assert json.loads(sibling.read_text()) == scores

    # ...but it is NOT part of the sealed evidence: not referenced by the manifest, so the
    # content_hash still verifies (scores never entered the hash scope).
    manifest = json.loads((out_dir / "pack.json").read_text())
    referenced: set[str] = set()
    for entry in manifest["fixtures"]:
        referenced.update(v for k, v in entry.items() if k != "fixture_id")
    assert sibling.name not in referenced
    assert verify_content_hash(out_dir) is True


def test_backfill_module_import_is_network_free() -> None:
    src = (_REPO / "veridex" / "ingest" / "backfill.py").read_text()
    tree = ast.parse(src)
    top_level_imports: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level_imports.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level_imports.add(node.module.split(".")[0])
    assert "httpx" not in top_level_imports
