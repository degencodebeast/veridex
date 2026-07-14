"""Gate#3 C-1 fix — SAF-009 venue-precision amount rounding cross-validation.

``veridex.dust_execution.mode_b_write_port.resolve_order_amounts`` is a PURE, in-lane, stdlib-only
COPY of the vendored ``OrderBuilder.get_order_amounts`` rounding path
(``veridex/venues/_vendor/polymarket_clob/client.py``) — never an import, because that vendored
module pulls ``eth_account`` / ``py_order_utils`` / ``httpx``, which the whole-of-
``veridex.dust_execution`` no-local-key AST denylist
(``tests/test_dust_execution_privy_signer.py::test_five_no_local_key_controls``) forbids anywhere in
this money-path package.

These tests cross-validate the copy against the REAL vendored helper across a matrix of
(side, size, price, tick) so the derived ``(maker_amount, taker_amount)`` — and therefore the
``eip712_digest`` / ``venue_order_key`` they feed via ``PolymarketV2SigningCompiler`` — are exactly
what a real venue would compute, never a drifted reimplementation.
"""

from __future__ import annotations

import pytest

from veridex.dust_execution.mode_b_write_port import resolve_order_amounts
from veridex.dust_execution.risk import FailClosed
from veridex.venues._vendor.polymarket_clob.client import ROUNDING_CONFIG, OrderBuilder

_TICKS = ("0.1", "0.01", "0.001", "0.0001")
_SIZES = (0.1, 1.0, 3.33, 12.345, 100.0)
_PRICES = (0.001, 0.0123, 0.5, 0.9999, 0.12345)
_SIDES = ("BUY", "SELL")


def _vendored_amounts(*, side: str, size: float, price: float, tick: str) -> tuple[str, str]:
    """The REAL vendored rounding output — ``self`` is unused by ``get_order_amounts``, so ``None``
    stands in for an ``OrderBuilder`` instance (avoids constructing one, which needs a live signer)."""
    _wire_side, maker_amount, taker_amount = OrderBuilder.get_order_amounts(
        None,  # type: ignore[arg-type]  # unbound call; get_order_amounts never reads self
        side,
        size,
        price,
        ROUNDING_CONFIG[tick],  # type: ignore[index]  # tick is a plain str, not narrowed to TickSize
    )
    return str(maker_amount), str(taker_amount)


@pytest.mark.parametrize("tick", _TICKS)
@pytest.mark.parametrize("price", _PRICES)
@pytest.mark.parametrize("size", _SIZES)
@pytest.mark.parametrize("side", _SIDES)
def test_resolve_order_amounts_matches_vendored_get_order_amounts_byte_for_byte(
    side: str, size: float, price: float, tick: str
) -> None:
    """POSITIVE CONTROL: the in-lane copy is byte-identical to the real vendored rounding for every
    (side, size, price, tick) combination — the digest a real venue would issue is reproduced exactly.
    """
    mine = resolve_order_amounts(side=side, size=size, native_price=price, tick_size=float(tick))
    vendored = _vendored_amounts(side=side, size=size, price=price, tick=tick)
    assert mine == vendored, (
        f"resolve_order_amounts drifted from the vendored get_order_amounts for "
        f"(side={side!r}, size={size!r}, price={price!r}, tick={tick!r}): {mine!r} != {vendored!r}"
    )


def test_resolve_order_amounts_rejects_unsupported_tick_size() -> None:
    """MUTATION TARGET: an unpinned tick size fails closed rather than guessing a rounding precision."""
    with pytest.raises(FailClosed):
        resolve_order_amounts(side="BUY", size=1.0, native_price=0.5, tick_size=0.005)


def test_resolve_order_amounts_rejects_unknown_side() -> None:
    with pytest.raises(FailClosed):
        resolve_order_amounts(side="HOLD", size=1.0, native_price=0.5, tick_size=0.01)
