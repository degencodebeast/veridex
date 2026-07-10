"""E8 post-session-analysis tests for the live-recorder lane (MM-R3, milestone E8).

Trust boundaries under test:

* E8-T1: gaps excluded from analysis (AC-008) — a cadence/lead-lag series computed from a
  sealed session must NEVER let a change event span a recorded gap window, and the analysis
  result carries no fill/PnL/rank/realized/edge field.
* E8-T2: COUNTERFACTUAL / observation only (EXE-003, CON-004, GUD-001) — the rendered report
  carries NO fill / fill-rate / realized-PnL / rank / "profitable" / "executable edge" claim,
  and an honest "no confirmed lead" verdict is stated plainly (never overclaimed via the
  lead-lag probe's fixed "FV LEADS" narrative).
"""

from __future__ import annotations

import dataclasses

from veridex.live_recorder.contracts import LiveRecorderSessionMeta
from veridex.live_recorder.recorder import LiveRecorder

FIXTURE_ID = 18209181
MARKET_REF = "1X2|home|full"

# A produced field/claim must never carry one of these substrings (case-insensitive).
_FORBIDDEN_FIELD_SUBSTRINGS = ("fill", "pnl", "realized", "edge", "rank", "score_run")

# Forbidden CLAIM phrases in rendered report text -- exact fill/PnL/rank/edge claims, never
# the honest disclaiming vocabulary already used throughout this lane (e.g. "COUNTERFACTUAL").
_FORBIDDEN_REPORT_PHRASES = (
    "fill_price",
    "filled_size",
    "realized_pnl",
    "real_executable_edge_bps",
    "executable edge",
    "profitable",
    "fill rate",
)


def _start_meta() -> LiveRecorderSessionMeta:
    return LiveRecorderSessionMeta(
        session_ts=1_700_000_000,
        endpoints={"venue": "wss://example.invalid"},
        tool_version="test-e8",
        config_hash="cfg-hash",
        source_provenance={"venue": "poly"},
        fixture_ids=(FIXTURE_ID,),
    )


def _fv(*, recv_ts: int, fv: float) -> dict:
    return {
        "event_type": "FairValueEvent",
        "source_ts": recv_ts // 1000,
        "recv_ts": recv_ts,
        "fixture_id": FIXTURE_ID,
        "market_ref": MARKET_REF,
        "side": "part1",
        "fv": fv,
        "phase": 1,
        "suspended": False,
        "message_id": None,
        "proof_ts": None,
        "proof_status": "unavailable_no_message_id",
    }


def _book(*, recv_ts: int, bid: float, ask: float) -> dict:
    return {
        "event_type": "VenueBookSnapshotEvent",
        "source_ts": None,
        "recv_ts": recv_ts,
        "token_id": "tok-1",
        "venue_market_ref": MARKET_REF,
        "book_ts": recv_ts,
        "tick_size": 0.01,
        "min_price_increment": 0.01,
        "bids": [{"price": bid, "size": 5.0}],
        "asks": [{"price": ask, "size": 5.0}],
        "is_snapshot": True,
    }


def _build_gapped_session(tmp_path) -> object:
    """A sealed session with a gap straddling exactly one FV pair: [200,400] crosses it."""
    session = tmp_path / "s1"
    rec = LiveRecorder(session, _start_meta())
    rec.record(_fv(recv_ts=100, fv=0.60))
    rec.record(_fv(recv_ts=200, fv=0.62))  # change 0.60->0.62, does NOT cross the gap
    rec.record_gap(from_ts=250, to_ts=350, source="venue", reason="disconnect")
    rec.record(_fv(recv_ts=400, fv=0.65))  # 200->400 CROSSES the gap: excluded
    rec.record(_fv(recv_ts=500, fv=0.55))  # change 0.65->0.55, does NOT cross the gap
    rec.record(_book(recv_ts=150, bid=0.59, ask=0.61))
    rec.record(_book(recv_ts=450, bid=0.63, ask=0.66))
    meta = rec.finalize(ended_ts=1_700_000_900)
    rec.close()
    return session, meta


