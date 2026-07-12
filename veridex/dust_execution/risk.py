"""R4-A realized-loss risk accumulator + Mode B fail-closed cap validation (SAF-002/002a).

Two responsibilities, both load-bearing for real-money (Mode B) safety:

* ``RiskAccumulator`` tracks **fee-inclusive** realized loss over a session and a UTC day. It is
  fed ONLY from real, venue-reconciled fills (``RealizedFillRecord``) and **rejects a paper /
  simulated source at the source** (``PaperReceipt`` -> ``ValueError``). A simulated fill can
  therefore never move a real-money loss cap. The exposed ``realized_loss_session`` /
  ``realized_loss_day`` are non-negative loss magnitudes threaded into
  ``PreQuoteContext`` so ``gate.evaluate_pre_quote`` can deny once a cap is crossed.

* ``authorize_mode_b`` is the Mode B admission gate: it **fails closed** on a non-finite
  (``nan``/``inf``), non-positive, or disabled (``<= 0``) ``max_session_loss`` /
  ``max_daily_loss``. Disabled caps are permitted ONLY in non-money modes (Mode A dry-run/fake),
  which simply do NOT call this gate.

Design note (frozen contract boundary): the R4-A lifecycle contracts in ``contracts.py`` are
``frozen=True, extra="forbid"`` and carry NO ``realized_pnl`` / ``fee`` field (SEC-002 forbids
a post-hoc PnL field leaking onto a sealed order/fill event). This module therefore defines its
OWN realized-fill carrier, ``RealizedFillRecord`` — sourced from a real venue-reconciled fill,
NOT from a sealed lifecycle event — so the frozen contracts stay untouched.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

from veridex.policy.envelope import PolicyEnvelope, cap_breached


class FailClosed(Exception):
    """A safety precondition could not be positively satisfied — reject rather than proceed.

    Raised by ``authorize_mode_b`` (bad/disabled cap on the real-money path) and by
    ``RiskAccumulator.apply_realized_fill`` when a fill's session identity does not match the
    accumulator's — in either case the safe action is to STOP, never to continue.
    """


@dataclass(frozen=True)
class RealizedFillRecord:
    """A REAL, venue-reconciled realized fill — the ONLY admissible input to the accumulator.

    Attributes:
        realized_pnl: Signed realized PnL for the fill in payout dollars (loss is negative).
        fee: Venue fee for the fill in payout dollars (``>= 0``); it reduces PnL, so a
            ``realized_pnl`` of ``-0.60`` with ``fee`` ``0.01`` is a fee-inclusive loss of ``0.61``.
        session_id: Immutable session identity this fill belongs to.
        fill_ts_ms: Venue fill time in integer epoch **milliseconds** (UTC), used for the
            UTC-day boundary.
        source: Provenance marker pinned to the real venue-reconciled source; a non-real
            source cannot construct this record (it is a distinct type, ``PaperReceipt``).
    """

    realized_pnl: float
    fee: float
    session_id: str
    fill_ts_ms: int
    source: str = "venue_reconciled"

    def __post_init__(self) -> None:
        if not math.isfinite(self.realized_pnl):
            raise ValueError(f"realized_pnl must be finite, got {self.realized_pnl!r}")
        if not math.isfinite(self.fee):
            raise ValueError(f"fee must be finite, got {self.fee!r}")
        if self.fee < 0.0:
            raise ValueError(f"fee must be >= 0, got {self.fee!r}")

    def net_pnl(self) -> float:
        """Fee-inclusive signed PnL for this fill (``realized_pnl - fee``)."""
        return self.realized_pnl - self.fee


@dataclass(frozen=True)
class PaperReceipt:
    """A simulated / paper fill receipt — NEVER admissible to the realized-loss accumulator.

    Exists so the anti-inert control is enforceable BY TYPE: ``apply_realized_fill`` accepts
    only ``RealizedFillRecord`` and rejects this at the source. A paper/simulated fill must not
    be able to move a real-money loss cap.
    """

    simulated_pnl: float
    session_id: str


class RiskAccumulator:
    """Fee-inclusive realized-loss accumulator over one session and the current UTC day.

    Session loss accumulates for the accumulator's lifetime; daily loss resets on the UTC-day
    boundary (a fill landing on a later UTC day starts a fresh daily total). Both are exposed as
    non-negative loss magnitudes: ``max(0, -net_pnl)`` — a net gain reports zero loss.
    """

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        self._net_session = 0.0
        self._net_day = 0.0
        self._current_day: datetime | None = None

    @classmethod
    def seeded(
        cls,
        *,
        session_id: str,
        net_session: float,
        net_day: float,
        current_day: datetime | None,
    ) -> RiskAccumulator:
        """Construct an accumulator pre-loaded with reconstructed session + current-UTC-day nets.

        Restart-safe seeding path for :func:`veridex.dust_execution.ledger.reconstruct_risk`. The
        ledger computes the fee-inclusive net PnL summed over ALL persisted fills (session) and
        over ONLY the fills in ``now``'s UTC day (daily) via a DIRECT UTC-day filter, then seeds
        both here. Daily is thereby DECOUPLED from replay ORDER and from any venue clock skew: a
        fill dated on a LATER UTC day than ``now`` can no longer advance the daily window past today
        (the forward-only-rollover under-count the old marker-replay path suffered — SAF-002c /
        Gate#1 MINOR-1). Subsequent LIVE fills still flow through :meth:`apply_realized_fill` — the
        anti-inert, type-gated entry point — whose forward-only rollover then rolls the day at the
        NEXT real UTC-midnight crossing because ``current_day`` is seeded to today.

        Args:
            session_id: Session identity for the accumulator.
            net_session: Fee-inclusive signed net PnL summed over ALL persisted fills.
            net_day: Fee-inclusive signed net PnL summed over ONLY ``now``'s-UTC-day fills.
            current_day: ``now``'s UTC day at midnight (defines the live daily window); ``None``
                only when there is no reference day at all.

        Returns:
            A fresh :class:`RiskAccumulator` with session + daily nets and the current UTC day set.
        """
        acc = cls(session_id)
        acc._net_session = net_session
        acc._net_day = net_day
        acc._current_day = current_day
        return acc

    def apply_realized_fill(self, fill: RealizedFillRecord) -> None:
        """Fold one REAL venue-reconciled fill into the session + UTC-day accumulators.

        Args:
            fill: A real ``RealizedFillRecord``. A ``PaperReceipt`` or any non-real source is
                rejected AT SOURCE by type (``ValueError``) — a simulated fill can never move a
                real-money loss cap.

        Raises:
            ValueError: ``fill`` is not a ``RealizedFillRecord`` (e.g. a ``PaperReceipt``).
            FailClosed: ``fill.session_id`` does not match this accumulator's session identity.
        """
        if not isinstance(fill, RealizedFillRecord):
            raise ValueError(
                "RiskAccumulator only admits a real venue-reconciled RealizedFillRecord; "
                f"refusing a {type(fill).__name__} source (paper/simulated fills cannot move a "
                "real-money loss cap)"
            )
        if fill.session_id != self._session_id:
            raise FailClosed(
                f"fill session_id {fill.session_id!r} does not match accumulator session "
                f"{self._session_id!r}"
            )

        fill_day = datetime.fromtimestamp(fill.fill_ts_ms / 1000.0, tz=UTC).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        if self._current_day is None or fill_day > self._current_day:
            self._current_day = fill_day
            self._net_day = 0.0

        net = fill.net_pnl()
        self._net_session += net
        self._net_day += net

    @property
    def realized_loss_session(self) -> float:
        """Non-negative fee-inclusive realized loss for the session (``0`` when net-positive)."""
        return max(0.0, -self._net_session)

    @property
    def realized_loss_day(self) -> float:
        """Non-negative fee-inclusive realized loss for the current UTC day."""
        return max(0.0, -self._net_day)

    def breaches_caps(self, envelope: PolicyEnvelope) -> bool:
        """Whether the accumulated fee-inclusive loss reaches either ENABLED loss cap (SAF-002d).

        Routes through the single ``cap_breached`` predicate (:func:`veridex.policy.envelope.cap_breached`)
        — the ONE source of truth shared with ``gate.evaluate_pre_quote`` and the
        ``manifest.StrategyAuthorizationDecision.evaluate`` admission decision — so the proactive
        breach-sweep, the pre-quote deny, and admission can never drift on the crossing point. A
        ``<= 0`` cap is disabled (no protection to breach); an enabled cap is breached once loss
        ``>=`` it (the conservative, fail-closed boundary — HALT when loss reaches the ceiling).

        This is the breach-DETECTION half of the atomic loss-safety path; the coordinating
        block+sweep lives in :meth:`veridex.dust_execution.emergency.SafetyController.on_realized_fill`.

        Args:
            envelope: The policy envelope carrying ``max_session_loss`` / ``max_daily_loss``.

        Returns:
            ``True`` iff the session OR daily accumulated loss reaches its enabled cap.
        """
        return cap_breached(envelope.max_session_loss, self.realized_loss_session) or cap_breached(
            envelope.max_daily_loss, self.realized_loss_day
        )


def authorize_mode_b(envelope: PolicyEnvelope) -> None:
    """Mode B (real-money) admission gate for the realized-loss caps — fail closed on a bad cap.

    Per SAF-002(a): Mode B REQUIRES a finite positive ``max_session_loss`` AND ``max_daily_loss``.
    A non-finite (``nan``/``inf``), non-positive, or disabled (``<= 0``) cap fails closed;
    disabled caps are permitted ONLY in non-money modes (which never call this gate).

    Args:
        envelope: The policy envelope Mode B would arm against.

    Raises:
        FailClosed: Either loss cap is non-finite, non-positive, or disabled.
    """
    for name, cap in (
        ("max_session_loss", envelope.max_session_loss),
        ("max_daily_loss", envelope.max_daily_loss),
    ):
        if not math.isfinite(cap) or cap <= 0.0:
            raise FailClosed(
                f"Mode B requires a finite positive {name}; refusing to arm with {cap!r} "
                "(a disabled/non-finite/non-positive cap gives no real max-loss protection)"
            )
