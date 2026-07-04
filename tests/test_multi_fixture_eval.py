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

import pytest

from tests.test_replay_pack import _write_session
from veridex.backtest.evaluation import (
    EvalProtocol,
    produce_results_by_fixture,
    run_multi_fixture_evaluation,
)
from veridex.backtest.runner import run_backtest
from veridex.backtest.venue_behavior_report import VenueDecision, build_venue_behavior_report
from veridex.backtest.vvv_report import vvv_report_with_estimated_edge
from veridex.ingest.marketstate import MarketState
from veridex.ingest.replay_pack import pack_from_session
from veridex.provenance import EvidenceRung
from veridex.runtime.window import RunWindow
from veridex.strategies.value_vs_venue import value_vs_venue_agent
from veridex.venues.venue_price_source import TimedVenueQuote

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
    """Every named baseline is run as an Agent through the SAME scored path drift uses (FU-3, DRIFT-1)."""
    pack_dir = _real_pack(tmp_path)
    names = ["no_trade", "favorite", "threshold_move", "seeded_random"]
    proto = _proto(fixture_ids=[5], strategy_configs=[], baselines=names)

    results = asyncio.run(produce_results_by_fixture(proto, packs={5: pack_dir}))

    kinds = {row["kind"] for row in results[5]}
    assert set(names) <= kinds, f"every baseline must produce a row; got {kinds}"
    # FU-3: each row is a real, valid decision — a WAIT abstention (null CLV) or a fired pick SCORED by
    # the law against the CON-040 close. clv_bps is null IFF the row is a WAIT (never a fabricated CLV,
    # never a null for a fired-and-closed pick). no_trade only ever WAITs → it stays null.
    for row in results[5]:
        assert row["action"] in {"WAIT", "FOLLOW_MOMENTUM"}
        assert (row["clv_bps"] is None) == (row["action"] == "WAIT")
    for row in (r for r in results[5] if r["kind"] == "no_trade"):
        assert row["clv_bps"] is None


