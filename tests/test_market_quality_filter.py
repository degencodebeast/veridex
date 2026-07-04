"""FU-2 — the market-quality filter wired into the S6 producer + the eligible-market manifest.

Codex requirement: ``produce_results_by_fixture`` scores drift + baselines over ALL markets, so
degenerate near-certain lines (e.g. O/U 0.5 at ~0.97) enter the official evaluation and can distort
CLV. FU-2 makes the M1 filter (:func:`veridex.strategies.market_quality.evaluate_market_quality`) an
OPT-IN eligibility gate: a pinned ``MarketQualityConfig`` builds the ELIGIBLE market allowlist BEFORE
scoring, that allowlist is applied CONSISTENTLY to drift AND baselines (identical eligible universe),
and an ELIGIBLE-MARKET MANIFEST is emitted (``filter_config_hash`` + eligible/excluded + reasons +
counts). Codex's hard rule: no filter claim without that manifest.

Trust invariants under test:
  * the filter is an ELIGIBILITY gate, NEVER a CLV-computation change (it only changes WHICH markets
    are scored, never HOW CLV is computed);
  * default / ``None`` config ⇒ current UNFILTERED behavior, byte-identical (regression);
  * the eligible set is IDENTICAL for drift and baselines (apples-to-apples preserved);
  * a fixture left with ZERO eligible markets is a NAMED skip in the manifest, never a silent empty.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.test_multi_fixture_eval import _patch_all_loaders, _proto
from veridex.backtest.evaluation import produce_results_by_fixture
from veridex.backtest.market_filter import (
    build_eligible_market_manifest,
    filter_marketstates_to_allowlist,
)
from veridex.ingest.marketstate import MarketState
from veridex.strategies.market_quality import DEFAULT_MARKET_QUALITY_CONFIG

# Two OVERUNDER lines that share a prefix (so a prefix-level allowlist could NOT separate them — the
# filter MUST work at full-key granularity). The degenerate 0.5 line sorts BEFORE the healthy 2.5 line,
# so the baseline agent (which latches the first sorted usable market) would decide over the degenerate
# line UNLESS it is filtered out of the feed — making the filter load-bearing for baselines too.
_DEGEN_KEY = "OVERUNDER_PARTICIPANT_GOALS||line=0.5"
_HEALTHY_KEY = "OVERUNDER_PARTICIPANT_GOALS||line=2.5"

_BASELINES = ["no_trade", "favorite"]


def _ou_market(over_bps: int, *, suspended: bool = False) -> dict:
    return {
        "stable_prob_bps": {"over": over_bps, "under": 10_000 - over_bps},
        "stable_price": {"over": 10_000 / over_bps, "under": 10_000 / (10_000 - over_bps)},
        "suspended": suspended,
    }


def _tick(fixture_id: int, *, tick_seq: int, ts: int, phase: int, markets: dict[str, dict]) -> MarketState:
    return MarketState(fixture_id=fixture_id, tick_seq=tick_seq, ts=ts, phase=phase, markets=markets, scores={})


def _states(fixture_id: int, *, degen_over_bps: int = 9_700, healthy_over_bps: int = 5_500) -> list[MarketState]:
    """32 pre-kickoff ticks (>= min_tick_count, >= min_horizon_s) carrying a degenerate + a healthy line,
    then a degenerate full-time in-running tick (the D2 kickoff cutoff)."""
    states: list[MarketState] = []
    for i in range(32):
        states.append(
            _tick(
                fixture_id,
                tick_seq=i,
                ts=i * 25,  # horizon spans 31*25 = 775s >= 600
                phase=0,
                markets={
                    _DEGEN_KEY: _ou_market(degen_over_bps),
                    _HEALTHY_KEY: _ou_market(healthy_over_bps + (i % 5) * 20),  # gentle drift, stays < 0.95
                },
            )
        )
    states.append(_tick(fixture_id, tick_seq=32, ts=1_000, phase=1, markets={}))  # full-time cutoff
    return states


def _markets_referenced(rows: list[dict]) -> set[str]:
    """Full market_keys a fired row references (WAIT/no-position rows carry ``"n/a"`` — excluded)."""
    return {row["market"] for row in rows if row["market"] != "n/a"}


# ------------------------------------------------------------------------------------------
# RED: a degenerate near-certain line is EXCLUDED from scoring and the manifest is emitted.
# ------------------------------------------------------------------------------------------


def test_market_quality_filter_excludes_degenerate_and_emits_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fixture_id = 71
    _patch_all_loaders(monkeypatch, _states(fixture_id))
    proto = _proto(fixture_ids=[fixture_id], strategy_configs=["cumulative-drift"], baselines=_BASELINES)
    manifest_sink: dict = {}

    results = asyncio.run(
        produce_results_by_fixture(
            proto,
            packs={fixture_id: tmp_path},
            market_quality_config=DEFAULT_MARKET_QUALITY_CONFIG,
            manifest_sink=manifest_sink,
        )
    )

    # The manifest is the required artifact — no filter claim without it.
    assert fixture_id in manifest_sink
    manifest = manifest_sink[fixture_id]
    assert manifest.filter_config_hash == DEFAULT_MARKET_QUALITY_CONFIG.filter_config_hash()
    assert manifest.eligible == [_HEALTHY_KEY]
    assert manifest.eligible_count == 1 and manifest.excluded_count == 1
    excluded = {ex.market_key: ex.reasons for ex in manifest.excluded}
    assert _DEGEN_KEY in excluded and "near_certain" in excluded[_DEGEN_KEY]

    # The degenerate line is NEVER scored (filtered from the feed); real rows still exist for the healthy line.
    assert results[fixture_id], "the healthy line must still be scored"
    assert _DEGEN_KEY not in _markets_referenced(results[fixture_id])


def test_filter_applies_to_drift_and_baselines_identically(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The SAME eligible universe governs drift AND baseline rows — no market scored for one, filtered for
    the other (apples-to-apples preserved)."""
    fixture_id = 72
    _patch_all_loaders(monkeypatch, _states(fixture_id))
    proto = _proto(fixture_ids=[fixture_id], strategy_configs=["cumulative-drift"], baselines=_BASELINES)
    manifest_sink: dict = {}

    results = asyncio.run(
        produce_results_by_fixture(
            proto,
            packs={fixture_id: tmp_path},
            market_quality_config=DEFAULT_MARKET_QUALITY_CONFIG,
            manifest_sink=manifest_sink,
        )
    )
    eligible = set(manifest_sink[fixture_id].eligible)

    drift_rows = [r for r in results[fixture_id] if r["kind"] == "cumulative-drift"]
    baseline_rows = [r for r in results[fixture_id] if r["kind"] in _BASELINES]
    # Every fired market — in drift rows AND in baseline rows — is inside the eligible set; the degenerate
    # line appears in NEITHER (it was filtered from the feed both strategies decide over).
    assert _markets_referenced(drift_rows) <= eligible
    assert _markets_referenced(baseline_rows) <= eligible
    assert _DEGEN_KEY not in _markets_referenced(results[fixture_id])


