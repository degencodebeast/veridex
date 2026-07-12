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

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from veridex.dust_execution.contracts import PreSubmitRecord
from veridex.dust_execution.l2_transport import (
    FillHistoryReader,
    InMemoryPreSubmitStore,
    ReconciledFill,
    reconcile_ack_lost,
)
from veridex.store import Store

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime coupling (E4-T1 injection style)
    from veridex.dust_execution.contracts import CancelAllCause
    from veridex.dust_execution.emergency import (
        CancelAllAck,
        CancelAllAdapter,
        DustSafetySession,
        SafetyController,
    )
    from veridex.venues.base import VenueReconciliationReads

#: The injected wire POST boundary — a zero-arg async callable returning the venue response. ONLY a
#: recording-fake in tests (never a live venue); Mode B is UNARMED.
WirePost = Callable[[], Awaitable[dict[str, Any]]]

#: The tri-state uncertain-submit verdict (IDM-002, AC-011/035). MUTUALLY EXCLUSIVE — exactly one.
#: NOTE: the underscore spelling is the E4-T2 in-code verdict form mandated by this task's signature;
#: the persisted-event form on :class:`~veridex.dust_execution.contracts.UncertainState` uses the
#: hyphenated ``DEFINITIVELY-ABSENT`` — the mapping is a deliberate boundary, not a divergence.
UncertainSubmitState = Literal["RESOLVED", "DEFINITIVELY_ABSENT", "AMBIGUOUS"]

#: get_order-by-id statuses that are POSITIVE TERMINAL evidence the uncertain submit is RESOLVED.
#: A resting/live order (``"live"``/``"open"``) is NOT terminal — it stays uncertain (AMBIGUOUS).
_TERMINAL_ORDER_STATUSES: frozenset[str] = frozenset(
    {"filled", "matched", "killed", "canceled", "cancelled", "expired", "rejected"}
)


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


# ---------------------------------------------------------------------------
# E4-T2 — tri-state uncertain-submit reconciliation vs COMPLETE VENUE TRUTH
# (IDM-002, AC-011/035, §6 group 3). The double-exposure guard.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UncertainSubmitVerdict:
    """The full evidence-bearing verdict behind :func:`reconcile_uncertain_submit`.

    Carries the MUTUALLY-EXCLUSIVE tri-state plus the evidence that produced it: which of the three
    complete-venue-truth surfaces were queried (proving all three ran BEFORE any retry), the matched
    fill size from history, and the count of indistinguishable open candidates. The bare
    :func:`reconcile_uncertain_submit` returns only :attr:`state`; this record is the auditable form.
    """

    state: UncertainSubmitState
    venue_order_key: str
    surfaces_queried: tuple[str, ...]
    matched_fill_size: float
    open_candidate_count: int


def _fill_size_for_key(trades: Iterable[Mapping[str, Any]], venue_order_key: str) -> float | None:
    """Sum matched sizes for trades whose OFFICIAL id equals ``venue_order_key`` (``None`` if none).

    Matches ONLY on the venue's official ids (``taker_order_id`` when we are taker, or a
    ``maker_orders[].order_id`` when we are the resting maker) — NEVER on Veridex's private
    ``client_order_id`` (which is never on the wire). Mirrors the E3-T8 restart-join matcher; kept
    local so this module consumes the read surfaces without depending on a private helper.
    """
    matched = 0.0
    found = False
    for trade in trades:
        if str(trade.get("taker_order_id", "")) == venue_order_key:
            matched += float(trade.get("size", 0.0) or 0.0)
            found = True
            continue
        for maker in trade.get("maker_orders", []) or []:
            if str(maker.get("order_id", "")) == venue_order_key:
                matched += float(maker.get("matched_amount", 0.0) or 0.0)
                found = True
    return matched if found else None


