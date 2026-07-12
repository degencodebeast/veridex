"""Offline TDD suite for ``scripts/maker/pmxt_archive_pull.py``.

Every test runs FULLY OFFLINE on a canned in-memory list of pmxt archive rows fed through
the reconstruction. No network, no DuckDB, no Gamma. The reconstruction REUSES the live
``BookStateMaintainer`` merge (identical to the R3 live path) via a thin archive-row → frame
adapter, so these tests also pin the merge semantics (BUY→bid / SELL→ask, ``size=="0"``
delete, best_bid/ask self-validation, honest gaps on delta-before-seed / checksum mismatch).
"""

from __future__ import annotations

import ast
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

import scripts.maker.pmxt_archive_pull as mod

# --------------------------------------------------------------------------- canned row helpers


def _book_row(
    *,
    asset_id: str,
    market: str = "0x" + "ab" * 32,
    recv: int,
    src: int,
    bids: list[list[str]],
    asks: list[list[str]],
) -> dict[str, Any]:
    return {
        "event_type": "book",
        "asset_id": asset_id,
        "market": market,
        "timestamp_received": recv,
        "timestamp": src,
        # archive delivers bids/asks as a JSON STRING of [["price","size"],...]
        "bids": json.dumps(bids),
        "asks": json.dumps(asks),
    }


def _pc_row(
    *,
    asset_id: str,
    market: str = "0x" + "ab" * 32,
    recv: int,
    src: int,
    side: str,
    price: str,
    size: str,
    best_bid: str,
    best_ask: str,
) -> dict[str, Any]:
    return {
        "event_type": "price_change",
        "asset_id": asset_id,
        "market": market,
        "timestamp_received": recv,
        "timestamp": src,
        "side": side,
        "price": price,
        "size": size,
        "best_bid": best_bid,
        "best_ask": best_ask,
    }


def _seed_A() -> dict[str, Any]:
    # best_bid 0.40 / best_ask 0.42 → mid 0.41
    return _book_row(
        asset_id="A",
        recv=1000,
        src=1000,
        bids=[["0.40", "100"], ["0.39", "200"]],
        asks=[["0.42", "150"], ["0.43", "250"]],
    )


# --------------------------------------------------------------------------- (a) evolving mid


def test_evolving_mid_across_deltas() -> None:
    rows = [
        _seed_A(),
        # BUY 0.41 → new best_bid 0.41, best_ask 0.42, mid 0.415
        _pc_row(asset_id="A", recv=2000, src=2000, side="BUY", price="0.41", size="50", best_bid="0.41", best_ask="0.42"),
        # SELL 0.42 size 0 → delete ask 0.42, best_ask 0.43, mid 0.42
        _pc_row(asset_id="A", recv=3000, src=3000, side="SELL", price="0.42", size="0", best_bid="0.41", best_ask="0.43"),
        # SELL 0.415 → new best_ask 0.415, mid 0.4125
        _pc_row(asset_id="A", recv=4000, src=4000, side="SELL", price="0.415", size="80", best_bid="0.41", best_ask="0.415"),
    ]
    series = mod.reconstruct_book_series(rows, interval_s=1.0, top_n=10)
    mids = [r["mid"] for r in series["A"]]
    assert mids == pytest.approx([0.41, 0.415, 0.42, 0.4125])


# --------------------------------------------------------------------------- (b) size==0 deletes


def test_size_zero_deletes_level() -> None:
    rows = [
        _seed_A(),
        # delete the ask at 0.42 → it must vanish from the book and best_ask → 0.43
        _pc_row(asset_id="A", recv=2000, src=2000, side="SELL", price="0.42", size="0", best_bid="0.40", best_ask="0.43"),
    ]
    series = mod.reconstruct_book_series(rows, interval_s=1.0, top_n=10)
    last = series["A"][-1]
    ask_prices = [lvl[0] for lvl in last["asks"]]
    assert 0.42 not in ask_prices
    assert last["best_ask"] == 0.43


# --------------------------------------------------------------------------- (c) BUY/SELL routing


def test_buy_maps_to_bid_sell_maps_to_ask() -> None:
    rows = [
        _seed_A(),
        # BUY adds a bid level 0.415 (best_bid moves up); ask side untouched
        _pc_row(asset_id="A", recv=2000, src=2000, side="BUY", price="0.415", size="10", best_bid="0.415", best_ask="0.42"),
        # SELL adds an ask level 0.418 (best_ask moves down); bid side untouched
        _pc_row(asset_id="A", recv=3000, src=3000, side="SELL", price="0.418", size="10", best_bid="0.415", best_ask="0.418"),
    ]
    series = mod.reconstruct_book_series(rows, interval_s=1.0, top_n=10)
    after_buy = series["A"][1]
    assert 0.415 in [lvl[0] for lvl in after_buy["bids"]]
    assert after_buy["best_bid"] == 0.415
    after_sell = series["A"][2]
    assert 0.418 in [lvl[0] for lvl in after_sell["asks"]]
    assert after_sell["best_ask"] == 0.418


# --------------------------------------------------------------------------- (d) downsampling


