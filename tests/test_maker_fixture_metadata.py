"""Additive fixture_metadata on the sealed maker envelope (raw IDs never mutated)."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from veridex.api.maker_router import (
    SEALED_FIXTURES_PATH,
    _build_fixture_metadata,
    build_maker_arena_result_response,
)
from veridex.api.router import create_app
from veridex.store import InMemoryStore

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _client() -> TestClient:
    return TestClient(create_app(store=InMemoryStore()))


def test_sealed_fixtures_array_is_unchanged_raw_ints() -> None:
    """The sealed result.fixtures[] stays a raw integer array, byte-equivalent to the seal."""
    sealed = json.loads(
        (_REPO_ROOT / "scripts" / "txline_live" / "cp1" / "maker-arena-result.json").read_text()
    )
    body = _client().get("/maker/arena-result").json()
    assert body["result"]["fixtures"] == sealed["fixtures"]
    assert all(isinstance(f, int) for f in body["result"]["fixtures"])


def test_fixture_metadata_present_18_rows_captured() -> None:
    """fixture_metadata has one captured row per sealed ID, raw fixture_id always an int."""
    body = _client().get("/maker/arena-result").json()
    meta = body["fixture_metadata"]
    assert len(meta) == 18
    sealed_ids = body["result"]["fixtures"]
    assert [row["fixture_id"] for row in meta] == sealed_ids
    for row in meta:
        assert isinstance(row["fixture_id"], int)
        assert row["label_source"] == "captured"
        assert row["home_team"] and row["away_team"]
        assert isinstance(row["kickoff_ts"], int)


def test_whole_file_fallback_still_returns_sealed_benchmark() -> None:
    """A missing/malformed fixtures.json still returns raw IDs, every label 'unavailable' — never crashes."""
    resp = build_maker_arena_result_response(fixtures_path=Path("/nonexistent/fixtures.json"))
    body = resp.model_dump(mode="json")
    assert body["result"]["maker_leaderboard"][0]["avg_toxicity_loss_bps"] == 129
    assert len(body["fixture_metadata"]) == 18
    for row in body["fixture_metadata"]:
        assert row["label_source"] == "unavailable"
        assert row["home_team"] is None and row["away_team"] is None and row["kickoff_ts"] is None
        assert isinstance(row["fixture_id"], int)


def test_malformed_fixtures_json_falls_back_to_all_unavailable(tmp_path: Path) -> None:
    """A MALFORMED fixtures.json (invalid JSON) still returns the sealed benchmark, every label 'unavailable'.

    The whole-file guard catches BOTH OSError (missing) and ValueError (json.loads on bad bytes) — the
    missing-path case above exercises OSError; this pins the ValueError branch so malformed content can
    never crash the sealed benchmark or leak a partial label.
    """
    bad = tmp_path / "fixtures.json"
    bad.write_text("{ this is not valid json ", encoding="utf-8")
    resp = build_maker_arena_result_response(fixtures_path=bad)
    body = resp.model_dump(mode="json")
    assert body["result"]["maker_leaderboard"][0]["avg_toxicity_loss_bps"] == 129
    assert len(body["fixture_metadata"]) == 18
    for row in body["fixture_metadata"]:
        assert row["label_source"] == "unavailable"
        assert row["home_team"] is None and row["away_team"] is None and row["kickoff_ts"] is None
        assert isinstance(row["fixture_id"], int)


def test_per_fixture_fallback_for_unmatched_id() -> None:
    """An ID absent from fixtures.json renders raw ID + 'unavailable', matched IDs stay 'captured'."""
    rows = _build_fixture_metadata((17588229, 99999999), SEALED_FIXTURES_PATH)
    assert rows[0]["fixture_id"] == 17588229 and rows[0]["label_source"] == "captured"
    assert rows[1]["fixture_id"] == 99999999 and rows[1]["label_source"] == "unavailable"
    assert rows[1]["home_team"] is None


def test_integrity_every_sealed_id_resolves_in_fixtures_json() -> None:
    """BUILD/TEST INTEGRITY GATE: every result.fixtures[] ID resolves in the packaged fixtures.json."""
    from veridex.maker.result import MakerArenaResult

    result_path = _REPO_ROOT / "scripts" / "txline_live" / "cp1" / "maker-arena-result.json"
    result = MakerArenaResult.model_validate(json.loads(result_path.read_text()))
    labels = json.loads(SEALED_FIXTURES_PATH.read_text())
    known = {row["fixture_id"] for row in labels}
    unresolved = [fid for fid in result.fixtures if fid not in known]
    assert unresolved == [], f"sealed IDs missing from cp1/fixtures.json: {unresolved}"
    rows = _build_fixture_metadata(result.fixtures, SEALED_FIXTURES_PATH)
    assert all(r["label_source"] == "captured" for r in rows)