async def _fill_history_evidence(
    adapter: VenueReconciliationReads, venue_order_key: str
) -> float | None:
    """Query ``get_fill_history`` keyed by the official id: matched size, or ``None`` (no proof).

    FAIL-CLOSED: a missing surface (the adapter lacks ``get_fill_history``) or a reader that raises
    yields ``None`` — "no proof of a fill", never a fabricated absence. This is exactly what keeps a
    taker with an unavailable history surface AMBIGUOUS and NEVER ``DEFINITIVELY_ABSENT``.
    """
    reader = getattr(adapter, "get_fill_history", None)
    if reader is None:
        return None
    try:
        page = await reader(venue_order_key=venue_order_key)
    except Exception:
        return None
    trades = (page.get("trades") or page.get("data") or []) if isinstance(page, Mapping) else page
    return _fill_size_for_key(trades or [], venue_order_key)


async def _terminal_status_evidence(
    adapter: VenueReconciliationReads, venue_order_key: str
) -> bool:
    """Query ``get_order`` by the official id: ``True`` iff it reports a POSITIVE TERMINAL status.

    A resting/live record (``status == "live"``) is NOT terminal → ``False``. FAIL-CLOSED: a missing
    surface, a raising reader, or an empty/absent record yields ``False`` (no terminal proof).
    """
    getter = getattr(adapter, "get_order", None)
    if getter is None:
        return False
    try:
        record = await getter(venue_order_key)
    except Exception:
        return False
    if not record:
        return False
    return str(record.get("status", "")).lower() in _TERMINAL_ORDER_STATUSES


async def _open_candidate_count(adapter: VenueReconciliationReads) -> int:
    """Query ``get_orders``: how many open orders could be ours (indistinguishable candidates).

    ``client_order_id`` is Veridex-LOCAL and never on the wire, so every open order is an
    indistinguishable candidate; the COUNT feeds the worst-case exposure accounting. FAIL-CLOSED: a
    missing/raising surface yields ``0`` (it never manufactures proof of absence on its own).
    """
    getter = getattr(adapter, "get_orders", None)
    if getter is None:
        return 0
    try:
        orders = await getter()
    except Exception:
        return 0
    return len(orders or [])


async def assess_uncertain_submit(
    presubmit_record: PreSubmitRecord, *, adapter: VenueReconciliationReads
) -> UncertainSubmitVerdict:
    """Classify an ACK-lost/uncertain submit against COMPLETE VENUE TRUTH (IDM-002, AC-011/035).

    Queries ALL THREE E3-T2 read surfaces — ``get_orders`` (is it resting?), ``get_order``-by-id
    (terminal status?), ``get_fill_history`` (did it fill?) — keyed by the OFFICIAL
    ``venue_order_key``, BEFORE any retry, and returns the MUTUALLY-EXCLUSIVE
    :class:`UncertainSubmitVerdict`:

    * ``RESOLVED`` — POSITIVE terminal evidence: a fill in history keyed by ``venue_order_key`` OR a
      terminal ``get_order`` status. An ACK-lost FAK that filled before the poll (absent from open
      orders but present in fill history) is ``RESOLVED``, NEVER "did not land".
    * ``AMBIGUOUS`` — everything else (fail-closed): bare zero-open, multiple indistinguishable
      candidates, a still-live resting order, or a missing/unavailable history surface. "I see no
      open order" is NOT proof the order never existed (it may have filled-and-closed in the gap).
    * ``DEFINITIVELY_ABSENT`` — a member of the closed set, but the POSITIVE-evidence path that
      yields it is deferred to E4-T6; this function NEVER fabricates it.

    Args:
        presubmit_record: The durable pre-submit record; its ``venue_order_key`` is the join key.
        adapter: A complete-venue-truth reader structurally matching
            :class:`~veridex.venues.base.VenueReconciliationReads` (consumed READ-ONLY).

    Returns:
        The evidence-bearing :class:`UncertainSubmitVerdict`.
    """
    key = presubmit_record.venue_order_key
    surfaces_queried: tuple[str, ...] = ("get_orders", "get_order", "get_fill_history")

    if not key:
        # No official join key → nothing can be queried by it → fail-closed (never a definitive verdict).
        return UncertainSubmitVerdict(
            state="AMBIGUOUS",
            venue_order_key=key,
            surfaces_queried=(),
            matched_fill_size=0.0,
            open_candidate_count=0,
        )

    # Query ALL THREE surfaces keyed by the official id BEFORE any retry (order-independent evidence).
    candidate_count = await _open_candidate_count(adapter)
    terminal = await _terminal_status_evidence(adapter, key)
    matched_size = await _fill_history_evidence(adapter, key)

    # RESOLVED requires POSITIVE TERMINAL evidence; everything else is AMBIGUOUS (fail-closed).
    if matched_size is not None or terminal:
        state: UncertainSubmitState = "RESOLVED"
    else:
        state = "AMBIGUOUS"

    return UncertainSubmitVerdict(
        state=state,
        venue_order_key=key,
        surfaces_queried=surfaces_queried,
        matched_fill_size=matched_size or 0.0,
        open_candidate_count=candidate_count,
    )


