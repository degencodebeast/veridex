"""E2 — two-dimensional no-look-ahead alignment (recv_ts eligibility → source freshness)."""
from veridex.live_recorder.alignment import eligible_fv, FvPoint


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
