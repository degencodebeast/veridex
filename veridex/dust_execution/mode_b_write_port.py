"""Gate#3 C-1 fix — the ONE narrow INJECTED Mode-B write port (REQ-016/018, AC-031, E4 §3d).

MONEY-NETWORK BOUNDARY. This module closes the confirmed finding that the Mode-B runner
orchestration did NOT consume the approved keyless Privy/V2 money path: both the taker and the
resting-maker submit sites signed via the Mode-A FAKE ``Signer.sign_order`` seam, built a
PROVISIONAL ``venue_order_key`` (``"provisional-vok:" + digest``), and submitted through the
generic ``adapter.submit_order`` / ``submit_resting_order`` surfaces — bytes a real venue never
sees, and a join key E4 ACK-lost reconciliation can never resolve against real fill history.

:class:`ModeBWritePort` is the ONE injected Protocol the armed Mode-B runner may submit through —
no default, no legacy implementation. The production :class:`KeylessModeBWritePort` introduces NO
new signing/persist/HMAC logic of its own: it COMPOSES the E3-T6
:class:`~veridex.dust_execution.signing_compiler.PolymarketV2SigningCompiler` (pure typed-data
compile) with the E3-T8 :class:`~veridex.dust_execution.l2_transport.KeylessL2Transport`, which
remains the SINGLE owner of persist-before-sign -> Privy typed-data sign -> submit-time byte-verify
-> L2 HMAC + send (Fable-MAJOR-1). The returned :class:`~veridex.dust_execution.l2_transport.L2SubmitResult`
carries the REAL compound :class:`~veridex.dust_execution.contracts.PreSubmitRecord` whose
``venue_order_key`` is the official V2 ``orderHash`` (:attr:`CompiledSigningPayload.eip712_digest`) —
never a provisional placeholder — so E4 ACK-lost reconciliation joins on the same key a real venue's
fill history is keyed by.

SAF-009 (round every proposed order to real venue precision immediately before signing):
:func:`resolve_order_amounts` derives ``makerAmount``/``takerAmount`` via a PURE, in-lane,
stdlib-only reimplementation of the vendored ``OrderBuilder.get_order_amounts`` rounding path
(``veridex/venues/_vendor/polymarket_clob/client.py``). It is a COPY, never an import: the vendored
module imports ``eth_account`` / ``py_order_utils`` / ``httpx``, which the whole-of-
``veridex.dust_execution`` no-local-key AST denylist
(``tests/test_dust_execution_privy_signer.py::test_five_no_local_key_controls``, which walks EVERY
module under this package) structurally forbids anywhere in this money-path graph. A dedicated test
cross-validates the copy byte-for-byte against the REAL vendored helper so the derived amounts — and
therefore the ``venue_order_key`` digest they feed — are exactly what a real venue would issue.
"""

from __future__ import annotations

import itertools
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol, runtime_checkable

from veridex.dust_execution.l2_transport import KeylessL2Transport, L2SubmitResult
from veridex.dust_execution.privy_control_plane import PrivyAuthContext
from veridex.dust_execution.risk import FailClosed
from veridex.dust_execution.signing_compiler import (
    AdmittedPostRoundingIntent,
    OrderMarket,
    PolymarketV2SigningCompiler,
    SignerBinding,
    WireSide,
)
from veridex.dust_execution.wallet_binding import ExecutionWalletBinding

__all__ = [
    "KeylessModeBWritePort",
    "ModeBWritePort",
    "resolve_order_amounts",
]


# ---------------------------------------------------------------------------
# SAF-009 — pure, in-lane, stdlib-only copy of the vendored venue-precision rounding path.
#
# Mirrors ``veridex/venues/_vendor/polymarket_clob/client.py``'s ``RoundConfig`` / ``ROUNDING_CONFIG``
# / ``round_down`` / ``round_normal`` / ``round_up`` / ``decimal_places`` / ``to_token_decimals`` /
# ``OrderBuilder.get_order_amounts`` EXACTLY (same formulas, same constants) — a COPY, never an
# import (the vendored module pulls eth_account/py_order_utils/httpx, banned anywhere in this
# no-local-key package; see the module docstring). ``tests/test_dust_execution_amounts.py``
# cross-validates this copy against the real vendored helper for the tick sizes the runner uses.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RoundConfig:
    price: int
    size: int
    amount: int