async def reconcile_uncertain_submit(
    presubmit_record: PreSubmitRecord, *, adapter: VenueReconciliationReads
) -> UncertainSubmitState:
    """Return the tri-state uncertain-submit verdict vs complete venue truth (IDM-002, AC-011/035).

    The mandated entry point (see :func:`assess_uncertain_submit` for the full evidence + the exact
    RESOLVED/AMBIGUOUS/DEFINITIVELY_ABSENT rules). Queries ``get_orders`` ∪ ``get_order``-by-id ∪
    ``get_fill_history`` keyed by the official ``venue_order_key`` BEFORE any retry and returns the
    MUTUALLY-EXCLUSIVE verdict. AMBIGUOUS callers MUST honor :func:`freezes_new_submits` (no retry)
    and reserve :func:`worst_case_uncertain_exposure`.

    Args:
        presubmit_record: The durable pre-submit record whose ``venue_order_key`` is the join key.
        adapter: A complete-venue-truth reader (consumed READ-ONLY).

    Returns:
        One of ``"RESOLVED"`` / ``"DEFINITIVELY_ABSENT"`` / ``"AMBIGUOUS"``.
    """
    verdict = await assess_uncertain_submit(presubmit_record, adapter=adapter)
    return verdict.state


def freezes_new_submits(state: UncertainSubmitState) -> bool:
    """``True`` iff ``state`` FREEZES new submits: only ``AMBIGUOUS`` does (no retry, AC-035).

    A possibly-live AMBIGUOUS order must NOT be re-sent — retrying it risks a double fill. A RESOLVED
    or DEFINITIVELY_ABSENT order carries no such freeze.
    """
    return state == "AMBIGUOUS"


def worst_case_uncertain_exposure(
    state: UncertainSubmitState, *, intended_size: float
) -> float:
    """Exposure a still-uncertain submit must RESERVE against caps + non-crossing (AC-035).

    ``AMBIGUOUS`` reserves the FULL ``intended_size`` (worst case: the order may be fully live or
    fully filled) so a possibly-live order is NEVER double-committed. ``DEFINITIVELY_ABSENT`` reserves
    nothing; a ``RESOLVED`` order's real matched size is accounted by its reconciled record, so its
    uncertainty reserve is released (``0.0``).

    Args:
        state: The tri-state verdict from :func:`reconcile_uncertain_submit`.
        intended_size: The size the uncertain order was submitted for.

    Returns:
        The worst-case size to reserve against caps + the non-crossing check.
    """
    if state == "AMBIGUOUS":
        return float(intended_size)
    return 0.0


# ---------------------------------------------------------------------------
# E4-T3 — AMBIGUOUS freeze + bounded venue-truth polling + cancel-all timeout
# fallback (IDM-002, AC-011, §6 group 3). The no-blind-retry safety loop.
# ---------------------------------------------------------------------------
#
# Builds directly on E4-T2: while :func:`reconcile_uncertain_submit` reports AMBIGUOUS the
# wallet/market is FROZEN (no new submit), and the three-surface reconcile is re-run on a bounded
# schedule against an INJECTED clock + INJECTED async sleep until it resolves — each poll RE-QUERIES
# venue truth by ``venue_order_key`` and NEVER re-submits. On timeout the loop falls back to the
# E2-T3 :meth:`~veridex.dust_execution.emergency.SafetyController.cancel_all_and_block` SWEEP
# (+ manual operator escalation), NEVER a blind retry — a blind re-POST is the exact double-exposure
# bug this guards.

