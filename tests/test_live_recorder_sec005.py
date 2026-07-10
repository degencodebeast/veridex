"""E5-T4 trust-anchor tests (SEC-005 seed) for the live-recorder lane (MM-R3).

A COUNTERFACTUAL :class:`ExecutabilityMeasurement` must NEVER be convertible into a fill,
a realized PnL, or a ranking metric. This asserts the measurement carries NONE of the
directional ``_rank_key`` metric fields the scorer sorts on
(``avg_clv_bps``/``total_clv_bps``/``brier``/``max_drawdown``/``action_count``/``agent_id``
— confirmed in ``veridex/scoring.py``) and NO ``fill_price``/``filled_size``/
``realized_pnl``/``real_executable_edge_bps`` field.

NOTE: E7-T3 finalizes the SEC-006 denylist cross-check on top of this seed; this file is
kept importable + green in the meantime.
"""

from veridex.live_recorder.contracts import ExecutabilityMeasurement

# The directional metric fields the scorer's `_rank_key` sorts a run on
# (veridex/scoring.py:229-238). An executability OBSERVATION must expose none of them.
_RANK_KEY_FIELDS = (
    "avg_clv_bps",
    "total_clv_bps",
    "brier",
    "max_drawdown",
    "action_count",
    "agent_id",
)

# Fill / PnL / realized-value fields that would turn an observation into a claimed fill.
_FILL_PNL_FIELDS = (
    "fill_price",
    "filled_size",
    "realized_pnl",
    "real_executable_edge_bps",
    "spread_capture",
    "fill_rate",
)


def _ex(**kw) -> ExecutabilityMeasurement:
    base = dict(
        candidate_price=0.60, available_size_at_price=8.0, cumulative_size_to_clear=8.0,
        spread=0.02, half_spread=0.01, cost_clearing_threshold=0.60, taker_fee_bps=0,
        fee_stress_multiplier=4, stale_window_s=120, clears=True, label="COUNTERFACTUAL",
    )
    base.update(kw)
    return ExecutabilityMeasurement(**base)


def test_executability_not_convertible_to_fill_or_rank():
    ex = _ex()
    dumped = ex.model_dump()

    # Trust anchor 1: none of the scorer's directional rank-key fields exist on the measurement.
    for field in _RANK_KEY_FIELDS:
        assert not hasattr(ex, field), f"executability must not carry rank-key field {field!r}"
        assert field not in dumped, f"executability dump must not carry rank-key field {field!r}"

    # Trust anchor 2: no fill / PnL / realized-value field exists on the measurement.
    for field in _FILL_PNL_FIELDS:
        assert not hasattr(ex, field), f"executability must not carry fill/PnL field {field!r}"
        assert field not in dumped, f"executability dump must not carry fill/PnL field {field!r}"

    # It IS and stays a COUNTERFACTUAL observation.
    assert ex.label == "COUNTERFACTUAL"


# ---------------------------------------------------------------------------
# E7-T3 — SEC-006 canonical R3/R4 rank denylist, enforced at BOTH rank inputs.
# These are ENFORCEMENT-POINT tests (not helper-only): they drive the real
# rank_makers / _rank_key entrypoints, proving a clean row passes and an
# R3/R4-carrying row raises.
# ---------------------------------------------------------------------------

import pytest  # noqa: E402

from veridex.rank_guards import R3_R4_RANK_DENYLIST  # noqa: E402


def _valid_maker_row() -> dict:
    """A minimal VALID maker rank row (mirrors aggregate_agent_metrics output)."""
    return {
        "agent_id": "mm-a",
        "avg_markout_bps": 1050,
        "avg_toxicity_loss_bps": 40,
        "quote_count": 100,
        "abstained": 0,
    }


def _valid_directional_metrics() -> dict:
    """A minimal VALID directional metric stack (the fields _rank_key reads)."""
    return {
        "agent_id": "dir-a",
        "avg_clv_bps": 12.0,
        "total_clv_bps": 24,
        "brier": None,
        "max_drawdown": 0.0,
        "action_count": 2,
    }


def test_maker_rank_rejects_r3r4_field():
    from veridex.maker.leaderboard import rank_makers

    # Clean row ranks fine (baseline: the guard is a NO-OP on clean input)...
    assert rank_makers([_valid_maker_row()])[0]["maker_rank"] == 1

    # ...but an R3 queue-observation field smuggled into the rank input must raise.
    poisoned = _valid_maker_row()
    poisoned["queue_ahead_size"] = 3.0
    with pytest.raises(Exception):
        rank_makers([poisoned])


def test_maker_rank_key_is_self_guarded():
    from veridex.maker.leaderboard import maker_rank_key

    # A clean maker row still returns a sort key (guard is a NO-OP on clean input)...
    assert isinstance(maker_rank_key(_valid_maker_row()), tuple)

    # ...but calling the exported key directly on an R3-poisoned row must raise
    # (the guard lives INSIDE the key, not only in rank_makers).
    poisoned = _valid_maker_row()
    poisoned["queue_ahead_size"] = 3.0
    with pytest.raises(Exception):
        maker_rank_key(poisoned)


