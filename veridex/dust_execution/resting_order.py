"""R4-A resting-order contract (E3-T3, REQ-016, AC-031, §6 group 16).

A PHYSICALLY DISTINCT order type for the Mode-B resting-maker lane. It exists so the directional
taker :class:`veridex.venues.base.Order` can stay FAK/FOK-only (its ``tif`` Literal excludes GTC, and
its ``test_order_tif_gtc_is_unrepresentable`` ban test stays green) while R4-A gets a real GTC/GTD
post-only resting primitive in ITS OWN lane. REQ-016 forbids overloading a single order type for both
maker and taker; here that overload is impossible *by construction*:

* :class:`RestingOrder` — ``tif: Literal["GTC","GTD"]``; RESTS on the book; carries a REAL post-only
  wire contract and a GTD ``expiration``. It can NEVER represent FAK/FOK.
* The taker ``Order`` — ``tif: Literal["FAK","FOK"]``; NEVER rests. It can NEVER represent GTC/GTD.

They are two separate Python types, so a maker demo cannot accidentally submit a taker order and a
directional taker can never emit a resting GTC order.

Built against the E3-T0 §6 pinned resting-maker wire (``docs/maker/r4a-clobv2-wire-contract.md``):

* **post-only field:** top-level ``postOnly`` (bool), the ALO ("add-liquidity-only") semantic — a
  crossing post-only order is rejected (``INVALID_POST_ONLY_ORDER``), never executed. Valid ONLY with
  GTC/GTD (with FOK/FAK → ``INVALID_POST_ONLY_ORDER_TYPE``), which is why post-only lives on the
  resting contract and not on the taker ``Order``.
* **GTC/GTD:** the ``orderType`` string enum, ``"GTC"`` (rest until filled/cancelled) or ``"GTD"``
  (rest until expiry).
* **GTD expiration:** carried as ``order.expiration`` (unix SECONDS, UTC), NOT part of the signed
  hash. §6 rule: expiration must be ≥ 3 minutes in the future; a resting venue rejects a too-soon
  expiration with ``INVALID_ORDER_EXPIRATION`` (enforced at submit-time against a clock, not here).

MONEY-NETWORK BOUNDARY: this module is a pure value contract + a thin submit seam. It performs NO
network I/O and NO signing itself; the ``client`` passed to :func:`submit_resting_order` MUST already
be armed (the Mode-B admission gate's job). Tested only against the Mode-A FAKE resting venue.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from veridex.venues.base import RestingOrderVenue, SubmitAck

# Native prices are probability-like share prices in the OPEN interval (0, 1). Absolute tolerance for
# tick-alignment / boundary checks — well below the smallest sane Polymarket tick (0.001).
# SINGLE SOURCE: ``veridex.dust_execution.noncrossing`` imports this exact constant so the two
# tick-alignment paths share one value and can never silently drift apart.
_TICK_ATOL: float = 1e-9


class RestingOrder(BaseModel):
    """A distinct GTC/GTD post-only resting order (rests on the book) — NOT the taker ``Order``.

    Frozen and ``extra="forbid"`` so it is an immutable, closed value: no stray wire field can be
    smuggled in (fail-closed). Native tick/size validation runs at construction, so an out-of-range or
    non-tick-aligned price can never reach the venue.

    Attributes:
        token_id: CLOB asset (token) id the order rests against.
        side: ``"BUY"`` or ``"SELL"`` — sets the sign of the wire ``amount``.
        size: Number of shares to rest (> 0).
        native_price: Native probability-like share price in ``(0, 1)``, tick-aligned to ``tick_size``.
        tick_size: The market's minimum price increment (> 0); ``native_price`` must be a multiple.
        tif: ``"GTC"`` (rest until filled/cancelled) or ``"GTD"`` (rest until expiry). NEVER FAK/FOK.
        post_only: The §6 ``postOnly`` ALO flag (a REAL wire field). Defaults to ``True`` — a maker
            never wants to cross the spread.
        gtd_expiration_ts: Unix SECONDS expiration; REQUIRED for GTD, and MUST be absent for GTC.
        client_order_id: Caller-supplied idempotency / dedup identity (REQUIRED).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    token_id: str
    side: Literal["BUY", "SELL"]
    size: float
    native_price: float
    tick_size: float
    tif: Literal["GTC", "GTD"]
    post_only: bool = True
    gtd_expiration_ts: int | None = None
    client_order_id: str

    @field_validator("size")
    @classmethod
    def _size_positive(cls, v: float) -> float:
        if v <= 0.0:
            raise ValueError(f"size must be > 0, got {v!r}")
        return v

    @field_validator("tick_size")
    @classmethod
    def _tick_positive(cls, v: float) -> float:
        if v <= 0.0:
            raise ValueError(f"tick_size must be > 0, got {v!r}")
        return v

    @field_validator("client_order_id")
    @classmethod
    def _client_order_id_nonempty(cls, v: str) -> str:
        if not v:
            raise ValueError("client_order_id is required (idempotency identity)")
        return v

    @model_validator(mode="after")
    def _validate_price_and_tif(self) -> RestingOrder:
        # Native price is a probability-like share price strictly inside (0, 1) and tick-aligned. A
        # crossing/degenerate price never reaches the money path (fail-closed at construction).
        if not 0.0 < self.native_price < 1.0:
            raise ValueError(
                f"native_price must be strictly inside (0, 1), got {self.native_price!r}"
            )
        steps = self.native_price / self.tick_size
        if abs(steps - round(steps)) > _TICK_ATOL:
            raise ValueError(
                f"native_price {self.native_price!r} is not aligned to tick_size {self.tick_size!r}"
            )
        # GTD ⇔ an expiration is present; the overload is incoherent otherwise (fail-closed).
        if self.tif == "GTD" and self.gtd_expiration_ts is None:
            raise ValueError("GTD resting order requires gtd_expiration_ts (unix seconds)")
        if self.tif == "GTC" and self.gtd_expiration_ts is not None:
            raise ValueError("GTC resting order must NOT carry gtd_expiration_ts")
        if self.gtd_expiration_ts is not None and self.gtd_expiration_ts <= 0:
            raise ValueError(
                f"gtd_expiration_ts must be a positive unix seconds value, got {self.gtd_expiration_ts!r}"
            )
        return self

    def signed_amount(self) -> float:
        """Return the venue-wire signed ``amount``: ``+size`` for BUY, ``-size`` for SELL."""
        return self.size if self.side == "BUY" else -self.size

    def to_wire_kwargs(self) -> dict[str, Any]:
        """Map this resting order onto the §6 resting-maker wire kwargs (a REAL wire, not a flag).

        Returns the exact keyword set :meth:`RestingOrderVenue.submit_resting_order` consumes:
        ``order_type`` (``tif``), ``post_only`` (the §6 ``postOnly`` ALO field), ``expiration`` (the
        GTD unix-seconds field; ``0`` for GTC), plus ``token_id``/``amount``/``native_price``/
        ``tick_size``.
        """
        return {
            "token_id": self.token_id,
            "amount": self.signed_amount(),
            "native_price": self.native_price,
            "order_type": self.tif,
            "post_only": self.post_only,
            "expiration": self.gtd_expiration_ts or 0,
            "tick_size": str(self.tick_size),
        }


