"""Native probability-space forward markout for market-maker quote quality.

All markout math operates in **native probability / share-price space** ``[0, 1]``
(TxLINE de-vigged probability and Polymarket ``native_price`` both live in ``[0, 1]``).

Decimal-odds values (``> 1``, e.g. ``1.667``) are **rejected**, never silently
computed: mixing decimal-odds and probability silently mis-scales the error (the
"Run-002 longshot-scale" bug class). Every price operand is bounds-checked via
:func:`assert_native_prob` *before* any arithmetic, so a decimal-odds price can
never reach the markout math without raising (AC-016).
"""

from __future__ import annotations

from veridex.maker.contracts import Side

__all__ = ["MarkoutError", "assert_native_prob", "forward_markout_bps"]


class MarkoutError(ValueError):
    """Raised when a markout operand is not a native probability in ``[0, 1]``."""


def assert_native_prob(x: float, name: str) -> float:
    """Return ``x`` if it is a native probability in ``[0, 1]``, else raise.

    Args:
        x: The candidate value, expected in native probability space ``[0, 1]``.
        name: Operand name, used in the error message for diagnosis.

    Returns:
        The input ``x`` unchanged when ``0.0 <= x <= 1.0``.

    Raises:
        MarkoutError: If ``x`` is outside ``[0, 1]`` (e.g. a decimal-odds value).
    """
    if 0.0 <= x <= 1.0:
        return x
    raise MarkoutError(f"{name} not in [0,1]: {x}")


def forward_markout_bps(
    *,
    side: Side,
    quote_price: float,
    ref_now: float,
    ref_future: float,
) -> int:
    """Compute forward quote-quality markout in basis points.

    Measures how the future reference fair value moved relative to the quoted
    price, signed by side (a bid is good when the reference rises above the
    quote; an ask is good when it falls below).

    All three price operands are bounds-checked via :func:`assert_native_prob`
    **before** any subtraction or division, so a decimal-odds operand (``> 1``)
    raises instead of silently mis-scaling the result.

    Args:
        side: ``Side.BID`` or ``Side.ASK``.
        quote_price: The quoted price, native probability in ``[0, 1]``.
        ref_now: Reference fair value at quote time, native probability in ``[0, 1]``.
        ref_future: Reference fair value in the future, native probability in ``[0, 1]``.

    Returns:
        Forward markout in basis points, rounded to the nearest integer.

    Raises:
        MarkoutError: If any price operand is outside ``[0, 1]``.
    """
    quote_price = assert_native_prob(quote_price, "quote_price")
    ref_now = assert_native_prob(ref_now, "ref_now")
    ref_future = assert_native_prob(ref_future, "ref_future")

    if ref_now == 0.0:
        raise MarkoutError("ref_now is zero; markout undefined")

    sign = 1 if side is Side.BID else -1
    return round(sign * (ref_future - quote_price) / ref_now * 1e4)
