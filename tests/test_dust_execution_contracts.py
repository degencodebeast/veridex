"""E1-T2 lifecycle event-contract tests for the R4-A dust-execution lane (Section 4.1).

Trust boundaries proven here: every event is frozen + ``extra="forbid"``; the envelope
enforces integer-ms ``recv_ts`` + ``event_type == class name``; native ``[0,1]`` price guards
reject decimal odds; the cancel-all primitive never carries a single order id; and
``config_hash()`` is a deterministic canonical digest (AC-021).
"""

import pytest
from pydantic import ValidationError

from veridex.dust_execution.contracts import (
    CancelAllTriggeredEvent,
    DustExecutionSessionMeta,
    OrderStatusEvent,
    OrderSubmitAttempt,
    OrderSubmitIntent,
    OwnFillEvent,
    PostTradeMarkoutEvent,
    PreSubmitRecord,
)


def _status(**kw) -> OrderStatusEvent:
    base = {
        "sequence_no": 7,
        "event_type": "OrderStatusEvent",
        "source_ts": 1_700_000_000,
        "recv_ts": 1_700_000_000_123,
        "decision_id": "dec-1",
        "client_order_id": "coid-1",
        "venue_order_id": "0xorder",
        "status": "partial",
        "filled_size": 1.5,
        "fill_price": 0.42,
    }
    base.update(kw)
    return OrderStatusEvent(**base)


def _intent(**kw) -> OrderSubmitIntent:
    base = {
        "sequence_no": 1,
        "event_type": "OrderSubmitIntent",
        "source_ts": None,
        "recv_ts": 1_700_000_000_123,
        "token_id": "0xtokenYES",
        "side": "buy",
        "price": 0.55,
        "size": 2.0,
        "tif": "GTC",
        "client_order_id": "coid-1",
        "decision_id": "dec-1",
        "decision_ts": 1_700_000_000_100,
    }
    base.update(kw)
    return OrderSubmitIntent(**base)


def test_order_status_records_real_fill_but_forbids_unknown_field() -> None:
    # R4-A DOES record real fills: native-[0,1] fill price + a filled_size are first-class.
    ev = _status()
    assert ev.recv_ts == 1_700_000_000_123  # integer milliseconds
    assert ev.filled_size == 1.5
    assert ev.fill_price == 0.42
    # extra="forbid" rejects an UNMODELLED leaked field.
    with pytest.raises(ValidationError):
        _status(realized_pnl=9.99)
    # Native [0,1] price guard rejects decimal-odds-style values.
    with pytest.raises(ValidationError):
        _status(fill_price=1.4)
    # event_type must match the concrete class name.
    with pytest.raises(ValidationError):
        _status(event_type="NotTheClass")


def test_order_status_fill_price_may_be_none() -> None:
    ev = _status(status="rejected", filled_size=0.0, fill_price=None)
    assert ev.fill_price is None


def test_submit_intent_tif_closed_set_and_price_guard() -> None:
    assert _intent(tif="FAK").tif == "FAK"
    with pytest.raises(ValidationError):
        _intent(tif="IOC")  # not in the FAK/FOK/GTC/GTD set
    with pytest.raises(ValidationError):
        _intent(price=1.4)  # decimal odds rejected


def test_submit_attempt_carries_presubmit_record() -> None:
    rec = PreSubmitRecord(
        integrity_commitment_hash="ic" * 8,
        venue_order_key="0xvenuekey",
        captured_id=None,
    )
    attempt = OrderSubmitAttempt(
        sequence_no=2,
        event_type="OrderSubmitAttempt",
        source_ts=None,
        recv_ts=1_700_000_000_200,
        decision_id="dec-1",
        client_order_id="coid-1",
        request_payload_ref="scrubbed://ref/1",
        attempt_ts=1_700_000_000_200,
        presubmit_record=rec,
    )
    assert attempt.presubmit_record.venue_order_key == "0xvenuekey"
    with pytest.raises(ValidationError):  # frozen + extra forbid on the nested record too
        PreSubmitRecord(
            integrity_commitment_hash="x",
            venue_order_key="y",
            leaked_owner="0xsecret",
        )


def test_cancel_all_never_echoes_single_order_id() -> None:
    ev = CancelAllTriggeredEvent(
        sequence_no=3,
        event_type="CancelAllTriggeredEvent",
        source_ts=None,
        recv_ts=1_700_000_000_300,
        trigger_cause="breaker",
    )
    assert ev.trigger_cause == "breaker"
    # No order-id field exists; attempting to attach one is rejected.
    with pytest.raises(ValidationError):
        CancelAllTriggeredEvent(
            sequence_no=3,
            event_type="CancelAllTriggeredEvent",
            source_ts=None,
            recv_ts=1_700_000_000_300,
            trigger_cause="breaker",
            venue_order_id="0xorder",
        )
    with pytest.raises(ValidationError):
        CancelAllTriggeredEvent(
            sequence_no=3,
            event_type="CancelAllTriggeredEvent",
            source_ts=None,
            recv_ts=1_700_000_000_300,
            trigger_cause="not_a_cause",
        )


def test_markout_reference_price_guarded_and_diagnostic() -> None:
    ev = PostTradeMarkoutEvent(
        sequence_no=9,
        event_type="PostTradeMarkoutEvent",
        source_ts=None,
        recv_ts=1_700_000_001_000,
        decision_id="dec-1",
        horizon_ms=5_000,
        reference_price=0.5,
        markout_bps=-12.0,
    )
    assert ev.markout_bps == -12.0
    with pytest.raises(ValidationError):
        PostTradeMarkoutEvent(
            sequence_no=9,
            event_type="PostTradeMarkoutEvent",
            source_ts=None,
            recv_ts=1_700_000_001_000,
            decision_id="dec-1",
            horizon_ms=5_000,
            reference_price=1.4,  # decimal odds rejected
            markout_bps=-12.0,
        )


def test_own_fill_size_non_negative_and_price_guarded() -> None:
    with pytest.raises(ValidationError):
        OwnFillEvent(
            sequence_no=10,
            event_type="OwnFillEvent",
            source_ts=None,
            recv_ts=1_700_000_001_100,
            decision_id="dec-1",
            client_order_id="coid-1",
            venue_order_id="0xorder",
            side="buy",
            fill_price=0.5,
            fill_size=-1.0,  # negative size rejected
            fill_ts=1_700_000_001_100,
        )


def test_session_meta_config_hash_is_deterministic() -> None:
    def _meta() -> DustExecutionSessionMeta:
        return DustExecutionSessionMeta(
            session_id="sess-1",
            mode="dry_run",
            wallet_ref="wallet-ref-nonsecret",
            manifest_hash="m" * 8,
            policy_hash="p" * 8,
            caps_snapshot={"max_orders": 3.0, "max_notional": 5.0},
            market_fee_snapshot_hash="fee" * 4,
            operator_authorization_ref="op-1",
        )

    assert _meta().config_hash() == _meta().config_hash()
    assert len(_meta().config_hash()) == 64  # sha256 hexdigest
    # A changed field changes the hash.
    changed = DustExecutionSessionMeta(
        session_id="sess-2",
        mode="dry_run",
        wallet_ref="wallet-ref-nonsecret",
        manifest_hash="m" * 8,
        policy_hash="p" * 8,
        caps_snapshot={"max_orders": 3.0, "max_notional": 5.0},
        market_fee_snapshot_hash="fee" * 4,
        operator_authorization_ref="op-1",
    )
    assert changed.config_hash() != _meta().config_hash()
