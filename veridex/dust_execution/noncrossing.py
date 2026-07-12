"""E5-T1 — post-tick same-token non-crossing over the full POSSIBLY-LIVE union (SAF-009, AC-027).

The self-cross guard: on a single (market, outcome token) YOUR own orders must never cross YOUR own
orders. Concretely, ``highest_own_bid < lowest_own_ask`` (STRICT — equal prices are a cross at the
same price) must hold across the UNION of four possibly-live legs:

  * ``PROPOSED``          — about to be placed;
  * ``OPEN``              — confirmed resting on the book;
  * ``UNCERTAIN_SUBMIT``  — an AMBIGUOUS ACK-lost submit (E4-T2): possibly-live;
  * ``UNCERTAIN_CANCEL``  — a cancel requested/ACKed but NOT reconciled-absent: possibly-STILL-live.

A cancel request/ACK is NOT proof the old order is gone. Until reconciliation (E4) confirms the
cancelled order is DEFINITIVELY absent — which for Polymarket is UNREACHABLE (E4-T6), so it stays
possibly-live — the uncertain-cancel order remains in the union. Every possibly-live leg counts at
its WORST CASE (it IS live); dropping ANY leg (especially the uncertain-cancel leg) lets a crossing
slip through. A leg drops out of the union ONLY when its reconciled state is ``DEFINITIVELY_ABSENT``.

The check is PURE (SEC-003 money-network boundary): it takes the order legs + ``tick_size`` (the
venue tick from :attr:`veridex.venues.polymarket_resolver.ResolvedMarket.tick_size`) and returns an
ADMIT/REJECT verdict, or raises on an invalid tick/price. It opens no connection and calls no venue.

Before comparing, every price is ROUNDED to the tick, and each price is VALIDATED as tick-aligned
AND within ``[tick_size, 1 - tick_size]`` — an outcome-token price cannot rest at exactly 0 or 1, and
a price below one tick or above ``1 - tick`` fails closed (the venue-execution bound). This module
imports only the standard library plus the E4-T2 :data:`UncertainSubmitState` type — no ranked-lane
and no ``live_recorder`` dependency.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from veridex.dust_execution.reconcile import UncertainSubmitState

__all__ = [
    "LegKind",
    "OwnOrderLeg",
    "TokenCrossing",
    "NonCrossingVerdict",
    "check_non_crossing",
]

# Absolute tolerance for tick-alignment / boundary checks — well below the smallest sane Polymarket
# tick (0.001). Mirrors ``veridex.dust_execution.resting_order._TICK_ATOL``.
_TICK_ATOL: float = 1e-9

#: Order sides: BUY is a bid (buys the outcome token), SELL is an ask (sells it). The self-cross
#: invariant compares the highest own BID against the lowest own ASK.
Side = Literal["BUY", "SELL"]

#: The verdict form: ADMIT (no self-cross) or REJECT (a self-cross exists on some token).
Verdict = Literal["ADMIT", "REJECT"]


class LegKind(str, Enum):
    """Provenance of an own-order leg. ALL FOUR are POSSIBLY-LIVE and count in the union.

    ``UNCERTAIN_SUBMIT`` / ``UNCERTAIN_CANCEL`` carry the E4-T2 reconciled
    :data:`UncertainSubmitState`; they leave the union ONLY on ``DEFINITIVELY_ABSENT``.
    """

    PROPOSED = "proposed"
    OPEN = "open"
    UNCERTAIN_SUBMIT = "uncertain_submit"
    UNCERTAIN_CANCEL = "uncertain_cancel"


@dataclass(frozen=True)
class OwnOrderLeg:
    """One of YOUR own order legs on a (market, outcome token), considered for the self-cross union.

    Attributes:
        token_id: The outcome-token (market/outcome) id the leg rests against — the grouping key.
        side: ``"BUY"`` (bid) or ``"SELL"`` (ask).
        price: The native ``[0,1]`` limit price; rounded to the tick and bound-checked by the guard.
        kind: The leg's provenance (:class:`LegKind`) — all four kinds are possibly-live.
        uncertain_state: For an uncertain-submit / uncertain-cancel leg, its E4-T2 reconciled state.
            ``None`` (the default, and the fail-safe for an unreconciled uncertain leg) is treated as
            possibly-live (worst case). A leg drops from the union ONLY on ``DEFINITIVELY_ABSENT``.
    """

    token_id: str
    side: Side
    price: float
    kind: LegKind
    uncertain_state: UncertainSubmitState | None = None


@dataclass(frozen=True)
class TokenCrossing:
    """A self-cross detected on one outcome token: ``highest_bid >= lowest_ask`` (post-tick)."""

    token_id: str
    highest_bid: float
    lowest_ask: float


@dataclass(frozen=True)
class NonCrossingVerdict:
    """The pure ADMIT/REJECT verdict of the self-cross guard.

    Attributes:
        admitted: ``True`` iff NO self-cross exists on any token (``highest_bid < lowest_ask``
            everywhere over the possibly-live union).
        crossings: One :class:`TokenCrossing` per token that crosses (empty when admitted).
    """

    admitted: bool
    crossings: tuple[TokenCrossing, ...]

    @property
    def verdict(self) -> Verdict:
        """``"ADMIT"`` when no self-cross exists, else ``"REJECT"``."""
        return "ADMIT" if self.admitted else "REJECT"


def _validate_tick_size(tick_size: float) -> None:
    """Fail closed unless ``tick_size`` is finite and in ``(0, 0.5]`` (so ``[tick, 1-tick]`` exists)."""
    if not math.isfinite(tick_size) or tick_size <= 0.0:
        raise ValueError(f"tick_size must be a finite positive number, got {tick_size!r}")
    # ``1 - tick_size >= tick_size`` requires ``tick_size <= 0.5``; otherwise the admissible price band
    # ``[tick_size, 1 - tick_size]`` is empty and no price could ever rest (fail closed).
    if tick_size > 0.5:
        raise ValueError(f"tick_size must be <= 0.5 for a non-empty price band, got {tick_size!r}")


def _round_to_tick(price: float, tick_size: float) -> float:
    """Round a price to the nearest whole ``tick_size`` multiple (the canonical comparison basis)."""
    return round(price / tick_size) * tick_size


def _validate_and_round_price(price: float, tick_size: float) -> float:
    """Validate a native price and return its post-tick-rounded value (fail closed on invalid).

    Rejects (raises ``ValueError``) a non-finite price, an OFF-TICK price (not a whole tick multiple),
    or a price outside ``[tick_size, 1 - tick_size]`` — an outcome-token price cannot rest at exactly
    0 or 1, and a price below one tick or above ``1 - tick`` fails closed. Returns the tick-rounded
    price used for the non-crossing comparison.
    """
    if not math.isfinite(price):
        raise ValueError(f"price must be a finite native probability, got {price!r}")
    # OFF-TICK fails closed: the raw price must be a whole multiple of the tick (validated on the
    # ORIGINAL price, before rounding snaps it back onto the grid).
    steps = price / tick_size
    if abs(steps - round(steps)) > _TICK_ATOL:
        raise ValueError(f"price {price!r} is not aligned to tick_size {tick_size!r}")
    rounded = _round_to_tick(price, tick_size)
    # Venue-execution bound: an outcome-token price must rest within [tick, 1 - tick]; exactly 0/1 (or
    # below one tick / above 1 - tick) is not a restable price -> fail closed.
    if rounded < tick_size - _TICK_ATOL or rounded > 1.0 - tick_size + _TICK_ATOL:
        raise ValueError(
            f"price {price!r} is outside the restable band "
            f"[{tick_size!r}, {1.0 - tick_size!r}] (cannot rest at 0/1)"
        )
    return rounded


def _leg_is_possibly_live(leg: OwnOrderLeg) -> bool:
    """``True`` iff ``leg`` is POSSIBLY-LIVE and must count in the non-crossing union (worst case).

    ``PROPOSED`` (about to place) and ``OPEN`` (confirmed resting) are always live. An uncertain
    leg — ``UNCERTAIN_SUBMIT`` (AMBIGUOUS ACK-lost submit) or ``UNCERTAIN_CANCEL`` (cancel
    requested/ACKed but not reconciled-absent) — is possibly-live at its worst case: it stays in the
    union UNLESS its reconciled state is ``DEFINITIVELY_ABSENT`` (which, on real Polymarket, is
    unreachable, so a cancel request/ACK never removes it). ``None`` (unreconciled) is worst-cased as
    possibly-live.
    """
    if leg.kind in (LegKind.PROPOSED, LegKind.OPEN):
        return True
    # Uncertain submit / cancel: a cancel request/ACK is NOT proof the order is gone. It counts unless
    # reconciliation has proven it DEFINITIVELY absent.
    return leg.uncertain_state != "DEFINITIVELY_ABSENT"


def _side_of(leg: OwnOrderLeg) -> Side:
    """Return the validated side, failing closed on any spelling other than ``BUY`` / ``SELL``."""
    if leg.side not in ("BUY", "SELL"):
        raise ValueError(f"side must be 'BUY' or 'SELL', got {leg.side!r}")
    return leg.side


def check_non_crossing(
    legs: list[OwnOrderLeg] | tuple[OwnOrderLeg, ...],
    *,
    tick_size: float,
) -> NonCrossingVerdict:
    """Return the pure ADMIT/REJECT self-cross verdict over the possibly-live union (SAF-009, AC-027).

    Groups the POSSIBLY-LIVE legs (see :func:`_leg_is_possibly_live`) by outcome token and, per token,
    requires ``highest_own_bid < lowest_own_ask`` (STRICT — equal prices are a cross) on the tick-
    rounded prices. Every price is validated as tick-aligned AND within ``[tick_size, 1 - tick_size]``
    (raises ``ValueError`` otherwise). PURE: no network, no venue call, deterministic.

    Args:
        legs: YOUR own order legs across the union {proposed, open, uncertain-submit, uncertain-cancel}.
        tick_size: The venue minimum price increment (from
            :attr:`veridex.venues.polymarket_resolver.ResolvedMarket.tick_size`); must be finite and in
            ``(0, 0.5]``.

    Returns:
        A :class:`NonCrossingVerdict` — ``admitted=True`` iff no token self-crosses.

    Raises:
        ValueError: ``tick_size`` is invalid, or any leg's price is non-finite / off-tick / outside the
            restable ``[tick_size, 1 - tick_size]`` band (fail closed).
    """
    _validate_tick_size(tick_size)

    # Per-token worst-case bid/ask extremes over the possibly-live union.
    highest_bid: dict[str, float] = {}
    lowest_ask: dict[str, float] = {}

    for leg in legs:
        # Every leg's price is validated + rounded regardless of liveness — an invalid price fails
        # closed even if the leg would drop from the union (never silently pass a bad price).
        price = _validate_and_round_price(leg.price, tick_size)
        side = _side_of(leg)
        if not _leg_is_possibly_live(leg):
            continue
        if side == "BUY":
            cur = highest_bid.get(leg.token_id)
            if cur is None or price > cur:
                highest_bid[leg.token_id] = price
        else:  # SELL
            cur = lowest_ask.get(leg.token_id)
            if cur is None or price < cur:
                lowest_ask[leg.token_id] = price

    crossings: list[TokenCrossing] = []
    for token_id in highest_bid.keys() & lowest_ask.keys():
        hb = highest_bid[token_id]
        la = lowest_ask[token_id]
        # STRICT: a self-cross is highest_bid >= lowest_ask (equal is a cross at the same price).
        if hb + _TICK_ATOL >= la:
            crossings.append(TokenCrossing(token_id=token_id, highest_bid=hb, lowest_ask=la))

    ordered = tuple(sorted(crossings, key=lambda c: c.token_id))
    return NonCrossingVerdict(admitted=not ordered, crossings=ordered)
