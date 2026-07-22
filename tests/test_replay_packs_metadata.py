"""Additive fixture_metadata on ReplayPackInfo (raw fixtures list unchanged)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from veridex.api.fixture_labels import fixture_metadata_row
from veridex.api.router import create_app
from veridex.store import InMemoryStore


def _client() -> TestClient:
    return TestClient(create_app(store=InMemoryStore()))


def test_fixture_metadata_row_captured_for_known_wc_qf_id() -> None:
    """A curated WC-QF id resolves to captured labels + a shipped kickoff_ts (raw id preserved)."""
    row = fixture_metadata_row(18209181)
    assert row == {
        "fixture_id": 18209181,
        "home_team": "France",
        "away_team": "Morocco",
        "kickoff_ts": 1783627200,
        "label_source": "captured",
    }


def test_fixture_metadata_row_unavailable_for_unknown_id() -> None:
    """An unmapped id renders raw id + 'unavailable', never a guessed matchup or time."""
    row = fixture_metadata_row(1)
    assert row == {
        "fixture_id": 1,
        "home_team": None,
        "away_team": None,
        "kickoff_ts": None,
        "label_source": "unavailable",
    }


def test_replay_packs_response_carries_raw_fixtures_and_metadata() -> None:
    """/replay-packs keeps raw fixtures: list[int] AND adds a parallel fixture_metadata list."""
    resp = _client().get("/replay-packs")
    assert resp.status_code == 200, resp.text
    packs = resp.json()["packs"]
    assert len(packs) >= 1
    pack = packs[0]
    assert all(isinstance(f, int) for f in pack["fixtures"])
    assert [m["fixture_id"] for m in pack["fixture_metadata"]] == pack["fixtures"]
    for m in pack["fixture_metadata"]:
        assert m["label_source"] in {"captured", "unavailable"}
        assert isinstance(m["fixture_id"], int)


def test_curated_label_and_kickoff_maps_are_key_consistent() -> None:
    """Integrity gate: every curated-labelled fixture also has a shipped kickoff_ts, so a
    ``captured`` row can never carry a null ``kickoff_ts`` (an honest-metadata invariant)."""
    from veridex.api.fixture_labels import FIXTURE_KICKOFF_TS, FIXTURE_LABELS

    assert FIXTURE_LABELS.keys() == FIXTURE_KICKOFF_TS.keys()
