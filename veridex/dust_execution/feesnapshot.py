"""E3-T2 — hashed, fail-closed per-market fee snapshot + venue-precision round5 (IDM-005, DAT-004).

MONEY-NETWORK BOUNDARY (fee side). The per-market fee is a load-bearing input to the fee-inclusive
realized-loss risk path (:meth:`veridex.dust_execution.risk.RealizedFillRecord.net_pnl`), so it is
sourced and pinned fail-closed:

* PINNED-ONCE, HASHED: :func:`pin_fee_snapshot` reads the fee descriptor from ``get_market``
  (``getClobMarketInfo``, E3-T0 §8) EXACTLY ONCE and returns a frozen :class:`FeeSnapshot`. Its
  :attr:`FeeSnapshot.snapshot_hash` (the canonical ``config_hash``) binds the pinned fee params and
  is what ``DustExecutionSessionMeta.market_fee_snapshot_hash`` records (SAF-010 §4.6 / DAT-004). A
  later fee computation uses the PINNED fields — NEVER a fresh venue call.

* FAIL-CLOSED IF UNAVAILABLE: a missing ``fd`` descriptor, an error from ``get_market``, or a
  non-finite / negative rate raises :class:`~veridex.dust_execution.risk.FailClosed` — never a silent
  ``0`` and never a guessed default (a fund-touching number is never invented).

* round5 (venue precision, E3-T0 §8 verbatim): "Fees are rounded to 5 decimal places. The smallest
  fee charged is 0.00001 USDC. Anything smaller rounds to zero." So: round to 5 dp; magnitude
  ``< 0.00001`` -> ``0``; smallest nonzero ``0.00001``; NO upward floor (a sub-threshold fee drops to
  ``0``, it is NOT bumped up to the minimum).

* TAKER FEE = ``round5(feeRate · shares · p · (1−p))`` — the E3-T0 §8 confirmed-verbatim symmetric
  form, peak at ``p=0.5``. Makers are never charged (V2 fee model, ``fd.to`` taker-only), so
  :meth:`FeeSnapshot.maker_fee` returns ``0`` SOURCED from the snapshot's ``taker_only`` flag — never
  a hardcoded call-site literal.

FEE-EXPONENT NOTE (carried forward from E3-T0 §13.4). Context7 (``/polymarket/py-clob-client-v2``)
confirms the fee descriptor is ``fd = {r: rate, e: exponent, to: taker_only}`` and that ``fd.e``
(``FeeDetails.exponent``, an int in ``1..2``) feeds the SEPARATE ``adjust_buy_amount_for_fees`` BUY
slippage-buffer utility — NOT the symmetric taker-fee formula above, which the docs pin as
``feeRate·p·(1−p)`` with no exponent term. We therefore CAPTURE ``fd.e`` in the frozen snapshot (it
participates in :attr:`FeeSnapshot.snapshot_hash` for audit) but do NOT invent exponent math into the
taker fee. If a future market's fee is exponent-sensitive, that is out of this pinned form and must
be reconfirmed against the live V2 fee math before it is relied on (fail-closed doctrine).

Intra-lane imports only (``veridex.dust_execution.*`` + stdlib). The market-info client is INJECTED as
a structural :class:`MarketInfoClient` Protocol, so this module does NOT import ``veridex.venues`` and
stays offline-import-safe and provider-neutral.
"""

from __future__ import annotations

import math
from typing import Any, Protocol, runtime_checkable

from veridex.dust_execution.contracts import _FrozenModel, _reject_price_out_of_unit_interval
from veridex.dust_execution.risk import FailClosed

# Venue fee precision (E3-T0 §8): 5 decimal places; the smallest nonzero fee is 0.00001 USDC.
_FEE_DP = 5
_FEE_MIN = 1e-5


def round5(x: float) -> float:
    """Apply the venue's fee precision: RAW magnitude ``< 0.00001`` -> ``0`` (checked BEFORE rounding);
    else round to 5 dp. No upward floor.

    Implements the E3-T0 §8 / AC-041 rule: a COMPUTED fee whose RAW magnitude is strictly below
    ``0.00001`` is zero — the threshold is tested on the raw value BEFORE rounding, so ``0.000009999``
    -> ``0.0`` (it is NOT rounded up to ``0.00001``); a raw magnitude at/above the threshold rounds to
    5 dp (``0.00001`` -> ``0.00001``). A sub-threshold value is NEVER bumped UP to the ``0.00001``
    minimum — it drops to zero.

    Args:
        x: The raw computed fee (must be finite; a fund-touching NaN/inf fails closed).

    Returns:
        The venue-precision fee: a multiple of ``0.00001`` (or ``0.0``).

    Raises:
        FailClosed: If ``x`` is non-finite (never round a NaN/inf fee).
    """
    if not math.isfinite(x):
        raise FailClosed(f"refusing to round a non-finite fee: {x!r}")
    # AC-041: the threshold is on the RAW COMPUTED magnitude, checked BEFORE rounding — a computed fee
    # below 0.00001 is zero. Rounding first would let a raw value in [5e-6, 1e-5) round UP to 1e-5
    # (an effective upward floor the spec forbids).
    if abs(x) < _FEE_MIN:
        return 0.0
    return round(x, _FEE_DP)


