"""B3 — Deterministic law (CLV recompute) tests (REQ-104 / AC-104, §4 contract).

TDD Iron Law: every test below was watched RED (ModuleNotFoundError: veridex.law.recompute
missing) before the production module was written.

§4 contract under test:
  recompute(entry_state, action, *, closing, source_mode="replay")
    -> {edge_bps, clv_bps|"pending", kelly_fraction, valid: bool, reason: str}
  - clv_bps = closing.stable_prob_bps[side] - entry.stable_prob_bps[side]  (de-vigged prob, bps)
  - edge_bps == clv_bps (recomputed; the LLM-claimed edge is NEVER used)
  - replay: closing absent / market absent -> invalid (with reason code)
  - live:   closing is None -> clv_bps == "pending" (valid=True)
  - invalid (valid=False, reason code) when at entry OR closing the market_key is:
      absent | suspended | missing the side | Pct="NA" (no stable_prob_bps[side])
  - missing market_key / side in params -> invalid
  - WAIT -> valid, unscored (clv_bps == "pending", reason "wait_unscored")
  - kelly_fraction in [0,1], advisory only — never flips valid
  - import-audit clean over veridex/law/ (CON-007)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from veridex.ingest.marketstate import MarketState
from veridex.law.recompute import PENDING, recompute
from veridex.runtime.schemas import AgentAction, SportsActionType

KEY = "OVERUNDER_PARTICIPANT_GOALS|half=1|line=1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _market(prob_bps: dict, *, price: dict | None = None, suspended: bool = False) -> dict:
    return {
        "stable_prob_bps": dict(prob_bps),
        "stable_price": dict(price) if price else {},
        "suspended": suspended,
    }


def _ms(markets: dict, *, tick_seq: int = 0) -> MarketState:
    return MarketState(
        fixture_id=1,
        tick_seq=tick_seq,
        ts=1000 + tick_seq,
        phase=0,
        markets=dict(markets),
        scores={},
    )


def _action(side="over", *, action_type=SportsActionType.FLAG_VALUE, market_key=KEY, **extra) -> AgentAction:
    params: dict = {}
    if market_key is not None:
        params["market_key"] = market_key
    if side is not None:
        params["side"] = side
    params.update(extra)
    return AgentAction(type=action_type, params=params)


# ---------------------------------------------------------------------------
# B3-1: positive CLV (closing prob > entry prob) -> positive clv, valid
# ---------------------------------------------------------------------------

def test_positive_clv_is_valid_and_positive():
    entry = _ms({KEY: _market({"over": 4684, "under": 5316})})
    closing = _ms({KEY: _market({"over": 5316, "under": 4684})}, tick_seq=9)
    res = recompute(entry, _action("over"), closing=closing, source_mode="replay")
    assert res["valid"] is True
    assert res["clv_bps"] == 5316 - 4684 == 632
    assert res["edge_bps"] == 632


# ---------------------------------------------------------------------------
# B3-2: negative CLV recomputed even when params claim a fat positive edge
# ---------------------------------------------------------------------------

def test_negative_clv_recomputed_claimed_edge_ignored():
    entry = _ms({KEY: _market({"over": 6000, "under": 4000})})
    closing = _ms({KEY: _market({"over": 5000, "under": 5000})}, tick_seq=9)
    # LLM claims a fat +9999 bps edge — must be ignored.
    action = _action("over", claimed_edge_bps=9999, edge_bps=9999)
    res = recompute(entry, action, closing=closing, source_mode="replay")
    assert res["clv_bps"] == 5000 - 6000 == -1000
    assert res["edge_bps"] == -1000
    assert res["clv_bps"] != 9999 and res["edge_bps"] != 9999
    assert res["valid"] is True  # legitimacy is independent of profitability
    assert "untrusted" in res["reason"]  # reuse of compute_clv_check reason


# ---------------------------------------------------------------------------
# B3-3: missing closing market — replay invalid; live pending
# ---------------------------------------------------------------------------

def test_replay_closing_none_is_invalid():
    entry = _ms({KEY: _market({"over": 4684, "under": 5316})})
    res = recompute(entry, _action("over"), closing=None, source_mode="replay")
    assert res["valid"] is False
    assert res["reason"] == "closing_missing"


def test_replay_closing_market_absent_is_invalid():
    entry = _ms({KEY: _market({"over": 4684, "under": 5316})})
    closing = _ms({}, tick_seq=9)  # closing MarketState lacks the market_key
    res = recompute(entry, _action("over"), closing=closing, source_mode="replay")
    assert res["valid"] is False
    assert res["reason"] == "closing_market_absent"


def test_live_no_closing_is_pending():
    entry = _ms({KEY: _market({"over": 4684, "under": 5316})})
    res = recompute(entry, _action("over"), closing=None, source_mode="live")
    assert res["valid"] is True
    assert res["clv_bps"] == PENDING
    assert res["reason"] == "pending_closing"


def test_live_with_closing_scores_like_replay():
    # live + a real later-horizon tick scores identically to replay (no longer pending).
    entry = _ms({KEY: _market({"over": 4684, "under": 5316})})
    closing = _ms({KEY: _market({"over": 5316, "under": 4684})}, tick_seq=9)
    live = recompute(entry, _action("over"), closing=closing, source_mode="live")
    replay = recompute(entry, _action("over"), closing=closing, source_mode="replay")
    assert live["valid"] is True
    assert live["clv_bps"] == 632  # numeric, not PENDING
    assert live["clv_bps"] != PENDING
    assert live == replay


def test_unknown_source_mode_raises():
    entry = _ms({KEY: _market({"over": 4684, "under": 5316})})
    closing = _ms({KEY: _market({"over": 5316, "under": 4684})}, tick_seq=9)
    with pytest.raises(ValueError, match="source_mode"):
        recompute(entry, _action("over"), closing=closing, source_mode="Live")


# ---------------------------------------------------------------------------
# B3-4: missing market_key / side in params -> invalid
# ---------------------------------------------------------------------------

def test_missing_side_in_params_is_invalid():
    entry = _ms({KEY: _market({"over": 4684, "under": 5316})})
    closing = _ms({KEY: _market({"over": 5316, "under": 4684})}, tick_seq=9)
    action = _action(side=None)  # market_key present, no side
    res = recompute(entry, action, closing=closing, source_mode="replay")
    assert res["valid"] is False
    assert res["reason"] == "side_missing"


def test_missing_market_key_in_params_is_invalid():
    entry = _ms({KEY: _market({"over": 4684, "under": 5316})})
    closing = _ms({KEY: _market({"over": 5316, "under": 4684})}, tick_seq=9)
    action = _action(side="over", market_key=None)
    res = recompute(entry, action, closing=closing, source_mode="replay")
    assert res["valid"] is False
    assert res["reason"] == "market_key_missing"


# ---------------------------------------------------------------------------
# B3-5: suspended OR Pct="NA" at entry or closing -> invalid/unscored
# ---------------------------------------------------------------------------

def test_entry_suspended_is_invalid():
    entry = _ms({KEY: _market({"over": 4684, "under": 5316}, suspended=True)})
    closing = _ms({KEY: _market({"over": 5316, "under": 4684})}, tick_seq=9)
    res = recompute(entry, _action("over"), closing=closing, source_mode="replay")
    assert res["valid"] is False
    assert res["reason"] == "entry_suspended"


def test_entry_pct_na_missing_side_is_invalid():
    # Pct="NA" => empty stable_prob_bps => side not present (B1 semantics).
    entry = _ms({KEY: _market({}, suspended=False)})
    closing = _ms({KEY: _market({"over": 5316, "under": 4684})}, tick_seq=9)
    res = recompute(entry, _action("over"), closing=closing, source_mode="replay")
    assert res["valid"] is False
    assert res["reason"] == "entry_side_missing"


def test_entry_market_absent_is_invalid():
    entry = _ms({})  # market_key absent at entry
    closing = _ms({KEY: _market({"over": 5316, "under": 4684})}, tick_seq=9)
    res = recompute(entry, _action("over"), closing=closing, source_mode="replay")
    assert res["valid"] is False
    assert res["reason"] == "entry_market_absent"


def test_closing_suspended_is_invalid():
    entry = _ms({KEY: _market({"over": 4684, "under": 5316})})
    closing = _ms({KEY: _market({"over": 5316, "under": 4684}, suspended=True)}, tick_seq=9)
    res = recompute(entry, _action("over"), closing=closing, source_mode="replay")
    assert res["valid"] is False
    assert res["reason"] == "closing_suspended"


def test_closing_side_missing_is_invalid():
    entry = _ms({KEY: _market({"over": 4684, "under": 5316})})
    closing = _ms({KEY: _market({"under": 4684})}, tick_seq=9)  # no 'over' at closing
    res = recompute(entry, _action("over"), closing=closing, source_mode="replay")
    assert res["valid"] is False
    assert res["reason"] == "closing_side_missing"


# ---------------------------------------------------------------------------
# B3-6: WAIT action -> valid, unscored
# ---------------------------------------------------------------------------

def test_wait_action_is_valid_and_unscored():
    entry = _ms({KEY: _market({"over": 4684, "under": 5316})})
    closing = _ms({KEY: _market({"over": 5316, "under": 4684})}, tick_seq=9)
    action = AgentAction(type=SportsActionType.WAIT, params={})
    res = recompute(entry, action, closing=closing, source_mode="replay")
    assert res["valid"] is True
    assert res["clv_bps"] == PENDING
    assert res["edge_bps"] == 0
    assert res["reason"] == "wait_unscored"


# ---------------------------------------------------------------------------
# B3-7: kelly_fraction in [0,1] and never flips valid
# ---------------------------------------------------------------------------

def test_kelly_fraction_in_unit_interval_for_scored_action():
    entry = _ms({KEY: _market({"over": 4684, "under": 5316},
                              price={"over": 2.135, "under": 1.881})})
    closing = _ms({KEY: _market({"over": 5316, "under": 4684})}, tick_seq=9)
    res = recompute(entry, _action("over"), closing=closing, source_mode="replay")
    # Pin the exact value: b=2.135-1=1.135, p=0.4684, q=0.5316.
    b, p, q = 2.135 - 1.0, 4684 / 10000.0, 1.0 - 4684 / 10000.0
    expected = (b * p - q) / b
    assert res["kelly_fraction"] == pytest.approx(expected)
    assert 0.0 <= res["kelly_fraction"] <= 1.0
    assert res["valid"] is True


def test_kelly_non_numeric_price_returns_zero_without_raising():
    # A non-numeric stable_price must never raise; kelly degrades to 0.0.
    entry = _ms({KEY: _market({"over": 4684, "under": 5316},
                              price={"over": "NA", "under": 1.881})})
    closing = _ms({KEY: _market({"over": 5316, "under": 4684})}, tick_seq=9)
    res = recompute(entry, _action("over"), closing=closing, source_mode="replay")
    assert res["kelly_fraction"] == 0.0
    assert res["valid"] is True


def test_kelly_clamps_negative_to_zero_without_flipping_valid():
    # p=0.30, decimal price 2.0 (b=1.0): (1.0*0.3 - 0.7)/1.0 = -0.4 -> clamp 0.0.
    entry = _ms({KEY: _market({"over": 3000, "under": 7000},
                              price={"over": 2.0, "under": 1.43})})
    closing = _ms({KEY: _market({"over": 3500, "under": 6500})}, tick_seq=9)
    res = recompute(entry, _action("over"), closing=closing, source_mode="replay")
    assert res["kelly_fraction"] == 0.0
    assert res["valid"] is True  # kelly never flips validity


def test_kelly_clamps_high_to_one():
    # Malformed p>1 forces raw kelly > 1 -> clamp to 1.0 (defensive upper bound).
    entry = _ms({KEY: _market({"over": 15000, "under": 0},
                              price={"over": 2.0})})
    closing = _ms({KEY: _market({"over": 15000, "under": 0})}, tick_seq=9)
    res = recompute(entry, _action("over"), closing=closing, source_mode="replay")
    assert res["kelly_fraction"] == 1.0


def test_kelly_present_and_advisory_on_invalid_action():
    # Even an invalid action returns a kelly_fraction (advisory) without raising.
    entry = _ms({KEY: _market({"over": 4684, "under": 5316}, suspended=True)})
    closing = _ms({KEY: _market({"over": 5316, "under": 4684})}, tick_seq=9)
    res = recompute(entry, _action("over"), closing=closing, source_mode="replay")
    assert res["valid"] is False
    assert 0.0 <= res["kelly_fraction"] <= 1.0


# ---------------------------------------------------------------------------
# B3-8: import-audit clean over veridex/law/ (CON-007)
# ---------------------------------------------------------------------------

def test_law_import_audit_clean():
    import veridex.law as law_pkg
    from veridex.verifier.import_audit import assert_no_llm_imports

    assert_no_llm_imports(Path(law_pkg.__file__).parent)  # raises AssertionError if dirty
