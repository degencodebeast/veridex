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
from typing import TYPE_CHECKING, Literal

from veridex.dust_execution.reconcile import UncertainSubmitState
from veridex.dust_execution.risk import FailClosed

if TYPE_CHECKING:  # pragma: no cover - typing only; keep the module import light + offline-safe.
    from veridex.dust_execution.feesnapshot import FeeSnapshot
    from veridex.venues.polymarket_resolver import ResolvedMarket

__all__ = [
    "LegKind",
    "OwnOrderLeg",
    "TokenCrossing",
    "NonCrossingVerdict",
    "check_non_crossing",
    "CanonicalOutcome",
    "LegRole",
    "OrderAction",
    "CanonicalLeg",
    "Leg",
    "RawOrder",
    "LockVerdict",
    "normalize",
    "complementary_lock_check",
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


# ===========================================================================
# E5-T2 — §4.6 YES/NO complementary-ECONOMIC non-crossing (SAF-010, AC-028/037).
#
# Same-token self-crossing (above) is necessary but NOT sufficient: a separate
# own-YES bid and own-NO bid can LOCK — pay >= $1 for a guaranteed $1 payout —
# while each token's own book passes SAF-009. Because ``YES + NO = $1`` at
# resolution, every raw BUY/SELL × YES/NO leg is first NORMALIZED (after venue-
# tick rounding) to a canonical BUY rung on {YES|NO}:
#
#     BUY  YES @ p  ->  BUY YES @ p
#     BUY  NO  @ p  ->  BUY NO  @ p
#     SELL YES @ p  ->  BUY NO  @ (1 − p)
#     SELL NO  @ p  ->  BUY YES @ (1 − p)
#
# Each side is kept as a canonical price-size LADDER (rungs NOT merged to one
# effective/average price — averaging HIDES a locked rung). The two ladders are
# paired WORST-PRICE-FIRST (sort DESC, walk together, ``q = min(top_yes, top_no)``
# per slice); a slice LOCKS iff ``b_yes + b_no + f_yes + f_no >= 1`` — STRICT
# ``< 1`` to admit. ANY positive-size slice that locks REJECTS the whole set;
# EVERY positive slice is tested because the taker fee ``feeRate·p·(1−p)`` is
# non-monotonic (peaks at p=0.5), so the dearest-priced slice is not always the
# costliest. A residual on one side after pairing is one-sided exposure (allowed).
# Per-rung fee comes from the PINNED hashed fee snapshot (maker 0, taker
# ``round5(feeRate·p·(1−p))``); a missing side↔token mapping or an unavailable/
# unknown fee fails closed — never admit on an unknown fund-touching number.
# ===========================================================================

#: The canonical outcome a normalized BUY rung rests on.
CanonicalOutcome = Literal["YES", "NO"]

#: A leg's execution role — MAKER (post-only, fee 0 in the V2 model) or TAKER (charged).
LegRole = Literal["MAKER", "TAKER"]

#: The raw order action normalized into a canonical BUY via ``YES + NO = $1``.
OrderAction = Literal["BUY", "SELL"]

#: The complement-lock verdict form.
LockVerdict = Literal["ADMIT", "REJECT"]


@dataclass(frozen=True)
class CanonicalLeg:
    """A canonical BUY rung on one outcome, produced by :func:`normalize` (the lock-check unit).

    Every raw BUY/SELL × YES/NO order is normalized to a canonical BUY on {YES|NO} via
    ``YES + NO = $1`` so both sides are comparable in one economic space. The rung carries its
    per-share, venue-precision fee (``round5`` of the pinned snapshot rate; maker 0), so the
    slice-wise lock check reads it directly — it never re-derives or invents a fee.

    Attributes:
        outcome: The canonical outcome the BUY rung rests on (``"YES"`` or ``"NO"``).
        price: The canonical BUY price ``b`` in ``[0, 1]`` (post-tick, complement-mapped for SELLs).
        size: The rung size (shares); a residual after pairing is one-sided exposure (allowed).
        role: ``"MAKER"`` or ``"TAKER"`` — the provenance of the attached fee.
        fee: The per-share venue-precision fee attached from the pinned snapshot. ``None`` marks an
            UNKNOWN/unavailable fee: the lock check fails closed on it (never admit on an unknown fee).
    """

    outcome: CanonicalOutcome
    price: float
    size: float
    role: LegRole
    fee: float | None = None


#: Test/call-site alias for :class:`CanonicalLeg` — the canonical rung the lock check consumes.
Leg = CanonicalLeg


@dataclass(frozen=True)
class RawOrder:
    """A raw own order before canonicalization — input to :func:`normalize`.

    Attributes:
        side: The BET-SIDE label (``"yes"``/``"no"``/``"home"``/``"away"``/``"under"``/``"over"`` …),
            resolved to a YES/NO token via :func:`veridex.venues.polymarket_resolver.side_to_token`.
        action: ``"BUY"`` or ``"SELL"`` — the wire direction, folded into the canonical BUY rung.
        price: The raw native ``[0, 1]`` limit price (venue-tick-rounded inside :func:`normalize`).
        size: The order size (shares).
        role: ``"MAKER"`` or ``"TAKER"`` — selects the maker/taker fee from the pinned snapshot.
    """

    side: str
    action: OrderAction
    price: float
    size: float
    role: LegRole


def _validate_role(role: str) -> LegRole:
    """Fail closed unless ``role`` is exactly ``"MAKER"`` or ``"TAKER"``."""
    if role not in ("MAKER", "TAKER"):
        raise FailClosed(f"role must be 'MAKER' or 'TAKER', got {role!r} (fail-closed)")
    return role  # type: ignore[return-value]


def normalize(
    order: RawOrder,
    *,
    resolved: ResolvedMarket,
    fee_snapshot: FeeSnapshot | None,
    tick_size: float,
) -> CanonicalLeg:
    """Normalize a raw BUY/SELL × YES/NO order to a canonical BUY rung (§4.6, SAF-010).

    Order of operations (load-bearing): (1) venue-tick ROUND the raw price; (2) apply the four-form
    map to a canonical BUY on {YES|NO} via ``YES + NO = $1`` (``SELL X @ p -> BUY ¬X @ (1 − p)``);
    (3) TICK-VALIDATE the normalized price with E5-T1's ``[tick, 1 − tick]`` guard; (4) attach the
    per-share fee from the PINNED snapshot (maker 0, taker ``round5(feeRate·p·(1−p))``). The fee
    formula is symmetric in ``p`` / ``1 − p``, so the canonical price yields the same fee as the raw.

    Args:
        order: The raw own order (:class:`RawOrder`).
        resolved: The resolved market — its YES/NO token ids drive the side↔token mapping.
        fee_snapshot: The PINNED hashed fee snapshot; ``None`` is an unavailable snapshot (fail closed).
        tick_size: The venue minimum price increment (must be finite and in ``(0, 0.5]``).

    Returns:
        The canonical :class:`CanonicalLeg` BUY rung with its per-share fee attached.

    Raises:
        FailClosed: On a missing/ambiguous side↔token mapping, an invalid action/role, or an
            unavailable fee snapshot — a fund-touching value is never invented.
        ValueError: On an invalid tick size or a normalized price outside ``[tick, 1 − tick]``.
    """
    _validate_tick_size(tick_size)
    role = _validate_role(order.role)
    if order.action not in ("BUY", "SELL"):
        raise FailClosed(f"action must be 'BUY' or 'SELL', got {order.action!r} (fail-closed)")

    # (1) side -> token, then token -> canonical outcome. A missing/ambiguous mapping fails closed
    #     (never silently route to a side): side_to_token raises ValueError; we re-raise as FailClosed.
    from veridex.venues.polymarket_resolver import side_to_token  # local: keep module import light.

    try:
        token = side_to_token(resolved, order.side)
    except ValueError as exc:
        raise FailClosed(
            f"side {order.side!r} has no unambiguous YES/NO token mapping (fail-closed)"
        ) from exc
    if token == resolved.token_id_yes:
        token_outcome: CanonicalOutcome = "YES"
    elif token == resolved.token_id_no:
        token_outcome = "NO"
    else:  # pragma: no cover - side_to_token only ever returns one of the two token ids.
        raise FailClosed(f"side {order.side!r} mapped to an unknown token {token!r} (fail-closed)")

    # (2) venue-tick round, then four-form map to a canonical BUY. SELL X @ p == BUY ¬X @ (1 − p).
    venue_rounded = _round_to_tick(order.price, tick_size)
    if order.action == "BUY":
        outcome = token_outcome
        canonical_price = venue_rounded
    else:  # SELL: flip the outcome and take the complement price.
        outcome = "NO" if token_outcome == "YES" else "YES"
        canonical_price = 1.0 - venue_rounded

    # (3) tick-validate the NORMALIZED price (reuse E5-T1's [tick, 1 − tick] guard); fail closed off-band.
    price = _validate_and_round_price(canonical_price, tick_size)

    # (4) attach the per-share (shares=1) venue-precision fee from the PINNED snapshot; None fails closed.
    if fee_snapshot is None:
        raise FailClosed(
            "fee snapshot is unavailable; refusing to normalize without a pinned fee (fail-closed)"
        )
    if role == "MAKER":
        fee = fee_snapshot.maker_fee(shares=1.0, price=price)
    else:
        fee = fee_snapshot.taker_fee(shares=1.0, price=price)

    return CanonicalLeg(outcome=outcome, price=price, size=order.size, role=role, fee=fee)


def _validate_lock_leg(leg: CanonicalLeg, expected: CanonicalOutcome) -> tuple[float, float, float]:
    """Validate one canonical rung for the lock walk; return ``(price, size, fee)`` (fail closed).

    An unknown fee (``None``), a non-finite/negative fee, a non-finite/out-of-``[0,1]`` price, or an
    outcome that does not match the ladder it was handed to is a fund-touching inconsistency and fails
    closed — the lock check never admits on an unknown or mismatched rung.
    """
    if leg.outcome != expected:
        raise FailClosed(
            f"rung outcome {leg.outcome!r} does not match its {expected!r} ladder (fail-closed)"
        )
    if leg.fee is None or not math.isfinite(leg.fee) or leg.fee < 0.0:
        raise FailClosed(
            f"rung fee {leg.fee!r} is unknown/invalid; refusing to admit an unknown fee (fail-closed)"
        )
    if not math.isfinite(leg.price) or leg.price < 0.0 or leg.price > 1.0:
        raise FailClosed(f"rung price {leg.price!r} is outside the unit interval (fail-closed)")
    if not math.isfinite(leg.size) or leg.size < 0.0:
        raise FailClosed(f"rung size {leg.size!r} is negative or non-finite (fail-closed)")
    return leg.price, leg.size, leg.fee


def complementary_lock_check(
    yes_ladder: list[CanonicalLeg] | tuple[CanonicalLeg, ...],
    no_ladder: list[CanonicalLeg] | tuple[CanonicalLeg, ...],
) -> LockVerdict:
    """Worst-price-first, slice-wise YES/NO complement-lock check (§4.6, SAF-010, AC-028/037).

    Both ladders are canonical BUY rungs (from :func:`normalize`). Sort each DESCENDING by price
    (worst-price-first — any own YES share can lock against any own NO share, so the adversarial
    pairing is dearest-against-dearest) and walk them together in ``q = min(remaining_yes,
    remaining_no)`` slices. For EACH positive-size slice test the STRICT inequality
    ``yes_price + no_price + yes_fee + no_fee < 1``; ANY slice that reaches ``>= 1`` LOCKS and REJECTS
    the whole set. EVERY positive slice is tested — the taker fee ``feeRate·p·(1−p)`` peaks at p=0.5,
    so a cheaper-priced slice can carry a higher fee; there is no shortcut on the dearest price. A
    residual on one side after the shorter ladder exhausts is one-sided exposure (allowed → ADMIT).

    NO collapsed effective/average price: averaging ``YES {0.60×1, 0.30×99}`` to ~0.303 would wrongly
    admit against ``NO {0.45×100}``, yet the top 1-share slice ``0.60 + 0.45 = 1.05 >= 1`` is a
    guaranteed-loss lock. One overlapping share ``>= $1`` is enough to REJECT.

    Args:
        yes_ladder: Canonical BUY-YES rungs (each ``outcome == "YES"``).
        no_ladder: Canonical BUY-NO rungs (each ``outcome == "NO"``).

    Returns:
        ``"ADMIT"`` if no positive-size slice locks, else ``"REJECT"``.

    Raises:
        FailClosed: If any rung has an unknown/invalid fee, an out-of-band price/size, or an outcome
            that does not match its ladder — never admit on an unknown fund-touching value.
    """
    # Validate + keep only positive-size rungs (a zero-size rung pairs nothing). Fail closed on any
    # unknown fee or inconsistent rung BEFORE walking — never admit on an unknown value.
    yes_rungs = [
        list(_validate_lock_leg(leg, "YES")) for leg in yes_ladder
    ]
    no_rungs = [list(_validate_lock_leg(leg, "NO")) for leg in no_ladder]
    yes_pos = sorted((r for r in yes_rungs if r[1] > 0.0), key=lambda r: r[0], reverse=True)
    no_pos = sorted((r for r in no_rungs if r[1] > 0.0), key=lambda r: r[0], reverse=True)

    i = j = 0
    while i < len(yes_pos) and j < len(no_pos):
        y_price, y_rem, y_fee = yes_pos[i]
        n_price, n_rem, n_fee = no_pos[j]
        q = min(y_rem, n_rem)
        if q > 0.0:
            # STRICT < 1 to admit; treat within-tolerance-of-1 as a lock (fail-closed at the boundary).
            slice_cost = y_price + n_price + y_fee + n_fee
            if slice_cost >= 1.0 - _TICK_ATOL:
                return "REJECT"
        # Consume q from BOTH tops; advance whichever ladder is exhausted (residual stays one-sided).
        yes_pos[i][1] = y_rem - q
        no_pos[j][1] = n_rem - q
        if yes_pos[i][1] <= 0.0:
            i += 1
        if no_pos[j][1] <= 0.0:
            j += 1

    return "ADMIT"
