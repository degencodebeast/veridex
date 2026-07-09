"""E2 — two-dimensional no-look-ahead alignment (recv_ts eligibility → source freshness)."""
from veridex.live_recorder.alignment import eligible_fv, replay_align, FvPoint


def hist():
    h = []
    for s, r, seq in [(100, 100000, 41), (110, 110000, 44), (105, 111000, 45)]:
        h.append(FvPoint(source_ts=s, recv_ts=r, value=s / 1000.0, sequence_no=seq))
    return h


def test_recv_ts_eligibility_then_source_freshness():
    h = hist()
    d107 = eligible_fv(h, decision_recv_ts=107000)     # only (100,100000) received
    assert d107.source_ts == 100
    d112 = eligible_fv(h, decision_recv_ts=112000)     # all three received → freshest source
    assert d112.source_ts == 110                        # NOT 105 (late arrival, older source)
    assert eligible_fv(h, decision_recv_ts=99000) is None   # nothing received yet → abstain


def test_sub_second_fv_is_ineligible():
    h = [FvPoint(source_ts=100, recv_ts=100000, value=0.6, sequence_no=1),
         FvPoint(source_ts=108, recv_ts=107600, value=0.7, sequence_no=2)]
    got = eligible_fv(h, decision_recv_ts=107000)
    assert got.source_ts == 100          # the 107_600ms arrival is NOT visible at 107_000ms


def test_duplicate_source_ts_correction_preserves_earlier_decision():
    # FV source_ts=105 first arrives value=0.60 (recv 105000), later a CORRECTION source_ts=105 value=0.70 (recv 130000)
    hist = [FvPoint(105, 105000, 0.60, 1), FvPoint(105, 130000, 0.70, 2)]
    decisions = [("d-110", 110000)]   # decided at recv 110000, before the correction arrived
    aligned = replay_align(decisions, hist)
    assert aligned["d-110"].value == 0.60   # sees the pre-correction value, NOT the final 0.70
