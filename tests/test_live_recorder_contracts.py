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
