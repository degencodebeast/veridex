"""B4b — deterministic baseline §4-scorable tests (TDD, strict RED→GREEN).

These tests were written RED before the production fix was applied.
They prove that ``deterministic_baseline_action`` emits actions that are
actually scorable by ``veridex.law.recompute`` (§4 law contract, REQ-104).

All tests use dict-form ``stable_prob_bps`` (the B1 normalized contract) and
verify the full recompute round-trip so the scorability claim is evidence-based.
"""

from __future__ import annotations

from veridex.ingest.marketstate import MarketState
from veridex.law.recompute import recompute
from veridex.runtime.baseline import deterministic_baseline_action
from veridex.runtime.schemas import AgentAction, SportsActionType

KEY = "OVERUNDER_PARTICIPANT_GOALS|half=1|line=2"


def _ms(markets: dict, *, tick_seq: int = 0) -> MarketState:
    """Build a minimal ``MarketState`` for testing."""
    return MarketState(
        fixture_id=1,
        tick_seq=tick_seq,
        ts=1000 + tick_seq,
        phase=0,
        markets=dict(markets),
        scores={},
    )


def _market(
    prob_bps: dict[str, int],
    *,
    price: dict[str, float] | None = None,
    suspended: bool = False,
) -> dict:
    """Build a normalized market dict (B1 dict-form stable_prob_bps)."""
    return {
        "stable_prob_bps": dict(prob_bps),
        "stable_price": dict(price) if price is not None else {},
        "suspended": suspended,
    }


# ---------------------------------------------------------------------------
# B4b-1: baseline emits FLAG_VALUE carrying market_key + side
# ---------------------------------------------------------------------------


def test_baseline_emits_flag_value_with_market_key_and_side() -> None:
    """High-conviction market → FLAG_VALUE action carries market_key and side."""
    entry = _ms({KEY: _market({"over": 6000, "under": 4000})})

    action = deterministic_baseline_action(entry)

    assert isinstance(action, AgentAction)
    assert action.type == SportsActionType.FLAG_VALUE
    assert action.params.get("market_key") == KEY
    assert action.params.get("side") == "over"
    assert action.params.get("stable_prob_bps") == 6000


# ---------------------------------------------------------------------------
# B4b-2: the action is scorable via recompute (the whole point of the fix)
# ---------------------------------------------------------------------------


def test_baseline_action_is_scorable_via_recompute() -> None:
    """Baseline FLAG_VALUE action survives recompute round-trip: valid=True, clv_bps int."""
    entry = _ms({KEY: _market({"over": 6000, "under": 4000})})
    closing = _ms({KEY: _market({"over": 6200, "under": 3800})}, tick_seq=9)

    action = deterministic_baseline_action(entry)
    result = recompute(entry, action, closing=closing, source_mode="replay")

    assert result["valid"] is True
    assert isinstance(result["clv_bps"], int)
    assert result["clv_bps"] == 6200 - 6000  # 200 bps positive CLV


# ---------------------------------------------------------------------------
# B4b-3: reproducibility — same input always yields the same action
# ---------------------------------------------------------------------------


def test_baseline_reproducible_same_input_same_action() -> None:
    """Identical MarketState always yields an identical AgentAction."""
    entry = _ms({KEY: _market({"over": 6000, "under": 4000})})

    a1 = deterministic_baseline_action(entry)
    a2 = deterministic_baseline_action(entry)

    assert a1 == a2


# ---------------------------------------------------------------------------
# B4b-4: low-conviction → WAIT
# ---------------------------------------------------------------------------


def test_baseline_low_conviction_emits_wait() -> None:
    """All side probs below FLAG_THRESHOLD_BPS (5000) → WAIT with empty params."""
    # over=4200, under=4800 — both below 5000
    entry = _ms({KEY: _market({"over": 4200, "under": 4800})})

    action = deterministic_baseline_action(entry)

    assert action.type == SportsActionType.WAIT
    assert action.params == {}


# ---------------------------------------------------------------------------
# B4b-5: all suspended → WAIT
# ---------------------------------------------------------------------------


def test_baseline_all_suspended_emits_wait() -> None:
    """When every market is suspended there is no scorable pick → WAIT."""
    entry = _ms(
        {
            KEY: _market({"over": 6000, "under": 4000}, suspended=True),
            "1X2|full|": _market({"Home": 5500, "Away": 4500}, suspended=True),
        }
    )

    action = deterministic_baseline_action(entry)

    assert action.type == SportsActionType.WAIT
    assert action.params == {}


# ---------------------------------------------------------------------------
# B4b-6: picks highest-conviction (key, side) pair across markets
# ---------------------------------------------------------------------------


def test_baseline_picks_highest_conviction_side() -> None:
    """With multiple markets, selects the (key, side) pair with the highest prob."""
    entry = _ms(
        {
            KEY: _market({"over": 7000, "under": 3000}),
            "1X2|full|": _market({"Home": 5500, "Away": 4500}),
        }
    )

    action = deterministic_baseline_action(entry)

    assert action.type == SportsActionType.FLAG_VALUE
    assert action.params["stable_prob_bps"] == 7000
    assert action.params["side"] == "over"
    assert action.params["market_key"] == KEY


# ---------------------------------------------------------------------------
# B4b-7: deterministic tiebreak by (market_key, side) when probs are equal
# ---------------------------------------------------------------------------


def test_baseline_ties_broken_by_market_key_then_side() -> None:
    """Equal probs → lexicographically smallest (market_key, side) wins."""
    entry = _ms(
        {
            "Z_market|full|": _market({"over": 6000, "under": 4000}),
            "A_market|full|": _market({"over": 6000, "under": 4000}),
        }
    )

    action = deterministic_baseline_action(entry)

    assert action.type == SportsActionType.FLAG_VALUE
    assert action.params["market_key"] == "A_market|full|"


# ---------------------------------------------------------------------------
# B4b-8: mixed suspended/active — skips suspended, acts on active only
# ---------------------------------------------------------------------------


def test_baseline_skips_suspended_considers_non_suspended() -> None:
    """Suspended market with high prob is ignored; active market ≥ 5000 is picked."""
    entry = _ms(
        {
            KEY: _market({"over": 8000, "under": 2000}, suspended=True),
            "1X2|full|": _market({"Home": 5100, "Away": 4900}),
        }
    )

    action = deterministic_baseline_action(entry)

    assert action.type == SportsActionType.FLAG_VALUE
    assert action.params["market_key"] == "1X2|full|"
    assert action.params["side"] == "Home"