#: The injected monotonic clock (integer ms) the bounded poll measures its timeout against. Injected
#: so the loop is DETERMINISTIC and fully offline in tests — mirrors the E2-T3 clock-injection style.
Clock = Callable[[], int]

#: The injected async sleep between poll ticks. In tests it advances the injected clock (not real
#: wall time), so the bounded schedule runs offline with no real ``asyncio.sleep``.
AsyncSleep = Callable[[float], Awaitable[None]]

#: The venue-truth re-query the poll re-runs each tick. It RE-QUERIES by ``venue_order_key`` across
#: the three E4-T2 surfaces and NEVER re-submits; defaults to :func:`reconcile_uncertain_submit`
#: bound to the record + adapter.
UncertainPoll = Callable[[], Awaitable[UncertainSubmitState]]

#: How the AMBIGUOUS freeze was left: the order resolved (``RESOLVED`` / ``DEFINITIVELY_ABSENT``, the
#: freeze lifts cleanly) or the bounded poll timed out and the loop fell back to the cancel-all sweep
#: (``SWEPT_ON_TIMEOUT`` — NEVER a blind retry).
AmbiguousOutcome = Literal["RESOLVED", "DEFINITIVELY_ABSENT", "SWEPT_ON_TIMEOUT"]


@dataclass(frozen=True)
class AmbiguousResolution:
    """The auditable outcome of one AMBIGUOUS freeze + bounded-poll + timeout-fallback loop.

    Attributes:
        outcome: How the freeze was left — a clean resolution (``RESOLVED`` /
            ``DEFINITIVELY_ABSENT``) or the timeout sweep (``SWEPT_ON_TIMEOUT``).
        polls: How many times venue truth was re-queried (the bound is provable from this).
        resolved_state: The terminal tri-state the poll settled on (``AMBIGUOUS`` when swept on
            timeout — it never positively resolved).
        cancel_all_ack: The E2-T3 sweep ack — set ONLY on the timeout fallback, ``None`` on a clean
            resolution (a clean lift never sweeps).
        elapsed_ms: Injected-clock elapsed time from the first poll to the exit.
    """

    outcome: AmbiguousOutcome
    polls: int
    resolved_state: UncertainSubmitState
    cancel_all_ack: CancelAllAck | None
    elapsed_ms: int


def retry_authorized_while(state: UncertainSubmitState, *, before_timeout: bool) -> bool:
    """``True`` iff a NEW submit is authorized for ``state`` at this phase — never while AMBIGUOUS.

    The double-exposure guard, in predicate form. A possibly-live AMBIGUOUS order is FROZEN, so a
    blind re-submit is authorized in NEITHER phase: not while the bounded poll is still running
    (``before_timeout=True``, frozen) and not once it elapses (``before_timeout=False`` — the
    fallback is the cancel-all sweep, never a retry). Any other state (``RESOLVED`` /
    ``DEFINITIVELY_ABSENT``) has lifted the freeze, so the wallet/market is free to submit again.

    Args:
        state: The tri-state verdict from :func:`reconcile_uncertain_submit`.
        before_timeout: Whether the bounded poll is still within its window; retained to make the
            invariant explicit — AMBIGUOUS authorizes no retry in EITHER phase.

    Returns:
        Whether a new submit/retry is authorized (always ``False`` for AMBIGUOUS).
    """
    # ``before_timeout`` is part of the contract but does NOT relax the guard: AMBIGUOUS authorizes
    # no retry in EITHER phase (frozen before timeout; cancel-all — not retry — on timeout).
    del before_timeout
    return state != "AMBIGUOUS"


