"""E5 counterfactual executability for the live-recorder lane (MM-R3).

This module turns an observed :class:`~veridex.live_recorder.sources.BookSnapshot`
into a COUNTERFACTUAL clearing measurement — *what the observed book WOULD have cost to
clear at time T* — and NEVER an own fill, fill rate, or realized value.

Trust boundaries enforced here (the whole point of E5):

* **COUNTERFACTUAL only** — :func:`measure_take` returns an
  :class:`~veridex.live_recorder.contracts.ExecutabilityMeasurement` whose ``label`` is
  pinned to ``"COUNTERFACTUAL"``. It records NO fill: ``clears`` means "the observed book
  showed enough resting size at/inside a cost-clearing price", an OBSERVATION, not a fill.
* **No fill/PnL/edge** — the E1 measurement model is ``extra="forbid"`` and has no
  ``fill_price``/``filled_size``/``realized_pnl``/``real_executable_edge_bps`` field; this
  module never tries to add one.
* **Queue-jump is DERIVED, not stored** — :func:`derive_queue_jump` computes
  ``outbid_within_ms``/``stepped_ahead_count`` from the post-decision book stream into a
  SEPARATE :class:`QueueJumpDerivation`; it MUST NOT mutate any ``QuoteIntentEvent`` (which
  ``extra="forbid"`` rejects those fields anyway).
* **No queue-fill probability / simulation** — no ``fill_probability``/``queue_fill``/
  ``queue_simulation`` is ever produced, regardless of what deltas/trades would allow.
* **Pinned fee config** — :func:`bind_fee_config` pins a
  :class:`~veridex.live_recorder.contracts.FillAssumptionConfig` hash BEFORE measurement;
  the Rose 4x variant is simply ``fee_stress_multiplier=4``.

Native ``[0,1]`` prices throughout. This module imports nothing from ``veridex.scoring``
or ``veridex.maker`` and touches no network.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from veridex.live_recorder.contracts import (
    BookLevel,
    ExecutabilityMeasurement,
    FillAssumptionConfig,
)
from veridex.live_recorder.sources import BookSnapshot

# One basis point as a fraction (fee bps -> price fraction).
_BPS = 1e4


def _best_price(levels: tuple[BookLevel, ...], *, side: str) -> float | None:
    """Best (top-of-book) price for a side, or ``None`` if the side is empty (never imputed)."""
    if not levels:
        return None
    if side == "bid":
        return max(level.price for level in levels)
    return min(level.price for level in levels)


def measure_take(
    snapshot: BookSnapshot,
    candidate_price: float,
    desired_size: float,
    fee_config: FillAssumptionConfig,
    *,
    stale_window_s: int = 0,
    pinned_config_hash: str | None = None,
) -> ExecutabilityMeasurement:
    """Walk observed ask depth into a COUNTERFACTUAL :class:`ExecutabilityMeasurement`.

    A take/buy walks the asks (mirrors ``LOB.get_cumulative_size(dir=1, price)``): every ask
    level with ``price <= candidate_price`` is resting size we could observably clear against.
    We compute ``available_size_at_price`` (resting size at exactly ``candidate_price``),
    ``cumulative_size_to_clear`` (cumulative resting size up to ``candidate_price``),
    ``spread``/``half_spread``, a fee-stressed ``cost_clearing_threshold``, and ``clears``
    (whether ``desired_size`` is observably clearable). ``label`` is ALWAYS
    ``"COUNTERFACTUAL"`` — this is an OBSERVATION of the book, never an own fill.

    When ``pinned_config_hash`` is given the passed ``fee_config`` is bound to it BEFORE
    measurement (see :func:`bind_fee_config`) so the fee assumptions cannot be edited after
    the fact.
    """
    if pinned_config_hash is not None:
        bind_fee_config(fee_config, pinned_config_hash)

    # Depth walk over the asks (mirror of LOB.get_cumulative_size, dir=1): accumulate resting
    # size at every ask level priced at or inside the candidate price.
    cumulative_size_to_clear = 0.0
    available_size_at_price = 0.0
    for level in snapshot.asks:
        if level.price > candidate_price:
            break
        cumulative_size_to_clear += level.size
        if level.price == candidate_price:
            available_size_at_price += level.size

    best_bid = _best_price(snapshot.bids, side="bid")
    best_ask = _best_price(snapshot.asks, side="ask")
    spread = (best_ask - best_bid) if (best_bid is not None and best_ask is not None) else 0.0
    half_spread = spread / 2.0

    # Fee-stressed clearing cost: the counterfactual take pays the (stressed) taker fee on top
    # of the candidate price. With taker_fee_bps=0 this is exactly the candidate price.
    fee_fraction = fee_config.taker_fee_bps * fee_config.fee_stress_multiplier / _BPS
    cost_clearing_threshold = candidate_price * (1.0 + fee_fraction)

    clears = desired_size <= cumulative_size_to_clear

    return ExecutabilityMeasurement(
        candidate_price=candidate_price,
        available_size_at_price=available_size_at_price,
        cumulative_size_to_clear=cumulative_size_to_clear,
        spread=spread,
        half_spread=half_spread,
        cost_clearing_threshold=cost_clearing_threshold,
        taker_fee_bps=fee_config.taker_fee_bps,
        fee_stress_multiplier=fee_config.fee_stress_multiplier,
        stale_window_s=stale_window_s,
        clears=clears,
        label="COUNTERFACTUAL",
    )


def bind_fee_config(fee_config: FillAssumptionConfig, pinned_hash: str) -> FillAssumptionConfig:
    """Assert *fee_config*'s hash equals *pinned_hash* (pinned BEFORE measurement), else raise.

    This is the AC-010/EXE-004 guard: the fee assumptions (incl the Rose 4x
    ``fee_stress_multiplier``) are pinned up front and can never be edited after the fact.
    """
    actual = fee_config.config_hash()
    if actual != pinned_hash:
        raise ValueError(
            "fee_config hash does not match the pinned hash "
            f"(pinned={pinned_hash!r}, got={actual!r}); the fee assumptions are pinned "
            "before measurement and may never be edited after the fact"
        )
    return fee_config


def fee_stress_grid(
    *,
    taker_fee_bps: float,
    fee_stress_multipliers: tuple[float, ...] = (1.0, 4.0),
    spread_assumption: float = 0.0,
    slippage_assumption: float = 0.0,
) -> tuple[FillAssumptionConfig, ...]:
    """Build the pinned fee-stress grid of :class:`FillAssumptionConfig`s.

    Every dimension is PINNED by the caller (never discovered after the fact): the taker fee
    is ALWAYS applied, and the multiplier axis spans the declared stress points — the Rose
    stress variant is ``fee_stress_multiplier=4``. Returns configs only; it produces NO
    ``fill_probability``/``queue_fill``/``queue_simulation`` output.
    """
    return tuple(
        FillAssumptionConfig(
            taker_fee_bps=taker_fee_bps,
            fee_stress_multiplier=multiplier,
            spread_assumption=spread_assumption,
            slippage_assumption=slippage_assumption,
        )
        for multiplier in fee_stress_multipliers
    )


class QueueJumpDecision(BaseModel):
    """A minimal decision handle for :func:`derive_queue_jump` (analysis-time only).

    Carries just the decision-time context queue-jump derivation needs — ``decision_id``,
    the ``side`` and ``native_price`` we quoted, and the decision ``recv_ts`` — so a
    post-decision book stream can be analysed WITHOUT touching the immutable intent event.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_id: str
    side: str
    native_price: float
    recv_ts: int


