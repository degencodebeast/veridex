"""M7 (S6) — predeclared multi-fixture evaluation + the results producer (Tasks 19 / 19b).

The S6 evaluation is DECLARED BEFORE it is run (CON-008): a committed ``EvalProtocol`` names the
fixtures, the strategy roster, the window/close semantics, and the baselines; ``run_multi_fixture_evaluation``
then reports whatever that one pass yields — it never synthesizes a protocol at runtime. Two honesty
gates live here:

  * StaleLine is admitted ONLY when the recorded quote cadence actually backs sub-minute freshness
    (AC-009): ``stale_line_included`` is ``True`` iff the protocol asked for it AND ``cadence_ok``.
  * Every reported metric carries a machine-readable evidence rung (one of the five
    :class:`~veridex.provenance.EvidenceRung` labels) — no metric is surfaced without its provenance.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from tests.test_replay_pack import _write_session
from veridex.backtest.evaluation import (
    EvalProtocol,
    produce_results_by_fixture,
    run_multi_fixture_evaluation,
)
from veridex.ingest.replay_pack import pack_from_session
from veridex.provenance import EvidenceRung

_RUNG_LABELS = {rung.value for rung in EvidenceRung}


def _proto(**overrides) -> EvalProtocol:
    defaults = {
        "protocol_id": "eval-m7",
        "fixture_ids": [5],
        "strategy_configs": ["cumulative-drift"],
        "window": "w_eval",
        "close_semantics": "pre_match",
        "baselines": ["no_trade"],
        "committed_at": "2026-07-01T00:00:00Z",
    }
    defaults.update(overrides)
    return EvalProtocol(**defaults)


def _row(*, fixture_id: int = 5, kind: str = "cumulative-drift", market: str = "1X2",
         action: str = "WAIT", clv_bps: int | None = None) -> dict:
    return {"fixture_id": fixture_id, "kind": kind, "market": market, "action": action, "clv_bps": clv_bps}


# ------------------------------------------------------------------------------------------
# AC-009 — StaleLine is cadence-gated: it may be included ONLY when sub-minute cadence is proven.
# ------------------------------------------------------------------------------------------


def test_stale_line_excluded_when_cadence_insufficient() -> None:
    proto = _proto(strategy_configs=["cumulative-drift", "stale-line"])
    out = run_multi_fixture_evaluation(proto, results_by_fixture={5: [_row()]}, cadence_ok=False)
    assert out["stale_line_included"] is False


def test_stale_line_allowed_only_when_cadence_ok() -> None:
    proto = _proto(strategy_configs=["cumulative-drift", "stale-line"])
    out = run_multi_fixture_evaluation(proto, results_by_fixture={5: [_row()]}, cadence_ok=True)
    assert out["stale_line_included"] is True

    # ...and a protocol that never asked for stale-line stays excluded even with good cadence.
    no_stale = _proto(strategy_configs=["cumulative-drift"])
    out2 = run_multi_fixture_evaluation(no_stale, results_by_fixture={5: [_row()]}, cadence_ok=True)
    assert out2["stale_line_included"] is False


# ------------------------------------------------------------------------------------------
# Every reported metric carries a machine-readable evidence rung (one of the five labels).
# ------------------------------------------------------------------------------------------


def test_every_metric_carries_an_evidence_rung() -> None:
    out = run_multi_fixture_evaluation(_proto(), results_by_fixture={5: [_row()]}, cadence_ok=True)
    assert out["per_metric_rung"], "at least one metric must be reported"
    assert all(rung in _RUNG_LABELS for rung in out["per_metric_rung"].values())


# ------------------------------------------------------------------------------------------
# Baselines named in the protocol are surfaced as the zero-edge comparison floor.
# ------------------------------------------------------------------------------------------


def test_baselines_are_included() -> None:
    proto = _proto(baselines=["no_trade", "favorite"])
    out = run_multi_fixture_evaluation(proto, results_by_fixture={5: [_row()]}, cadence_ok=True)
    assert out["baselines_included"] == ["no_trade", "favorite"]


# ------------------------------------------------------------------------------------------
# Nulls (no-CLV rows) and abstentions (WAIT rows) are counted honestly — never dropped or zeroed.
# ------------------------------------------------------------------------------------------


def test_nulls_and_abstentions_are_counted_honestly() -> None:
    results_by_fixture = {
        5: [
            _row(action="FOLLOW_MOMENTUM", clv_bps=50),   # scored: neither null nor abstention
            _row(action="FOLLOW_MOMENTUM", clv_bps=None),  # null #1 (fired, but no closing CLV yet)
            _row(action="WAIT", clv_bps=None),             # null #2 AND abstention #1
        ]
    }
    out = run_multi_fixture_evaluation(_proto(), results_by_fixture=results_by_fixture, cadence_ok=True)
    assert out["nulls"] == 2
    assert out["abstentions"] == 1


# ==========================================================================================
# Task 19b — produce_results_by_fixture: run the predeclared roster over REAL packs (S6).
# ==========================================================================================


def _real_pack(tmp_path: Path) -> Path:
    """A real, hashed 1X2 pack (fixture 5) built through the same normalizer the live loop uses."""
    session_dir = _write_session(tmp_path)
    pack_dir = tmp_path / "pack"
    pack_from_session(session_dir, pack_dir)
    return pack_dir


def test_producer_generates_results_then_feeds_the_evaluation(tmp_path: Path) -> None:
    """The producer runs the roster on a real pack, then its output feeds the S6 evaluation end-to-end."""
    pack_dir = _real_pack(tmp_path)
    proto = _proto(fixture_ids=[5], strategy_configs=["cumulative-drift"], baselines=["no_trade"])

    results = asyncio.run(produce_results_by_fixture(proto, packs={5: pack_dir}))

    # REAL rows for the fixture — not an empty stub.
    assert 5 in results and isinstance(results[5], list)
    assert results[5], "the producer must yield real rows (drift decisions + the no_trade baseline)"
    # The load-bearing 19b claim: the drift Agent actually produced rows via run_backtest (not just
    # the baseline row satisfying non-emptiness).
    assert any(row["kind"] == "cumulative-drift" for row in results[5])
    assert any(row["kind"] == "no_trade" for row in results[5])

    out = run_multi_fixture_evaluation(proto, results_by_fixture=results, cadence_ok=True)
    assert out["baselines_included"] == ["no_trade"]
    assert all(rung in _RUNG_LABELS for rung in out["per_metric_rung"].values())


def test_producer_runs_all_named_baselines(tmp_path: Path) -> None:
    """Every named baseline is dispatched with ITS OWN signature and yields a row (DRIFT-1)."""
    pack_dir = _real_pack(tmp_path)
    names = ["no_trade", "favorite", "threshold_move", "seeded_random"]
    proto = _proto(fixture_ids=[5], strategy_configs=[], baselines=names)

    results = asyncio.run(produce_results_by_fixture(proto, packs={5: pack_dir}))

    kinds = {row["kind"] for row in results[5]}
    assert set(names) <= kinds, f"every baseline must produce a row; got {kinds}"
    # Each baseline row is a real, valid decision (WAIT or a fired pick) — never a fabricated CLV.
    for row in results[5]:
        assert row["action"] in {"WAIT", "FOLLOW_MOMENTUM"}
        assert row["clv_bps"] is None  # baselines are called directly (no law-scored CLV)


def test_producer_runs_value_vs_venue_only_with_a_source(tmp_path: Path) -> None:
    """VvV runs (and its estimated-edge metric appears at the venue rung) ONLY when a source is given (DRIFT-2)."""
    pack_dir = _real_pack(tmp_path)
    proto = _proto(fixture_ids=[5], strategy_configs=["value-vs-venue"], baselines=[])

    # venue decimal 5.0 makes every fixture-5 side's edge positive, so the agent FIRES.
    results = asyncio.run(
        produce_results_by_fixture(proto, packs={5: pack_dir}, venue_price_source=lambda mk: 5.0)
    )

    vvv_rows = [row for row in results[5] if row["kind"] == "value-vs-venue"]
    assert vvv_rows, "value-vs-venue must produce rows when a venue source is provided"
    assert any(row["action"] == "FOLLOW_MOMENTUM" for row in vvv_rows), "venue 5.0 must fire a pick"

    out = run_multi_fixture_evaluation(proto, results_by_fixture=results, cadence_ok=True)
    assert out["per_metric_rung"]["estimated_executable_edge_bps"] == "backfilled-price-history"


def test_producer_skips_value_vs_venue_without_a_source(tmp_path: Path) -> None:
    """No venue source ⇒ VvV is honestly skipped (no rows, no crash) — never a faked venue price (DRIFT-2)."""
    pack_dir = _real_pack(tmp_path)
    proto = _proto(fixture_ids=[5], strategy_configs=["value-vs-venue"], baselines=[])

    results = asyncio.run(produce_results_by_fixture(proto, packs={5: pack_dir}))  # no venue_price_source

    assert 5 in results
    assert [row for row in results[5] if row["kind"] == "value-vs-venue"] == []