def test_downsampling_one_snapshot_per_bucket_keeps_last() -> None:
    rows = [
        _seed_A(),  # recv 1000 → bucket 1
        # two deltas inside the SAME 1s bucket (recv 2100, 2600 → bucket 2)
        _pc_row(asset_id="A", recv=2100, src=2100, side="BUY", price="0.41", size="50", best_bid="0.41", best_ask="0.42"),
        _pc_row(asset_id="A", recv=2600, src=2600, side="BUY", price="0.415", size="50", best_bid="0.415", best_ask="0.42"),
        _pc_row(asset_id="A", recv=3000, src=3000, side="SELL", price="0.45", size="10", best_bid="0.415", best_ask="0.42"),
    ]
    series = mod.reconstruct_book_series(rows, interval_s=1.0, top_n=10)
    # bucket 1 (seed) + bucket 2 (collapsed, last state) + bucket 3 = 3 rows
    assert len(series["A"]) == 3
    bucket2 = series["A"][1]
    # LAST state in bucket 2 wins → best_bid 0.415, mid 0.4175
    assert bucket2["best_bid"] == 0.415
    assert bucket2["mid"] == 0.4175
    assert bucket2["ts_ms"] == 2000  # bucket start (recv-time)


# --------------------------------------------------------------------------- (e) delta before seed


def test_price_change_before_book_emits_gap_not_guess() -> None:
    rows = [
        # a delta arrives with NO prior book seed → must be an honest gap, never a guessed book
        _pc_row(asset_id="A", recv=1000, src=1000, side="BUY", price="0.41", size="50", best_bid="0.41", best_ask="0.42"),
        _seed_A(),  # book seed arrives afterwards at recv 1000... use later ts below
    ]
    # keep recv ordering sane: reseed after the gap
    rows[1]["timestamp_received"] = 2000
    rows[1]["timestamp"] = 2000
    series = mod.reconstruct_book_series(rows, interval_s=1.0, top_n=10)
    a_rows = series["A"]
    gaps = [r for r in a_rows if r["gap"]]
    assert len(gaps) == 1
    assert gaps[0]["gap_reason"] == "delta_before_seed"
    # the pre-seed delta must NOT have produced a book snapshot (no guessed state)
    non_gap_before_seed = [r for r in a_rows if not r["gap"] and r["source_ts_ms"] == 1000]
    assert non_gap_before_seed == []


# --------------------------------------------------------------------------- (f) checksum mismatch


def test_best_price_mismatch_forces_gap_and_reseed() -> None:
    rows = [
        _seed_A(),
        # claim a best_bid/best_ask that CANNOT match the computed book → checksum divergence
        _pc_row(asset_id="A", recv=2000, src=2000, side="BUY", price="0.41", size="50", best_bid="0.99", best_ask="0.42"),
        # a fresh book reseeds after the gap
        _book_row(asset_id="A", recv=3000, src=3000, bids=[["0.50", "100"]], asks=[["0.52", "100"]]),
    ]
    series = mod.reconstruct_book_series(rows, interval_s=1.0, top_n=10)
    a_rows = series["A"]
    gaps = [r for r in a_rows if r["gap"]]
    assert len(gaps) == 1
    assert gaps[0]["gap_reason"] == "checksum_or_stale_reseed"
    # after reseed the book is the fresh one (mid 0.51), not a drifted/guessed state
    assert a_rows[-1]["mid"] == 0.51


# --------------------------------------------------------------------------- interleave two tokens


def test_two_asset_interleave_reconstructed_independently() -> None:
    rows = [
        _seed_A(),
        _book_row(asset_id="B", recv=1000, src=1000, bids=[["0.60", "100"]], asks=[["0.62", "100"]]),
        _pc_row(asset_id="A", recv=2000, src=2000, side="BUY", price="0.41", size="50", best_bid="0.41", best_ask="0.42"),
        _pc_row(asset_id="B", recv=2000, src=2000, side="SELL", price="0.61", size="50", best_bid="0.60", best_ask="0.61"),
    ]
    series = mod.reconstruct_book_series(rows, interval_s=1.0, top_n=10)
    assert set(series.keys()) == {"A", "B"}
    assert series["A"][-1]["mid"] == 0.415
    assert series["B"][-1]["mid"] == 0.605


# --------------------------------------------------------------------------- top-N truncation


def test_top_n_truncates_each_side() -> None:
    rows = [
        _book_row(
            asset_id="A",
            recv=1000,
            src=1000,
            bids=[["0.40", "1"], ["0.39", "1"], ["0.38", "1"]],
            asks=[["0.42", "1"], ["0.43", "1"], ["0.44", "1"]],
        ),
    ]
    series = mod.reconstruct_book_series(rows, interval_s=1.0, top_n=2)
    row = series["A"][-1]
    assert len(row["bids"]) == 2
    assert len(row["asks"]) == 2
    # top-2 bids are the two highest prices (descending)
    assert [lvl[0] for lvl in row["bids"]] == [0.40, 0.39]
    assert [lvl[0] for lvl in row["asks"]] == [0.42, 0.43]


# --------------------------------------------------------------------------- (g) lazy-import audit


def test_module_import_performs_no_network_import() -> None:
    banned = {"duckdb", "requests", "httpx", "aiohttp", "urllib", "pyarrow", "pandas"}
    src = Path(mod.__file__).read_text()
    tree = ast.parse(src)
    module_level_roots: set[str] = set()
    for node in ast.iter_child_nodes(tree):  # ONLY module-scope statements
        if isinstance(node, ast.Import):
            module_level_roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            module_level_roots.add(node.module.split(".")[0])
    assert banned.isdisjoint(module_level_roots), f"network module imported at module scope: {banned & module_level_roots}"


