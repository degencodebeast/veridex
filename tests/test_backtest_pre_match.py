"""D2 — pre_match backtest close semantics: stop at kickoff + per-market CON-040 close.

These are OFFLINE unit tests over the PURE planning helpers (``veridex.backtest.pre_match``) driven
with tiny synthetic ``MarketState`` lists carrying explicit ``phase``/``market_key`` combinations —
no pack, no network, no LLM. They pin the corrected ``pre_match`` contract:

  * decisions stop at the FIRST in-running (``phase == 1``) tick (kickoff);
  * the close is the per-market LAST pre-kickoff (``phase == 0``) line, folded into ONE snapshot that
    covers EVERY scored market (so no market silently falls back to its entry tick → CLV 0);
  * the three honest degrade/marker edges (never-in-running, all-in-running, incomplete close).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from veridex.backtest.pre_match import (
    PreMatchPlan,
    first_inrunning_index,
    plan_pre_match_backtest,
    pre_match_close_gap,
    reconstruct_pre_match_close,
)
from veridex.backtest.runner import run_backtest
from veridex.ingest.marketstate import MarketState
from veridex.ingest.recorder import SessionMeta, envelope_line
from veridex.ingest.replay_pack import pack_from_session
from veridex.runtime.orchestrator import deterministic_agent
from veridex.runtime.window import RunWindow
from veridex.scoring import is_scored

_FIXTURE_ID = 1
_PACK_FIXTURE_ID = 777


def _ms(tick_seq: int, ts: int, phase: int, markets: dict[str, int], *, suspended: set[str] | None = None) -> MarketState:
    """One synthetic tick: ``markets`` maps ``market_key -> Under prob (bps)``; Over is the complement.

    A ``market_key`` listed in ``suspended`` is emitted as a suspended (unpriced) market.
    """
    suspended = suspended or set()
    return MarketState(
        fixture_id=_FIXTURE_ID,
        tick_seq=tick_seq,
        ts=ts,
        phase=phase,
        markets={
            mk: (
                {"stable_prob_bps": {}, "stable_price": {}, "suspended": True}
                if mk in suspended
                else {
                    "stable_prob_bps": {"Under": prob, "Over": 10_000 - prob},
                    "stable_price": {"Under": 2.0, "Over": 2.0},
                    "suspended": False,
                }
            )
            for mk, prob in markets.items()
        },
        scores={},
    )


def _under(state: MarketState, market_key: str) -> Any:
    return state.markets[market_key]["stable_prob_bps"]["Under"]


# ---------------------------------------------------------------------------
# first_inrunning_index — the kickoff cutoff
# ---------------------------------------------------------------------------


def test_first_inrunning_index_finds_first_kickoff() -> None:
    states = [_ms(0, 100, 0, {"A": 6000}), _ms(1, 110, 0, {"A": 6100}), _ms(2, 120, 1, {"A": 6200})]
    assert first_inrunning_index(states) == 2


def test_first_inrunning_index_none_when_never_in_running() -> None:
    states = [_ms(0, 100, 0, {"A": 6000}), _ms(1, 110, 0, {"A": 6100})]
    assert first_inrunning_index(states) is None


def test_first_inrunning_index_zero_when_first_tick_in_running() -> None:
    states = [_ms(0, 100, 1, {"A": 6000}), _ms(1, 110, 1, {"A": 6100})]
    assert first_inrunning_index(states) == 0


# ---------------------------------------------------------------------------
# Kickoff cutoff — decisions stop at the first in-running tick
# ---------------------------------------------------------------------------


def test_pre_match_stops_at_first_inrunning() -> None:
    """A full-match pack (phase 0,0,1,1): decisions use ONLY pre-kickoff states, none phase==1."""
    states = [
        _ms(0, 100, 0, {"A": 6000}),
        _ms(1, 110, 0, {"A": 6100}),
        _ms(2, 120, 1, {"A": 6200}),
        _ms(3, 130, 1, {"A": 6300}),
    ]
    plan = plan_pre_match_backtest(states)

    assert isinstance(plan, PreMatchPlan)
    assert not plan.degraded
    assert plan.decision_states == states[:2]
    assert all(s.phase == 0 for s in plan.decision_states)  # never a decision on an in-running tick
    assert plan.closing_state is not None


# ---------------------------------------------------------------------------
# Core completeness test — per-market close, no silent CLV 0
# ---------------------------------------------------------------------------


def test_pre_match_close_is_per_market_complete() -> None:
    """>=2 market_keys last-updated pre-kickoff at DIFFERENT ticks → close carries BOTH last values.

    Without folding, the close would be the single last pre-kickoff tick (only B), and A would fall
    back to its own last decision tick → a silent CLV 0. The folded close carries A@t1 and B@t3.
    """
    states = [
        _ms(0, 100, 0, {"A": 6000}),
        _ms(1, 110, 0, {"A": 6100}),  # A last updated here (pre-kickoff)
        _ms(2, 120, 0, {"B": 5500}),
        _ms(3, 130, 0, {"B": 5600}),  # B last updated here (pre-kickoff), a DIFFERENT tick
        _ms(4, 140, 1, {"A": 9000, "B": 9000}),  # kickoff / full-time line — must NOT be the close
    ]
    plan = plan_pre_match_backtest(states)

    assert not plan.degraded
    close = plan.closing_state
    assert close is not None
    # BOTH markets present in the ONE close snapshot, each at its OWN last pre-kickoff value.
    assert set(close.markets) == {"A", "B"}
    assert _under(close, "A") == 6100  # A's last phase-0 line (not 6000 entry, not 9000 full-time)
    assert _under(close, "B") == 5600  # B's last phase-0 line (not 5500 entry, not 9000 full-time)
    # No scored market is left uncovered → no silent fall-back to entry.
    assert pre_match_close_gap(plan.decision_states, close) == set()


def test_reconstruct_close_reuses_single_source_snapshot() -> None:
    """When one pre-kickoff tick already carries every market, the close IS that tick (byte-identity)."""
    states = [_ms(0, 100, 0, {"A": 6000}), _ms(1, 110, 0, {"A": 6100})]
    close = reconstruct_pre_match_close(states)
    assert close is states[-1]  # same object — the legacy pre-match-only path stays byte-identical


# ---------------------------------------------------------------------------
# Edge: all in-running → fail closed (never fabricate a pre-match close)
# ---------------------------------------------------------------------------


def test_pre_match_all_inrunning_fails_closed() -> None:
    """Every state phase==1 → no fabricated close; degrade with a NAMED reason (not a silent 0)."""
    states = [_ms(0, 100, 1, {"A": 6000}), _ms(1, 110, 1, {"A": 6100})]
    plan = plan_pre_match_backtest(states)

    assert plan.degraded is True
    assert plan.decision_states == []  # nothing pre-kickoff → nothing to decide on
    assert plan.closing_state is None  # never fabricated
    assert plan.closing_note is not None
    assert "pre-kickoff" in plan.closing_note.lower()


# ---------------------------------------------------------------------------
# Edge: never in-running → allow, but MARK "no verified kickoff"
# ---------------------------------------------------------------------------


def test_pre_match_never_inrunning_labels_no_transition() -> None:
    """All phase==0 → scores against the last per-market phase-0 close AND carries the marker."""
    states = [
        _ms(0, 100, 0, {"A": 6000}),
        _ms(1, 110, 0, {"A": 6100}),
        _ms(2, 120, 0, {"A": 6200}),
    ]
    plan = plan_pre_match_backtest(states)

    assert plan.degraded is False  # a pre-match-only pack still scores true CLV
    assert plan.decision_states == states[:-1]  # last tick held out as the close proxy
    assert plan.closing_state is not None
    assert plan.closing_note is not None
    assert "no in-running transition" in plan.closing_note.lower()


# ---------------------------------------------------------------------------
# Edge: multiple transitions → the FIRST kickoff wins
# ---------------------------------------------------------------------------


def test_pre_match_multiple_transitions_first_kickoff_wins() -> None:
    """0->1->0: cutoff at the FIRST phase==1; the LATER phase==0 is NOT used for decisions or close."""
    states = [
        _ms(0, 100, 0, {"A": 6000}),
        _ms(1, 110, 0, {"A": 6100}),  # last pre-(first-)kickoff line
        _ms(2, 120, 1, {"A": 6200}),  # FIRST kickoff — cutoff here
        _ms(3, 130, 0, {"A": 7000}),  # a later phase-0 (halftime re-quote) — MUST be ignored
        _ms(4, 140, 0, {"A": 7100}),
    ]
    plan = plan_pre_match_backtest(states)

    assert not plan.degraded
    assert plan.decision_states == states[:2]  # only ticks before the FIRST kickoff
    assert plan.closing_state is not None
    assert _under(plan.closing_state, "A") == 6100  # NOT 7100 from the post-kickoff phase-0 tick


# ---------------------------------------------------------------------------
# Edge: incomplete per-market close → fail closed (defensive gate + its wiring)
# ---------------------------------------------------------------------------


def test_pre_match_close_gap_flags_uncovered_scored_market() -> None:
    """The gate NAMES every scored market a close fails to cover (would-be silent CLV-0 fall-backs)."""
    decision_states = [_ms(0, 100, 0, {"A": 6000}), _ms(1, 110, 0, {"A": 6100, "B": 5500})]
    incomplete_close = _ms(2, 120, 0, {"A": 6100})  # B is missing from the close
    assert pre_match_close_gap(decision_states, incomplete_close) == {"B"}
    # A close of None leaves EVERY seen market uncovered.
    assert pre_match_close_gap(decision_states, None) == {"A", "B"}


def test_pre_match_reconstructed_close_never_leaves_a_gap() -> None:
    """The fold GUARANTEES completeness: a close it builds covers every pre-kickoff market."""
    decision_states = [
        _ms(0, 100, 0, {"A": 6000}),
        _ms(1, 110, 0, {"B": 5500}),
        _ms(2, 120, 0, {"C": 4000}),
    ]
    close = reconstruct_pre_match_close(decision_states)
    assert close is not None
    assert pre_match_close_gap(decision_states, close) == set()


# ---------------------------------------------------------------------------
# Integration — the real run_backtest pre_match path (small full-match packs)
# ---------------------------------------------------------------------------


def _ou_record(ts_ms: int, under_pct: float, *, in_running: bool) -> dict[str, Any]:
    """One raw native TxLINE OU record; ``in_running`` drives the normalized ``phase``."""
    return {
        "FixtureId": _PACK_FIXTURE_ID,
        "Ts": ts_ms,
        "InRunning": in_running,
        "SuperOddsType": "OU",
        "MarketPeriod": None,
        "MarketParameters": "line=2.5",
        "PriceNames": ["Over", "Under"],
        "Prices": [1900, 1900],
        "Pct": [round(100.0 - under_pct, 1), round(under_pct, 1)],
    }


def _build_full_match_pack(tmp_path: Path, phases: list[bool]) -> Path:
    """Build a hashed ReplayPack: one tick per ``phases`` entry (True ⇒ in-running); Under drifts up."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    lines = [
        envelope_line(_ou_record(100_000 + i * 10_000, 60.0 + i * 0.5, in_running=ir), 100 + i * 10)
        for i, ir in enumerate(phases)
    ]
    (session_dir / "records.jsonl").write_text("\n".join(lines) + "\n")
    (session_dir / "meta.json").write_text(
        SessionMeta(started_ts=99, endpoints=["/odds/stream"], tool_version="t").model_dump_json()
    )
    out_dir = tmp_path / "pack"
    pack_from_session(session_dir, out_dir)
    return out_dir