#: The four Polymarket tick sizes and their rounding precisions — verbatim from the vendored
#: ``ROUNDING_CONFIG`` (client.py). Keyed by the SAME string the runner already threads into
#: ``SigningPayload.tick_size`` (``f"{tick_size}"``) — no second tick-size source (Gate#3 Stage-2).
_ROUNDING_CONFIG: dict[str, _RoundConfig] = {
    "0.1": _RoundConfig(price=1, size=2, amount=3),
    "0.01": _RoundConfig(price=2, size=2, amount=4),
    "0.001": _RoundConfig(price=3, size=2, amount=5),
    "0.0001": _RoundConfig(price=4, size=2, amount=6),
}


def _round_down(x: float, sig_digits: int) -> float:
    return math.floor(x * (10**sig_digits)) / (10**sig_digits)


def _round_normal(x: float, sig_digits: int) -> float:
    return round(x * (10**sig_digits)) / (10**sig_digits)


def _round_up(x: float, sig_digits: int) -> float:
    return math.ceil(x * (10**sig_digits)) / (10**sig_digits)


def _decimal_places(x: float) -> int:
    exponent = Decimal(str(x)).as_tuple().exponent
    # ``exponent`` is only a non-int sentinel ("n"/"N"/"F") for NaN/sNaN/Infinity, which a finite
    # rounding input (``x`` derived from validated order amounts) never produces.
    assert isinstance(exponent, int)  # noqa: S101 - narrows the typeshed Decimal union, not a runtime gate
    return abs(exponent)


def _to_token_decimals(x: float) -> int:
    f = (10**6) * x
    if _decimal_places(f) > 0:
        f = _round_normal(f, 0)
    return int(f)


def resolve_order_amounts(
    *, side: str, size: float, native_price: float, tick_size: float
) -> tuple[str, str]:
    """Derive the venue-precision ``(maker_amount, taker_amount)`` integer strings (SAF-009).

    A PURE, in-lane, stdlib-only reimplementation of the vendored
    ``OrderBuilder.get_order_amounts`` rounding path for ``side`` ``"BUY"``/``"SELL"`` — identical
    formulas, so the derived amounts (and therefore the ``eip712_digest``/``venue_order_key`` they
    feed via :class:`~veridex.dust_execution.signing_compiler.PolymarketV2SigningCompiler`) are
    byte-identical to what a real venue would compute for the same ``(side, size, price, tick)``.

    Raises:
        FailClosed: ``tick_size`` is not one of the four pinned venue tick sizes, or ``side`` is
            neither ``"BUY"`` nor ``"SELL"`` — fail closed rather than guess a rounding precision.
    """
    key = f"{tick_size}"
    config = _ROUNDING_CONFIG.get(key)
    if config is None:
        raise FailClosed(
            f"unsupported tick_size {tick_size!r} for venue-precision amount rounding; known ticks: "
            f"{sorted(_ROUNDING_CONFIG)} (fail closed rather than guess a rounding precision)"
        )
    raw_price = _round_normal(native_price, config.price)
    if side == "BUY":
        raw_taker_amt = _round_down(size, config.size)
        raw_maker_amt = raw_taker_amt * raw_price
        if _decimal_places(raw_maker_amt) > config.amount:
            raw_maker_amt = _round_up(raw_maker_amt, config.amount + 4)
            if _decimal_places(raw_maker_amt) > config.amount:
                raw_maker_amt = _round_down(raw_maker_amt, config.amount)
        maker_amount = _to_token_decimals(raw_maker_amt)
        taker_amount = _to_token_decimals(raw_taker_amt)
    elif side == "SELL":
        raw_maker_amt = _round_down(size, config.size)
        raw_taker_amt = raw_maker_amt * raw_price
        if _decimal_places(raw_taker_amt) > config.amount:
            raw_taker_amt = _round_up(raw_taker_amt, config.amount + 4)
            if _decimal_places(raw_taker_amt) > config.amount:
                raw_taker_amt = _round_down(raw_taker_amt, config.amount)
        maker_amount = _to_token_decimals(raw_maker_amt)
        taker_amount = _to_token_decimals(raw_taker_amt)
    else:
        raise FailClosed(f"side must be 'BUY' or 'SELL', got {side!r} (fail closed)")
    return str(maker_amount), str(taker_amount)