def test_producer_runs_value_vs_venue_only_with_a_source(tmp_path: Path) -> None:
    """VvV runs (and its estimated-edge metric appears at the venue rung) ONLY when a source is given (DRIFT-2)."""
    pack_dir = _real_pack(tmp_path)
    proto = _proto(fixture_ids=[5], strategy_configs=["value-vs-venue"], baselines=[])

    # venue decimal 5.0 makes every fixture-5 side's edge positive, so the agent FIRES.
    results = asyncio.run(
        produce_results_by_fixture(
            proto,
            packs={5: pack_dir},
            venue_price_source=lambda fid, mk, side, ts: TimedVenueQuote(venue_decimal_price=5.0, staleness_s=0),
            venue_source_id="quote-artifact-hash-abc",
        )
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


def test_producer_requires_explicit_venue_source_identity_for_vvv(tmp_path: Path) -> None:
    """VvV needs an EXPLICIT venue_source_id (Codex M7): a price source alone must NOT auto-derive one.

    The old producer synthesized the identity from ``callable.__name__`` — two different lambdas both
    resolve to ``"<lambda>"``, so ``lambda:2.0`` (fires) and ``lambda:None`` (waits) would share a
    config_hash while sealing DIFFERENT actions: the exact M6 reproducibility gap, re-opened. The
    producer must instead fail CLOSED: run VvV only when a distinct explicit identity is supplied.
    """
    pack_dir = _real_pack(tmp_path)
    proto = _proto(fixture_ids=[5], strategy_configs=["value-vs-venue"], baselines=[])

    # Source present but NO explicit identity -> VvV is SKIPPED (fail closed), never run with "<lambda>".
    res = asyncio.run(
        produce_results_by_fixture(proto, packs={5: pack_dir}, venue_price_source=lambda fid, mk, side, ts: TimedVenueQuote(venue_decimal_price=2.0, staleness_s=0))
    )
    assert [row for row in res.get(5, []) if row["kind"] == "value-vs-venue"] == []

    # An empty identity is no identity — still skipped.
    res_empty = asyncio.run(
        produce_results_by_fixture(
            proto, packs={5: pack_dir}, venue_price_source=lambda fid, mk, side, ts: TimedVenueQuote(venue_decimal_price=2.0, staleness_s=0), venue_source_id=""
        )
    )
    assert [row for row in res_empty.get(5, []) if row["kind"] == "value-vs-venue"] == []

    # WITH a distinct explicit identity -> VvV runs and is bound to THAT identity.
    res2 = asyncio.run(
        produce_results_by_fixture(
            proto,
            packs={5: pack_dir},
            venue_price_source=lambda fid, mk, side, ts: TimedVenueQuote(venue_decimal_price=2.0, staleness_s=0),
            venue_source_id="quote-artifact-hash-abc",
        )
    )
    assert [row for row in res2.get(5, []) if row["kind"] == "value-vs-venue"]


# ==========================================================================================
# FU-1 — baselines must EMIT rows on FULL-MATCH packs (Pilot-0 zero-rows gap).
#
# The producer derives baseline inputs from the fixture's ticks. The latent bug: it keyed off
# ``marketstates[-1]`` — on a full-match pack that final tick is FULL-TIME (in-running), often with
# no usable market/prob map. ``_baseline_inputs`` then returned ``None`` and the producer's
# ``baseline_inputs is None`` guard SILENTLY skipped every baseline (Pilot-0: baseline_rows == 0).
# The fix derives inputs from the PRE-KICKOFF decision states (D2 windowing), the same universe drift
# decides over, so the four baselines emit their rows. FU-3 then made those acting baselines SCORED
# (run as Agents through run_backtest), so a fired pick now carries a real clv_bps and no_trade stays null.
# ==========================================================================================

_BASELINE_NAMES = ["no_trade", "favorite", "threshold_move", "seeded_random"]


def _patch_all_loaders(monkeypatch: pytest.MonkeyPatch, states: list[MarketState]) -> None:
    """Drive the whole path off synthetic ``states`` without a real on-disk pack.

    FU-3: the acting baselines are now scored via ``run_backtest``, which independently loads the pack
    (``runner.load_pack_marketstates``) and reads its content hash (``runner._pack_content_hash``) — so
    both, plus the producer's own ``evaluation.load_pack_marketstates``, are patched to the same tape.
    """
    monkeypatch.setattr(
        "veridex.backtest.evaluation.load_pack_marketstates", lambda pack_dir, fid, **kw: states
    )
    monkeypatch.setattr(
        "veridex.backtest.runner.load_pack_marketstates", lambda pack_dir, fid, **kw: states
    )
    monkeypatch.setattr("veridex.backtest.runner._pack_content_hash", lambda pack_dir: "deadbeefcafe0000")


def _1x2_tick(fixture_id: int, *, tick_seq: int, ts: int, phase: int, home_bps: int) -> MarketState:
    """A usable 1X2 pre-kickoff tick (Home prob in bps; Away is the complement)."""
    return MarketState(
        fixture_id=fixture_id,
        tick_seq=tick_seq,
        ts=ts,
        phase=phase,
        markets={
            "1X2||": {
                "stable_prob_bps": {"Home": home_bps, "Away": 10_000 - home_bps},
                "stable_price": {"Home": 2.0, "Away": 2.0},
                "suspended": False,
            }
        },
        scores={},
    )


def _full_match_states(fixture_id: int) -> list[MarketState]:
    """Pre-kickoff ticks with a usable 1X2 map, then a DEGENERATE full-time in-running final tick.

    The final (``phase == 1``) tick carries NO usable market map — exactly the Pilot-0 shape where
    ``_baseline_inputs``, keyed off ``marketstates[-1]``, returned ``None`` and skipped every baseline.
    """
    return [
        _1x2_tick(fixture_id, tick_seq=0, ts=100, phase=0, home_bps=5_000),
        _1x2_tick(fixture_id, tick_seq=1, ts=110, phase=0, home_bps=5_200),
        _1x2_tick(fixture_id, tick_seq=2, ts=120, phase=0, home_bps=6_000),
        # full-time in-running final tick: empty/degenerate market map (no usable prob map).
        MarketState(fixture_id=fixture_id, tick_seq=3, ts=200, phase=1, markets={}, scores={}),
    ]


def _pre_match_only_states(fixture_id: int) -> list[MarketState]:
    """A pre-match-only pack (never goes in-running) — the legacy path that already worked."""
    return [
        _1x2_tick(fixture_id, tick_seq=0, ts=100, phase=0, home_bps=5_000),
        _1x2_tick(fixture_id, tick_seq=1, ts=110, phase=0, home_bps=5_400),
        _1x2_tick(fixture_id, tick_seq=2, ts=120, phase=0, home_bps=6_000),
    ]


def test_baselines_emit_rows_on_full_match_pack(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """On a full-match pack whose FINAL tick is a degenerate full-time tick, all four baselines emit
    rows (with ``clv_bps is None``) and the calibration report's ``by_kind`` includes the baseline kinds.

    RED against the ``marketstates[-1]`` path: the empty final tick makes ``_baseline_inputs`` return
    ``None`` → the producer skips every baseline → ``baseline_rows == 0`` and ``by_kind`` has no baselines.
    """
    fixture_id = 42
    states = _full_match_states(fixture_id)
    _patch_all_loaders(monkeypatch, states)
    proto = _proto(fixture_ids=[fixture_id], strategy_configs=[], baselines=_BASELINE_NAMES)

    results = asyncio.run(produce_results_by_fixture(proto, packs={fixture_id: tmp_path}))

    baseline_rows = [row for row in results[fixture_id] if row["kind"] in _BASELINE_NAMES]
    assert baseline_rows, "the baselines must EMIT rows on a full-match pack (Pilot-0 gap: was 0)"
    kinds = {row["kind"] for row in baseline_rows}
    assert set(_BASELINE_NAMES) <= kinds, f"all four baselines must emit; got {kinds}"
    for row in baseline_rows:
        assert row["action"] in {"WAIT", "FOLLOW_MOMENTUM"}
        # FU-3: null IFF a WAIT abstention; a fired pick is SCORED vs the CON-040 close (never a fake CLV).
        assert (row["clv_bps"] is None) == (row["action"] == "WAIT")

    out = run_multi_fixture_evaluation(proto, results_by_fixture=results, cadence_ok=True)
    assert set(_BASELINE_NAMES) <= set(out["calibration"].by_kind), "by_kind must include baseline kinds"


def test_baselines_still_emit_on_pre_match_only_pack(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Regression: a pre-match-only pack (never in-running) STILL emits all four baseline rows."""
    fixture_id = 43
    states = _pre_match_only_states(fixture_id)
    _patch_all_loaders(monkeypatch, states)
    proto = _proto(fixture_ids=[fixture_id], strategy_configs=[], baselines=_BASELINE_NAMES)

    results = asyncio.run(produce_results_by_fixture(proto, packs={fixture_id: tmp_path}))

    kinds = {row["kind"] for row in results[fixture_id]}
    assert set(_BASELINE_NAMES) <= kinds, f"pre-match-only regression: all baselines must emit; got {kinds}"
    # FU-3: null IFF a WAIT abstention; a fired pick is scored vs the held-out per-market close (no lookahead).
    for row in results[fixture_id]:
        assert (row["clv_bps"] is None) == (row["action"] == "WAIT")


# ==========================================================================================
# C-6 — Run-002-VvV producer wiring: the venue leg never disturbs rung-1 drift, the estimated-edge
# attach never touches the sealed evidence hash, and the producer COLLECTS the VvV decision
# opportunities (decision_quote_coverage) with matched decisions carrying their staleness (6b).
# ==========================================================================================

#: A fixed 15m freshness bound is far wider than the synthetic packs' second-scale ts gaps, so a
#: constant-price source always matches — enough to exercise the collection paths deterministically.
def _fires_src(price: float = 5.0, staleness_s: int = 120):
    return lambda fid, mk, side, ts: TimedVenueQuote(venue_decimal_price=price, staleness_s=staleness_s)


def _never_src():
    return lambda fid, mk, side, ts: None


def test_rung1_drift_unchanged_by_venue_leg(tmp_path: Path) -> None:
    """GUD-003: adding the value-vs-venue leg leaves the drift rows BYTE-identical.

    The venue leg runs its OWN sealed ``run_backtest`` (via ``vvv_report_with_estimated_edge``), so it
    can legitimately add VvV rows but must NEVER perturb the drift run's rows/evidence.
    """
    pack_dir = _real_pack(tmp_path)
    base_proto = _proto(fixture_ids=[5], strategy_configs=["cumulative-drift"], baselines=[])
    venue_proto = _proto(
        fixture_ids=[5], strategy_configs=["cumulative-drift", "value-vs-venue"], baselines=[]
    )

    base = asyncio.run(produce_results_by_fixture(base_proto, packs={5: pack_dir}))
    withv = asyncio.run(
        produce_results_by_fixture(
            venue_proto, packs={5: pack_dir}, venue_price_source=_fires_src(), venue_source_id="src#1"
        )
    )

    drift_base = [row for row in base[5] if row["kind"] == "cumulative-drift"]
    drift_withv = [row for row in withv[5] if row["kind"] == "cumulative-drift"]
    assert drift_base, "the drift leg must produce rows (guard is meaningless on an empty set)"
    assert drift_base == drift_withv, "the venue leg must leave rung-1 drift rows byte-unchanged"


def test_post_build_estimated_edge_does_not_mutate_sealed_hash(tmp_path: Path) -> None:
    """CON-004 + evidence-precision note: the POST-build estimated-edge attach never touches the seal.

    A bare venue-free ``run_backtest`` of the SAME VvV agent and the ``vvv_report_with_estimated_edge``
    producer path must seal the IDENTICAL ``evidence_hash`` — the estimated edge is attached to the
    report via ``model_copy`` AFTER the venue-free build, so the sealed run never learns it exists.
    """
    pack_dir = _real_pack(tmp_path)
    window = RunWindow(
        window_id="w_vvv", fixture_id=5, market_allowlist=["1X2"], end_rule="pre_match", min_clv_horizon_s=0
    )
    src = _fires_src(staleness_s=0)

    agent = value_vs_venue_agent(venue_price_source=src, venue_source_id="src#1")
    result_plain, report_plain = asyncio.run(run_backtest(pack_dir, 5, [agent], window=window))
    result_vvv, report_vvv = asyncio.run(
        vvv_report_with_estimated_edge(
            pack_dir, 5, venue_price_source=src, venue_source_id="src#1", window=window,
            assumptions={"source": "c6-test"},
        )
    )

    # The venue-free seal is byte-identical across the plain build and the estimated-edge producer path.
    assert result_vvv.evidence_hash == result_plain.evidence_hash
    assert report_vvv.evidence_hash == report_plain.evidence_hash
    # ...yet the estimate IS attached, and the executable (live-fill) edge stays null (CON-003).
    assert report_vvv.estimated_executable_edge_bps is not None
    assert report_vvv.real_executable_edge_bps is None


def test_vvv_producer_collects_decision_quote_coverage_with_staleness(tmp_path: Path) -> None:
    """The producer records a VvV decision per tick; matched decisions carry staleness (6b) and flow
    into ``decision_quote_coverage`` computed over ALL decisions (CON-012), not just fired picks."""
    pack_dir = _real_pack(tmp_path)
    proto = _proto(fixture_ids=[5], strategy_configs=["value-vs-venue"], baselines=[])

    decision_sink: dict[int, list[VenueDecision]] = {}
    row_sink: dict[int, list] = {}
    asyncio.run(
        produce_results_by_fixture(
            proto,
            packs={5: pack_dir},
            venue_price_source=_fires_src(staleness_s=120),
            venue_source_id="src#1",
            venue_decision_sink=decision_sink,
            venue_behavior_row_sink=row_sink,
        )
    )

    decisions = decision_sink[5]
    assert decisions and all(isinstance(d, VenueDecision) for d in decisions)
    fired_matched = [d for d in decisions if d.fired and d.quote_matched]
    assert fired_matched, "venue 5.0 must fire+match at least one decision"
    # 6b: every matched decision carries a non-None staleness (else the C-5 model would have raised).
    assert all(d.staleness_s is not None for d in decisions if d.quote_matched)

    report = build_venue_behavior_report(
        row_sink[5],
        decisions,
        haircut_ladder_bps=[0, 100, 200, 300],
        prob_bands=[(0, 100)],
        ttc_buckets=["<1h", "1-6h", "6-24h", ">24h"],
        freshness_buckets=["<=2m", "<=5m", "<=15m"],
    )
    cov = report.decision_quote_coverage
    assert cov.decision_count == len(decisions)
    assert cov.quote_matched_count == sum(1 for d in decisions if d.quote_matched)
    # 6b flow: matched staleness (120s → <=2m) is bucketed, summing exactly to quote_matched_count.
    assert sum(cov.freshness_bucket_counts_for_used_quotes.values()) == cov.quote_matched_count


def test_vvv_producer_records_unmatched_decisions_when_source_is_none(tmp_path: Path) -> None:
    """A source that never has a quote yields all-WAIT, all-unmatched decisions — the honest
    "could not price under the bound" coverage signal, never a fabricated match (CON-012)."""
    pack_dir = _real_pack(tmp_path)
    proto = _proto(fixture_ids=[5], strategy_configs=["value-vs-venue"], baselines=[])

    decision_sink: dict[int, list[VenueDecision]] = {}
    asyncio.run(
        produce_results_by_fixture(
            proto,
            packs={5: pack_dir},
            venue_price_source=_never_src(),
            venue_source_id="src#1",
            venue_decision_sink=decision_sink,
        )
    )

    decisions = decision_sink[5]
    assert decisions, "decision opportunities are recorded even when nothing could be priced"
    assert all(not d.quote_matched for d in decisions)
    assert all(not d.fired for d in decisions), "no quote ⇒ no edge ⇒ every tick WAITs"
