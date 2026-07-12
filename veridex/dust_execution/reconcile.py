"""E4-T1 — durable pre-submit record persisted BEFORE the wire POST (IDM-005, AC-040).

MONEY-NETWORK BOUNDARY. This module is PURE orchestration over an INJECTED durable store and an
INJECTED async wire POST: it holds NO credential, opens NO connection, and never imports an order
I/O client. The wire POST is a caller-supplied ``post`` callable (a recording-fake in tests, never
a live venue); Mode B is UNARMED here.

Why this module exists (closes the follow-up E3-T8 explicitly flagged). E3-T8's
:class:`~veridex.dust_execution.l2_transport.KeylessL2Transport` persists the compound
:class:`~veridex.dust_execution.contracts.PreSubmitRecord`
``{integrity_commitment_hash, venue_order_key, captured_id?}`` to an INJECTED in-memory
:class:`~veridex.dust_execution.l2_transport.PreSubmitStore` BEFORE the POST, and reconciles an
ACK-lost fill by ``venue_order_key`` — but E3-T8 flagged that the DURABLE backing was a follow-up.
This module provides that durable backing on top of the E2-T2 append-only ledger pattern
(:meth:`veridex.store.Store.append_presubmit` / :meth:`~veridex.store.Store.list_presubmit`,
INSERT-only, unique-seq, never updated/deleted):

  * :func:`submit_with_durable_presubmit` writes the durable record as the FIRST side-effect, BEFORE
    the wire POST is attempted (IDM-005) — so the order is identifiable in complete venue truth even
    if the ACK is lost or it never appears in open orders;
  * :func:`recover_presubmit_records` rebuilds the compound records from the persisted rows after a
    restart (a fresh read over the durable rows — the E2-T2 reconstruction pattern);
  * :func:`reconcile_durable_ack_lost` recovers the durable records and delegates to E3-T8's proven
    :func:`~veridex.dust_execution.l2_transport.reconcile_ack_lost`, which joins fill history ONLY by
    ``venue_order_key`` (the official V2 order hash) — NEVER by the private integrity digest.

The record shape here is EXACTLY E3-T8's compound record (from
:mod:`veridex.dust_execution.contracts`) — compatible, not divergent. Only the non-secret fields are
persisted: the one-way ``integrity_commitment_hash`` (a sha256 digest the venue has never seen; the
raw ``owner`` cannot be recovered from it), the public ``venue_order_key``, and an optional captured
id. NO raw owner / L2 cred / signature is ever written.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from veridex.dust_execution.contracts import PreSubmitRecord
from veridex.dust_execution.l2_transport import (
    FillHistoryReader,
    InMemoryPreSubmitStore,
    ReconciledFill,
    reconcile_ack_lost,
)
from veridex.store import Store

#: The injected wire POST boundary — a zero-arg async callable returning the venue response. ONLY a
#: recording-fake in tests (never a live venue); Mode B is UNARMED.
WirePost = Callable[[], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class DurableSubmitResult:
    """The result of one persist-before-POST submit: the durable seq + the record + the response."""

    presubmit_seq: int
    presubmit_record: PreSubmitRecord
    response: dict[str, Any]


async def persist_presubmit_record(
    store: Store, *, session_id: str, record: PreSubmitRecord
) -> int:
    """Persist the compound pre-submit record to the durable append-only ledger; return its ``seq``.

    Writes ONLY the non-secret fields of ``record`` (the one-way integrity digest, the public venue
    join key, and the optional captured id) — the durable store column set structurally cannot hold a
    raw owner / cred / signature.

    Args:
        store: The durable append-only store (IDM-005 ``presubmit_ledger``).
        session_id: Session identity the record belongs to.
        record: The compound :class:`~veridex.dust_execution.contracts.PreSubmitRecord` to persist.

    Returns:
        The store-assigned monotonic unique ``seq`` for the appended row.
    """
    return await store.append_presubmit(
        session_id=session_id,
        integrity_commitment_hash=record.integrity_commitment_hash,
        venue_order_key=record.venue_order_key,
        captured_id=record.captured_id,
    )


async def recover_presubmit_records(
    store: Store, session_id: str
) -> tuple[PreSubmitRecord, ...]:
    """Rebuild the compound pre-submit records from the durable rows (restart-safe fresh read).

    Reconstructs each :class:`~veridex.dust_execution.contracts.PreSubmitRecord` from its OWN
    persisted row (never a submit-time in-memory object), mirroring the E2-T2 realized-fill ledger
    reconstruction — so a record recovered after a "restart" (a new store view over the same rows)
    carries the exact durable ``{integrity_commitment_hash, venue_order_key, captured_id}``.

    Args:
        store: The durable store to read the append-only pre-submit ledger from.
        session_id: The session whose pre-submit records to recover.

    Returns:
        The reconstructed compound records, in durable ``seq`` order.
    """
    rows = await store.list_presubmit(session_id)
    return tuple(
        PreSubmitRecord(
            integrity_commitment_hash=row.integrity_commitment_hash,
            venue_order_key=row.venue_order_key,
            captured_id=row.captured_id,
        )
        for row in rows
    )


async def submit_with_durable_presubmit(
    *,
    store: Store,
    session_id: str,
    record: PreSubmitRecord,
    post: WirePost,
) -> DurableSubmitResult:
    """Persist the durable compound record BEFORE the wire POST, then run the POST (IDM-005).

    The persist is the FIRST side-effect: the durable record is written to the append-only ledger
    BEFORE ``post`` is awaited, so an ACK lost during (or after) the POST still leaves the order
    identifiable in complete venue truth. Reordering the persist to AFTER the POST breaks IDM-005 —
    the durability-before-POST test proves it.

    Args:
        store: The durable append-only store (IDM-005 ``presubmit_ledger``).
        session_id: Session identity the record belongs to.
        record: The compound :class:`~veridex.dust_execution.contracts.PreSubmitRecord` to persist.
        post: The injected async wire POST (a recording-fake in tests, never a live venue).

    Returns:
        A :class:`DurableSubmitResult` carrying the durable seq, the persisted record, and the
        venue response.
    """
    # PERSIST BEFORE the POST — the durable record must exist before any wire attempt (IDM-005).
    seq = await persist_presubmit_record(store, session_id=session_id, record=record)
    response = await post()
    return DurableSubmitResult(presubmit_seq=seq, presubmit_record=record, response=response)


async def reconcile_durable_ack_lost(
    store: Store, session_id: str, fill_reader: FillHistoryReader
) -> list[ReconciledFill]:
    """Reconcile durable ACK-lost records against fill history keyed by ``venue_order_key`` (AC-011).

    Recovers the durable compound records (a fresh read over the persisted rows — restart-safe) and
    delegates to E3-T8's :func:`~veridex.dust_execution.l2_transport.reconcile_ack_lost`, which joins
    fill history ONLY by the official ``venue_order_key`` (the V2 order hash) and NEVER by Veridex's
    private integrity digest: a matching fill resolves ``RESOLVED`` (with size); no match resolves
    fail-closed to ``AMBIGUOUS`` (never fabricated). This reuses the proven matching rather than
    diverging from it (compatible with E3-T8).

    Args:
        store: The durable store to recover the pre-submit records from.
        session_id: The session whose ACK-lost records to reconcile.
        fill_reader: A reader keyed ONLY by ``venue_order_key`` (the official V2 order id).

    Returns:
        One :class:`~veridex.dust_execution.l2_transport.ReconciledFill` per recovered record.
    """
    recovered = InMemoryPreSubmitStore()
    for record in await recover_presubmit_records(store, session_id):
        recovered.append_presubmit(record)
    return await reconcile_ack_lost(recovered, fill_reader)


__all__ = [
    "DurableSubmitResult",
    "WirePost",
    "persist_presubmit_record",
    "recover_presubmit_records",
    "reconcile_durable_ack_lost",
    "submit_with_durable_presubmit",
]
