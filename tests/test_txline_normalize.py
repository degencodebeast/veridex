"""B1 — Production TxLINE odds normalizer tests (REQ-101 / AC-101).

TDD Iron Law: every test was watched RED (ImportError/AttributeError — feature missing)
before the production module was written.

Behaviors under test:
- market_key composition (normal case; null period/params → empty segments).
- group_by_fixture splits two fixtures with correct counts.
- marketstate_from_txline_odds: correct fixture_id, ts (ms→s), de-vig bps, price scaling.
- Pct="NA" market → empty stable_prob_bps + suspended=True.
- InRunning=true → phase==1; all False → phase==0.
- Mixed-fixture input → ValueError mentioning "single fixture".
- Import-audit clean over veridex/ingest/ (CON-007).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from veridex.ingest.txline_normalize import (
    group_by_fixture,
    market_key,
    marketstate_from_txline_odds,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "txline_native_messages.json"


def _load_fixture() -> list[dict]:
    return json.loads(FIXTURE_PATH.read_text())


def _msgs_for_fixture(fid: int) -> list[dict]:
    return [m for m in _load_fixture() if m["FixtureId"] == fid]


# ---------------------------------------------------------------------------
# B1-1: market_key composition — normal case
# ---------------------------------------------------------------------------

def test_market_key_normal_case():
    """market_key joins SuperOddsType|MarketPeriod|MarketParameters."""
    msg = {
        "SuperOddsType": "OVERUNDER_PARTICIPANT_GOALS",
        "MarketPeriod": "half=1",
        "MarketParameters": "line=1",
    }
    assert market_key(msg) == "OVERUNDER_PARTICIPANT_GOALS|half=1|line=1"


def test_market_key_null_period_and_params():
    """Null period and params collapse to empty strings, producing key with trailing pipes."""
    msg = {
        "SuperOddsType": "1X2_PARTICIPANT_RESULT",
        "MarketPeriod": None,
        "MarketParameters": None,
    }
    assert market_key(msg) == "1X2_PARTICIPANT_RESULT||"


def test_market_key_missing_period_acts_like_null():
    """Absent keys behave the same as None (empty segment)."""
    msg = {"SuperOddsType": "SOME_TYPE"}
    assert market_key(msg) == "SOME_TYPE||"


# ---------------------------------------------------------------------------
# B1-2: group_by_fixture — two fixtures with correct counts
# ---------------------------------------------------------------------------

def test_group_by_fixture_produces_correct_keys_and_counts():
    """group_by_fixture must bucket by FixtureId and preserve all messages."""
    messages = _load_fixture()
    grouped = group_by_fixture(messages)
    # The fixture has 3 msgs for 17588404 and 1 msg for 17588405
    assert set(grouped.keys()) == {17588404, 17588405}
    assert len(grouped[17588404]) == 3
    assert len(grouped[17588405]) == 1


def test_group_by_fixture_preserves_order():
    """Messages within each bucket must appear in original (stable) order."""
    messages = _msgs_for_fixture(17588404)
    grouped = group_by_fixture(messages)
    result = grouped[17588404]
    for orig, bucketed in zip(messages, result):
        assert orig["SuperOddsType"] == bucketed["SuperOddsType"]


# ---------------------------------------------------------------------------
# B1-3: fold → MarketState: correct fixture_id and ts (ms → s)
# ---------------------------------------------------------------------------

def test_fold_correct_fixture_id():
    """marketstate_from_txline_odds must use the FixtureId from the messages."""
    msgs = _msgs_for_fixture(17588404)
    ms = marketstate_from_txline_odds(msgs)
    assert ms.fixture_id == 17588404


def test_fold_ts_ms_to_s():
    """ts must be max(Ts)//1000 (milliseconds → seconds)."""
    msgs = _msgs_for_fixture(17588404)
    max_ts_ms = max(m["Ts"] for m in msgs)
    expected_ts = max_ts_ms // 1000
    ms = marketstate_from_txline_odds(msgs)
    assert ms.ts == expected_ts


def test_fold_tick_seq_passed_through():
    """tick_seq kwarg must be propagated to the MarketState."""
    msgs = _msgs_for_fixture(17588404)
    ms = marketstate_from_txline_odds(msgs, tick_seq=7)
    assert ms.tick_seq == 7


def test_fold_scores_empty():
    """scores must be empty (scores stream out of scope for B1)."""
    msgs = _msgs_for_fixture(17588404)
    ms = marketstate_from_txline_odds(msgs)
    assert ms.scores == {}


# ---------------------------------------------------------------------------
# B1-4: de-vig — OVER/UNDER bps sum to 10000
# ---------------------------------------------------------------------------

def test_devig_over_under_bps_sum_to_10000():
    """OVER/UNDER stable_prob_bps must sum to exactly 10000 for the fixture data.

    Pct: ["46.838","53.163"]
    round(46.838*100)=4684, round(53.163*100)=5316 → 4684+5316=10000.
    """
    msgs = _msgs_for_fixture(17588404)
    ms = marketstate_from_txline_odds(msgs)
    key = "OVERUNDER_PARTICIPANT_GOALS|half=1|line=1"
    assert key in ms.markets
    probs = ms.markets[key]["stable_prob_bps"]
    assert set(probs.keys()) == {"over", "under"}
    assert sum(probs.values()) == 10000