def test_unfiltered_default_unchanged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``market_quality_config=None`` (and omitting it entirely) ⇒ existing behavior, byte-identical."""
    fixture_id = 73
    _patch_all_loaders(monkeypatch, _states(fixture_id))
    proto = _proto(fixture_ids=[fixture_id], strategy_configs=["cumulative-drift"], baselines=_BASELINES)

    baseline_no_param = asyncio.run(produce_results_by_fixture(proto, packs={fixture_id: tmp_path}))

    manifest_sink: dict = {}
    explicit_none = asyncio.run(
        produce_results_by_fixture(
            proto, packs={fixture_id: tmp_path}, market_quality_config=None, manifest_sink=manifest_sink
        )
    )

    # None path is byte-identical to the pre-FU-2 call, and emits NO manifest (unfiltered ⇒ no filter claim).
    assert explicit_none == baseline_no_param
    assert manifest_sink == {}
    # And unfiltered, the degenerate line IS in the scored universe (proving the filter is what removes it).
    assert _DEGEN_KEY in _markets_referenced(baseline_no_param[fixture_id])


def test_zero_eligible_is_named_skip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A fixture whose every market is degenerate ⇒ a NAMED skip in the manifest, never a silent empty."""
    fixture_id = 74
    # Both lines near-certain (over 9700) → every market excluded.
    _patch_all_loaders(monkeypatch, _states(fixture_id, degen_over_bps=9_700, healthy_over_bps=9_700))
    proto = _proto(fixture_ids=[fixture_id], strategy_configs=["cumulative-drift"], baselines=_BASELINES)
    manifest_sink: dict = {}

    results = asyncio.run(
        produce_results_by_fixture(
            proto,
            packs={fixture_id: tmp_path},
            market_quality_config=DEFAULT_MARKET_QUALITY_CONFIG,
            manifest_sink=manifest_sink,
        )
    )

    assert results[fixture_id] == []  # empty, but NOT silent...
    manifest = manifest_sink[fixture_id]
    assert manifest.zero_eligible is True  # ...it is a named skip.
    assert manifest.eligible == [] and manifest.eligible_count == 0
    assert manifest.excluded_count == 2
    for ex in manifest.excluded:
        assert "near_certain" in ex.reasons