# --------------------------------------------------------------------------- (h) fake resolver


def test_resolve_markets_uses_injected_resolver_no_gamma() -> None:
    calls: list[Mapping[str, Any]] = []

    def fake_resolver(fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
        calls.append(fixture)
        return [
            {"condition_id": "0x01", "asset_id": "tokHome", "outcome_label": "home_win", "question": "Will France win?"},
            {"condition_id": "0x02", "asset_id": "tokDraw", "outcome_label": "draw", "question": "Draw?"},
            {"condition_id": "0x03", "asset_id": "tokAway", "outcome_label": "away_win", "question": "Will Morocco win?"},
        ]

    fixtures = [{"fixture_id": 18209181, "event_slug": "fifwc-fra-mar-2026-07-09", "home_team": "France", "away_team": "Morocco", "kickoff_ts": 1783627200}]
    resolved = mod.resolve_markets(fixtures, resolver=fake_resolver)
    assert len(calls) == 1  # the FAKE resolver was used (no Gamma)
    assert len(resolved) == 3
    labels = {m.outcome_label for m in resolved}
    assert labels == {"home_win", "draw", "away_win"}
    assert all(m.fixture_id == 18209181 for m in resolved)
    assert {m.asset_id for m in resolved} == {"tokHome", "tokDraw", "tokAway"}


# --------------------------------------------------------------------------- meta passthrough + write


def test_meta_by_asset_populates_output_and_jsonl_roundtrips(tmp_path: Path) -> None:
    meta = {"A": {"condition_id": "0xcond", "fixture_id": 18209181, "outcome_label": "home_win"}}
    series = mod.reconstruct_book_series([_seed_A()], interval_s=1.0, top_n=10, meta_by_asset=meta)
    row = series["A"][-1]
    assert row["condition_id"] == "0xcond"
    assert row["fixture_id"] == 18209181
    assert row["outcome_label"] == "home_win"
    assert row["asset_id"] == "A"

    out_dir = tmp_path / "books"
    path = mod.write_series_jsonl(series["A"], out_dir=out_dir, fixture_id=18209181, outcome_label="home_win")
    assert path.exists()
    loaded = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert loaded == series["A"]


# --------------------------------------------------------------------------- CLI --help offline


def test_build_parser_help_offline() -> None:
    parser = mod.build_parser()
    text = parser.format_help()
    assert "--fixtures" in text
    assert "--interval" in text
    assert "--top-n" in text


# =========================================================================== OPTION A
# The direct top-of-book primary series, depth/trade artifacts, shared extractor, and
# per-artifact summary/meta. All still fully offline on canned rows.


def _trade_row(
    *,
    asset_id: str,
    market: str = "0x" + "ab" * 32,
    recv: int,
    src: int,
    price: str,
    size: str,
    side: str | None = None,
    transaction_hash: str = "0xdeadbeef",
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "event_type": "last_trade_price",
        "asset_id": asset_id,
        "market": market,
        "timestamp_received": recv,
        "timestamp": src,
        "price": price,
        "size": size,
        "transaction_hash": transaction_hash,
    }
    if side is not None:
        row["side"] = side
    return row


# --------------------------------------------------------- (A) shared extractor — pure/deterministic


def test_extract_top_of_book_ok_computes_mid_and_spread() -> None:
    from veridex.live_recorder.top_of_book import extract_top_of_book

    tob = extract_top_of_book("0.40", "0.42")
    assert tob.status == "ok"
    assert tob.reason is None
    assert tob.bid == 0.40
    assert tob.ask == 0.42
    assert tob.mid == pytest.approx(0.41)
    assert tob.spread == pytest.approx(0.02)


def test_extract_top_of_book_missing_best_is_gap_not_zero() -> None:
    from veridex.live_recorder.top_of_book import extract_top_of_book

    tob = extract_top_of_book("0.40", None)
    assert tob.status == "gap"
    assert tob.mid is None
    assert tob.spread is None
    # never a silent zero
    assert tob.ask != 0.0


def test_extract_top_of_book_non_numeric_is_gap() -> None:
    from veridex.live_recorder.top_of_book import extract_top_of_book

    tob = extract_top_of_book("not-a-number", "0.42")
    assert tob.status == "gap"
    assert tob.mid is None


def test_extract_top_of_book_non_finite_is_gap_never_ok() -> None:
    # NaN/inf survive float() and fail-open every guard (nan<=0 is False, nan>=ask is False),
    # so without an isfinite gate they leak an ``ok`` row with mid=nan → invalid JSON + poisoned
    # coverage. A non-finite best price MUST be a gap, never ok. (Codex code-review Major.)
    import math

    from veridex.live_recorder.top_of_book import extract_top_of_book

    for bad in ("nan", "inf", "-inf", float("nan"), float("inf"), float("-inf")):
        for pair in ((bad, "0.42"), ("0.40", bad)):
            tob = extract_top_of_book(*pair)
            assert tob.status == "gap", f"{pair!r} should be gap, got {tob.status}"
            assert tob.reason == "non_numeric_best", f"{pair!r} reason={tob.reason}"
            assert tob.mid is None and tob.spread is None
            # no non-finite value leaks into the emitted bid/ask either
            assert tob.bid is None or math.isfinite(tob.bid)
            assert tob.ask is None or math.isfinite(tob.ask)


def test_extract_top_of_book_crossed_is_excluded_and_disclosed() -> None:
    from veridex.live_recorder.top_of_book import extract_top_of_book

    tob = extract_top_of_book("0.50", "0.48")
    assert tob.status == "excluded"
    assert tob.reason == "crossed"
    # crossed values are DISCLOSED (not silently dropped), but no misleading mid
    assert tob.bid == 0.50
    assert tob.ask == 0.48
    assert tob.mid is None


def test_extract_top_of_book_empty_side_sentinel_is_gap_not_excluded() -> None:
    from veridex.live_recorder.top_of_book import extract_top_of_book

    # venue "0" (or empty) is the documented one-sided/empty sentinel → gap, never "excluded"
    tob = extract_top_of_book("0", "0.42")
    assert tob.status == "gap"
    assert tob.reason == "one_sided"
    assert tob.mid is None


def test_extract_top_of_book_is_pure_and_deterministic() -> None:
    from veridex.live_recorder.top_of_book import extract_top_of_book

    a = extract_top_of_book("0.40", "0.42")
    b = extract_top_of_book("0.40", "0.42")
    assert a == b  # same inputs → identical value (no hidden state)


def test_top_of_book_module_import_performs_no_network_import() -> None:
    from veridex.live_recorder import top_of_book as tob_mod

    banned = {"duckdb", "requests", "httpx", "aiohttp", "urllib", "pyarrow", "pandas"}
    tree = ast.parse(Path(tob_mod.__file__).read_text())
    roots: set[str] = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    assert banned.isdisjoint(roots), f"network import at module scope: {banned & roots}"


# --------------------------------------------------------- (B) direct top-of-book series


def test_top_of_book_ok_row_has_mid_and_spread() -> None:
    rows = [
        _pc_row(asset_id="A", recv=1000, src=1000, side="BUY", price="0.40", size="10", best_bid="0.40", best_ask="0.42"),
    ]
    series = mod.build_top_of_book_series(rows, interval_ms=1000)
    row = series["A"][-1]
    assert row["status"] == "ok"
    assert row["best_bid"] == 0.40
    assert row["best_ask"] == 0.42
    assert row["mid"] == pytest.approx(0.41)
    assert row["spread"] == pytest.approx(0.02)
    assert row["recv_ts_ms"] == 1000  # TRUE receipt of the winning row
    assert row["bucket_ts_ms"] == 1000  # bucket grid start (here equal to the receipt)
    assert row["source_ts_ms"] == 1000


def test_top_of_book_missing_best_marks_gap() -> None:
    rows = [
        _pc_row(asset_id="A", recv=1000, src=1000, side="BUY", price="0.40", size="10", best_bid="0.40", best_ask=""),
    ]
    series = mod.build_top_of_book_series(rows, interval_ms=1000)
    row = series["A"][-1]
    assert row["status"] == "gap"
    assert row["mid"] is None


def test_top_of_book_crossed_marks_excluded_and_is_counted() -> None:
    rows = [
        _pc_row(asset_id="A", recv=1000, src=1000, side="BUY", price="0.50", size="10", best_bid="0.50", best_ask="0.48"),
    ]
    series = mod.build_top_of_book_series(rows, interval_ms=1000)
    row = series["A"][-1]
    assert row["status"] == "excluded"
    assert row["status_reason"] == "crossed"


def test_top_of_book_event_bucket_keeps_last_row_in_bucket() -> None:
    rows = [
        _pc_row(asset_id="A", recv=2100, src=2100, side="BUY", price="0.40", size="10", best_bid="0.40", best_ask="0.42"),
        _pc_row(asset_id="A", recv=2600, src=2600, side="BUY", price="0.44", size="10", best_bid="0.44", best_ask="0.46"),
    ]
    series = mod.build_top_of_book_series(rows, interval_ms=1000)
    assert len(series["A"]) == 1  # both in bucket 2 → collapsed
    row = series["A"][0]
    assert row["recv_ts_ms"] == 2600  # TRUE receipt of the winning (last) row, not the bucket start
    assert row["bucket_ts_ms"] == 2000  # bucket grid start
    assert row["mid"] == pytest.approx(0.45)  # LAST row in the bucket wins


def test_top_of_book_empty_bucket_is_omitted_no_stale_carry() -> None:
    # THE load-bearing no-stale-as-fresh test: bucket 1 and bucket 3 have updates, bucket 2 is
    # EMPTY. Bucket 2 must be OMITTED (not carried forward from bucket 1), and bucket 3 must
    # reflect its OWN update — a bucket-1 row must never resurface as a fresh ok for bucket 2/3.
    rows = [
        _pc_row(asset_id="A", recv=1000, src=1000, side="BUY", price="0.40", size="10", best_bid="0.40", best_ask="0.42"),
        _pc_row(asset_id="A", recv=3000, src=3000, side="BUY", price="0.44", size="10", best_bid="0.44", best_ask="0.46"),
    ]
    series = mod.build_top_of_book_series(rows, interval_ms=1000)
    buckets = [r["bucket_ts_ms"] for r in series["A"]]
    assert buckets == [1000, 3000]  # bucket 2 (bucket_ts_ms 2000) is OMITTED, never carried
    assert all(r["bucket_ts_ms"] != 2000 for r in series["A"])
    assert series["A"][-1]["mid"] == pytest.approx(0.45)  # bucket-3 row is its OWN update


def test_top_of_book_no_look_ahead_row_lands_in_its_own_bucket() -> None:
    rows = [
        _pc_row(asset_id="A", recv=5300, src=5300, side="BUY", price="0.40", size="10", best_bid="0.40", best_ask="0.42"),
    ]
    series = mod.build_top_of_book_series(rows, interval_ms=1000)
    assert series["A"][0]["bucket_ts_ms"] == 5000  # bucket 5 grid start, never an earlier bucket
    assert series["A"][0]["recv_ts_ms"] == 5300  # TRUE receipt preserved (no look-ahead either way)


# --------------------------------------------------------- (C) depth series from book rows


def test_depth_series_from_book_rows() -> None:
    rows = [_seed_A()]
    series = mod.build_depth_series(rows, top_n=10)
    row = series["A"][-1]
    assert row["best_bid"] == 0.40
    assert row["best_ask"] == 0.42
    assert row["mid"] == pytest.approx(0.41)
    assert [lvl[0] for lvl in row["bids"]] == [0.40, 0.39]
    assert [lvl[0] for lvl in row["asks"]] == [0.42, 0.43]
    assert row["tick_size"] == 0.01
    assert row["recv_ts_ms"] == 1000
    assert row["source_ts_ms"] == 1000


# --------------------------------------------------------- (D) trades series from last_trade_price


def test_trades_series_from_last_trade_price() -> None:
    rows = [
        _seed_A(),  # a book row must be ignored by the trades builder
        _trade_row(asset_id="A", recv=1500, src=1500, price="0.41", size="25", side="BUY", transaction_hash="0xabc"),
    ]
    series = mod.build_trades_series(rows)
    assert list(series.keys()) == ["A"]
    trade = series["A"][-1]
    assert trade["price"] == 0.41
    assert trade["size"] == 25.0
    assert trade["side"] == "BUY"
    assert trade["transaction_hash"] == "0xabc"
    assert trade["recv_ts_ms"] == 1500
    assert trade["source_ts_ms"] == 1500


# --------------------------------------------------------- (E) summary counts + coverage


def test_summary_counts_and_coverage() -> None:
    pc_rows = [
        _pc_row(asset_id="A", recv=1000, src=1000, side="BUY", price="0.40", size="10", best_bid="0.40", best_ask="0.42"),
        _pc_row(asset_id="A", recv=2000, src=2000, side="BUY", price="0.50", size="10", best_bid="0.50", best_ask="0.48"),
        _pc_row(asset_id="A", recv=3000, src=3000, side="BUY", price="0.40", size="10", best_bid="0.40", best_ask=""),
    ]
    book_rows = [_seed_A()]
    trade_rows = [_trade_row(asset_id="A", recv=1500, src=1500, price="0.41", size="25")]
    top = mod.build_top_of_book_series(pc_rows, interval_ms=1000)["A"]
    depth = mod.build_depth_series(book_rows, top_n=10)["A"]
    trades = mod.build_trades_series(trade_rows)["A"]

    summary = mod.summarize_outcome(top, depth, trades, total_price_change=len(pc_rows), interval_ms=1000)
    assert summary["price_change_rows"] == 3
    assert summary["gap"] == 1
    assert summary["excluded"] == 1
    assert summary["crossed"] == 1
    assert summary["book_snapshots"] == 1
    assert summary["trades"] == 1
    assert summary["interval_ms"] == 1000
    # observed span = buckets 1..3 = 3 buckets; 1 ok bucket → span_coverage 1/3
    assert summary["span_buckets"] == 3
    assert summary["ok_buckets"] == 1
    assert summary["span_coverage"] == pytest.approx(1 / 3)
    # no requested window passed → window fields are explicit None (never silently the span number)
    assert summary["window_buckets"] is None
    assert summary["window_coverage"] is None


def test_summary_window_coverage_uses_requested_window_not_observed_span() -> None:
    # One quote 2h into a wide requested window must NOT read as 100% coverage. Coverage is computed
    # over the REQUESTED window, and interval_ms + window bounds are in the summary so the artifact
    # set is self-describing (a consumer can recompute bucket edges / honest coverage).
    pc_rows = [
        _pc_row(asset_id="A", recv=1000, src=1000, side="BUY", price="0.40", size="10", best_bid="0.40", best_ask="0.42"),
    ]
    top = mod.build_top_of_book_series(pc_rows, interval_ms=1000)["A"]
    summary = mod.summarize_outcome(
        top, [], [], total_price_change=1, interval_ms=1000,
        window_start_ms=0, window_end_ms=10000,
    )
    assert summary["interval_ms"] == 1000
    assert summary["window_start_ms"] == 0
    assert summary["window_end_ms"] == 10000
    assert summary["window_buckets"] == 11  # buckets 0..10 inclusive over the requested window
    assert summary["window_coverage"] == pytest.approx(1 / 11)
    assert summary["window_coverage"] < 1.0  # the near-dead market is NOT reported as 100%
    # the observed-span number is preserved but named span_* (100% over its own 1-bucket span)
    assert summary["span_buckets"] == 1
    assert summary["span_coverage"] == pytest.approx(1.0)


# --------------------------------------------------------- (F) four artifacts + merge-diagnostic flag


def _outcome_inputs() -> dict[str, Any]:
    pc_rows = [_pc_row(asset_id="A", recv=1000, src=1000, side="BUY", price="0.40", size="10", best_bid="0.40", best_ask="0.42")]
    return {
        "top_rows": mod.build_top_of_book_series(pc_rows, interval_ms=1000)["A"],
        "depth_rows": mod.build_depth_series([_seed_A()], top_n=10)["A"],
        "trades_rows": mod.build_trades_series([_trade_row(asset_id="A", recv=1500, src=1500, price="0.41", size="25")])["A"],
        "merge_rows": mod.reconstruct_book_series([_seed_A()], interval_s=1.0, top_n=10)["A"],
    }


def test_write_outcome_artifacts_emits_three_by_default(tmp_path: Path) -> None:
    inputs = _outcome_inputs()
    summary = mod.summarize_outcome(inputs["top_rows"], inputs["depth_rows"], inputs["trades_rows"], total_price_change=1, interval_ms=1000)
    mod.write_outcome_artifacts(
        out_dir=tmp_path, fixture_id=18209181, outcome_label="home_win",
        top_rows=inputs["top_rows"], depth_rows=inputs["depth_rows"], trades_rows=inputs["trades_rows"],
        merge_rows=inputs["merge_rows"], summary=summary, emit_merge_diagnostic=False,
    )
    assert (tmp_path / "18209181_home_win.top_of_book.jsonl").exists()
    assert (tmp_path / "18209181_home_win.depth.jsonl").exists()
    assert (tmp_path / "18209181_home_win.trades.jsonl").exists()
    assert (tmp_path / "18209181_home_win.summary.json").exists()
    assert (tmp_path / "18209181_home_win.meta.json").exists()
    # merge diagnostic is OFF by default
    assert not (tmp_path / "18209181_home_win.merge_diagnostic.jsonl").exists()


def test_write_outcome_artifacts_emits_merge_diagnostic_with_flag(tmp_path: Path) -> None:
    inputs = _outcome_inputs()
    summary = mod.summarize_outcome(inputs["top_rows"], inputs["depth_rows"], inputs["trades_rows"], total_price_change=1, interval_ms=1000)
    mod.write_outcome_artifacts(
        out_dir=tmp_path, fixture_id=18209181, outcome_label="home_win",
        top_rows=inputs["top_rows"], depth_rows=inputs["depth_rows"], trades_rows=inputs["trades_rows"],
        merge_rows=inputs["merge_rows"], summary=summary, emit_merge_diagnostic=True,
    )
    assert (tmp_path / "18209181_home_win.merge_diagnostic.jsonl").exists()


def test_meta_json_is_honesty_labeled_and_rows_stay_pure(tmp_path: Path) -> None:
    inputs = _outcome_inputs()
    summary = mod.summarize_outcome(inputs["top_rows"], inputs["depth_rows"], inputs["trades_rows"], total_price_change=1, interval_ms=1000)
    mod.write_outcome_artifacts(
        out_dir=tmp_path, fixture_id=18209181, outcome_label="home_win",
        top_rows=inputs["top_rows"], depth_rows=inputs["depth_rows"], trades_rows=inputs["trades_rows"],
        merge_rows=inputs["merge_rows"], summary=summary, emit_merge_diagnostic=False,
    )
    meta = json.loads((tmp_path / "18209181_home_win.meta.json").read_text())
    blob = json.dumps(meta).lower()
    assert "research-grade" in blob
    assert "recorder clock" in blob
    # JSONL rows stay PURE — no honesty-label/meta key leaked into a data row
    top_line = json.loads((tmp_path / "18209181_home_win.top_of_book.jsonl").read_text().splitlines()[0])
    assert set(top_line.keys()) == {
        "recv_ts_ms", "bucket_ts_ms", "source_ts_ms", "best_bid", "best_ask", "mid", "spread", "status",
        "status_reason", "condition_id", "asset_id", "fixture_id", "outcome_label",
    }


def test_jsonl_roundtrips_for_all_artifacts(tmp_path: Path) -> None:
    inputs = _outcome_inputs()
    summary = mod.summarize_outcome(inputs["top_rows"], inputs["depth_rows"], inputs["trades_rows"], total_price_change=1, interval_ms=1000)
    mod.write_outcome_artifacts(
        out_dir=tmp_path, fixture_id=18209181, outcome_label="home_win",
        top_rows=inputs["top_rows"], depth_rows=inputs["depth_rows"], trades_rows=inputs["trades_rows"],
        merge_rows=inputs["merge_rows"], summary=summary, emit_merge_diagnostic=True,
    )
    for name, expected in [
        ("18209181_home_win.top_of_book.jsonl", inputs["top_rows"]),
        ("18209181_home_win.depth.jsonl", inputs["depth_rows"]),
        ("18209181_home_win.trades.jsonl", inputs["trades_rows"]),
        ("18209181_home_win.merge_diagnostic.jsonl", inputs["merge_rows"]),
    ]:
        loaded = [json.loads(x) for x in (tmp_path / name).read_text().splitlines() if x.strip()]
        assert loaded == expected


# --------------------------------------------------------- (G) CLI flag


def test_build_parser_has_emit_merge_diagnostic_flag_off_by_default() -> None:
    parser = mod.build_parser()
    assert "--emit-merge-diagnostic" in parser.format_help()
    args = parser.parse_args(["--fixtures", "f.json"])
    assert args.emit_merge_diagnostic is False


# =========================================================================== FABLE REVIEW FIXES
# TDD for the Fable code-review findings (2 HIGH + 4 MEDIUM + 2 cheap LOW). All still offline.


# --------------------------------------------------------- HIGH-1: non-finite in depth / trades


def test_depth_series_non_finite_level_is_dropped_and_counted() -> None:
    # A book row with NaN/inf in a ladder level must NOT leak the non-finite value into depth.jsonl
    # (invalid strict JSON + undefined sorted() ordering). The level is dropped and COUNTED.
    import math

    rows = [
        _book_row(
            asset_id="A",
            recv=1000,
            src=1000,
            bids=[["NaN", "100"], ["0.39", "50"]],
            asks=[["0.42", "150"], ["inf", "10"]],
        ),
    ]
    series = mod.build_depth_series(rows, top_n=10)
    row = series["A"][-1]
    for lvl in row["bids"] + row["asks"]:
        assert math.isfinite(lvl[0]) and math.isfinite(lvl[1])
    assert row["best_bid"] == 0.39  # the NaN bid level was dropped
    assert row["best_ask"] == 0.42  # the inf ask level was dropped
    assert row["mid"] == pytest.approx((0.39 + 0.42) / 2.0)
    assert row["dropped_levels"] == 2  # NaN bid + inf ask, counted — never silently emitted


def test_trades_series_non_finite_price_or_size_becomes_none() -> None:
    rows = [
        _trade_row(asset_id="A", recv=1500, src=1500, price="nan", size="inf"),
    ]
    series = mod.build_trades_series(rows)
    trade = series["A"][-1]
    assert trade["price"] is None  # NaN price dropped to None, never emitted as NaN
    assert trade["size"] is None  # Infinity size dropped to None


def test_write_jsonl_rejects_non_finite_values(tmp_path: Path) -> None:
    # Defense-in-depth boundary guard: any future non-finite leak must fail LOUDLY at write time
    # (allow_nan=False) rather than silently writing non-standard NaN/Infinity JSON tokens.
    path = tmp_path / "bad.jsonl"
    with pytest.raises(ValueError):
        mod._write_jsonl([{"x": float("nan")}], path)


# --------------------------------------------------------- HIGH-2: preserve TRUE receipt time


def test_top_of_book_preserves_true_receipt_not_just_bucket_start() -> None:
    # A row received at bucket_end-1 (ts_recv=1999, interval 1000) must expose its TRUE receipt
    # (1999), not only the bucket grid start (1000). A downstream `true_recv <= decision_ts` join
    # must NOT admit it early — bucket-start labeling alone is a one-directional OPTIMISTIC skew,
    # the dangerous direction for a maker fill/spread backtest.
    rows = [
        _pc_row(asset_id="A", recv=1999, src=1999, side="BUY", price="0.40", size="10", best_bid="0.40", best_ask="0.42"),
    ]
    series = mod.build_top_of_book_series(rows, interval_ms=1000)
    row = series["A"][0]
    assert row["recv_ts_ms"] == 1999  # TRUE receipt recoverable, not just the 1000 bucket start
    assert row["bucket_ts_ms"] == 1000  # bucket grid start still present and DISTINCT
    # a no-look-ahead join keyed on the TRUE receipt must NOT admit a 1999 quote for decision 1500
    admitted = [r for r in series["A"] if r["recv_ts_ms"] <= 1500]
    assert admitted == []


# --------------------------------------------------------- MEDIUM-2: crossed depth → no fake mid


def test_depth_series_crossed_book_has_no_mid_and_is_flagged_and_counted() -> None:
    # A crossed book snapshot (best_bid >= best_ask) must NOT emit a fabricated mid in depth.jsonl —
    # the primary top-of-book EXCLUDES crossed moments, so depth must not disagree. It gets
    # mid=None + a crossed flag, and is counted in the summary.
    rows = [
        _book_row(asset_id="A", recv=1000, src=1000, bids=[["0.50", "100"]], asks=[["0.48", "100"]]),
    ]
    series = mod.build_depth_series(rows, top_n=10)
    row = series["A"][-1]
    assert row["best_bid"] == 0.50
    assert row["best_ask"] == 0.48
    assert row["mid"] is None  # no fabricated mid at a crossed moment
    assert row["crossed"] is True
    summary = mod.summarize_outcome([], series["A"], [], total_price_change=0, interval_ms=1000)
    assert summary["crossed_depth"] == 1


# --------------------------------------------------------- MEDIUM-3: defensive global row ordering


def test_top_of_book_out_of_order_rows_latest_recv_ts_wins_bucket() -> None:
    # Rows arriving OUT of global order within one bucket: [recv=1900, then recv=1100]. "Last wins"
    # must mean last BY RECEIPT TIME (1900), not last-in-iteration (1100) — a defensive stable sort
    # by timestamp_received keeps last-in-bucket == latest-by-recv-ts.
    rows = [
        _pc_row(asset_id="A", recv=1900, src=1900, side="BUY", price="0.44", size="10", best_bid="0.44", best_ask="0.46"),
        _pc_row(asset_id="A", recv=1100, src=1100, side="BUY", price="0.40", size="10", best_bid="0.40", best_ask="0.42"),
    ]
    series = mod.build_top_of_book_series(rows, interval_ms=1000)
    assert len(series["A"]) == 1  # both land in bucket 1
    row = series["A"][0]
    assert row["recv_ts_ms"] == 1900  # latest-by-recv wins, not the later-in-iteration 1100 row
    assert row["mid"] == pytest.approx(0.45)  # the 1900 row's quote (0.44/0.46), not the 1100 row's


# --------------------------------------------------------- MEDIUM-4: duplicate-label overwrite guard


def test_resolve_markets_duplicate_label_per_fixture_fails_loudly() -> None:
    # Two resolved markets mapping to the SAME {fixture}_{label} would silently overwrite each
    # other's six artifact files. The resolver must FAIL loudly on the collision, not drop one.
    def dup_resolver(fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [
            {"condition_id": "0x01", "asset_id": "tokA", "outcome_label": "home_win", "question": "Will France win?"},
            {"condition_id": "0x02", "asset_id": "tokB", "outcome_label": "home_win", "question": "Will France win in regulation?"},
        ]

    fixtures = [{"fixture_id": 1, "event_slug": "x", "home_team": "France", "away_team": "Morocco", "kickoff_ts": 1}]
    with pytest.raises(ValueError, match="duplicate"):
        mod.resolve_markets(fixtures, resolver=dup_resolver)


def test_resolve_markets_same_label_different_fixtures_is_ok() -> None:
    # The guard is PER FIXTURE — the same label across DIFFERENT fixtures is fine (distinct stems).
    def resolver(fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [{"condition_id": "0x01", "asset_id": f"tok{fixture['fixture_id']}", "outcome_label": "home_win", "question": "q"}]

    fixtures = [{"fixture_id": 1}, {"fixture_id": 2}]
    resolved = mod.resolve_markets(fixtures, resolver=resolver)
    assert {m.fixture_id for m in resolved} == {1, 2}


# --------------------------------------------------------- LOW: NULL timestamp_received crash guard


def test_top_of_book_null_timestamp_is_skipped_not_crash() -> None:
    # A single NULL timestamp_received must be SKIPPED, never crash the whole pull; the other rows
    # still process (fail-closed on the bad row, not all-or-nothing).
    good = _pc_row(asset_id="A", recv=1000, src=1000, side="BUY", price="0.40", size="10", best_bid="0.40", best_ask="0.42")
    bad = _pc_row(asset_id="A", recv=2000, src=2000, side="BUY", price="0.44", size="10", best_bid="0.44", best_ask="0.46")
    bad["timestamp_received"] = None
    series = mod.build_top_of_book_series([good, bad], interval_ms=1000)
    assert len(series["A"]) == 1  # only the good row survived; the NULL-ts row was dropped
    assert series["A"][0]["recv_ts_ms"] == 1000


def test_depth_series_null_timestamp_is_skipped_not_crash() -> None:
    good = _seed_A()  # recv 1000
    bad = _book_row(asset_id="A", recv=2000, src=2000, bids=[["0.50", "1"]], asks=[["0.52", "1"]])
    bad["timestamp_received"] = None
    series = mod.build_depth_series([good, bad], top_n=10)
    assert len(series["A"]) == 1  # NULL-ts book row dropped, no crash
    assert series["A"][0]["recv_ts_ms"] == 1000


def test_trades_series_null_timestamp_is_skipped_not_crash() -> None:
    good = _trade_row(asset_id="A", recv=1500, src=1500, price="0.41", size="25")
    bad = _trade_row(asset_id="A", recv=2500, src=2500, price="0.42", size="5")
    bad["timestamp_received"] = None
    series = mod.build_trades_series([good, bad])
    assert len(series["A"]) == 1  # NULL-ts trade dropped, no crash
    assert series["A"][0]["recv_ts_ms"] == 1500


# --------------------------------------------------------------------------- (N1) malformed tick_size
def test_build_depth_series_malformed_tick_size_does_not_crash() -> None:
    # N1 (Fable re-review nit): new_tick_size parsed with bare float() crashed the whole pull on
    # "abc" and put NaN on depth rows for "nan". Route through _to_float_or_none: a malformed tick
    # is ignored (default kept), never crashes and never leaks a non-finite tick_size.
    import math

    book = _book_row(asset_id="A", recv=1000, src=1000, bids=[["0.40", "100"]], asks=[["0.42", "100"]])
    for bad in ("abc", "nan", "inf"):
        tick_row = {
            "event_type": "tick_size_change", "asset_id": "A", "market": "0x" + "ab" * 32,
            "timestamp_received": 500, "timestamp": 500, "new_tick_size": bad,
        }
        series = mod.build_depth_series([tick_row, book], top_n=10)  # must NOT raise
        row = series["A"][-1]
        assert row["tick_size"] is not None and math.isfinite(row["tick_size"])


# --------------------------------------------------------------------------- (N2) merge-series sort
def test_reconstruct_book_series_sorts_out_of_order_rows() -> None:
    # N2 (Fable re-review nit): the opt-in merge diagnostic assumed pre-sorted rows. Mirror the
    # other builders' defensive recv-ts sort so emitted snapshots are in receipt order even if the
    # archive delivers rows out of order.
    seed_late = _book_row(asset_id="A", recv=2000, src=2000, bids=[["0.50", "100"]], asks=[["0.52", "100"]])
    seed_early = _book_row(asset_id="A", recv=1000, src=1000, bids=[["0.40", "100"]], asks=[["0.42", "100"]])
    series = mod.reconstruct_book_series([seed_late, seed_early], interval_s=1.0, top_n=10)
    ts_order = [r["ts_ms"] for r in series["A"]]
    assert ts_order == [1000, 2000]
