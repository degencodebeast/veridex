"""E1 event-contract tests for the live-recorder lane (MM-R3).

Trust-boundary tests: every event/config model is frozen + ``extra="forbid"``,
timestamps are integer ms (``source_ts`` stays seconds and may be ``None``), proof
statuses serialize lowercase, prices are native ``[0,1]``, and NO fill/PnL/edge or
post-decision fields may ever be stored on the immutable events.
"""

import pytest

from veridex.live_recorder.contracts import (
    FairValueEvent,
    RecorderHeartbeatEvent,
    VenueBookSnapshotEvent,
)


def test_envelope_ms_recv_ts_and_extra_forbid():
    e = RecorderHeartbeatEvent(
        sequence_no=1,
        event_type="RecorderHeartbeatEvent",
        source_ts=None,
        recv_ts=1_700_000_000_123,  # integer ms
        poll_index=0,
        venue_mids_seen=2,
        fv_points_recv=5,
        fv_aligned=True,
    )
    assert e.recv_ts == 1_700_000_000_123
    assert e.source_ts is None  # source_ts may be None
    with pytest.raises(Exception):  # extra="forbid" rejects unknown field
        RecorderHeartbeatEvent(
            sequence_no=1,
            event_type="RecorderHeartbeatEvent",
            source_ts=None,
            recv_ts=1_700_000_000_123,
            poll_index=0,
            venue_mids_seen=2,
            fv_points_recv=5,
            fv_aligned=True,
            unknown_field="nope",
        )


def _fv(**kw):
    base = dict(sequence_no=1, event_type="FairValueEvent", source_ts=100, recv_ts=100000,
                fixture_id=18209181, market_ref="1X2|home|full", side="part1", fv=0.6,
                phase=1, suspended=False, message_id=None, proof_ts=None,
                proof_status="unavailable_no_message_id"); base.update(kw); return FairValueEvent(**base)


def test_fair_value_missing_message_id_is_unavailable_lowercase():
    e = _fv(); assert e.proof_status == "unavailable_no_message_id"
    with pytest.raises(Exception): _fv(proof_status="PROVEN")                       # uppercase rejected
    with pytest.raises(Exception): _fv(message_id=None, proof_status="proven")      # no msg_id but claims proven → reject
    with pytest.raises(Exception): _fv(fv=1.4)                                       # decimal/out-of-range rejected


def _snap(**kw):
    base = dict(sequence_no=3, event_type="VenueBookSnapshotEvent", source_ts=None, recv_ts=200000,
                token_id="tok-1", venue_market_ref="poly|home|full", book_ts=200000,
                tick_size=0.01, min_price_increment=0.01,
                bids=({"price": 0.60, "size": 8.0},), asks=({"price": 0.62, "size": 5.0},),
                is_snapshot=True); base.update(kw); return VenueBookSnapshotEvent(**base)


def test_book_snapshot_stores_price_size_levels_not_mid():
    e = _snap()
    assert e.bids[0].price == 0.60 and e.bids[0].size == 8.0          # (price,size) levels, not a mid
    assert e.asks[0].price == 0.62 and e.asks[0].size == 5.0
    empty = _snap(bids=())
    assert empty.bids == ()                                            # empty side allowed, NOT imputed
    with pytest.raises(Exception): _snap(mid=0.61)                    # mid-only summary rejected (extra=forbid)
    with pytest.raises(Exception): _snap(bids=({"price": 1.4, "size": 8.0},))  # price out of [0,1] rejected