def test_devig_1x2_bps_sum_to_10000():
    """1X2 three-outcome stable_prob_bps must sum to 10000 for the fixture data.

    Pct: ["45.000","25.000","30.000"] → 4500+2500+3000=10000.
    """
    msgs = _msgs_for_fixture(17588404)
    ms = marketstate_from_txline_odds(msgs)
    key = "1X2_PARTICIPANT_RESULT||"
    assert key in ms.markets
    probs = ms.markets[key]["stable_prob_bps"]
    assert set(probs.keys()) == {"home", "draw", "away"}
    assert sum(probs.values()) == 10000


# ---------------------------------------------------------------------------
# B1-5: price scaling — Prices×1000 → decimal (2135 → 2.135)
# ---------------------------------------------------------------------------

def test_price_scaling_over_under():
    """Prices must be divided by 1000: 2135 → 2.135, 1881 → 1.881."""
    msgs = _msgs_for_fixture(17588404)
    ms = marketstate_from_txline_odds(msgs)
    key = "OVERUNDER_PARTICIPANT_GOALS|half=1|line=1"
    prices = ms.markets[key]["stable_price"]
    assert prices["over"] == pytest.approx(2.135)
    assert prices["under"] == pytest.approx(1.881)


# ---------------------------------------------------------------------------
# B1-6: Pct="NA" → empty stable_prob_bps + suspended=True
# ---------------------------------------------------------------------------

def test_na_pct_market_has_empty_stable_prob_bps():
    """A market where all Pct entries are 'NA' must have an empty stable_prob_bps dict."""
    msgs = _msgs_for_fixture(17588404)
    ms = marketstate_from_txline_odds(msgs)
    key = "ASIAN_HANDICAP|half=full|line=0"
    assert key in ms.markets
    assert ms.markets[key]["stable_prob_bps"] == {}


def test_na_pct_market_is_suspended():
    """A market where all Pct entries are 'NA' must be marked suspended=True."""
    msgs = _msgs_for_fixture(17588404)
    ms = marketstate_from_txline_odds(msgs)
    key = "ASIAN_HANDICAP|half=full|line=0"
    assert ms.markets[key]["suspended"] is True


def test_suspended_market_retains_last_known_prices():
    """A suspended (all-NA Pct) market keeps numeric stable_price as last-known.

    Locks the intended retention semantics: even when stable_prob_bps is empty
    and suspended is True, the numeric Prices remain available as last-known odds.
    """
    msgs = _msgs_for_fixture(17588404)
    ms = marketstate_from_txline_odds(msgs)
    key = "ASIAN_HANDICAP|half=full|line=0"
    market = ms.markets[key]
    assert market["stable_prob_bps"] == {}
    assert market["suspended"] is True
    assert market["stable_price"]["home"] == pytest.approx(1.9)
    assert market["stable_price"]["away"] == pytest.approx(1.9)


def test_priced_market_is_not_suspended():
    """A market with valid Pct values must have suspended=False."""
    msgs = _msgs_for_fixture(17588404)
    ms = marketstate_from_txline_odds(msgs)
    key = "OVERUNDER_PARTICIPANT_GOALS|half=1|line=1"
    assert ms.markets[key]["suspended"] is False


# ---------------------------------------------------------------------------
# B1-7: InRunning=true → phase==1; all False → phase==0
# ---------------------------------------------------------------------------

def test_in_running_true_sets_phase_1():
    """A batch where any message has InRunning=true must produce phase=1."""
    msgs = _msgs_for_fixture(17588405)  # the InRunning=true message
    ms = marketstate_from_txline_odds(msgs)
    assert ms.phase == 1


def test_all_in_running_false_sets_phase_0():
    """A batch where all messages have InRunning=false must produce phase=0."""
    msgs = _msgs_for_fixture(17588404)  # all InRunning=false
    ms = marketstate_from_txline_odds(msgs)
    assert ms.phase == 0


# ---------------------------------------------------------------------------
# B1-8: Mixed-fixture input → ValueError mentioning "single fixture"
# ---------------------------------------------------------------------------

def test_mixed_fixture_raises_value_error():
    """marketstate_from_txline_odds must raise ValueError for multi-fixture input."""
    all_msgs = _load_fixture()  # spans fixture 17588404 and 17588405
    with pytest.raises(ValueError, match="single fixture"):
        marketstate_from_txline_odds(all_msgs)


def test_empty_messages_raises_value_error():
    """An empty message list must raise a clear ValueError (not a bare StopIteration)."""
    with pytest.raises(ValueError, match="at least one message"):
        marketstate_from_txline_odds([])


# ---------------------------------------------------------------------------
# B1-9: Import-audit clean over veridex/ingest/ (CON-007)
# ---------------------------------------------------------------------------

def test_ingest_import_audit_clean():
    """No LLM SDK imports (agno, anthropic, openai, etc.) in the ingest package."""
    from pathlib import Path

    import veridex.ingest as ingest_pkg
    from veridex.verifier.import_audit import assert_no_llm_imports

    ingest_dir = Path(ingest_pkg.__file__).parent
    assert_no_llm_imports(ingest_dir)  # raises AssertionError if dirty
