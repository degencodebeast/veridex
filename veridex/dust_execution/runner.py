"""E6-T1 — ``run_dust_execution`` skeleton + submit gates (SAF-007, AC-010/017, §6 group 6).

The REAL-fill dust-execution runner's SKELETON and the SAFETY-CORE submit gates. Everything is
INJECTED — the venue ``adapter``, the ``signer`` control plane, the quote ``sources``, the
``now_fn`` / ``sleep_fn`` clocks, the ``envelope`` + ``manifest`` pins, and the execution ``mode``
— so the runner holds no wall-clock, opens no connection, and (in Mode A) places NO orders. This
matches the dust lane's async discipline (injected clocks, deterministic tests, Mode B UNARMED).

Submit gates (the safety core of E6-T1). The runner REFUSES to submit (abstains — no order reaches
the adapter) whenever ANY of the following holds for a token's quote:

* **stale by age** — ``now - quote_ts_s > envelope.max_quote_age_s`` (AC-010);
* **stale / gapped source** — ``sources.read_quote`` raises :class:`StaleVenueBook` (the source is
  disconnected / mid-resync / gapped and refuses to serve a stale book);
* **event-suspended market** — ``quote.event_suspended``;
* **no-quote / boundary state** — ``quote.no_quote``;
* **negative-liquidity book** — a book side with ``size < 0``;
* **missing book side** — a side is absent; it is ABSTAINED, **never imputed / fabricated**.

Only when EVERY gate is clear AND the mode is ``live_guarded`` (Mode B) does the runner build and
submit an order on the wire; in ``dry_run`` (Mode A) a clean quote still places NO order. The
decision telemetry is boolean/id/closed-vocab only — no secret, signer artifact, order, or raw
venue handle ever crosses into :class:`SubmitDecision` (SEC-005 discipline).

SCOPE (E6-T1): skeleton + submit gates ONLY. The following are DELIBERATELY left as clean seams for
later E6 tasks and are NOT wired here — full lifecycle-event emission (E6-T2); risk / emergency /
reconcile / non-crossing wiring and ``SafetyController`` delegation (E6-T3); the Mode A→B arming
gate, manifest authorization, and ``resolve_dust_size`` binding + native→decimal pricing (E6-T4);
the startup sweep (E6-T5); shutdown (E6-T6); losing-session status (E6-T7). The Mode B order built
here uses PROVISIONAL price/size placeholders purely to exercise the (recording-fake, offline)
submit wire the gates protect — real sizing/pricing binding is E6-T4.

SEC-003: this module imports only intra-lane ``veridex.dust_execution.*``, the shared
``veridex.policy.envelope`` (the single breach-boundary source of truth, not a ranked lane), and
``veridex.venues.base`` (the pure adapter Protocol/value types) — never ``veridex.live_recorder``
and never a ranked maker/scoring/leaderboard module. :class:`StaleVenueBook` is defined IN-LANE
(the live-recorder lane owns its own same-named exception; this is a copy, not an import).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from veridex.dust_execution.contracts import ExecutionMode
from veridex.dust_execution.manifest import StrategyExperimentManifest
from veridex.dust_execution.signer import Signer, SigningPayload
from veridex.policy.envelope import PolicyEnvelope
from veridex.venues.base import Order, VenueAdapter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# In-lane staleness signal (SEC-003: NOT imported from veridex.live_recorder)
# ---------------------------------------------------------------------------


class StaleVenueBook(Exception):
    """The injected quote source refuses to serve a stale / gapped / disconnected / mid-resync book.

    Mirrors the ``veridex.live_recorder.ws_book_source.StaleVenueBook`` CONCEPT but is defined here,
    in-lane: SEC-003 keeps ``veridex.dust_execution`` isolated from ``veridex.live_recorder``, so the
    source raises THIS exception (a copy, never an import) and the runner catches it as a submit gate.
    """


# ---------------------------------------------------------------------------
# Injected quote-source value types + Protocol (the E1-T2 venue-book read seam)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BookSide:
    """One side of a book: a native ``[0,1]`` price and its resting liquidity ``size``.

    A negative ``size`` is a negative-liquidity book (a submit gate); prices are validated
    downstream by the E5 non-crossing check (consumed by a later E6 task), not here.
    """

    price: float
    size: float


@dataclass(frozen=True)
class DustQuote:
    """A venue-book snapshot for one outcome token, as read from the injected source.

    Attributes:
        token_id: The outcome-token id the quote is for.
        quote_ts_s: Source-clock capture time in integer SECONDS (age is compared against
            ``envelope.max_quote_age_s``, which is also seconds).
        event_suspended: ``True`` when the market is event-suspended (a submit gate).
        no_quote: ``True`` for an explicit no-quote / boundary state (a submit gate).
        bid: The bid side, or ``None`` when absent — a MISSING side is abstained, never imputed.
        ask: The ask side, or ``None`` when absent — a MISSING side is abstained, never imputed.
    """

    token_id: str
    quote_ts_s: int
    event_suspended: bool = False
    no_quote: bool = False
    bid: BookSide | None = None
    ask: BookSide | None = None


@runtime_checkable
class QuoteSource(Protocol):
    """The injected async venue-book read seam (a recording-fake in tests, never a live venue).

    Raises :class:`StaleVenueBook` when the underlying source is gapped / disconnected / mid-resync
    and cannot serve a fresh book — the runner treats that as a submit gate (abstain, no wire).
    """

    async def read_quote(self, token_id: str) -> DustQuote: ...


# ---------------------------------------------------------------------------
# Submit-gate outcome telemetry (boolean / id / closed-vocab ONLY — no secret)
# ---------------------------------------------------------------------------

#: The single closed vocabulary of abstain reasons — boolean-safe, id-free telemetry (SEC-005).
AbstainReason = Literal[
    "stale_quote_age",
    "stale_source",
    "event_suspended",
    "no_quote",
    "missing_book_side",
    "negative_liquidity",
    "mode_a_no_orders",
]

#: Tuple form of :data:`AbstainReason` for membership checks / iteration.
ABSTAIN_REASONS: tuple[AbstainReason, ...] = (
    "stale_quote_age",
    "stale_source",
    "event_suspended",
    "no_quote",
    "missing_book_side",
    "negative_liquidity",
    "mode_a_no_orders",
)


@dataclass(frozen=True)
class SubmitDecision:
    """The per-token submit/abstain decision — carries ONLY JSON-primitive, non-secret telemetry.

    Never carries a raw order, signer artifact, or venue handle (mirrors the ``facade`` boundary
    discipline): ``abstain_reason`` is a closed-vocabulary label, ``venue_order_id`` a non-secret id.
    """

    token_id: str
    submitted: bool
    abstain_reason: AbstainReason | None
    venue_order_id: str | None = None


@dataclass(frozen=True)
class DustExecutionResult:
    """The result of one dust-execution pass over the manifest universe."""

    mode: ExecutionMode
    decisions: tuple[SubmitDecision, ...]

    @property
    def submitted_count(self) -> int:
        """How many decisions actually reached the submit wire (0 in Mode A)."""
        return sum(1 for d in self.decisions if d.submitted)

    @property
    def abstained_count(self) -> int:
        """How many decisions abstained (did NOT submit)."""
        return sum(1 for d in self.decisions if not d.submitted)


# ---------------------------------------------------------------------------
# The submit gate: pure, deterministic, fail-closed to abstain
# ---------------------------------------------------------------------------


def _evaluate_submit_gate(quote: DustQuote, *, now_s: int, max_quote_age_s: int) -> AbstainReason | None:
    """Return the abstain reason gating this quote, or ``None`` when EVERY gate is clear.

    Order is chosen so the most structural refusals report first, but ALL of them abstain (no order
    reaches the wire). A missing book side returns ``"missing_book_side"`` and is NEVER imputed — the
    absent side is not fabricated to let the quote through.
    """
    if quote.event_suspended:
        return "event_suspended"
    if quote.no_quote:
        return "no_quote"
    if quote.bid is None or quote.ask is None:
        # A missing side is ABSTAINED, never imputed/fabricated (AC-017).
        return "missing_book_side"
    if quote.bid.size < 0.0 or quote.ask.size < 0.0:
        return "negative_liquidity"
    # Staleness-by-age gate (AC-010) — THE mutation target. ``max_quote_age_s`` and ``quote_ts_s``
    # are both integer seconds; strictly-greater-than age fails closed to abstain.
    if now_s - quote.quote_ts_s > max_quote_age_s:
        return "stale_quote_age"
    return None


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------


async def run_dust_execution(
    *,
    adapter: VenueAdapter,
    signer: Signer,
    sources: QuoteSource,
    now_fn: Callable[[], int],
    sleep_fn: Callable[[float], Awaitable[None]],
    envelope: PolicyEnvelope,
    manifest: StrategyExperimentManifest,
    mode: ExecutionMode,
) -> DustExecutionResult:
    """Run one dust-execution pass over the manifest universe, applying the submit gates.

    For each token the runner reads the injected source, applies the submit gates, and abstains
    (no order on the wire) on any gate. Only when EVERY gate is clear AND ``mode == "live_guarded"``
    (Mode B) does it build and submit an order; in ``dry_run`` (Mode A) a clean quote still places
    NO order.

    ``sleep_fn`` is the injected async delay seam for the E6 polling loop (added by a later task);
    this skeleton makes a single deterministic pass and does not sleep. The Mode B order uses
    PROVISIONAL price/size placeholders purely to exercise the (offline recording-fake) submit wire
    the gates protect — real sizing/pricing (``resolve_dust_size`` + native→decimal) is E6-T4.

    Args:
        adapter: Injected venue adapter (a recording-fake in tests; never a live venue in E6-T1).
        signer: Injected provider-neutral signing control plane (Mode-A fake offline).
        sources: Injected quote source; raises :class:`StaleVenueBook` when gapped/disconnected.
        now_fn: Injected clock returning integer SECONDS (used for the staleness gate).
        sleep_fn: Injected async delay seam (unused in this single-pass skeleton; wired later).
        envelope: Policy envelope providing ``max_quote_age_s`` and the venue allowlist.
        manifest: Pinned strategy manifest providing the token ``universe`` to quote.
        mode: Execution mode — ``"dry_run"`` (Mode A, no orders) or ``"live_guarded"`` (Mode B).

    Returns:
        A :class:`DustExecutionResult` with one :class:`SubmitDecision` per token in the universe.
    """
    decisions: list[SubmitDecision] = []
    for token_id in manifest.universe:
        decisions.append(
            await _decide_and_submit(
                token_id,
                adapter=adapter,
                signer=signer,
                sources=sources,
                now_fn=now_fn,
                envelope=envelope,
                manifest=manifest,
                mode=mode,
            )
        )
    return DustExecutionResult(mode=mode, decisions=tuple(decisions))


async def _decide_and_submit(
    token_id: str,
    *,
    adapter: VenueAdapter,
    signer: Signer,
    sources: QuoteSource,
    now_fn: Callable[[], int],
    envelope: PolicyEnvelope,
    manifest: StrategyExperimentManifest,
    mode: ExecutionMode,
) -> SubmitDecision:
    """Gate one token's quote and, only when clear AND Mode B, submit it on the wire."""
    try:
        quote = await sources.read_quote(token_id)
    except StaleVenueBook:
        # A gapped / disconnected / mid-resync source — abstain, nothing reaches the wire.
        return _abstain(token_id, "stale_source")

    reason = _evaluate_submit_gate(quote, now_s=now_fn(), max_quote_age_s=envelope.max_quote_age_s)
    if reason is not None:
        return _abstain(token_id, reason)

    if mode != "live_guarded":
        # Mode A (dry_run) places NO orders even when every gate is clear (AC-017).
        return _abstain(token_id, "mode_a_no_orders")

    # Mode B, every gate clear: sign then submit the ONE order the gates protect. Both sides are
    # present here (missing-side would have abstained above), so the assertions below are safe.
    assert quote.bid is not None and quote.ask is not None  # noqa: S101 - narrows for type-checker
    ack = await _sign_and_submit(quote, adapter=adapter, signer=signer, envelope=envelope, manifest=manifest)
    logger.info(
        "dust_execution.submit",
        extra={"token_id": token_id, "submitted": True, "mode": mode},
    )
    return SubmitDecision(
        token_id=token_id,
        submitted=True,
        abstain_reason=None,
        venue_order_id=ack,
    )