def maker_mode_b_resting_primitive_ready() -> bool:
    """Return ``True`` — the resting-order primitive exists, so Mode-B maker admission may proceed.

    Mode-B maker admission is BLOCKED until this distinct resting primitive exists (importing it is
    the gate). Its presence — and this predicate returning ``True`` — is what unblocks admission.
    """
    return True


async def submit_resting_order(
    resting_order: RestingOrder, *, client: RestingOrderVenue
) -> SubmitAck:
    """Submit a distinct :class:`RestingOrder` through an ARMED resting venue ``client``.

    The seam only maps the resting order onto the §6 wire and delegates — it performs NO arming, NO
    signing, and NO network of its own (the ``client`` must already be armed; that is the Mode-B
    admission gate's responsibility). Because the parameter is a :class:`RestingOrder`, a taker
    ``Order`` is physically un-passable here — the resting lane can never carry a FAK/FOK order.

    Args:
        resting_order: The GTC/GTD post-only resting order to rest on the book.
        client: An armed venue write client implementing :class:`RestingOrderVenue`.

    Returns:
        A :class:`~veridex.venues.base.SubmitAck` parsed from the venue response; ``accepted`` follows
        the venue ``success`` flag (a rejected post-only / expiration order is ``accepted=False`` and
        never rests).
    """
    response = await client.submit_resting_order(**resting_order.to_wire_kwargs())
    if not isinstance(response, dict):
        return SubmitAck(venue_order_id="", accepted=False)
    order_id = str(response.get("orderID") or response.get("id") or "")
    accepted = bool(response.get("success", bool(order_id)))
    return SubmitAck(venue_order_id=order_id, accepted=accepted)
