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

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from veridex.dust_execution.risk import RealizedFillRecord, RiskAccumulator


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

    def reserve(self, *, session_identity: str, now: datetime, attempt_id: str) -> None:
        """Durably reserve a possibly-live attempt BEFORE the wire; raise on a durable-store failure.

        The reserved attempt counts toward the session/UTC-day caps immediately and durably, so a
        subsequent ``load`` (this run's or a later call's) includes it. ``attempt_id`` is a STABLE
        idempotency identity: a retry with the same id RECONCILES to the existing reservation rather
        than double-reserving. A raise signals the facade to FAIL CLOSED (no wire I/O).
        """
        ...

    def settle(self, *, attempt_id: str, committed: bool) -> None:
        """Record a reserved attempt's outcome AFTER the run.

        ``committed=True`` keeps the reserved attempt counted (the wire fired — a possibly-live
        attempt). ``committed=False`` RELEASES the reservation (no wire fired — the slot is freed). A
        reserved-but-unsettled row (``settle`` never called, or its write failed) stays COUNTED: a
        possibly-live attempt is conservatively held against the cap until reconciled against
        pre-submit/venue truth.
        """
        ...


@dataclass
class _Reservation:
    """One durable possibly-live attempt row (SEC-005: a session id + a committed bool only)."""

    session_identity: str
    committed: bool


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

    def load(self, *, session_id: str, now: datetime) -> DurableSessionState:
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

    def reserve(self, *, session_identity: str, now: datetime, attempt_id: str) -> None:
        # Idempotent: a retry with the SAME attempt_id reconciles to the existing reservation rather
        # than double-reserving (the durable cap is not double-charged for one logical attempt).
        self._reservations.setdefault(
            attempt_id, _Reservation(session_identity=session_identity, committed=False)
        )

    def settle(self, *, attempt_id: str, committed: bool) -> None:
        reservation = self._reservations.get(attempt_id)
        if reservation is None:
            return
        if committed:
            # The wire fired: keep the possibly-live attempt counted (durable cap consumption stands).
            reservation.committed = True
        else:
            # No wire fired: RELEASE the reservation so it does not wrongly consume a cap slot.
            del self._reservations[attempt_id]

    def record_reconciled_fills(
        self, *, session_identity: str, fills: Sequence[RealizedFillRecord]
    ) -> None:
        """Append venue-reconciled realized fills to the durable ledger (the realized-loss seam).

        Append-only so a persisted realized loss can never be silently rewritten. Fed by the provider's
        durable venue-reconciliation path (where PnL is computed), NEVER fabricated from the facade's
        sealed R4-A events (SEC-002).
        """
        if fills:
            self._fills.setdefault(session_identity, []).extend(fills)

    def _count(self, session_id: str) -> int:
        return sum(
            1 for res in self._reservations.values() if res.session_identity == session_id
        )

    def attempts(self, session_id: str) -> int:
        """The durably-reserved (unreleased) possibly-live attempt count for ``session_id`` (test introspection)."""
        return self._count(session_id)


__all__ = [
    "DurableSessionState",
    "DurableSessionStateProvider",
    "InMemoryDurableSessionStateProvider",
]
