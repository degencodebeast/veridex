"""Durable operator-interlock store — ISSUES and STORE-VERIFIES the arming receipt (Gate#3 M-1).

Gate#3 MAJOR-1 follow-up: the operator-interlock proof the runner consumes must be UNFORGEABLE /
STORE-VERIFIABLE, not caller-fabricated. Presence of a recording callback (and a SELF-computed
digest) is NOT evidence that the five REQ-005/006 human-precondition events were durably persisted:
a direct caller of the public runner could otherwise construct
``OperatorInterlockProof(True, "forged:anything")`` and arm real money, and a no-op
``lambda event: None`` sink could arm while persisting nothing.

This module closes that with the SAME append-only ledger idiom the lane already uses for the
E2-T2 pre-submit store (:class:`~veridex.dust_execution.l2_transport.PreSubmitStore` +
:class:`~veridex.dust_execution.l2_transport.InMemoryPreSubmitStore`): a narrow
:class:`OperatorInterlockStore` ``Protocol`` (``record`` + ``verify``) with a minimal append-only
in-memory fake for offline tests.

The store ISSUES a receipt BOUND to ``(session_id, ordered event CONTENT, operator authorization,
arming attempt)`` and later VERIFIES a presented receipt against its ACTUAL rows — so an altered
event, a wrong session, a wrong arming attempt, a receipt the store never issued, or a
self-computed digest with no persisted row all FAIL to verify. Only ids / digests / non-secret refs
ever enter a row (SEC-005): never an operator secret, a live handle, or a key.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from veridex.dust_execution.contracts import OperatorInterlockEvent


@dataclass(frozen=True)
class OperatorInterlockRow:
    """One durably-appended interlock record: the bound content, its digest, and the issued receipt.

    Carries the ordered ``events`` (the REQ-005 audit trail — non-secret refs/bools only, SEC-005),
    the ``binding_digest`` committing to ``(session_id, events, operator authorization, arming
    attempt)``, and the store-ISSUED ``receipt``. Never a secret, key, or live handle.
    """

    session_id: str
    events: tuple[OperatorInterlockEvent, ...]
    operator_authorization_ref: str | None
    arming_attempt_ref: str
    binding_digest: str
    receipt: str


def interlock_binding_digest(
    *,
    session_id: str,
    events: tuple[OperatorInterlockEvent, ...],
    operator_authorization_ref: str | None,
    arming_attempt_ref: str,
) -> str:
    """sha256 over the ORDERED event content + the session / operator-auth / arming-attempt it binds.

    The digest is the CONTENT COMMITMENT the receipt is issued against: any change to the session,
    the ordered ``(sequence_no, precondition, satisfied, operator_authorization_ref,
    first_order_authorized)`` event content, the operator-authorization ref, or the arming-attempt
    ref yields a DIFFERENT digest (and therefore a receipt the store never issued for this run). It
    is a REFERENCE digest over non-secret fields only — never a secret or a live handle (SEC-005).
    """
    canonical = json.dumps(
        {
            "session_id": session_id,
            "operator_authorization_ref": operator_authorization_ref,
            "arming_attempt_ref": arming_attempt_ref,
            "events": [
                (
                    event.sequence_no,
                    event.precondition,
                    event.satisfied,
                    event.operator_authorization_ref,
                    event.first_order_authorized,
                )
                for event in events
            ],
        },
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@runtime_checkable
class OperatorInterlockStore(Protocol):
    """Durable, append-only store that ISSUES and STORE-VERIFIES the operator-interlock receipt.

    ``record`` durably APPENDS the interlock for this run and returns a store-ISSUED receipt bound to
    ``(session_id, events, operator_authorization_ref, arming_attempt_ref)``. ``verify`` returns
    ``True`` ONLY when the store ACTUALLY recorded exactly that bound content AND issued that exact
    receipt — a forged/never-issued receipt, a self-computed digest with no persisted row, a
    wrong-session/altered-event/wrong-attempt binding all return ``False``. The concrete store is
    INJECTED (like the E2-T2 pre-submit store); tests use :class:`InMemoryOperatorInterlockStore`.
    """

    def record(
        self,
        *,
        session_id: str,
        events: tuple[OperatorInterlockEvent, ...],
        operator_authorization_ref: str | None,
        arming_attempt_ref: str,
    ) -> str: ...

    def verify(
        self,
        *,
        session_id: str,
        events: tuple[OperatorInterlockEvent, ...],
        operator_authorization_ref: str | None,
        arming_attempt_ref: str,
        receipt: str,
    ) -> bool: ...


class InMemoryOperatorInterlockStore:
    """A minimal append-only in-memory :class:`OperatorInterlockStore` (E2-T2 ledger pattern).

    Deliberately append-only (no update/delete) so a persisted interlock row can never be silently
    rewritten. A row is the ``(binding_digest, receipt)`` pair the store ISSUED at ``record`` time;
    ``verify`` re-derives the binding digest from the presented content and confirms an ACTUAL row
    exists whose digest AND issued receipt both match — so callback presence or a self-computed
    digest (with no persisted row) is never mistaken for a durable write.
    """

    def __init__(self) -> None:
        self._rows: list[OperatorInterlockRow] = []

    def record(
        self,
        *,
        session_id: str,
        events: tuple[OperatorInterlockEvent, ...],
        operator_authorization_ref: str | None,
        arming_attempt_ref: str,
    ) -> str:
        digest = interlock_binding_digest(
            session_id=session_id,
            events=events,
            operator_authorization_ref=operator_authorization_ref,
            arming_attempt_ref=arming_attempt_ref,
        )
        # The store ISSUES the receipt (an opaque ref committing to the bound content); it is only
        # ever meaningful because the store ALSO persists the row it is bound to.
        receipt = f"operator-interlock:{session_id}:{digest[:32]}"
        self._rows.append(
            OperatorInterlockRow(
                session_id=session_id,
                events=events,
                operator_authorization_ref=operator_authorization_ref,
                arming_attempt_ref=arming_attempt_ref,
                binding_digest=digest,
                receipt=receipt,
            )
        )
        return receipt

    def verify(
        self,
        *,
        session_id: str,
        events: tuple[OperatorInterlockEvent, ...],
        operator_authorization_ref: str | None,
        arming_attempt_ref: str,
        receipt: str,
    ) -> bool:
        digest = interlock_binding_digest(
            session_id=session_id,
            events=events,
            operator_authorization_ref=operator_authorization_ref,
            arming_attempt_ref=arming_attempt_ref,
        )
        return any(row.binding_digest == digest and row.receipt == receipt for row in self._rows)

    def rows(self) -> tuple[OperatorInterlockRow, ...]:
        """The durably-appended rows (append-only); the recorded REQ-005 audit trail is on each row."""
        return tuple(self._rows)


__all__ = [
    "InMemoryOperatorInterlockStore",
    "OperatorInterlockRow",
    "OperatorInterlockStore",
    "interlock_binding_digest",
]