# ---------------------------------------------------------------------------
# The narrow injected Mode-B write port Protocol — no default, no legacy implementation.
# ---------------------------------------------------------------------------


@runtime_checkable
class ModeBWritePort(Protocol):
    """The ONE injected surface an ARMED Mode-B run may submit a REAL order through.

    Deliberately narrow: it takes the EXACT admitted order fields the runner already resolved
    (token/side/price/size/tif/post_only/tick — the C-2/C-4/C-3/M-3-admitted values, unmodified) and
    returns the real :class:`~veridex.dust_execution.l2_transport.L2SubmitResult` — never touching
    the generic :class:`~veridex.venues.base.VenueAdapter` submit surfaces. There is NO default
    implementation and NO legacy fallback: an armed Mode-B run with no port injected refuses before
    any I/O (the runner's structural guard), and this Protocol is the ONLY money-moving surface.
    """

    async def submit_order(
        self,
        *,
        token_id: str,
        side: WireSide,
        native_price: float,
        size: float,
        tif: str,
        post_only: bool,
        tick_size: float,
        binding: ExecutionWalletBinding,
        auth: PrivyAuthContext,
        expiration_s: int = 0,
        neg_risk: bool = False,
    ) -> L2SubmitResult: ...


def _default_salt_fn() -> Callable[[], str]:
    """A fresh, deterministic, per-instance salt counter (offline, no wall-clock/randomness)."""
    counter = itertools.count(1)
    return lambda: str(next(counter))


@dataclass
class KeylessModeBWritePort:
    """Production :class:`ModeBWritePort`: compiles via E3-T6, submits via the E3-T8 transport.

    Owns NO signing/persist/HMAC logic itself — :meth:`submit_order` derives the venue-precision
    amounts (SAF-009, :func:`resolve_order_amounts`), compiles the EXACT admitted order via
    :class:`~veridex.dust_execution.signing_compiler.PolymarketV2SigningCompiler`, and delegates
    the whole compile-done->persist->sign->byte-verify->HMAC+send sequence to the injected
    :class:`~veridex.dust_execution.l2_transport.KeylessL2Transport` (the single owner of that
    ordering, Fable-MAJOR-1). Holds NO local signing key anywhere in its object graph.

    ``owner`` is the L2 API-key UUID (a SECRET) that travels in the transient, unsigned wire wrapper
    — it MUST be the SAME ``L2ApiCredentials.api_key`` bound to ``transport``'s HMAC creds, never
    persisted raw (the transport's commitment-over-owner discipline covers it once compiled).
    """

    transport: KeylessL2Transport
    owner: str
    compiler: PolymarketV2SigningCompiler = field(default_factory=PolymarketV2SigningCompiler)
    salt_fn: Callable[[], str] = field(default_factory=_default_salt_fn)
    now_s: Callable[[], int] = field(default=lambda: int(time.time()))

    async def submit_order(
        self,
        *,
        token_id: str,
        side: WireSide,
        native_price: float,
        size: float,
        tif: str,
        post_only: bool,
        tick_size: float,
        binding: ExecutionWalletBinding,
        auth: PrivyAuthContext,
        expiration_s: int = 0,
        neg_risk: bool = False,
    ) -> L2SubmitResult:
        """Compile the EXACT admitted order and submit it through the keyless L2 transport."""
        maker_amount, taker_amount = resolve_order_amounts(
            side=side, size=size, native_price=native_price, tick_size=tick_size
        )
        intent = AdmittedPostRoundingIntent(
            side=side,
            maker_amount=maker_amount,
            taker_amount=taker_amount,
            native_price=native_price,
            size=size,
            tif=tif,
            post_only=post_only,
            defer_exec=False,
            expiration=str(expiration_s),
        )
        market = OrderMarket(token_id=token_id, neg_risk=neg_risk)
        signer_binding = SignerBinding(
            salt=self.salt_fn(),
            maker=binding.wallet_address,
            owner=self.owner,
            timestamp=str(self.now_s()),
            signer=binding.wallet_address,
        )
        compiled = self.compiler.compile(intent, market=market, binding=signer_binding)
        return await self.transport.submit_live_order(compiled, binding=binding, auth=auth)