def _window() -> RunWindow:
    return RunWindow(
        window_id="w_bt", fixture_id=_PACK_FIXTURE_ID, market_allowlist=["OU"], end_rule="pre_match", min_clv_horizon_s=0
    )


async def test_run_backtest_pre_match_stops_at_kickoff(tmp_path: Path) -> None:
    """A full-match pack (4 pre-kickoff + 3 in-running): NO decision is scored on an in-running tick."""
    # ticks 0..3 pre-kickoff (phase 0), ticks 4..6 in-running (phase 1) → kickoff cutoff at tick_seq 4.
    pack_dir = _build_full_match_pack(tmp_path, phases=[False, False, False, False, True, True, True])

    result, report = await run_backtest(pack_dir, _PACK_FIXTURE_ID, [deterministic_agent("baseline")], window=_window())

    scored_tick_seqs = [row["tick_seq"] for row in result.score_rows]
    assert scored_tick_seqs, "the pre-kickoff ticks should have produced decisions"
    assert max(scored_tick_seqs) < 4  # every decision is strictly pre-kickoff — none on an in-running tick
    # A clean full-match pre_match scores TRUE CLV (against the last pre-kickoff line, not the full-time line).
    assert report.avg_clv is not None
    assert all(is_scored(row) for row in result.score_rows if row.get("valid"))
    assert report.closing_note is None  # verified kickoff + complete close → no degrade marker


async def test_run_backtest_all_inrunning_degrades_with_named_reason(tmp_path: Path) -> None:
    """A pack already in-running at tick 0 → fail closed: named reason, NO fabricated true CLV."""
    pack_dir = _build_full_match_pack(tmp_path, phases=[True, True, True])

    _, report = await run_backtest(pack_dir, _PACK_FIXTURE_ID, [deterministic_agent("baseline")], window=_window())

    assert report.closing_note is not None
    assert "pre-kickoff" in report.closing_note.lower()  # the named degrade reason (asserted, not a silent 0)
    assert report.avg_clv is None  # no true pre-match CLV was fabricated
    assert report.clv_distribution.count == 0
