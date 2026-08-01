"""E2 — the replay-market projection endpoint (last-known odds per market, honest-suspended).

``GET /replay-packs/{pack_id}/fixtures/{fixture_id}/markets`` projects a replay fixture's
LAST-KNOWN state per ``market_key`` folded across the WHOLE hash-bound tape (M11 — not just the
final tick, which alone carries 1 of the fixture's 30 markets). HONESTY is load-bearing:

* a NON-suspended market carries a non-empty ``stable_prob_bps`` AND ``stable_price``;
* a SUSPENDED market keeps ``stable_prob_bps == {}`` (EMPTY — never filled) while RETAINING a
  non-empty ``stable_price`` (last-known odds);
* the DTO carries NO ``finished`` / ``closing`` / ``edge`` / eligibility / feed-health keys;
* a tampered pack (bytes swapped after catalog admission) fails closed with a 4xx.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from veridex.api.router import create_app
from veridex.ingest.replay_catalog import build_catalog
from veridex.store import InMemoryStore

# The R-1 banked GENUINE seed pack (the single curated production pack). Fixture 18213979 is the M11
# regression anchor: 30 distinct market keys across the tape, 13 of them suspended.
_SEED_PACK_DIR = Path(__file__).resolve().parents[1] / "scripts" / "fixtures" / "demo_pack_real"
_PACK_ID = "demo_pack_real"
_FIXTURE_ID = 18213979
_EXPECTED_MARKET_COUNT = 30


def _client_over(pack_dir: Path) -> TestClient:
    """A demo app whose ``app.state.replay_catalog`` is the R-2 catalog over ``pack_dir``."""
    catalog = build_catalog(str(pack_dir))
    return TestClient(create_app(store=InMemoryStore(), replay_catalog=catalog))


def _markets_url(pack_id: str, fixture_id: int | str) -> str:
    return f"/replay-packs/{pack_id}/fixtures/{fixture_id}/markets"


def _get_markets(client: TestClient) -> dict:
    resp = client.get(_markets_url(_PACK_ID, _FIXTURE_ID))
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- (a) M11: the fold yields ALL 30 markets (not the 1 in the final tick) --


def test_projection_folds_all_thirty_markets() -> None:
    body = _get_markets(_client_over(_SEED_PACK_DIR))
    keys = [m["market_key"] for m in body["markets"]]
    assert len(keys) == _EXPECTED_MARKET_COUNT, keys
    assert len(set(keys)) == _EXPECTED_MARKET_COUNT  # distinct — one row per market_key


# --- (b) a NON-suspended market: prob AND price both non-empty ---------------


def test_non_suspended_market_has_prob_and_price() -> None:
    body = _get_markets(_client_over(_SEED_PACK_DIR))
    live = [m for m in body["markets"] if not m["suspended"]]
    assert live, "expected at least one non-suspended market"
    for m in live:
        assert m["stable_prob_bps"], m
        assert m["stable_price"], m


# --- (c) a SUSPENDED market: EMPTY prob map PRESERVED, price retained --------


def test_suspended_market_preserves_empty_prob_map() -> None:
    body = _get_markets(_client_over(_SEED_PACK_DIR))
    suspended = [m for m in body["markets"] if m["suspended"]]
    assert len(suspended) == 13, f"expected 13 suspended markets, got {len(suspended)}"
    for m in suspended:
        # HONESTY: the empty prob map is PRESERVED, never back-filled from the retained price.
        assert m["stable_prob_bps"] == {}, m
        assert m["stable_price"], m  # last-known odds retained


# --- (d) NO finished / closing / edge / eligibility keys in the DTO ----------


def test_dto_omits_finished_closing_edge_keys() -> None:
    body = _get_markets(_client_over(_SEED_PACK_DIR))
    assert body["label"] == "CAPTURED REPLAY"
    banned = {"finished", "closing", "edge", "eligibility", "feed_health"}
    assert not (banned & set(body)), body.keys()
    for m in body["markets"]:
        assert not (banned & set(m)), m.keys()
        assert set(m) == {"market_key", "in_running", "suspended", "ts", "stable_prob_bps", "stable_price"}, m.keys()


# --- (e) label ---------------------------------------------------------------


def test_projection_label_is_captured_replay() -> None:
    assert _get_markets(_client_over(_SEED_PACK_DIR))["label"] == "CAPTURED REPLAY"


# --- (f) unknown pack / uncatalogued fixture -> 404 -------------------------


def test_unknown_pack_is_404() -> None:
    client = _client_over(_SEED_PACK_DIR)
    assert client.get(_markets_url("does-not-exist", _FIXTURE_ID)).status_code == 404


def test_uncatalogued_fixture_is_404() -> None:
    client = _client_over(_SEED_PACK_DIR)
    assert client.get(_markets_url(_PACK_ID, 999_999)).status_code == 404


# --- (g) a TAMPERED pack (bytes swapped after admission) fails closed 4xx ----


def test_tampered_pack_fails_closed_4xx(tmp_path: Path) -> None:
    dst = tmp_path / _PACK_ID
    shutil.copytree(_SEED_PACK_DIR, dst)
    # Catalog admits the pack while its bytes are still coherent (verify-before-promote).
    client = _client_over(dst)
    # TOCTOU: swap the fixture's on-disk bytes AFTER admission. The bound load recomputes the
    # content_hash from the tampered bytes and refuses to replay them (fail-closed 4xx).
    odds = dst / f"odds_{_FIXTURE_ID}.jsonl"
    odds.write_bytes(odds.read_bytes() + b'{"FixtureId":18213979,"Ts":1,"SuperOddsType":"X"}\n')
    resp = client.get(_markets_url(_PACK_ID, _FIXTURE_ID))
    assert 400 <= resp.status_code < 500, resp.status_code


def test_tampered_pack_422_detail_leaks_no_filesystem_path(tmp_path: Path) -> None:
    """SECURITY: the tamper 422 surfaces the STABLE reason code, never the internal pack_dir path.

    ``PackIntegrityError`` carries a machine-usable ``.reason`` precisely so the handler can report a
    stable code over HTTP without echoing the raw message (which embeds the ABSOLUTE server pack_dir).
    The browser addresses packs by ``pack_id`` only — the filesystem path is DELIBERATELY never exposed.
    """
    dst = tmp_path / _PACK_ID
    shutil.copytree(_SEED_PACK_DIR, dst)
    client = _client_over(dst)
    odds = dst / f"odds_{_FIXTURE_ID}.jsonl"
    odds.write_bytes(odds.read_bytes() + b'{"FixtureId":18213979,"Ts":1,"SuperOddsType":"X"}\n')
    resp = client.get(_markets_url(_PACK_ID, _FIXTURE_ID))
    assert resp.status_code == 422, resp.status_code
    detail = resp.json()["detail"]
    # The stable reason code — appending bytes drifts the recomputed content_hash.
    assert detail == "content_hash_drift", detail
    # No filesystem path may leak: neither a path separator nor the abs pack_dir string.
    assert "/" not in detail, detail
    assert str(dst) not in detail, detail
