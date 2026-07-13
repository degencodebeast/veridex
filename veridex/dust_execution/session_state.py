"""Durable session-state provider for the agent-callable MM facade money path (Gate#3 MAJOR-2 + R4-MAJOR-1).

The public MM facade (:func:`veridex.dust_execution.facade.propose_mm_execution`) is the
agent-callable entry point. Before the MAJOR-2 fold it drove
:func:`veridex.dust_execution.runner.run_dust_execution` with a FRESH zero
:class:`~veridex.dust_execution.risk.RiskAccumulator` and zero prior order counts on EVERY call,
so the runner's durable run/session/UTC-day order caps and realized-loss caps RESET each
invocation. Two calls with ``max_orders_per_session == 1`` both reached the keyless write port,
and a prior realized loss was never reconstructed before the next live arming (SAF-002). The M-3
fold made the RUNNER enforce those caps GIVEN honest durable inputs — this module makes the facade
SUPPLY them.

It gives the facade ONE authoritative durable session-state source: a narrow
:class:`DurableSessionStateProvider` ``Protocol`` (a BEFORE-run :meth:`~DurableSessionStateProvider.load`
+ a BEFORE-wire :meth:`~DurableSessionStateProvider.reserve` + an AFTER-run
:meth:`~DurableSessionStateProvider.settle`) plus a minimal append-only in-memory fake for offline
tests, following the SAME injected-seam idiom the lane already uses for the operator-interlock store
and the pre-submit store. The provider supplies, BEFORE any live arming:

* an operator-assigned IMMUTABLE session identity — the authoritative safety/ledger join key the
  runner runs under (NOT the provisional ``strategy_id:mode`` seam). The operator-assigned id is
  ``MMExecutionToolRequest.session_id`` (a REQUIRED, frozen field — immutable by construction); the
  provider adopts it as the durable join key and echoes it back as :attr:`DurableSessionState.session_identity`;
* the durable :class:`RiskAccumulator` carrying prior session + UTC-day realized loss
  (``reconstruct_risk`` — restart-safe, mirrors :func:`veridex.dust_execution.ledger.reconstruct_risk`);
* the persisted session/UTC-day possibly-live attempt counts — which now COUNT reserved-but-unsettled
  attempts (see below), so a possibly-live attempt consumes a cap slot the instant it is reserved.

Gate#3 R4-MAJOR-1 (crash-consistent cap): the possibly-live attempt is durably RESERVED BEFORE the
wire, not counted AFTER the run. The old ``load -> run -> record_run`` order recorded
``submitted_count`` only AFTER the fund-touching runner returned, so a durable-store failure or a
process crash AFTER the wire never landed the cap consumption — the possibly-live attempt existed at
the venue but the durable count reset, and the NEXT call submitted a SECOND order despite the cap. The
contract is now ``load -> reserve -> run -> settle`` (the durable-cap analog of the lane's
persist-BEFORE-sign discipline): :meth:`~DurableSessionStateProvider.reserve` durably appends a
possibly-live attempt row BEFORE the runner reaches the write port (its failure fails the facade
CLOSED), and :meth:`~DurableSessionStateProvider.settle` records the outcome AFTER the run —
``committed=True`` keeps the reserved attempt counted (the wire fired), ``committed=False`` RELEASES it
(no wire fired). A reserved-but-UNSETTLED row (a post-wire ``settle`` failure or a crash before
``settle``) stays COUNTED: a possibly-live attempt is conservatively held against the cap until
reconciled against pre-submit/venue truth.

R4-A's sealed lifecycle carries no ``realized_pnl`` (SEC-002), so realized-fill LOSS is reconstructed
by the provider's OWN durable venue-reconciliation ledger (fed where PnL is computed) — never
fabricated from the sealed events. SEC-005: rows carry only ids / counts / non-secret refs / bools —
never an operator secret, a key, or a live handle.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, runtime_checkable

from veridex.dust_execution.risk import RealizedFillRecord, RiskAccumulator

# The closed vocabulary the idempotent reserve-OR-load (:meth:`DurableSessionStateProvider.reserve`)
# returns for a derived ``attempt_id`` (Gate#3 R5-MAJOR-1):
#   * ``RESERVED``          — no prior row (never reserved, or a prior attempt was RELEASED): a FRESH
#                             possibly-live slot was appended; the caller may proceed to the wire;
#   * ``PENDING_RECONCILE`` — a prior UNSETTLED (possibly-live) row already exists for this stable id
#                             (an identical retry atop a crash/outage): the caller must FREEZE — no new
#                             wire atop a possibly-live first order, pending venue-truth reconcile;
#   * ``COMMITTED``         — a prior RESOLVED row exists (the first order went AND reconciled against
#                             venue truth): the caller replays that outcome idempotently — no new wire.
ReservationOutcome = Literal["RESERVED", "PENDING_RECONCILE", "COMMITTED"]

# The closed vocabulary the ATOMIC reserve-or-freeze (:meth:`DurableSessionStateProvider.reserve_or_freeze`)
# returns (Gate#3 R6-MAJOR-2). It fuses the session-scope freeze check with the reserve-or-load into ONE
# critical section, so it adds ``SCOPE_FROZEN`` to :data:`ReservationOutcome`:
#   * ``RESERVED`` / ``PENDING_RECONCILE`` / ``COMMITTED`` — as in :data:`ReservationOutcome` (a fresh
#                             possibly-live slot, a same-id retry atop an unsettled row, or a same-id
#                             replay atop a resolved row);
#   * ``SCOPE_FROZEN``      — a DIFFERENT attempt_id while an UNRESOLVED possibly-live reservation already
#                             occupies the session freeze scope: the caller must FREEZE (no new wire),
#                             pending venue-truth reconcile. No new row is appended.
ScopedReservationOutcome = Literal[
    "RESERVED", "PENDING_RECONCILE", "COMMITTED", "SCOPE_FROZEN"
]

# The RECONCILIATION disposition of a reservation row (Gate#3 R5-MAJOR-1 / IDM-002) — the axis the
# FREEZE keys on, DISTINCT from the wire/cap axis (a slot is consumed the instant it is reserved and
# stays counted thereafter). Mirrors the E4 tri-state (:data:`veridex.dust_execution.contracts.
# UncertainState`) plus the ``PENDING`` reserve-time default:
#   * ``PENDING``            — reserved, the run's reconciliation has not been recorded yet (a crash /
#                              lost settle leaves the row here): possibly-live → FREEZES the scope;
#   * ``AMBIGUOUS``          — a real order reached the wire but its fill is UNCONFIRMED against venue
#                              truth: possibly-live → FREEZES the scope (wire-fired does NOT clear it);
#   * ``RESOLVED``           — a real order reached the wire AND reconciled (definite fill): STOPS
#                              freezing but STAYS COUNTED (the cap slot remains consumed);
#   * ``DEFINITIVELY_ABSENT``— no live order (abstained-no-wire, or venue-confirmed never placed): the
#                              row is RELEASED — it stops freezing AND is uncounted.
# Only ``PENDING`` / ``AMBIGUOUS`` freeze; ``RESOLVED`` counts-without-freezing; ``DEFINITIVELY_ABSENT``
# releases. The freeze is NEVER cleared by wire-fired / submitted / ACK alone — only by a reconciliation
# RESOLUTION.
ReconciliationState = Literal["PENDING", "RESOLVED", "AMBIGUOUS", "DEFINITIVELY_ABSENT"]


def _utc_day(ts_ms: int) -> datetime:
    """Return the UTC-midnight day boundary for an integer epoch-millisecond timestamp."""
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


@dataclass(frozen=True)
class DurableSessionState:
    """The authoritative durable session-state the provider hands the facade BEFORE live arming.

    Carries ONLY the four durable safety inputs the runner needs to enforce its caps honestly:

    * ``session_identity`` — the operator-assigned IMMUTABLE safety/ledger join key the runner runs
      under (the ``MMExecutionToolRequest.session_id`` the provider adopts), NOT the provisional
      ``strategy_id:mode`` seam;
    * ``risk`` — the durable :class:`RiskAccumulator` reconstructed from the persisted realized-fill
      ledger (prior session + UTC-day realized loss), so a prior loss at/over an enabled cap DENIES
      the next live arming instead of being reset to zero;
    * ``prior_session_order_count`` / ``prior_day_order_count`` — the persisted possibly-live attempt
      counts (INCLUDING reserved-but-unsettled attempts) folded into the runner's session/UTC-day
      order-cap gate, so a per-session/day cap is enforced ACROSS calls and process restarts.

    A frozen snapshot (SEC-005: ids / counts / a risk accumulator — never a secret or live handle).
    """

    session_identity: str
    risk: RiskAccumulator
    prior_session_order_count: int
    prior_day_order_count: int


@runtime_checkable
class DurableSessionStateProvider(Protocol):
    """The trusted durable session-state source the facade composes on the money path (MAJOR-2 + R4-MAJOR-1).

    ``load`` returns the authoritative :class:`DurableSessionState` for a session BEFORE any live
    arming — the immutable identity, the reconstructed realized-loss accumulator, and the persisted
    session/UTC-day attempt counts (which COUNT reserved-but-unsettled attempts). ``reserve`` durably
    appends a possibly-live attempt row BEFORE the wire (its failure is converted to a fail-closed by
    the facade); ``settle`` records the run outcome AFTER the wire (``committed=True`` keeps the reserved
    attempt counted, ``committed=False`` releases it). The concrete provider is INJECTED (like the
    operator-interlock / pre-submit stores); offline tests use
    :class:`InMemoryDurableSessionStateProvider`. Live mode FAILS CLOSED when no provider is supplied —
    the facade never falls back to a fresh/zero default on the money path.
    """

    def load(self, *, session_id: str, now: datetime) -> DurableSessionState:
        """Return the durable state for ``session_id`` (``now`` defines the current UTC day)."""
        ...

    def reserve(
        self,
        *,
        session_identity: str,
        now: datetime,
        attempt_id: str,
        venue_order_key: str | None = None,
    ) -> ReservationOutcome:
        """Idempotently reserve-OR-load a possibly-live attempt BEFORE the wire (Gate#3 R5-MAJOR-1).

        ``attempt_id`` is a STABLE idempotency identity derived from the COMPLETE admitted-order
        fingerprint (independent of any mutable durable count), so an IDENTICAL retry derives the SAME
        id and reconciles to the existing reservation rather than minting a new id and double-reserving.
        The return value tells the caller how to proceed (see :data:`ReservationOutcome`):

        * ``RESERVED`` — no prior row (never reserved, or a prior attempt was RELEASED): a FRESH
          possibly-live slot was appended (it counts toward the session/UTC-day caps immediately and
          durably, so a subsequent ``load`` includes it); the caller may proceed to the wire.
        * ``PENDING_RECONCILE`` — a prior UNSETTLED (possibly-live) row already exists: the caller must
          FREEZE — no new wire atop a possibly-live first order, regardless of spare cap — pending the
          production venue-truth reconcile. No new row is appended.
        * ``COMMITTED`` — a prior COMMITTED row exists (the first order already went): the caller
          replays that outcome idempotently. No new row is appended.

        ``venue_order_key`` is an OPTIONAL nullable pre-submit / official venue-order join key bound to
        the reservation row WHEN AVAILABLE, so the production reconcile-to-resolve can later resolve a
        possibly-live attempt's terminal state (offline it is ``None`` — SEC-002 carries no venue truth).
        A raise (durable-store outage) signals the facade to FAIL CLOSED (no wire I/O).
        """
        ...

    def reserve_or_freeze(
        self,
        *,
        session_identity: str,
        now: datetime,
        attempt_id: str,
        venue_order_key: str | None = None,
    ) -> ScopedReservationOutcome:
        """Atomically reserve-or-load an attempt AND enforce the session freeze scope (Gate#3 R6-MAJOR-2).

        The SINGLE critical-section op the facade money path calls instead of a separate
        :meth:`has_unresolved_reservation` followed by :meth:`reserve`. Fusing the scope-freeze check and
        the reserve-or-load closes the TOCTOU where two concurrent requests both observe an empty scope
        and both reserve — standing up two possibly-live orders. The outcome DISCRIMINATES (see
        :data:`ScopedReservationOutcome`):

        * ``PENDING_RECONCILE`` / ``COMMITTED`` — a SAME-``attempt_id`` row already exists: an idempotent
          replay of THIS attempt's own possibly-live/committed outcome (same-id takes priority over the
          scope — an identical retry is never a foreign conflict). No new row is appended.
        * ``SCOPE_FROZEN`` — a FRESH ``attempt_id`` while an UNRESOLVED possibly-live reservation
          (``recon_state`` ``PENDING`` or ``AMBIGUOUS``) already occupies the session scope: the caller
          must FREEZE — no new wire around an unresolved first order, regardless of spare cap, pending
          the production venue-truth reconcile. No new row is appended.
        * ``RESERVED`` — a FRESH ``attempt_id`` with nothing unresolved in scope: a fresh possibly-live
          slot is appended (it counts toward the caps immediately and durably); the caller may proceed.

        A raise (durable-store outage) signals the facade to FAIL CLOSED (no wire I/O). This method MUST
        execute the scope check AND the reserve/insert in ONE critical section spanning BOTH: a durable
        provider uses a single serializable transaction / row lock / advisory lock; the in-memory provider
        holds a per-provider lock across the whole check-and-insert. A bare synchronous method is NOT
        sufficient — the GIL switches threads at bytecode boundaries, so an unlocked check-then-insert lets
        two concurrent callers both observe the empty scope before either inserts and both reserve.
        """
        ...

    def settle(
        self,
        *,
        attempt_id: str,
        recon_state: ReconciliationState,
        venue_order_key: str | None = None,
    ) -> None:
        """Record a reserved attempt's RECONCILIATION outcome AFTER the run (IDM-002).

        ``recon_state`` is the E4 tri-state (never a mere wire/ACK/``submitted`` flag — a wire-fired
        order is NOT reconciliation-resolved):

        * ``RESOLVED`` — a real order reached the wire AND reconciled against venue truth (definite
          fill): the row STOPS freezing the scope but STAYS COUNTED (the cap slot remains consumed);
        * ``AMBIGUOUS`` — a real order reached the wire but its fill is UNCONFIRMED: the row stays
          possibly-live and KEEPS FREEZING the scope (counted);
        * ``DEFINITIVELY_ABSENT`` — no live order (abstained-no-wire, or venue-confirmed never placed):
          the row is RELEASED — it stops freezing AND is uncounted.

        A reserved-but-never-settled row (``settle`` never called, or its write failed / crashed) stays
        ``PENDING`` — possibly-live, still freezing and counted, until a real reconciliation RESOLUTION
        (or operator intervention) clears it. ``venue_order_key`` optionally binds the official
        venue-order join key WHEN AVAILABLE (the production reconcile hook; offline it is ``None``).
        """
        ...

    def has_unresolved_reservation(self, session_identity: str) -> bool:
        """Return whether ANY UNRESOLVED possibly-live reservation occupies the SESSION freeze scope.

        Gate#3 R5-MAJOR-1 / IDM-002 — the SECOND idempotency layer, keyed on the RECONCILIATION axis
        (NOT wire/cap). Stable-``attempt_id`` dedup alone is insufficient: a caller could change
        ``client_order_id`` / token / any intent field to derive a DISTINCT ``attempt_id`` and submit
        AROUND an unresolved first order — still double exposure. So BEFORE any new reserve+wire the
        facade asks whether an UNRESOLVED possibly-live attempt (``recon_state`` ``PENDING`` or
        ``AMBIGUOUS``) already exists in the session freeze scope and REFUSES EVERY new submit —
        INCLUDING a genuinely distinct ``attempt_id`` — until it RESOLVES (venue-truth reconcile, the
        production hook) or an operator clears it.

        The freeze is NEVER cleared by wire-fired / ``submitted`` / ACK alone — a submitted order with
        AMBIGUOUS reconciliation is possibly-live and STILL freezes. Only a ``RESOLVED`` (definite fill)
        or ``DEFINITIVELY_ABSENT`` (released) reconciliation stops freezing. In normal operation
        ``reserve -> wire -> settle(RESOLVED)`` leaves no unresolved row, so this only bites when a prior
        submit is stuck possibly-live (AMBIGUOUS reconcile / post-wire settle failure / crash) — exactly
        IDM-002's ambiguous-freeze. Session scope is the required minimum at dust scale.
        """
        ...


@dataclass
class _Reservation:
    """One durable possibly-live attempt row, on TWO independent axes (IDM-002).

    * ``recon_state`` — the RECONCILIATION axis the FREEZE keys on (:data:`ReconciliationState`):
      ``PENDING`` / ``AMBIGUOUS`` are possibly-live (freeze); ``RESOLVED`` counts-without-freezing;
      ``DEFINITIVELY_ABSENT`` never persists (the row is released). A wire-fired / committed reservation
      is NOT reconciliation-resolved — only a venue-truth RESOLUTION moves it out of a freezing state.
    * the wire/cap axis is implicit: any persisted (non-released) row consumes a cap slot, whatever its
      ``recon_state`` — so a ``RESOLVED`` row still counts toward the cap.

    SEC-005: a session id + a closed-vocab recon state + an OPTIONAL non-secret venue-order join REF
    only. ``venue_order_key`` is the nullable pre-submit / official venue-order key bound WHEN AVAILABLE
    so the production reconcile-to-resolve can resolve a possibly-live attempt's terminal state (offline
    it stays ``None`` — the sealed R4-A lifecycle carries no venue truth, SEC-002).
    """

    session_identity: str
    recon_state: ReconciliationState = "PENDING"
    venue_order_key: str | None = None


class InMemoryDurableSessionStateProvider:
    """A minimal append-only in-memory :class:`DurableSessionStateProvider` for offline tests.

    Reservations are keyed by the STABLE ``attempt_id`` so ``reserve`` is idempotent (a retry
    reconciles) and ``settle`` can address exactly one attempt. ``load`` counts every reservation for a
    session — reserved OR committed alike — so a possibly-live attempt consumes a cap slot the instant
    it is reserved and keeps consuming it until it is explicitly RELEASED (``settle(committed=False)``).
    A reserved-but-unsettled row therefore stays counted across a restart (conservative/fail-safe).

    Realized-fill LOSS is fed by the provider's OWN durable venue-reconciliation ledger via
    :meth:`record_reconciled_fills` (append-only, so a persisted loss can never be silently rewritten),
    NOT by the attempt reserve/settle contract; ``load`` reconstructs the durable :class:`RiskAccumulator`
    by the SAME direct UTC-day filter :func:`veridex.dust_execution.ledger.reconstruct_risk` uses
    (session loss over ALL fills; daily loss over ONLY ``now``'s-UTC-day fills). The attempt count is
    reported for BOTH the session and the UTC-day prior (the fixed offline clock keeps a test's attempts
    within one session+day; a real provider buckets the day count by UTC day). SEC-005: stores ids /
    counts / bools / fill records only.
    """

    def __init__(self) -> None:
        self._reservations: dict[str, _Reservation] = {}
        self._fills: dict[str, list[RealizedFillRecord]] = {}
        # R7-MAJOR-1: ONE per-provider state lock guarding EVERY access to the shared ``_reservations`` /
        # ``_fills`` state. A single synchronous Python method is NOT a critical section across worker
        # threads — the GIL switches at bytecode boundaries, so an unlocked ``reserve_or_freeze``
        # (scope-check THEN insert) lets two concurrent callers both observe the empty scope before either
        # inserts and BOTH reserve, standing up two possibly-live orders. This lock makes the whole
        # scope-check-and-insert (and every other mutation/consistent read) a REAL critical section. It is
        # a NON-REENTRANT :class:`threading.Lock`: every PUBLIC method acquires it exactly once and calls
        # ONLY the lock-free private helpers (``_reserve_or_load`` / ``_has_unresolved`` / ``_count``)
        # while holding it, so it can never be re-entered under itself (no self-deadlock).
        self._lock = threading.Lock()

    def load(self, *, session_id: str, now: datetime) -> DurableSessionState:
        with self._lock:
            today = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
            fills = self._fills.get(session_id, [])
            net_session = sum(fill.net_pnl() for fill in fills)
            net_day = sum(fill.net_pnl() for fill in fills if _utc_day(fill.fill_ts_ms) == today)
            risk = RiskAccumulator.seeded(
                session_id=session_id,
                net_session=net_session,
                net_day=net_day,
                current_day=today,
            )
            count = self._count(session_id)
            return DurableSessionState(
                session_identity=session_id,
                risk=risk,
                prior_session_order_count=count,
                prior_day_order_count=count,
            )

    def reserve(
        self,
        *,
        session_identity: str,
        now: datetime,
        attempt_id: str,
        venue_order_key: str | None = None,
    ) -> ReservationOutcome:
        # Reserve-OR-load (Gate#3 R5-MAJOR-1): a retry with the SAME stable attempt_id reconciles to the
        # existing reservation rather than minting a new id and double-reserving. The return value tells
        # the facade how to proceed; only a FRESH slot (no prior row / a prior RELEASED attempt) appends
        # a new possibly-live row and returns RESERVED. This is the STANDALONE reserve-or-load (no session
        # scope check); the facade money path uses the ATOMIC :meth:`reserve_or_freeze` instead.
        with self._lock:
            return self._reserve_or_load(
                session_identity=session_identity,
                attempt_id=attempt_id,
                venue_order_key=venue_order_key,
            )

    def _reserve_or_load(
        self, *, session_identity: str, attempt_id: str, venue_order_key: str | None = None
    ) -> ReservationOutcome:
        # The reserve-or-load primitive shared by :meth:`reserve` and :meth:`reserve_or_freeze`, so the
        # atomic op never round-trips through the PUBLIC ``reserve`` (a recording spy can then prove the
        # facade calls only the single atomic op on the money path). Lock-free primitive: ONLY called from
        # public methods already holding ``self._lock`` — so its read-then-insert stays inside the caller's
        # held-lock critical section (no re-entry, no self-deadlock).
        existing = self._reservations.get(attempt_id)
        if existing is not None:
            if existing.recon_state == "RESOLVED":
                # The first order went AND reconciled (definite fill): idempotent replay, no new wire.
                return "COMMITTED"
            # A prior UNRESOLVED (PENDING / AMBIGUOUS) possibly-live row: FREEZE unconditionally — no new
            # wire atop a possibly-live first order, regardless of spare cap, pending venue-truth reconcile.
            return "PENDING_RECONCILE"
        self._reservations[attempt_id] = _Reservation(
            session_identity=session_identity, recon_state="PENDING", venue_order_key=venue_order_key
        )
        return "RESERVED"

    def reserve_or_freeze(
        self,
        *,
        session_identity: str,
        now: datetime,
        attempt_id: str,
        venue_order_key: str | None = None,
    ) -> ScopedReservationOutcome:
        # Gate#3 R6-MAJOR-2: the ATOMIC reserve-or-freeze — the SINGLE critical section that fuses the
        # session-scope freeze check with the reserve-or-load, so two requests can NEVER both observe an
        # empty scope and both reserve (the split check-then-reserve TOCTOU).
        #
        # R7-MAJOR-1: a bare synchronous method is NOT that critical section across worker threads — the
        # GIL switches threads at bytecode boundaries, so two concurrent callers can BOTH run the scope
        # check (observe the empty scope) before EITHER inserts, and both reserve. The whole scope-check
        # AND insert therefore run under the per-provider ``_lock``, so the check-and-insert is indivisible
        # (the ``_has_unresolved`` / ``_reserve_or_load`` helpers are lock-free and run under this held
        # lock — no re-entry, no self-deadlock).
        #
        # The outcome DISCRIMINATES (SEC-005 closed vocab — no id/secret leaked):
        #   * a SAME-id row already exists → idempotent replay of THIS attempt's own outcome
        #     (``PENDING_RECONCILE`` atop an unsettled row, ``COMMITTED`` atop a resolved row) — an
        #     identical retry is never a foreign scope conflict, so same-id takes priority over the scope;
        #   * a FRESH id while an UNRESOLVED possibly-live reservation occupies the session scope →
        #     ``SCOPE_FROZEN`` (no row appended): a genuinely distinct attempt must NOT submit AROUND an
        #     unresolved first order, regardless of spare cap, pending venue-truth reconcile;
        #   * a FRESH id with nothing unresolved in scope → ``RESERVED`` (a fresh possibly-live slot).
        with self._lock:
            if attempt_id not in self._reservations and self._has_unresolved(session_identity):
                return "SCOPE_FROZEN"
            return self._reserve_or_load(
                session_identity=session_identity,
                attempt_id=attempt_id,
                venue_order_key=venue_order_key,
            )

    def settle(
        self,
        *,
        attempt_id: str,
        recon_state: ReconciliationState,
        venue_order_key: str | None = None,
    ) -> None:
        with self._lock:
            reservation = self._reservations.get(attempt_id)
            if reservation is None:
                return
            if recon_state == "DEFINITIVELY_ABSENT":
                # No live order (abstain-no-wire OR venue-confirmed absent): RELEASE — uncounted, unfrozen.
                del self._reservations[attempt_id]
                return
            # RESOLVED or AMBIGUOUS: the possibly-live attempt STAYS COUNTED (the cap slot is consumed).
            # Only RESOLVED stops freezing; AMBIGUOUS keeps freezing (wire-fired does NOT clear the freeze).
            reservation.recon_state = recon_state
            if venue_order_key is not None:
                # Bind the official venue-order join key WHEN AVAILABLE (production reconcile hook).
                reservation.venue_order_key = venue_order_key

    def record_reconciled_fills(
        self, *, session_identity: str, fills: Sequence[RealizedFillRecord]
    ) -> None:
        """Append venue-reconciled realized fills to the durable ledger (the realized-loss seam).

        Append-only so a persisted realized loss can never be silently rewritten. Fed by the provider's
        durable venue-reconciliation path (where PnL is computed), NEVER fabricated from the facade's
        sealed R4-A events (SEC-002).
        """
        if fills:
            with self._lock:
                self._fills.setdefault(session_identity, []).extend(fills)

    def _count(self, session_id: str) -> int:
        # Lock-free primitive: ONLY called from public methods already holding ``self._lock``.
        return sum(
            1 for res in self._reservations.values() if res.session_identity == session_id
        )

    def has_unresolved_reservation(self, session_identity: str) -> bool:
        # IDM-002 session-scope freeze (RECONCILIATION axis): True iff ANY possibly-live row whose
        # reconciliation is UNRESOLVED (``PENDING`` or ``AMBIGUOUS``) exists for this session. A RESOLVED
        # row (definite fill) does NOT freeze though it stays counted; a released row is gone. Wire-fired
        # / ``submitted`` alone never clears this — only a reconciliation RESOLUTION does. The facade money
        # path no longer calls this separately — it uses the ATOMIC :meth:`reserve_or_freeze` — but it
        # remains a standalone predicate for provider introspection / tests.
        with self._lock:
            return self._has_unresolved(session_identity)

    def _has_unresolved(self, session_identity: str) -> bool:
        # The unresolved-scope predicate shared by :meth:`has_unresolved_reservation` and the atomic
        # :meth:`reserve_or_freeze`, so the atomic op never round-trips through the PUBLIC scope check.
        # Lock-free primitive: ONLY called from public methods already holding ``self._lock`` — so the
        # scope check and the insert in :meth:`reserve_or_freeze` stay inside ONE held-lock critical
        # section (no re-entry).
        return any(
            res.session_identity == session_identity and res.recon_state in ("PENDING", "AMBIGUOUS")
            for res in self._reservations.values()
        )

    def attempts(self, session_id: str) -> int:
        """The durably-reserved (unreleased) possibly-live attempt count for ``session_id`` (test introspection)."""
        with self._lock:
            return self._count(session_id)


__all__ = [
    "DurableSessionState",
    "DurableSessionStateProvider",
    "InMemoryDurableSessionStateProvider",
    "ReconciliationState",
    "ReservationOutcome",
    "ScopedReservationOutcome",
]