async def _sign_and_submit(
    quote: DustQuote,
    *,
    adapter: VenueAdapter,
    signer: Signer,
    envelope: PolicyEnvelope,
    manifest: StrategyExperimentManifest,
) -> str:
    """Sign the (provisional) payload then submit the order; return the venue order id.

    PROVISIONAL binding: the price/size here are skeleton placeholders that exercise the submit
    wire only — real ``resolve_dust_size`` sizing and native→decimal pricing land in E6-T4.
    """
    assert quote.ask is not None  # gate guaranteed both sides present
    client_order_id = f"{manifest.strategy_id}:{quote.token_id}"
    payload = SigningPayload(
        token_id=quote.token_id,
        side="BUY",
        native_price=quote.ask.price,
        size=1.0,  # provisional placeholder — real size binding is E6-T4 (resolve_dust_size)
        tif="FOK",
        tick_size="0.01",
        client_order_id=client_order_id,
    )
    await signer.sign_order(payload)
    order = Order(
        market_ref=manifest.market,
        side="BUY",
        size=1.0,  # provisional placeholder — real size binding is E6-T4
        price=1.0 / quote.ask.price,  # provisional native→decimal — real pricing is E6-T4
        venue=envelope.venue_allowlist[0] if envelope.venue_allowlist else "dust",
        client_order_id=client_order_id,
    )
    ack = await adapter.submit_order(order)
    return ack.venue_order_id


def _abstain(token_id: str, reason: AbstainReason) -> SubmitDecision:
    """Build an abstaining decision (no order on the wire) with boolean/id-only telemetry."""
    logger.info(
        "dust_execution.abstain",
        extra={"token_id": token_id, "submitted": False, "abstain_reason": reason},
    )
    return SubmitDecision(token_id=token_id, submitted=False, abstain_reason=reason, venue_order_id=None)


__all__ = [
    "ABSTAIN_REASONS",
    "AbstainReason",
    "BookSide",
    "DustExecutionResult",
    "DustQuote",
    "QuoteSource",
    "StaleVenueBook",
    "SubmitDecision",
    "run_dust_execution",
]