async def resolve_ambiguous_submit(
    presubmit_record: PreSubmitRecord,
    *,
    adapter: VenueReconciliationReads,
    controller: SafetyController,
    session: DustSafetySession,
    cancel_adapter: CancelAllAdapter,
    clock_ms: Clock,
    sleep: AsyncSleep,
    poll_interval_ms: int,
    timeout_ms: int,
    timeout_cause: CancelAllCause = "manual",
    poll_fn: UncertainPoll | None = None,
) -> AmbiguousResolution:
    """Freeze on AMBIGUOUS, poll venue truth on a bounded schedule, and on timeout SWEEP not retry.

    The no-blind-retry safety loop (IDM-002, AC-011). While the E4-T2 three-surface reconcile reports
    AMBIGUOUS the wallet/market is FROZEN — no new submit is authorized (:func:`retry_authorized_while`).
    On the INJECTED clock, the reconcile is re-run every ``poll_interval_ms`` (each poll RE-QUERIES
    venue truth by ``venue_order_key`` — it does NOT re-submit) until it resolves to a non-AMBIGUOUS
    terminal (``RESOLVED`` / ``DEFINITIVELY_ABSENT``), which lifts the freeze CLEANLY (no sweep), or
    ``timeout_ms`` elapses. On timeout the loop falls back to the E2-T3
    :meth:`~veridex.dust_execution.emergency.SafetyController.cancel_all_and_block` SWEEP under
    ``timeout_cause`` (which also blocks new submits and hands off to manual operator intervention) —
    it NEVER blindly re-POSTs the order.

    Args:
        presubmit_record: The durable pre-submit record whose ``venue_order_key`` is re-queried.
        adapter: The complete-venue-truth reader the default poll re-queries (consumed READ-ONLY).
        controller: The E2-T3 emergency orchestrator whose single cancel-all primitive the timeout
            fallback routes through.
        session: The mutable runtime session the sweep blocks (its submit-block flag).
        cancel_adapter: The venue cancel-all seam the timeout sweep fires.
        clock_ms: Injected monotonic clock (integer ms) the timeout is measured against.
        sleep: Injected async sleep between poll ticks (advances the injected clock in tests).
        poll_interval_ms: Delay between re-queries (the bounded poll cadence).
        timeout_ms: The freeze budget; once elapsed, the loop sweeps instead of retrying.
        timeout_cause: The honest E2-T3 cause the timeout sweep is labelled with (defaults to
            ``"manual"`` — the sweep hands off to manual operator intervention).
        poll_fn: Override for the venue-truth re-query (defaults to :func:`reconcile_uncertain_submit`
            bound to the record + adapter); it RE-QUERIES, it never re-submits.

    Returns:
        The auditable :class:`AmbiguousResolution` describing how the freeze was left.
    """

    async def _default_poll() -> UncertainSubmitState:
        # RE-QUERY venue truth by venue_order_key across all three surfaces — never a re-submit.
        return await reconcile_uncertain_submit(presubmit_record, adapter=adapter)

    poll: UncertainPoll = poll_fn if poll_fn is not None else _default_poll

    start = clock_ms()
    polls = 0
    while True:
        state = await poll()
        polls += 1

        if state != "AMBIGUOUS":
            # RESOLVED or DEFINITIVELY_ABSENT: only AMBIGUOUS holds the freeze, so it lifts CLEANLY —
            # NO cancel-all sweep, NO retry.
            return AmbiguousResolution(
                outcome=state,
                polls=polls,
                resolved_state=state,
                cancel_all_ack=None,
                elapsed_ms=clock_ms() - start,
            )

        # Still AMBIGUOUS → FROZEN (no new submit authorized). Bounded: on timeout, SWEEP not retry.
        if clock_ms() - start >= timeout_ms:
            ack = await controller.cancel_all_and_block(
                timeout_cause, adapter=cancel_adapter, session=session
            )
            return AmbiguousResolution(
                outcome="SWEPT_ON_TIMEOUT",
                polls=polls,
                resolved_state="AMBIGUOUS",
                cancel_all_ack=ack,
                elapsed_ms=clock_ms() - start,
            )

        # Wait one bounded tick, then RE-QUERY (never re-submit) again.
        await sleep(poll_interval_ms / 1000.0)


