"""II-8 — the pinned-checkpoint state machine for the §3 LLM-call contract.

This module holds the TWO reusable primitives that bind EVERY LLM contestant (addendum §3),
kept deliberately free of any drift/prompt/model specifics so a future LLM contestant (Sharp-Move,
Convergence) reuses the SAME machine:

  * :class:`CheckpointPolicy` — the pinned decision cadence (default 45s, sanctioned 30-60s) plus an
    optional predeclared feature-change threshold. Its parameters are what get pinned into a
    contestant's ``config_hash`` (never per-tick content).
  * :class:`InflightGuard` — enforces "one physical call in flight" and THE EXPIRY-CONFIRMATION rule.
    Each call is bound to its evidence coordinate (the checkpoint snapshot's ``evidence_hash``). A
    checkpoint that occurs while a call is in flight NEVER supersedes it. Staleness is defined ONLY
    by the pinned evidence-age limit; on expiry the guard cancels the physical call and launches
    NOTHING until that cancellation/termination is CONFIRMED.

The guard is model-agnostic: it drives any handle that quacks like an :class:`asyncio.Future`/``Task``
(``done`` / ``cancel`` / ``cancelled`` / ``exception`` / ``result``), so production wraps a real
``asyncio.Task`` and the offline suite injects a hand-controlled fake — the state machine is identical.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

# --- ServiceOutcome.status values — the transitions the guard reports per checkpoint tick --------
IDLE = "idle"  # no physical call occupies the single in-flight slot
IN_FLIGHT = "in_flight"  # a call is active and still within its pinned evidence-age limit
AWAITING_CONFIRMATION = "awaiting_confirmation"  # expired → cancel requested → NOTHING may launch yet
CONFIRMED_TERMINATED = "confirmed_terminated"  # the expired call's cancellation/termination is CONFIRMED
COMPLETED_FRESH = "completed_fresh"  # completed within the evidence-age limit → accept (carries ``raw``)
COMPLETED_STALE = "completed_stale"  # completed but beyond the evidence-age limit → drop
FAILED = "failed"  # completed with a provider exception → fail-closed (carries ``error``)


class CallHandle(Protocol):
    """The :class:`asyncio.Future`/``Task``-shaped surface the guard polls (never awaits inline).

    Modelling the in-flight call as a poll-able handle (rather than an inline ``await``) is what lets
    ONE physical call span MULTIPLE checkpoints — the guard advances it a step at a time from each
    per-tick ``decide`` call, exactly as the §3 contract requires.
    """

    def done(self) -> bool: ...
    def cancel(self) -> None: ...
    def cancelled(self) -> bool: ...
    def exception(self) -> BaseException | None: ...
    def result(self) -> Any: ...


@dataclass(frozen=True)
class ServiceOutcome:
    """The typed result of advancing the in-flight call one step (see the status constants above)."""

    status: str
    raw: Any = None  # the raw model output on COMPLETED_FRESH (pre-revalidation)
    error: BaseException | None = None  # the provider exception on FAILED
    evidence_hash: str | None = None  # the call's evidence coordinate (for skipped-inflight records)


@dataclass(frozen=True)
class CheckpointPolicy:
    """The pinned decision-checkpoint policy — the unit of decision for a drift contestant.

    Attributes:
        cadence_s: Fixed decision cadence in seconds (default 45; the sanctioned range is 30-60).
            Pinned into the contestant's ``config_hash`` — a change is a new deployed revision.
        evidence_age_limit_s: The ONLY definition of staleness. A response is accepted iff, at the
            tick it is observed, ``now - evidence_captured_at <= evidence_age_limit_s``; otherwise it
            is dropped. Never a newer raw tick, never the mere occurrence of a later checkpoint.
        feature_delta_threshold: Optional predeclared feature-change threshold. When set, a
            same-side ``|cum_logit_drift|`` move of at least this size since the last snapshot also
            opens a checkpoint (a regime move need not wait a full cadence).
    """

    cadence_s: float = 45.0
    evidence_age_limit_s: float = 30.0
    feature_delta_threshold: float | None = None

    def __post_init__(self) -> None:
        if self.cadence_s <= 0:
            raise ValueError("cadence_s must be positive")
        if self.evidence_age_limit_s <= 0:
            raise ValueError("evidence_age_limit_s must be positive")
        if self.feature_delta_threshold is not None and self.feature_delta_threshold <= 0:
            raise ValueError("feature_delta_threshold, when set, must be positive")

    def pinned_identity(self) -> str:
        """A stable identity string for the pinned params — folded into the contestant config hash."""
        return (
            f"checkpoint_policy:cadence_s={self.cadence_s}:"
            f"evidence_age_limit_s={self.evidence_age_limit_s}:"
            f"feature_delta_threshold={self.feature_delta_threshold}"
        )

    def is_checkpoint(
        self,
        *,
        now: float,
        last_checkpoint_at: float | None,
        snapshot: Any | None = None,
        last_snapshot: Any | None = None,
    ) -> bool:
        """Return whether ``now`` opens a decision checkpoint under the pinned policy.

        The FIRST decision opportunity (``last_checkpoint_at is None``) is always a checkpoint;
        thereafter a checkpoint opens when a full cadence has elapsed OR (when configured) a
        predeclared feature-change threshold is crossed since the last snapshot.

        Args:
            now: The current time on the injected clock.
            last_checkpoint_at: The clock reading at the last LAUNCHED checkpoint (or ``None``).
            snapshot: The current feature snapshot (read only for the feature-delta trigger).
            last_snapshot: The previous feature snapshot (read only for the feature-delta trigger).

        Returns:
            ``True`` if a decision checkpoint opens now.
        """
        if last_checkpoint_at is None:
            return True
        if now - last_checkpoint_at >= self.cadence_s:
            return True
        if (
            self.feature_delta_threshold is not None
            and snapshot is not None
            and last_snapshot is not None
        ):
            delta = abs(snapshot.cum_logit_drift - last_snapshot.cum_logit_drift)
            if delta >= self.feature_delta_threshold:
                return True
        return False


@dataclass
class _ActiveCall:
    """The single physical call occupying the in-flight slot (active OR terminating)."""

    handle: CallHandle
    evidence_hash: str
    captured_at: float
    cancel_requested: bool = False


class InflightGuard:
    """Enforces one-physical-call-in-flight, evidence-age staleness, and expiry-confirmation.

    The slot is occupied from ``launch`` until the call terminates — by completion, by a provider
    exception, or (on expiry) by a CONFIRMED cancellation. While occupied, :meth:`launch` refuses a
    new call, so a checkpoint during flight can never supersede the active call, and an expired call
    can never be replaced until its physical termination is confirmed.
    """

    def __init__(self, *, evidence_age_limit_s: float, clock: Callable[[], float]) -> None:
        """Initialise the guard.

        Args:
            evidence_age_limit_s: The pinned evidence-age limit (the ONLY staleness definition).
            clock: A zero-arg clock (seconds). Injected so tests drive evidence-age deterministically;
                production passes ``time.monotonic`` (or, for exact replay, a snapshot-ts-driven clock).
        """
        self._limit = evidence_age_limit_s
        self._clock = clock
        self._call: _ActiveCall | None = None

    @property
    def busy(self) -> bool:
        """``True`` while a physical call occupies the single slot (active OR awaiting confirmation)."""
        return self._call is not None

    def evidence_coordinate(self) -> str | None:
        """The in-flight call's evidence coordinate (its checkpoint snapshot hash), or ``None``."""
        return self._call.evidence_hash if self._call is not None else None

    def launch(self, handle: CallHandle, *, evidence_hash: str) -> None:
        """Occupy the slot with a new physical call bound to ``evidence_hash``.

        Raises:
            RuntimeError: If a physical call already occupies the slot (one-in-flight; and, on the
                expiry path, until the prior call's termination is confirmed — the guard that makes
                relaunch impossible during the unconfirmed window).
        """
        if self._call is not None:
            raise RuntimeError("cannot launch: a physical call is already in flight")
        self._call = _ActiveCall(handle=handle, evidence_hash=evidence_hash, captured_at=self._clock())

    def service(self) -> ServiceOutcome:
        """Advance the in-flight call one step and report the transition.

        Returns:
            An :class:`ServiceOutcome`:
              * ``IDLE`` — the slot is free (nothing to service).
              * ``COMPLETED_FRESH`` — completed within the evidence-age limit; ``raw`` carries the
                (unvalidated) model output; the slot is freed.
              * ``COMPLETED_STALE`` — completed but beyond the evidence-age limit; dropped; slot freed.
              * ``FAILED`` — completed with a provider exception (``error``); fail-closed; slot freed.
              * ``CONFIRMED_TERMINATED`` — an expired/cancelled call confirmed its termination; slot
                freed (ONLY now may the next checkpoint launch).
              * ``AWAITING_CONFIRMATION`` — expired while in flight; cancellation requested (once);
                the slot stays occupied — NOTHING may launch until confirmation.
              * ``IN_FLIGHT`` — active and still within the evidence-age limit.
        """
        call = self._call
        if call is None:
            return ServiceOutcome(status=IDLE)

        handle = call.handle
        if handle.done():
            evidence_hash = call.evidence_hash
            # An expiry-cancelled OR otherwise-cancelled call confirming termination — the expiry
            # path resolved; the response (if any) was already conceptually dropped on expiry.
            if call.cancel_requested or handle.cancelled():
                self._call = None
                return ServiceOutcome(status=CONFIRMED_TERMINATED, evidence_hash=evidence_hash)
            exc = handle.exception()
            if exc is not None:
                self._call = None
                return ServiceOutcome(status=FAILED, error=exc, evidence_hash=evidence_hash)
            # Completed cleanly → staleness is judged ONLY by the pinned evidence-age limit.
            age = self._clock() - call.captured_at
            raw = handle.result()
            self._call = None
            if age <= self._limit:
                return ServiceOutcome(status=COMPLETED_FRESH, raw=raw, evidence_hash=evidence_hash)
            return ServiceOutcome(status=COMPLETED_STALE, evidence_hash=evidence_hash)

        # Still physically in flight.
        age = self._clock() - call.captured_at
        if age > self._limit:
            if not call.cancel_requested:
                # EXPIRY: request cancellation exactly once, then hold the slot until it is CONFIRMED.
                handle.cancel()
                call.cancel_requested = True
            return ServiceOutcome(status=AWAITING_CONFIRMATION, evidence_hash=call.evidence_hash)
        return ServiceOutcome(status=IN_FLIGHT, evidence_hash=call.evidence_hash)