class FeeSnapshot(_FrozenModel):
    """A frozen, hashed per-market fee snapshot pinned once from ``get_market`` (E3-T0 §8).

    Immutable (``frozen=True``) so a pinned fee param can never be mutated post-hoc. The canonical
    :meth:`~veridex.dust_execution.contracts._FrozenModel.config_hash` over the pinned fields IS the
    snapshot hash (:attr:`snapshot_hash`) — deterministic across processes (AC-021).

    Attributes:
        condition_id: The market condition id the fee params were pinned for.
        fee_rate: ``fd.r`` — the market's base fee rate (a fraction, e.g. ``0.05``).
        fee_exponent: ``fd.e`` — captured for audit / hash pinning; see the module FEE-EXPONENT NOTE
            (it is NOT applied to the symmetric taker fee).
        taker_only: ``fd.to`` — the V2 taker-only flag; when true, makers are never charged.
    """

    condition_id: str
    fee_rate: float
    fee_exponent: int
    taker_only: bool

    @property
    def snapshot_hash(self) -> str:
        """The canonical ``sha256`` hash of the pinned fee params (binds ``market_fee_snapshot_hash``)."""
        return self.config_hash()

    def taker_fee(self, *, shares: float, price: float) -> float:
        """Taker fee ``round5(feeRate · shares · p · (1−p))`` — peak at ``p=0.5`` (E3-T0 §8).

        Args:
            shares: Number of shares matched (``C``).
            price: Native probability price ``p`` in ``[0, 1]`` (decimal-odds values are rejected).

        Returns:
            The venue-precision (round5) taker fee, ``>= 0``.

        Raises:
            ValueError: If ``price`` is not a native probability in ``[0, 1]``.
        """
        p = _reject_price_out_of_unit_interval(price)
        raw = self.fee_rate * shares * p * (1.0 - p)
        return round5(raw)

    def maker_fee(self, *, shares: float, price: float) -> float:
        """Maker fee — ``0`` when the market is taker-only (V2 model), SOURCED from the snapshot.

        The maker rung fee is not a hardcoded ``0`` at the call site: it is DERIVED from the pinned
        ``taker_only`` flag. In the V2 fee model makers are never charged, so a taker-only market
        yields ``0``. A non-taker-only market (not expected in V2) would charge the same symmetric
        fee — computed from the pinned rate, still not invented.

        Args:
            shares: Number of shares matched (``C``).
            price: Native probability price ``p`` in ``[0, 1]``.

        Returns:
            The venue-precision maker fee (``0.0`` for a taker-only market).
        """
        if self.taker_only:
            return 0.0
        return self.taker_fee(shares=shares, price=price)


@runtime_checkable
class MarketInfoClient(Protocol):
    """Structural client the snapshot reads the per-market fee descriptor from (``get_market``).

    Matches the vendored / exposed ``get_market(condition_id)`` read (E3-T0 §8). Injected so this
    module never imports ``veridex.venues`` — tests supply a fake returning the pinned ``fd`` shape.
    """

    async def get_market(self, condition_id: str) -> dict[str, Any]:
        """Return per-market info incl. the ``fd`` fee descriptor (E3-T0 §8)."""
        ...


async def pin_fee_snapshot(client: MarketInfoClient, condition_id: str) -> FeeSnapshot:
    """Read the per-market fee descriptor ONCE from ``get_market`` and pin a hashed snapshot.

    Fails closed on any of: an error from ``get_market``, a non-dict record, a missing/malformed
    ``fd`` descriptor, or a non-finite / negative rate — the snapshot is never constructed with a
    silent ``0`` or a guessed fee (a fund-touching number is never invented).

    Args:
        client: The injected market-info client (:class:`MarketInfoClient`).
        condition_id: The market condition id to pin the fee params for.

    Returns:
        A frozen :class:`FeeSnapshot` pinned from the venue's fee descriptor.

    Raises:
        FailClosed: If the fee descriptor is unavailable, malformed, or carries a bad rate.
    """
    try:
        info = await client.get_market(condition_id)
    except FailClosed:
        raise
    except Exception as exc:  # noqa: BLE001 — any venue error must fail closed, never a silent 0.
        raise FailClosed(
            f"get_market({condition_id!r}) failed; refusing to pin a fee snapshot without a "
            "confirmed fee (fail-closed, never a silent 0)"
        ) from exc

    if not isinstance(info, dict):
        raise FailClosed(f"get_market({condition_id!r}) returned a non-dict record: {info!r}")

    fd = info.get("fd")
    if not isinstance(fd, dict) or "r" not in fd:
        raise FailClosed(
            f"get_market({condition_id!r}) carries no fee descriptor (fd.r); refusing to pin a fee "
            "snapshot (fail-closed, never a silent 0 or guessed default)"
        )

    try:
        fee_rate = float(fd["r"])
        fee_exponent = int(fd.get("e", 1))
        taker_only = bool(fd.get("to", True))
    except (TypeError, ValueError) as exc:
        raise FailClosed(
            f"malformed fee descriptor {fd!r} for {condition_id!r}; refusing to pin (fail-closed)"
        ) from exc

    if not math.isfinite(fee_rate) or fee_rate < 0.0:
        raise FailClosed(
            f"fee rate {fee_rate!r} for {condition_id!r} is non-finite or negative; refusing to pin "
            "(fail-closed, never a guessed fee)"
        )

    return FeeSnapshot(
        condition_id=condition_id,
        fee_rate=fee_rate,
        fee_exponent=fee_exponent,
        taker_only=taker_only,
    )


__all__ = [
    "FeeSnapshot",
    "MarketInfoClient",
    "pin_fee_snapshot",
    "round5",
]
