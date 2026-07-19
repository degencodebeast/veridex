"""R-3 — the Replay catalog API + pack-bound competition identity (trust-relevant).

These tests pin the R-3 contract:

* ``GET /replay-packs`` lists the AUTHORITATIVE R-2 catalog (verified ``content_hash`` +
  honest ``provenance`` + ``fixtures``) and NEVER leaks the internal ``pack_dir`` filesystem path;
  ``GET /replay-packs/{pack_id}`` returns one verified pack, unknown -> 404.
* The browser-facing ``POST /backtests`` body NO LONGER accepts a client-provided ``pack_dir``
  filesystem path — the path-traversal surface is GONE. The browser sends ``pack_id`` + ``fixture_id``
  ONLY; the server resolves the pack via ``app.state.replay_catalog`` (the R-2 verified catalog).
* A backtest bound to a valid ``pack_id`` + ``fixture_id`` records the catalog-derived (SERVER-side)
  ``content_hash`` alongside ``pack_id`` into the sealed report. An unknown ``pack_id`` or a fixture
  not catalogued for the pack is a 4xx — a client can NEVER drive a replay from a filesystem path.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from veridex.api.router import create_app
from veridex.ingest.recorder import SessionMeta, envelope_line
from veridex.ingest.replay_catalog import build_catalog
from veridex.ingest.replay_pack import pack_from_session
from veridex.store import InMemoryStore

_FIXTURE_ID = 555


def _ou_record(ts_ms: int, under_pct: float) -> dict:
    """One raw native TxLINE OU record where 'Under' carries a scoreable (>=50%) prob."""
    return {
        "FixtureId": _FIXTURE_ID,
        "Ts": ts_ms,
        "InRunning": False,
        "SuperOddsType": "OU",
        "MarketPeriod": None,
        "MarketParameters": "line=2.5",
        "PriceNames": ["Over", "Under"],
        "Prices": [1900, 1900],
        "Pct": [round(100.0 - under_pct, 1), round(under_pct, 1)],
    }


def _build_pack(tmp_path: Path, n_ticks: int = 6) -> Path:
    """Build a self-describing, hashed ReplayPack for one fixture (curated-catalog seed shape)."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    lines = [envelope_line(_ou_record(100_000 + i * 10_000, 60.0 + i * 0.5), 100 + i * 10) for i in range(n_ticks)]
    (session_dir / "records.jsonl").write_text("\n".join(lines) + "\n")
    (session_dir / "meta.json").write_text(
        SessionMeta(started_ts=99, endpoints=["/odds/stream"], tool_version="t").model_dump_json()
    )
    out_dir = tmp_path / "curated"
    pack_from_session(session_dir, out_dir)
    return out_dir


def _client_with_catalog(pack_dir: Path) -> TestClient:
    """A demo app whose ``app.state.replay_catalog`` is the R-2 catalog over the built pack."""
    catalog = build_catalog(str(pack_dir))
    return TestClient(create_app(store=InMemoryStore(), replay_catalog=catalog))


def _stored_hash(pack_dir: Path) -> str:
    return str(json.loads((pack_dir / "pack.json").read_text())["content_hash"])


# --- (a) GET /replay-packs lists the R-2 catalog; unknown pack_id -> 404 -----


def test_replay_packs_lists_catalog_with_hash_and_provenance(tmp_path: Path) -> None:
    pack_dir = _build_pack(tmp_path)
    client = _client_with_catalog(pack_dir)

    resp = client.get("/replay-packs")
    assert resp.status_code == 200, resp.text
    packs = resp.json()["packs"]
    assert len(packs) == 1
    entry = packs[0]
    assert entry["pack_id"] == pack_dir.name
    assert entry["content_hash"] == _stored_hash(pack_dir)
    assert "provenance" in entry
    assert "is_genuine" in entry
    assert _FIXTURE_ID in entry["fixtures"]
    # The internal filesystem path must NEVER leak to the browser.
    assert "pack_dir" not in entry