class QueueJumpDerivation(BaseModel):
    """A SEPARATE analysis object — the DERIVED post-decision queue-jump outcome.

    This is produced at analysis time from the post-decision book stream and is NEVER stored
    on :class:`~veridex.live_recorder.contracts.QuoteIntentEvent`: ``outbid_within_ms`` and
    ``stepped_ahead_count`` live here, keyed back to the decision by ``decision_id``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_id: str
    outbid_within_ms: int | None
    stepped_ahead_count: int


def queue_ahead_at(snapshot: BookSnapshot, side: str, native_price: float) -> float | None:
    """Decision-time resting size AHEAD of us on our side at ``native_price``, or ``None``.

    For a resting bid (the default; a ``"join"`` posts a bid on our token book), the size
    ahead of us is every bid resting at a price at least as good as ours — bids with
    ``price >= native_price`` (better-priced or same-priced-and-earlier orders fill first).
    For an ask/sell we mirror it (``price <= native_price``). If the relevant book side is
    empty we return ``None`` (illiquid side is honest — a size is NEVER imputed). This value
    is what gets stored on ``QuoteIntentEvent.queue_ahead_size``.
    """
    if side.strip().lower() in {"ask", "asks", "sell", "offer"}:
        if not snapshot.asks:
            return None
        return sum(level.size for level in snapshot.asks if level.price <= native_price)
    if not snapshot.bids:
        return None
    return sum(level.size for level in snapshot.bids if level.price >= native_price)


def _stepped_ahead(levels: tuple[BookLevel, ...], side: str, native_price: float) -> bool:
    """Whether *levels* show someone resting strictly AHEAD of ``native_price`` on our side."""
    if side.strip().lower() in {"ask", "asks", "sell", "offer"}:
        return any(level.price < native_price for level in levels)
    return any(level.price > native_price for level in levels)


def derive_queue_jump(
    decision: QueueJumpDecision,
    subsequent_book_events: list[BookSnapshot],
) -> QueueJumpDerivation:
    """DERIVE the post-decision queue-jump outcome — a SEPARATE object, never a stored field.

    Walks the post-decision book stream (``subsequent_book_events``, book snapshots observed
    AFTER ``decision.recv_ts``) and, keyed by ``decision.decision_id``, computes:

    * ``stepped_ahead_count`` — how many post-decision books showed someone resting strictly
      ahead of our ``native_price`` on our side;
    * ``outbid_within_ms`` — the ms from ``decision.recv_ts`` to the FIRST such book's
      ``book_ts`` (``None`` if nobody ever stepped ahead).

    This NEVER mutates ``decision`` or any ``QuoteIntentEvent`` — the result is a fresh
    :class:`QueueJumpDerivation`. No queue-fill probability / simulation is produced.
    """
    stepped_ahead_count = 0
    outbid_within_ms: int | None = None
    for book in subsequent_book_events:
        if book.book_ts < decision.recv_ts:
            continue
        side_levels = book.asks if decision.side.strip().lower() in {
            "ask",
            "asks",
            "sell",
            "offer",
        } else book.bids
        if _stepped_ahead(side_levels, decision.side, decision.native_price):
            stepped_ahead_count += 1
            if outbid_within_ms is None:
                outbid_within_ms = int(book.book_ts - decision.recv_ts)
    return QueueJumpDerivation(
        decision_id=decision.decision_id,
        outbid_within_ms=outbid_within_ms,
        stepped_ahead_count=stepped_ahead_count,
    )