# --------------------------------------------------------------------------- E8-T1
def test_analysis_excludes_gaps_and_is_counterfactual_only(tmp_path):
    from veridex.live_recorder.analysis import analyze_session

    session, meta = _build_gapped_session(tmp_path)

    result = analyze_session(session)

    key = (FIXTURE_ID, MARKET_REF)
    cadence = {summary.key: summary for summary in result.cadence_by_market}
    assert key in cadence
    # 4 FV events -> 3 possible consecutive pairs, but the [200,400] pair CROSSES the gap and
    # must be excluded: only 2 gap-safe changes ((100,200) and (400,500)), never 3.
    assert cadence[key].n_gap_safe_changes == 2
    assert cadence[key].n_fv_events == 4

    # sealed session round-trips byte-identically
    assert result.session_meta.content_hash == meta.content_hash
    assert result.replay_reproduced is True
    assert result.n_gaps == 1

    # no forbidden field ANYWHERE on the frozen result (fill/pnl/realized/edge/rank as a
    # produced claim) -- only counterfactual/observation fields are ever produced.
    for obj in (result, *result.cadence_by_market, result.leadlag, *result.queue_jump):
        for field in dataclasses.fields(obj):
            lowered = field.name.lower()
            for bad in _FORBIDDEN_FIELD_SUBSTRINGS:
                assert bad not in lowered, f"forbidden field {field.name!r} on {type(obj)}"

    # the honest lead-lag verdict is one of the probe's own closed set (never fabricated)
    assert result.leadlag.verdict in {
        "NO DATA",
        "NO CONFIRMED LEAD",
        "FV LEADS (modest, latency-driven)",
    }


# --------------------------------------------------------------------------- E8-T2
def test_report_is_observation_labeled(tmp_path):
    from veridex.live_recorder.analysis import analyze_session, render_session_report

    # A small session with insufficient gap-safe evidence for a confirmed lead (well below
    # the leadlag probe's warmup) -- the honest verdict must be a "no lead" one, not "FV LEADS".
    session = tmp_path / "s2"
    rec = LiveRecorder(session, _start_meta())
    rec.record(_fv(recv_ts=100, fv=0.60))
    rec.record(_fv(recv_ts=200, fv=0.62))
    rec.record(_book(recv_ts=110, bid=0.59, ask=0.61))
    rec.record(_book(recv_ts=210, bid=0.61, ask=0.63))
    rec.finalize(ended_ts=1_700_000_500)
    rec.close()

    result = analyze_session(session)
    assert not result.leadlag.verdict.startswith("FV LEADS")  # confirms the honest-no-lead path

    report = render_session_report(result)

    # executability is labeled COUNTERFACTUAL wherever it is referenced
    assert "COUNTERFACTUAL" in report

    # no forbidden CLAIM phrase anywhere in the rendered text
    lowered_report = report.lower()
    for phrase in _FORBIDDEN_REPORT_PHRASES:
        assert phrase not in lowered_report

    # a no-lead result is stated HONESTLY -- the verdict appears, and the probe's fixed
    # "FV LEADS" narrative (which would overclaim a lead) is never spliced in
    assert result.leadlag.verdict in report
    assert "FV LEADS the venue mid" not in report
    assert "no overclaim" in report.lower() or "honestly" in report.lower()


# --------------------------------------------------------------------------- E8-T2 (honesty)
def test_report_does_not_overclaim_r4_readiness(tmp_path):
    """A toy session satisfying ONLY the R3-local replay/cadence gate (gate 1 of 4) must
    never let the report claim R4 readiness. R4 go/no-go needs FOUR independent gates
    (R3 records+replays; live FV lead CONFIRMED; make-vs-take EV positive under Rose 4x
    fee stress; guarded-live safety wiring) -- gates 2-4 are NOT evaluated here."""
    from veridex.live_recorder.analysis import analyze_session, render_session_report

    # This session reproduces on replay, is sealed with an ended_ts + fixtures, and has
    # >=1 gap-safe FV change -- i.e. it satisfies the R3-local gate ONLY. It has NO
    # confirmed lead, no fee-stress EV, no safety wiring.
    session, _meta = _build_gapped_session(tmp_path)

    result = analyze_session(session)
    # the R3-local gate is genuinely met for this session (only gate 1 of 4)
    assert result.r3_replay_prereq_met is True
    # ...yet the lead-lag evidence does NOT confirm a lead (gate 2 not satisfied)
    assert not result.leadlag.verdict.startswith("FV LEADS")

    report = render_session_report(result)
    lowered = report.lower()

    # (a) NO phrasing that asserts R4 readiness / prerequisites-met as True
    assert "r4 prerequisites met: true" not in lowered
    assert "r4 prerequisites met" not in lowered
    assert "r4 prerequisites: met" not in lowered

    # (b) the R3-local gate IS stated honestly as gate 1 of 4...
    assert "r3 replay/cadence prerequisite (r4 gate 1 of 4): met" in lowered
    # ...AND R4 gates 2-4 are explicitly declared not evaluated (declared-gated, not run)
    assert "r4 gates 2-4" in lowered
    assert "not evaluated" in lowered
    assert "declared-gated" in lowered