# ---------------------------------------------------------------------------
# E4-T4 — a duplicate client_order_id (or a re-submit after reconciliation)
# creates NO duplicate live exposure (IDM-003, AC-012, §6 group 3).
# ---------------------------------------------------------------------------
#
# The double-submit dedup guard. ``client_order_id`` is Veridex-LOCAL and NEVER on the wire (IDM-001),
# so the venue has no record keyed by it — dedup by comparing ``client_order_id``s is structurally
# impossible (there is nothing on the venue side to compare against). The ONLY sound dedup is against
# COMPLETE VENUE TRUTH: reconcile the PRIOR submit's ``venue_order_key`` through the E4-T2 three-surface
# reconcile (:func:`reconcile_uncertain_submit`) BEFORE authorizing the (re-)submit. If the prior is
# already live/filled the duplicate is DENIED (no second live order); a fresh submit is authorized
# ONLY when the prior is DEFINITIVELY_ABSENT — RESOLVED (live/filled) and AMBIGUOUS (frozen per E4-T3)
# both DENY. This is STRICTER than :func:`retry_authorized_while` (which frees a RESOLVED order for a
# NEW, different intent): re-using the SAME id while its prior is RESOLVED would duplicate exposure.

#: The auditable reason a duplicate/re-submit was authorized or denied — pinned to a closed set so the
#: decision provenance can never be a free-form afterthought.
ResubmitDecisionReason = Literal[
    "AUTHORIZED_PRIOR_DEFINITIVELY_ABSENT",
    "DENIED_PRIOR_RESOLVED_LIVE_OR_FILLED",
    "DENIED_PRIOR_AMBIGUOUS_FROZEN",
]


def duplicate_resubmit_authorized(prior_state: UncertainSubmitState) -> bool:
    """``True`` iff a duplicate/re-submit is authorized for ``prior_state``: ONLY DEFINITIVELY_ABSENT.

    The no-duplicate-exposure guard, in predicate form (IDM-003, AC-012). Re-using a
    ``client_order_id`` (or re-submitting after reconciliation) is authorized ONLY when the prior
    order provably never landed (``DEFINITIVELY_ABSENT``). A ``RESOLVED`` prior is live/filled — a
    second submit would create a SECOND live exposure — and an ``AMBIGUOUS`` prior is possibly-live and
    frozen (E4-T3); BOTH deny. This is intentionally stricter than :func:`retry_authorized_while`,
    which frees a RESOLVED order for a NEW, different intent (not a duplicate of the same id).

    Args:
        prior_state: The tri-state verdict the prior submit reconciled to against venue truth.

    Returns:
        Whether a duplicate/re-submit is authorized (always ``False`` unless DEFINITIVELY_ABSENT).
    """
    return prior_state == "DEFINITIVELY_ABSENT"


@dataclass(frozen=True)
class ResubmitAuthorization:
    """The auditable verdict of the double-submit dedup guard (IDM-003, AC-012).

    Attributes:
        authorized: Whether the (re-)submit is authorized — ``True`` ONLY for a DEFINITIVELY_ABSENT
            prior (no duplicate live exposure).
        prior_state: The tri-state the PRIOR submit reconciled to against complete venue truth (the
            basis of the decision — derived from ``venue_order_key``, never from the local id).
        venue_order_key: The official venue join key the prior was reconciled by (proving the dedup is
            keyed on venue truth, not on the Veridex-local ``client_order_id``).
        reason: The closed-set decision provenance.
    """

    authorized: bool
    prior_state: UncertainSubmitState
    venue_order_key: str
    reason: ResubmitDecisionReason


