"""Durable session-state provider for the agent-callable MM facade money path (Gate#3 MAJOR-2).

The public MM facade (:func:`veridex.dust_execution.facade.propose_mm_execution`) is the
agent-callable entry point. Before this fold it drove
:func:`veridex.dust_execution.runner.run_dust_execution` with a FRESH zero
:class:`~veridex.dust_execution.risk.RiskAccumulator` and zero prior order counts on EVERY call,
so the runner's durable run/session/UTC-day order caps and realized-loss caps RESET each
invocation. Two calls with ``max_orders_per_session == 1`` both reached the keyless write port,
and a prior realized loss was never reconstructed before the next live arming (SAF-002). The M-3
fold made the RUNNER enforce those caps GIVEN honest durable inputs — this module makes the facade
SUPPLY them.

It gives the facade ONE authoritative durable session-state source: a narrow
:class:`DurableSessionStateProvider` ``Protocol`` (a BEFORE-run :meth:`~DurableSessionStateProvider.load`
+ an AFTER-run :meth:`~DurableSessionStateProvider.record_run`) plus a minimal append-only in-memory
fake for offline tests, following the SAME injected-seam idiom the lane already uses for the
operator-interlock store and the pre-submit store. The provider supplies, BEFORE any live arming:

* an operator-assigned IMMUTABLE session identity — the authoritative safety/ledger join key the
  runner runs under (NOT the provisional ``strategy_id:mode`` seam). The operator-assigned id is
  ``MMExecutionToolRequest.session_id`` (a REQUIRED, frozen field — immutable by construction); the
  provider adopts it as the durable join key and echoes it back as :attr:`DurableSessionState.session_identity`;
* the durable :class:`RiskAccumulator` carrying prior session + UTC-day realized loss
  (``reconstruct_risk`` — restart-safe, mirrors :func:`veridex.dust_execution.ledger.reconstruct_risk`);
* the persisted session/UTC-day possibly-live attempt counts.

After the run the facade persists the possibly-live attempt-count delta (accepted OR uncertain-ACK)
and any venue-reconciled realized fills back THROUGH the SAME identity, so the NEXT call reads them
as the ``prior_*`` counts and the reconstructed loss. SEC-005: rows carry only ids / counts /
non-secret refs / bools — never an operator secret, a key, or a live handle.
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
      counts folded into the runner's session/UTC-day order-cap gate, so a per-session/day cap is
      enforced ACROSS calls and process restarts.

    A frozen snapshot (SEC-005: ids / counts / a risk accumulator — never a secret or live handle).
    """

    session_identity: str
    risk: RiskAccumulator
    prior_session_order_count: int
    prior_day_order_count: int


@runtime_checkable
class DurableSessionStateProvider(Protocol):
    """The trusted durable session-state source the facade composes on the money path (MAJOR-2).

    ``load`` returns the authoritative :class:`DurableSessionState` for a session BEFORE any live
    arming — the immutable identity, the reconstructed realized-loss accumulator, and the persisted
    session/UTC-day attempt counts. ``record_run`` persists the run's possibly-live attempt-count
    delta and any venue-reconciled realized fills back through the SAME identity, so the NEXT
    ``load`` reads them. The concrete provider is INJECTED (like the operator-interlock / pre-submit
    stores); offline tests use :class:`InMemoryDurableSessionStateProvider`. Live mode FAILS CLOSED
    when no provider is supplied — the facade never falls back to a fresh/zero default on the money
    path.
    """

    def load(self, *, session_id: str, now: datetime) -> DurableSessionState:
        """Return the durable state for ``session_id`` (``now`` defines the current UTC day)."""
        ...

    def record_run(
        self,
        *,
        session_identity: str,
        attempts: int,
        fills: Sequence[RealizedFillRecord] = (),
    ) -> None:
        """Persist the run's possibly-live attempt-count delta + venue-reconciled realized fills."""
        ...


class InMemoryDurableSessionStateProvider:
    """A minimal append-only in-memory :class:`DurableSessionStateProvider` for offline tests.

    Keyed on the operator-assigned ``session_id``. It durably ACCUMULATES the possibly-live attempt
    count and appends venue-reconciled realized fills, and reconstructs the durable
    :class:`RiskAccumulator` on ``load`` by the SAME direct UTC-day filter
    :func:`veridex.dust_execution.ledger.reconstruct_risk` uses (session loss over ALL fills; daily
    loss over ONLY ``now``'s-UTC-day fills). Deliberately append-only for the fills so a persisted
    realized loss can never be silently rewritten. The attempt count is reported for BOTH the session
    and the UTC-day prior (the fixed offline clock keeps a test's attempts within one session+day; a
    real provider buckets the day count by UTC day). SEC-005: stores ids / counts / fill records only.
    """

    def __init__(self) -> None:
        self._attempts: dict[str, int] = {}
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
        count = self._attempts.get(session_id, 0)
        return DurableSessionState(
            session_identity=session_id,
            risk=risk,
            prior_session_order_count=count,
            prior_day_order_count=count,
        )

    def record_run(
        self,
        *,
        session_identity: str,
        attempts: int,
        fills: Sequence[RealizedFillRecord] = (),
    ) -> None:
        if attempts < 0:
            raise ValueError(f"attempts delta must be >= 0, got {attempts!r}")
        self._attempts[session_identity] = self._attempts.get(session_identity, 0) + attempts
        if fills:
            self._fills.setdefault(session_identity, []).extend(fills)

    def attempts(self, session_id: str) -> int:
        """The durably-accumulated possibly-live attempt count for ``session_id`` (test introspection)."""
        return self._attempts.get(session_id, 0)


__all__ = [
    "DurableSessionState",
    "DurableSessionStateProvider",
    "InMemoryDurableSessionStateProvider",
]