def test_replay_pack_detail_and_unknown_is_404(tmp_path: Path) -> None:
    pack_dir = _build_pack(tmp_path)
    client = _client_with_catalog(pack_dir)

    ok = client.get(f"/replay-packs/{pack_dir.name}")
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["content_hash"] == _stored_hash(pack_dir)
    assert body["pack_id"] == pack_dir.name
    assert "pack_dir" not in body

    assert client.get("/replay-packs/does-not-exist").status_code == 404


# --- (b) the browser body NO LONGER accepts a filesystem pack_dir ------------


def test_backtest_body_rejects_client_pack_dir(tmp_path: Path) -> None:
    pack_dir = _build_pack(tmp_path)
    client = _client_with_catalog(pack_dir)

    # A body carrying a filesystem pack_dir (and no pack_id) is rejected — the field is GONE.
    resp = client.post(
        "/backtests",
        json={
            "pack_dir": str(pack_dir),
            "fixture_id": _FIXTURE_ID,
            "window_id": "w_api",
            "market_allowlist": ["OU"],
            "end_rule": "pre_match",
            "min_clv_horizon_s": 0,
        },
    )
    assert resp.status_code == 422, resp.text

    # Even cramming a filesystem path into pack_id never loads from disk — it is just an
    # unknown catalog key (404), NEVER a path traversal / filesystem read.
    traversal = client.post(
        "/backtests",
        json={
            "pack_id": "../../etc/passwd",
            "fixture_id": _FIXTURE_ID,
            "window_id": "w_api",
            "market_allowlist": ["OU"],
            "end_rule": "pre_match",
            "min_clv_horizon_s": 0,
        },
    )
    assert traversal.status_code == 404, traversal.text


# --- (c) valid pack_id+fixture_id binds pack_id + SERVER-derived content_hash --


def test_backtest_binds_pack_id_and_server_derived_content_hash(tmp_path: Path) -> None:
    pack_dir = _build_pack(tmp_path)
    catalog = build_catalog(str(pack_dir))
    client = TestClient(create_app(store=InMemoryStore(), replay_catalog=catalog))

    resp = client.post(
        "/backtests",
        json={
            "pack_id": pack_dir.name,
            "fixture_id": _FIXTURE_ID,
            "window_id": "w_api",
            "market_allowlist": ["OU"],
            "end_rule": "pre_match",
            "min_clv_horizon_s": 0,
        },
    )
    assert resp.status_code == 200, resp.text
    backtest_id = resp.json()["backtest_id"]

    report = client.get(f"/backtests/{backtest_id}").json()
    assert report["pack_id"] == pack_dir.name
    # The bound content_hash is SERVER-derived from the R-2 verified catalog entry —
    # never a client-provided value.
    assert report["content_hash"] == catalog.get(pack_dir.name).content_hash
    assert report["content_hash"] == _stored_hash(pack_dir)


# --- (d) unknown/unverified pack_id or an uncatalogued fixture -> 4xx --------


def test_backtest_unknown_pack_is_404(tmp_path: Path) -> None:
    pack_dir = _build_pack(tmp_path)
    client = _client_with_catalog(pack_dir)

    resp = client.post(
        "/backtests",
        json={
            "pack_id": "unknown-pack",
            "fixture_id": _FIXTURE_ID,
            "window_id": "w_api",
            "market_allowlist": ["OU"],
            "end_rule": "pre_match",
            "min_clv_horizon_s": 0,
        },
    )
    assert resp.status_code == 404, resp.text


def test_backtest_fixture_not_in_pack_is_422(tmp_path: Path) -> None:
    pack_dir = _build_pack(tmp_path)
    client = _client_with_catalog(pack_dir)

    resp = client.post(
        "/backtests",
        json={
            "pack_id": pack_dir.name,
            "fixture_id": 999_999,  # not a catalogued fixture of this pack
            "window_id": "w_api",
            "market_allowlist": ["OU"],
            "end_rule": "pre_match",
            "min_clv_horizon_s": 0,
        },
    )
    assert resp.status_code == 422, resp.text
