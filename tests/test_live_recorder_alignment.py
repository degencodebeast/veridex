"""E2 — two-dimensional no-look-ahead alignment (recv_ts eligibility → source freshness).

E3-T3 appends the pair-aware GLOBAL mint boundary (``eligible_fv_pair``): visibility on the FULL
global ``(recv_ts, sequence_no)`` pair (equal recv_ts → the global sequence decides), selection by
the SAME reviewed greatest-``(source_ts, sequence_no)`` freshness rule. The scalar ``eligible_fv`` is
byte-identical (append-only edit) — guarded here (REQ-020(d2) / AC-058 / RED-54).
"""
import hashlib
import inspect

import pytest

from veridex.live_recorder.alignment import (
    FvPoint,
    assert_append_order,
    eligible_fv,
    eligible_fv_pair,
    replay_align,
)


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


def test_sequence_no_is_append_order_nondecreasing_recv_ts():
    # (a) sequence_no is the append order — later sequence_no MUST NOT have an earlier recv_ts.
    bad = [FvPoint(source_ts=100, recv_ts=110000, value=0.5, sequence_no=1),
           FvPoint(source_ts=101, recv_ts=105000, value=0.6, sequence_no=2)]  # seq 2 arrived BEFORE seq 1
    with pytest.raises(ValueError):
        assert_append_order(bad)
    # a well-ordered history does not raise.
    good = [FvPoint(source_ts=100, recv_ts=105000, value=0.5, sequence_no=1),
            FvPoint(source_ts=101, recv_ts=110000, value=0.6, sequence_no=2)]
    assert_append_order(good)

    # (b) freshness tie on equal source_ts resolves to the greatest sequence_no.
    tie = [FvPoint(source_ts=105, recv_ts=105000, value=0.60, sequence_no=1),
           FvPoint(source_ts=105, recv_ts=106000, value=0.70, sequence_no=2)]
    got = eligible_fv(tie, decision_recv_ts=110000)
    assert got.sequence_no == 2
    assert got.value == 0.70


# --- E3-T3: pair-aware GLOBAL mint boundary (REQ-020(d2) / AC-058 / RED-54) -----------------

# Byte-identity guard for the scalar helper this append-only edit must NOT touch. The hash pins the
# EXACT source of ``eligible_fv`` (:41-63); any change — logic, docstring, whitespace — flips it.
_SCALAR_ELIGIBLE_FV_SHA256 = (
    "199aadff4f7a2556fb2309cc91d34486a5d80425af6b8b54be90f0898b30e86c"
)


def test_scalar_eligible_fv_unchanged():
    """The scalar ``eligible_fv`` is byte-identical after the append-only ``eligible_fv_pair`` edit.

    Signature, source bytes, AND behavior are all pinned — the pair-aware op REUSES the scalar's
    reviewed freshness rule, it must never mutate the scalar itself.
    """
    # signature — the scalar still takes exactly (fv_history, decision_recv_ts).
    params = list(inspect.signature(eligible_fv).parameters)
    assert params == ["fv_history", "decision_recv_ts"]

    # source bytes — pinned SHA-256 of the exact function source (guards the append-only promise).
    src = inspect.getsource(eligible_fv)
    assert hashlib.sha256(src.encode()).hexdigest() == _SCALAR_ELIGIBLE_FV_SHA256

    # behavior — the scalar's recv_ts eligibility + greatest (source_ts, seq) freshness are unchanged.
    h = hist()
    assert eligible_fv(h, decision_recv_ts=107000).source_ts == 100  # only earliest arrival visible
    assert eligible_fv(h, decision_recv_ts=112000).source_ts == 110  # freshest source, not late 105
    assert eligible_fv(h, decision_recv_ts=99000) is None  # nothing arrived → abstain
    # the scalar STILL admits an equal-recv_ts later-seq point (the hole the pair-aware op closes).
    same_ms = [FvPoint(source_ts=100, recv_ts=1200, value=0.5, sequence_no=9),
               FvPoint(source_ts=200, recv_ts=1200, value=0.9, sequence_no=11)]
    assert eligible_fv(same_ms, decision_recv_ts=1200).sequence_no == 11


def test_same_ms_boundary_fv_before_visible_after_invisible():
    """Mint pair ``(1200, 10)``: FV ``(1200, 9)`` is VISIBLE, FV ``(1200, 11)`` is INVISIBLE (RED-54).

    At an EQUAL ``recv_ts`` the GLOBAL ``sequence_no`` decides visibility: the same-millisecond point
    that arrived AFTER the trigger (seq 11 > 10) is not visible, even though its ``source_ts`` is
    fresher — no look-ahead within the millisecond. The mutation ``recv_ts <= mint_recv_ts`` (dropping
    the sequence tiebreak) leaks the ``(1200, 11)`` point in and this assertion fails.
    """
    fv_before = FvPoint(source_ts=100, recv_ts=1200, value=0.5, sequence_no=9)
    fv_after = FvPoint(source_ts=200, recv_ts=1200, value=0.9, sequence_no=11)  # fresher source, leaks under mutation

    got = eligible_fv_pair([fv_before, fv_after], mint_recv_ts=1200, mint_sequence_no=10)
    assert got is fv_before  # the same-ms BEFORE point, NOT the fresher-source AFTER point
    assert got.value == 0.5

    # each in isolation: before is the only visible point; after is invisible → abstain.
    assert eligible_fv_pair([fv_before], mint_recv_ts=1200, mint_sequence_no=10) is fv_before
    assert eligible_fv_pair([fv_after], mint_recv_ts=1200, mint_sequence_no=10) is None
    # the trigger's OWN pair (equal recv_ts, equal seq) is also not visible (strictly-below).
    fv_at = FvPoint(source_ts=300, recv_ts=1200, value=0.7, sequence_no=10)
    assert eligible_fv_pair([fv_at], mint_recv_ts=1200, mint_sequence_no=10) is None


def test_late_older_correction_never_overrides():
    """A late-arriving OLDER-source correction never overrides newer source data (AC-058).

    Both points are visible below the mint pair. Selection reuses the reviewed greatest
    ``(source_ts, sequence_no)`` rule: the source-NEW ``0.60`` (arrived earlier, recv 1000) beats the
    source-OLD ``0.20`` (arrived later, recv 1100) — arrival order does not override source freshness.
    """
    source_new = FvPoint(source_ts=200, recv_ts=1000, value=0.60, sequence_no=1)  # newer source, earlier arrival
    source_old = FvPoint(source_ts=100, recv_ts=1100, value=0.20, sequence_no=2)  # older source, later arrival

    got = eligible_fv_pair([source_new, source_old], mint_recv_ts=2000, mint_sequence_no=100)
    assert got is source_new
    assert got.value == 0.60

    # raw points retained: no eligible point below the pair → abstain, never impute.
    assert eligible_fv_pair([source_new, source_old], mint_recv_ts=999, mint_sequence_no=1) is None
