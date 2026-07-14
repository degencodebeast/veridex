"""Gate#3 MAJOR-1: the operator-interlock store validates the canonical-5 event SEMANTICS.

The M-1 fold made the interlock receipt store-ISSUED + store-VERIFIED (closing the authenticity axes:
forged / wrong-session / altered / never-issued). But ``record`` issued a receipt over ANY ``events``
tuple with NO semantic check — so a genuine receipt could certify FIVE FALSE operator-interlock
events. These tests pin the DEFENCE-IN-DEPTH layer: ``record`` REFUSES to issue a receipt over
non-canonical events, and the shared :func:`interlock_events_are_canonical` validator accepts exactly
the armed emission and rejects every malformed / semantically-false set.
"""

from __future__ import annotations

import pytest

from veridex.dust_execution.contracts import OPERATOR_PRECONDITIONS, OperatorInterlockEvent
from veridex.dust_execution.operator_interlock_store import (
    InMemoryOperatorInterlockStore,
    interlock_events_are_canonical,
)

_NOW_S = 1_700_000_000
_AUTH_REF = "op-ref-1"


def _canonical_events(
    *, operator_authorization_ref: str = _AUTH_REF
) -> tuple[OperatorInterlockEvent, ...]:
    """The five fully-satisfied REQ-005 audit-trail events, in the fixed precondition order (exactly
    what the facade's ``evaluate_operator_interlock`` emits for a fully-satisfied, armed interlock)."""
    return tuple(
        OperatorInterlockEvent(
            sequence_no=index,
            event_type="OperatorInterlockEvent",
            source_ts=None,
            recv_ts=_NOW_S * 1000,
            precondition=name,
            satisfied=True,
            operator_authorization_ref=operator_authorization_ref,
            first_order_authorized=True,
        )
        for index, name in enumerate(OPERATOR_PRECONDITIONS, start=1)
    )


def test_validator_accepts_the_canonical_five_armed_emission() -> None:
    """POSITIVE CONTROL: the exact canonical-5 emission (all satisfied, first-order authorized,
    consistent non-empty auth ref, canonical names in order) is accepted — makes the rejections below
    non-vacuous."""
    assert interlock_events_are_canonical(_canonical_events()) is True


@pytest.mark.parametrize(
    "events",
    [
        pytest.param(
            tuple(
                event.model_copy(update={"satisfied": False, "first_order_authorized": False})
                for event in _canonical_events()
            ),
            id="all_false",
        ),
        pytest.param(_canonical_events()[:-1], id="missing_precondition"),
        pytest.param(
            (_canonical_events()[0], _canonical_events()[0]) + _canonical_events()[2:],
            id="duplicate_precondition",
        ),
        pytest.param(
            (_canonical_events()[1], _canonical_events()[0]) + _canonical_events()[2:],
            id="reordered_preconditions",
        ),
        pytest.param(
            (_canonical_events()[0].model_copy(update={"precondition": "unknown_precondition"}),)
            + _canonical_events()[1:],
            id="unknown_precondition",
        ),
        pytest.param(
            tuple(
                event.model_copy(update={"first_order_authorized": False})
                for event in _canonical_events()
            ),
            id="first_order_unauthorized",
        ),
        pytest.param(
            tuple(
                event.model_copy(update={"operator_authorization_ref": ""})
                for event in _canonical_events()
            ),
            id="empty_operator_auth_ref",
        ),
        pytest.param(
            (_canonical_events()[0].model_copy(update={"operator_authorization_ref": "other"}),)
            + _canonical_events()[1:],
            id="inconsistent_operator_auth_ref",
        ),
        pytest.param((), id="no_events"),
    ],
)
def test_validator_rejects_non_canonical_events(
    events: tuple[OperatorInterlockEvent, ...],
) -> None:
    """Every non-canonical event set (semantically false OR structurally malformed) is rejected."""
    assert interlock_events_are_canonical(events) is False


def test_record_refuses_to_issue_a_receipt_over_all_false_events() -> None:
    """DEFENCE IN DEPTH (Codex's exact object): the REAL store must NOT issue a receipt over five
    canonical events all ``satisfied=False`` / ``first_order_authorized=False``. Before the fix
    ``record`` returned a genuine receipt (persistence integrity is NOT semantic truth); after, it
    fails closed by refusing to record.

    RED before the fix: ``record`` returns a receipt (no exception) → the ``pytest.raises`` fails.
    GREEN after: ``record`` refuses (raises) → no receipt exists to certify a false interlock.
    """
    store = InMemoryOperatorInterlockStore()
    false_events = tuple(
        event.model_copy(update={"satisfied": False, "first_order_authorized": False})
        for event in _canonical_events()
    )

    with pytest.raises(ValueError):
        store.record(
            session_id="dust-maker-v0:live_guarded",
            events=false_events,
            operator_authorization_ref=_AUTH_REF,
            arming_attempt_ref="idem-attempt-1",
        )

    assert store.rows() == (), "a refused record must leave NO durable row"


def test_record_issues_a_receipt_over_the_canonical_five() -> None:
    """POSITIVE CONTROL: the store still issues (and durably appends) a receipt for the canonical-5."""
    store = InMemoryOperatorInterlockStore()
    receipt = store.record(
        session_id="dust-maker-v0:live_guarded",
        events=_canonical_events(),
        operator_authorization_ref=_AUTH_REF,
        arming_attempt_ref="idem-attempt-1",
    )
    assert receipt.startswith("operator-interlock:dust-maker-v0:live_guarded:")
    assert len(store.rows()) == 1