def test_direct_sorted_by_maker_rank_key_rejects_r3r4():
    from veridex.maker.leaderboard import maker_rank_key

    # A direct sort bypassing rank_makers must NOT smuggle an R3/R4 field through.
    poisoned = _valid_maker_row()
    poisoned["queue_ahead_size"] = 3.0
    with pytest.raises(Exception):
        sorted([poisoned], key=maker_rank_key)


def test_directional_rank_rejects_r3r4_field():
    from veridex.scoring import _rank_key

    # Clean metrics produce a sort key (NO-OP on clean input)...
    assert isinstance(_rank_key(_valid_directional_metrics()), tuple)

    # ...but an R4 own-fill field on the directional rank input must raise.
    poisoned = _valid_directional_metrics()
    poisoned["own_fill"] = 1
    with pytest.raises(Exception):
        _rank_key(poisoned)


def test_denylist_excludes_generic_legit_names():
    for legit in ("side", "spread", "size", "label", "ranked"):
        assert legit not in R3_R4_RANK_DENYLIST
    for bad in ("queue_ahead_size", "own_fill", "realized_pnl"):
        assert bad in R3_R4_RANK_DENYLIST


# ---------------------------------------------------------------------------
# E7-T4 — directional score_run + sealed maker result stay byte-identical when an
# R3 live_recorder session runs (SEC-005/AC-011). The rank guards wired in E7-T3
# are raise-only NO-OPs on clean input, so scoring output is unchanged and the
# sealed maker JSON on disk is never rewritten.
# ---------------------------------------------------------------------------

import hashlib  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

from veridex.live_recorder.contracts import (  # noqa: E402
    ExecutabilityMeasurement,
    QuoteIntentEvent,
)
from veridex.maker.result import assert_score_run_untouched  # noqa: E402
from veridex.runtime.orchestrator import RunResult  # noqa: E402
from veridex.scoring import score_run  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SEALED_MAKER_RESULT = "scripts/txline_live/cp1/maker-arena-result.json"


def _dir_row(agent_id: str, tick_seq: int, clv_bps: int, confidence: float | None = None) -> dict:
    """A directional score row in the shape score_run consumes."""
    params: dict = {"market_key": "OU_2_5", "side": "over"}
    if confidence is not None:
        params["confidence"] = confidence
    return {
        "agent_id": agent_id,
        "tick_seq": tick_seq,
        "clv_bps": clv_bps,
        "valid": True,
        "reason": "",
        "raw_prescore": {"raw_action": {"type": "FLAG_VALUE", "params": params}},
    }


def _representative_run() -> RunResult:
    rows = [
        _dir_row("agent-alpha", 0, 15, confidence=0.7),
        _dir_row("agent-alpha", 1, -5, confidence=0.4),
        _dir_row("agent-beta", 0, 8, confidence=0.55),
        _dir_row("agent-beta", 1, 3),
    ]
    return RunResult(
        run_id="run-e7t4",
        source_mode="replay",
        agent_ids=["agent-alpha", "agent-beta"],
        run_events=[],
        score_rows=rows,
        evidence_hash="",
        proof_mode_map={"agent-alpha": "reproducible", "agent-beta": "reproducible"},
    )


def _simulate_r3_live_recorder_session() -> None:
    """Exercise the MM-R3 recorder lane: build the COUNTERFACTUAL executability + a
    queue-aware quote intent (both carry R3 denylist field names the recorder PERMITS).
    Constructing these must not touch the directional scorer in any way."""
    ExecutabilityMeasurement(
        candidate_price=0.60, available_size_at_price=8.0, cumulative_size_to_clear=8.0,
        spread=0.02, half_spread=0.01, cost_clearing_threshold=0.60, taker_fee_bps=0,
        fee_stress_multiplier=4, stale_window_s=120, clears=True, label="COUNTERFACTUAL",
    )
    QuoteIntentEvent(
        sequence_no=1, event_type="QuoteIntentEvent", source_ts=None, recv_ts=1_000,
        decision_id="d1", native_price=0.60, desired_size=5.0, side="buy", ladder_rung=1,
        quote_intent_type="join", queue_ahead_size=3.0,
    )


def test_score_run_untouched_after_live_recorder():
    run = _representative_run()
    before = score_run(run)

    _simulate_r3_live_recorder_session()

    after = score_run(run)
    # AC-011: the directional leaderboard is byte-identical across an R3 recorder session.
    assert_score_run_untouched(before, after)  # raises iff before != after

    # The sealed maker arena result on disk is unchanged vs its committed content.
    sealed = _REPO_ROOT / _SEALED_MAKER_RESULT
    on_disk = sealed.read_bytes()
    committed = subprocess.run(
        ["git", "show", f"HEAD:{_SEALED_MAKER_RESULT}"],
        cwd=_REPO_ROOT, capture_output=True, check=True,
    ).stdout
    assert hashlib.sha256(on_disk).hexdigest() == hashlib.sha256(committed).hexdigest(), (
        "sealed maker-arena-result.json must be byte-identical to its committed content"
    )