def test_filter_config_hash_is_present_and_stable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The manifest pins a stable, config-only ``filter_config_hash`` (config identity fixed before results)."""
    fixture_id = 75
    _patch_all_loaders(monkeypatch, _states(fixture_id))
    proto = _proto(fixture_ids=[fixture_id], strategy_configs=[], baselines=_BASELINES)

    sinks = []
    for _ in range(2):
        sink: dict = {}
        asyncio.run(
            produce_results_by_fixture(
                proto,
                packs={fixture_id: tmp_path},
                market_quality_config=DEFAULT_MARKET_QUALITY_CONFIG,
                manifest_sink=sink,
            )
        )
        sinks.append(sink[fixture_id].filter_config_hash)

    assert sinks[0] == sinks[1] == DEFAULT_MARKET_QUALITY_CONFIG.filter_config_hash()
    assert len(sinks[0]) == 64


# ==========================================================================================
# Review Finding B (SHIP-BLOCKER) — ``phase`` is NOT monotonic: a halftime/goal/VAR suspension
# re-reports InRunning=false → phase==0 AGAIN, deep IN-PLAY. So the LAST phase==0 tick is often a
# post-kickoff in-play re-quote. Eligibility MUST be judged off the PRE-KICKOFF decision slice (the
# same slice D2 scores), never the trailing in-play line — else a market HEALTHY at kickoff is judged
# degenerate off an in-play re-quote and wrongly excluded (can zero out the whole fixture).
# ==========================================================================================


def _healthy_pre_then_trailing_inplay(fixture_id: int) -> list[MarketState]:
    """31 healthy pre-kickoff ticks @0.55, the kickoff (``phase==1``) tick, then a TRAILING degenerate
    ``phase==0`` in-play re-quote @0.98 (a halftime/VAR suspension re-report — phase back to 0 mid-match)."""
    states: list[MarketState] = []
    for i in range(31):
        states.append(_tick(fixture_id, tick_seq=i, ts=i * 25, phase=0, markets={_HEALTHY_KEY: _ou_market(5_500)}))
    states.append(_tick(fixture_id, tick_seq=31, ts=800, phase=1, markets={_HEALTHY_KEY: _ou_market(5_500)}))
    states.append(_tick(fixture_id, tick_seq=32, ts=850, phase=0, markets={_HEALTHY_KEY: _ou_market(9_800)}))
    return states


def test_manifest_judges_eligibility_off_pre_kickoff_slice_not_trailing_in_play() -> None:
    """RED on the all-phase==0 bug: the trailing @0.98 in-play re-quote is read as the close → the
    healthy market is wrongly excluded (near_certain) → ``eligible == []``. GREEN after the fix: the
    market is judged off its LAST PRE-KICKOFF line (@0.55) and stays ELIGIBLE."""
    manifest = build_eligible_market_manifest(60, _healthy_pre_then_trailing_inplay(60), DEFAULT_MARKET_QUALITY_CONFIG)
    assert manifest.eligible == [_HEALTHY_KEY], "healthy-at-kickoff market must not be excluded off an in-play re-quote"
    assert manifest.excluded == []
    assert manifest.zero_eligible is False


def test_market_seen_only_after_kickoff_is_not_counted() -> None:
    """A market that first appears in a TRAILING in-play ``phase==0`` re-quote (never pre-kickoff) is
    outside the decision universe — it must be neither eligible nor excluded (not counted at all)."""
    fixture_id = 61
    states = [
        _tick(fixture_id, tick_seq=0, ts=0, phase=0, markets={_HEALTHY_KEY: _ou_market(5_500)}),
        _tick(fixture_id, tick_seq=1, ts=700, phase=0, markets={_HEALTHY_KEY: _ou_market(5_500)}),
        _tick(fixture_id, tick_seq=2, ts=800, phase=1, markets={_HEALTHY_KEY: _ou_market(5_500)}),  # kickoff
        # a NEW market appears only in a post-kickoff in-play re-quote:
        _tick(fixture_id, tick_seq=3, ts=850, phase=0, markets={_DEGEN_KEY: _ou_market(9_800)}),
    ]
    manifest = build_eligible_market_manifest(fixture_id, states, DEFAULT_MARKET_QUALITY_CONFIG)
    all_keys = set(manifest.eligible) | {ex.market_key for ex in manifest.excluded}
    assert _DEGEN_KEY not in all_keys, "a market seen only after kickoff must not be counted"


# ------------------------------------------------------------------------------------------
# Review Finding 3 — excluded markets carry the NUMERIC evidence (implied_prob/tick_count/horizon_s),
# so a Run-001 skeptic sees "near_certain at 0.97", not just the label.
# ------------------------------------------------------------------------------------------


def test_excluded_market_carries_numeric_evidence() -> None:
    fixture_id = 62
    states = _states(fixture_id)  # _DEGEN_KEY @0.97 near-certain, _HEALTHY_KEY @0.55
    manifest = build_eligible_market_manifest(fixture_id, states, DEFAULT_MARKET_QUALITY_CONFIG)
    degen = next(ex for ex in manifest.excluded if ex.market_key == _DEGEN_KEY)
    assert "near_certain" in degen.reasons
    assert degen.implied_prob == pytest.approx(0.97, abs=1e-6)  # the MAX side prob at the pre-kickoff close
    assert degen.tick_count == 32
    assert degen.horizon_s == 31 * 25


# ------------------------------------------------------------------------------------------
# Review Finding 4 — direct unit tests: the 0.5-neutral-unmapped branch, a mapped-but-suspended close,
# a >1-eligible multi-market pack, and filter_marketstates_to_allowlist key-stripping.
# ------------------------------------------------------------------------------------------


def test_unmapped_suspended_close_is_excluded_honestly_not_near_certain() -> None:
    """An unmapped (empty prob map) + suspended close is excluded by ``unmapped``/``close_suspended``,
    NOT mislabeled ``near_certain`` (implied_prob is a mid-band neutral 0.5 when there is no prob)."""
    fixture_id = 63
    unmapped = {"stable_prob_bps": {}, "stable_price": {}, "suspended": True}
    states = [
        _tick(fixture_id, tick_seq=0, ts=0, phase=0, markets={_HEALTHY_KEY: unmapped}),
        _tick(fixture_id, tick_seq=1, ts=700, phase=0, markets={_HEALTHY_KEY: unmapped}),
        _tick(fixture_id, tick_seq=2, ts=800, phase=1, markets={_HEALTHY_KEY: unmapped}),
    ]
    manifest = build_eligible_market_manifest(fixture_id, states, DEFAULT_MARKET_QUALITY_CONFIG)
    ex = next(e for e in manifest.excluded if e.market_key == _HEALTHY_KEY)
    assert "near_certain" not in ex.reasons
    assert "unmapped" in ex.reasons and "close_suspended" in ex.reasons
    assert ex.implied_prob == pytest.approx(0.5)


def test_multi_market_pack_yields_more_than_one_eligible() -> None:
    """A 3-market pre-kickoff pack: two healthy lines eligible, one degenerate excluded (sorted, >1 eligible)."""
    fixture_id = 64
    k1, k2, k3 = "1X2_PARTICIPANT_RESULT||", _HEALTHY_KEY, _DEGEN_KEY
    states: list[MarketState] = []
    for i in range(31):
        states.append(
            _tick(fixture_id, tick_seq=i, ts=i * 25, phase=0, markets={
                k1: {"stable_prob_bps": {"home": 4_800, "draw": 2_600, "away": 2_600},
                     "stable_price": {"home": 2.08, "draw": 3.85, "away": 3.85}, "suspended": False},
                k2: _ou_market(5_500),
                k3: _ou_market(9_700),
            })
        )
    states.append(_tick(fixture_id, tick_seq=31, ts=800, phase=1, markets={}))
    manifest = build_eligible_market_manifest(fixture_id, states, DEFAULT_MARKET_QUALITY_CONFIG)
    assert manifest.eligible == sorted([k1, k2])  # deterministic ordering, >1 eligible
    assert manifest.eligible_count == 2 and manifest.excluded_count == 1
    assert [ex.market_key for ex in manifest.excluded] == [k3]


def test_filter_marketstates_to_allowlist_preserves_ticks_strips_keys() -> None:
    fixture_id = 65
    states = [
        _tick(fixture_id, tick_seq=0, ts=0, phase=0, markets={_HEALTHY_KEY: _ou_market(5_500), _DEGEN_KEY: _ou_market(9_700)}),
        _tick(fixture_id, tick_seq=1, ts=800, phase=1, markets={_HEALTHY_KEY: _ou_market(5_500)}),
    ]
    out = filter_marketstates_to_allowlist(states, {_HEALTHY_KEY})
    assert len(out) == len(states), "every tick is preserved (phase structure intact)"
    assert all(set(s.markets) <= {_HEALTHY_KEY} for s in out), "non-allowlisted keys stripped from every tick"
    assert _HEALTHY_KEY in out[0].markets and _DEGEN_KEY not in out[0].markets


# ------------------------------------------------------------------------------------------
# Review Finding A — filtering with nowhere to retain the named-skip manifest is a footgun: a config
# WITHOUT a sink must FAIL LOUD (the manifest is the "no filter claim without it" artifact).
# ------------------------------------------------------------------------------------------


def test_filtering_without_a_manifest_sink_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fixture_id = 66
    _patch_all_loaders(monkeypatch, _states(fixture_id))
    proto = _proto(fixture_ids=[fixture_id], strategy_configs=[], baselines=_BASELINES)
    with pytest.raises(ValueError, match="manifest_sink"):
        asyncio.run(
            produce_results_by_fixture(
                proto, packs={fixture_id: tmp_path}, market_quality_config=DEFAULT_MARKET_QUALITY_CONFIG
            )
        )
