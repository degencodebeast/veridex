"""E8 post-session-analysis tests for the live-recorder lane (MM-R3, milestone E8).

Trust boundary under test here (E8-T1): gaps excluded from analysis (AC-008) — a
cadence/lead-lag series computed from a sealed session must NEVER let a change event span
a recorded gap window, and the analysis result carries no fill/PnL/rank/realized/edge field.
"""

from __future__ import annotations

import dataclasses

from veridex.live_recorder.contracts import LiveRecorderSessionMeta
from veridex.live_recorder.recorder import LiveRecorder

FIXTURE_ID = 18209181
MARKET_REF = "1X2|home|full"

# A produced field/claim must never carry one of these substrings (case-insensitive).
_FORBIDDEN_FIELD_SUBSTRINGS = ("fill", "pnl", "realized", "edge", "rank", "score_run")


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