async def authorize_resubmit_against_venue_truth(
    prior_record: PreSubmitRecord,
    *,
    client_order_id: str,
    adapter: VenueReconciliationReads,
    reconcile_fn: UncertainPoll | None = None,
) -> ResubmitAuthorization:
    """Authorize a duplicate/re-submit ONLY if the prior reconciles DEFINITIVELY_ABSENT (IDM-003).

    The double-submit dedup guard. It CONSULTS complete venue truth BEFORE authorizing: the PRIOR
    submit's ``prior_record.venue_order_key`` is reconciled through the E4-T2 three-surface reconcile
    (``get_orders`` ∪ ``get_order``-by-id ∪ ``get_fill_history``, keyed by the OFFICIAL id), and a
    (re-)submit re-using ``client_order_id`` is authorized ONLY when that prior reconciles
    DEFINITIVELY_ABSENT (:func:`duplicate_resubmit_authorized`). A RESOLVED (live/filled) or AMBIGUOUS
    (possibly-live, frozen) prior DENIES the duplicate, so a second live order is never created.

    ``client_order_id`` is IDM-001 Veridex-LOCAL — it is NEVER on the wire and NEVER used as a lookup
    key here (the venue has no record keyed by it); it is carried only for the caller's audit trail.
    The dedup is driven ENTIRELY by the reconciled venue-truth state.

    Args:
        prior_record: The durable pre-submit record of the PRIOR order whose ``venue_order_key`` is
            reconciled against venue truth (the join key — never the local id).
        client_order_id: The Veridex-local id being re-used (audit only; NEVER a venue lookup key).
        adapter: A complete-venue-truth reader (consumed READ-ONLY) the default reconcile queries.
        reconcile_fn: Override for the venue-truth reconcile (defaults to
            :func:`reconcile_uncertain_submit` bound to ``prior_record`` + ``adapter``). The
            positive-evidence path that yields DEFINITIVELY_ABSENT is deferred to E4-T6, so tests
            inject that terminal here; it RE-QUERIES venue truth, it NEVER re-submits.

    Returns:
        The auditable :class:`ResubmitAuthorization`.
    """
    # ``client_order_id`` is Veridex-LOCAL (IDM-001) — it is deliberately NOT consulted for the dedup
    # decision; the venue has never seen it, so no venue record is keyed by it. Retained for audit.
    del client_order_id

    async def _default_reconcile() -> UncertainSubmitState:
        # Reconcile the PRIOR order by its OFFICIAL venue_order_key across the three surfaces — this is
        # the venue-truth dedup key, NEVER the Veridex-local client_order_id.
        return await reconcile_uncertain_submit(prior_record, adapter=adapter)

    reconcile: UncertainPoll = reconcile_fn if reconcile_fn is not None else _default_reconcile

    prior_state = await reconcile()
    authorized = duplicate_resubmit_authorized(prior_state)

    if authorized:
        reason: ResubmitDecisionReason = "AUTHORIZED_PRIOR_DEFINITIVELY_ABSENT"
    elif prior_state == "RESOLVED":
        reason = "DENIED_PRIOR_RESOLVED_LIVE_OR_FILLED"
    else:
        reason = "DENIED_PRIOR_AMBIGUOUS_FROZEN"

    return ResubmitAuthorization(
        authorized=authorized,
        prior_state=prior_state,
        venue_order_key=prior_record.venue_order_key,
        reason=reason,
    )


__all__ = [
    "AmbiguousOutcome",
    "AmbiguousResolution",
    "AsyncSleep",
    "Clock",
    "DurableSubmitResult",
    "ResubmitAuthorization",
    "ResubmitDecisionReason",
    "UncertainPoll",
    "UncertainSubmitState",
    "UncertainSubmitVerdict",
    "WirePost",
    "assess_uncertain_submit",
    "authorize_resubmit_against_venue_truth",
    "duplicate_resubmit_authorized",
    "freezes_new_submits",
    "persist_presubmit_record",
    "reconcile_durable_ack_lost",
    "reconcile_uncertain_submit",
    "recover_presubmit_records",
    "resolve_ambiguous_submit",
    "retry_authorized_while",
    "submit_with_durable_presubmit",
    "worst_case_uncertain_exposure",
]
