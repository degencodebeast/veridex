"""Read-only Gamma market-status authority (REQ-027 live provider / AC-053).

The ONE production-code addition outside ``veridex/mm_strategy/`` for MM-R4-B: a read-only
:class:`GammaMarketStatusAuthority` that turns the SAME Gamma market-metadata surface the
resolver already fetches into a typed :data:`~veridex.mm_strategy.contracts.MarketStatus`. The
resolver's :func:`~veridex.venues.polymarket_resolver._parse_gamma_market` parses
``conditionId``/``outcomes``/``clobTokenIds``/``orderPriceMinTickSize`` and DISCARDS the market
object's ``active``/``closed`` flags; this module RETAINS exactly those two flags and maps them:

* ``active ∧ ¬closed`` → ``ACTIVE``
* ``closed``           → ``CLOSED``   (``closed`` WINS — a closed market is CLOSED even if the
                                       object still flags ``active``, so a stale ``active`` can
                                       never re-open a settled market)
* ``¬active ∧ ¬closed`` → ``HALTED``
* parse/fetch failure / missing / malformed flag → ``UNKNOWN`` (recv_ts=None, epoch=None)

**Fail closed (trust-critical, AC-053).** A fabricated ``ACTIVE`` would let the strategy place
an order into a halted or settled market, so ANY failure — the fetch raising, a non-mapping
result, an absent or non-boolean ``active``/``closed`` — maps to ``UNKNOWN`` with the
``(None, None)`` sentinels. ``UNKNOWN`` is the honest "I could not prove a definite status";
the assembler's market-active gate (REQ-026) then fails closed on it.

**Read-only.** This module imports NOTHING that submits, cancels, or signs — no
``veridex.venues.base``, no ``veridex.venues.sx_bet``, no ``submit_order``/``cancel_order``,
no private key / EIP-712 surface, no LLM SDK. ``ResolvedMarket`` and the resolver are UNCHANGED;
this is purely additive.

**Offline-safe + injectable.** The Gamma read is an INJECTED ``fetch`` seam
(``Callable[[str], object]``) so the whole surface is exercised with canned metadata in tests
and never opens a socket at import or in the suite. An operator wires a live-backed sync fetcher
(over the resolver's read-only Gamma ``get_markets`` surface) only when driving a real venue; the
test suite never does. ``recv_ts`` comes from an injected ``clock`` (a fixed clock in tests); the
default is a millisecond wall clock, matching the recorder's millisecond ``recv_ts`` convention.

**Epoch (source generation).** ``epoch`` is the authority's generation counter, supplied at
construction (default ``0``) and returned unchanged on every successful read. A restart is a new
generation: the operator/assembler constructs a fresh authority with ``epoch = prior + 1`` so
``read`` satisfies REQ-027's "restart ⇒ epoch increment" while the durable source-generation
state stays owned by the assembler (E3-T2), the sole author of source epochs.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from veridex.mm_strategy.contracts import MarketStatus


class _GammaStatusUnavailable(Exception):
    """Internal fail-closed marker: the metadata cannot yield a definite ACTIVE/HALTED/CLOSED.

    Never escapes :meth:`GammaMarketStatusAuthority.read` — it is always caught there and mapped
    to ``UNKNOWN`` with the ``(None, None)`` sentinels.
    """


def status_from_gamma_market(market: Mapping[str, Any]) -> MarketStatus:
    """Map a Gamma market object's ``active``/``closed`` flags to a definite :data:`MarketStatus`.

    Retains the two flags :func:`_parse_gamma_market` discards. ``closed`` WINS: a closed market
    is ``CLOSED`` regardless of ``active``. Raises :class:`_GammaStatusUnavailable` when either
    flag is missing or not a real boolean (the caller maps that to ``UNKNOWN``) — a string/int is
    malformed metadata, never silently coerced.
    """
    active = market.get("active")
    closed = market.get("closed")
    if not isinstance(active, bool) or not isinstance(closed, bool):
        raise _GammaStatusUnavailable(
            f"Gamma market missing/malformed active/closed flags: "
            f"active={active!r}, closed={closed!r}"
        )
    if closed:
        return "CLOSED"
    if active:
        return "ACTIVE"
    return "HALTED"


def _default_recv_ts_ms() -> int:
    """Default receive clock: integer milliseconds (recorder ``recv_ts`` convention)."""
    return time.time_ns() // 1_000_000


class GammaMarketStatusAuthority:
    """Read-only :class:`~veridex.mm_strategy.contracts.MarketStatusAuthority` over Gamma metadata.

    Parses the SAME Gamma market object the resolver fetches, retaining ``active``/``closed``.
    Fails closed to ``UNKNOWN`` on any fetch/parse failure — never fabricating a definite status.
    """

    def __init__(
        self,
        *,
        fetch: Callable[[str], Any],
        clock: Callable[[], int] | None = None,
        epoch: int = 0,
    ) -> None:
        """Wire the injectable seams.

        Args:
            fetch: Sync seam returning the Gamma market object for a ``venue_market_ref``. Tests
                inject a pure lookup over canned metadata; an operator injects a live-backed
                fetcher (over the resolver's read-only Gamma surface). Any exception it raises is
                caught and mapped to ``UNKNOWN``.
            clock: Receive clock (integer milliseconds). Defaults to a wall clock; tests inject a
                fixed clock for determinism. Called ONLY on a successful, definite read.
            epoch: This authority's source generation. Returned on every successful read; a
                restart constructs a new authority with ``epoch + 1``.
        """
        self._fetch = fetch
        self._clock: Callable[[], int] = clock if clock is not None else _default_recv_ts_ms
        self._epoch = epoch

    def read(
        self, venue_market_ref: str
    ) -> tuple[MarketStatus, int | None, int | None]:
        """Return ``(status, recv_ts, epoch)`` for *venue_market_ref*.

        A definite status (``ACTIVE``/``HALTED``/``CLOSED``) carries a non-None ``recv_ts`` (the
        read clock) and ``epoch`` (the authority generation). Any failure — the fetch raising, a
        non-mapping result, a missing/malformed ``active``/``closed`` flag, or an IDENTITY MISMATCH
        (the returned object describes a different market) — maps to ``("UNKNOWN", None, None)``
        (fail closed; the ``None`` sentinels iff ``UNKNOWN``).

        **Market-identity binding (trust-critical, REQ-027, Gate #2 MAJOR-3).** The response is
        BOUND to the request: the returned Gamma object's ``conditionId`` (the SAME on-chain identity
        the resolver's :func:`_parse_gamma_market` keys on) must EQUAL *venue_market_ref* before any
        flag is trusted. A fetch for ``0xEXPECTED`` returning ``{conditionId: 0xFOREIGN, active,
        ¬closed}`` is a FOREIGN market's status and fails closed to ``UNKNOWN`` — a stale/misrouted
        fetch can never let another market's ACTIVE authorize placement into this one. A missing or
        non-string ``conditionId`` is equally unprovable and also fails closed.
        """
        try:
            market = self._fetch(venue_market_ref)
            if not isinstance(market, Mapping):
                raise _GammaStatusUnavailable(
                    f"Gamma fetch for venue_market_ref={venue_market_ref!r} returned a "
                    f"non-mapping result: {type(market).__name__}"
                )
            returned_ref = market.get("conditionId")
            if not isinstance(returned_ref, str) or returned_ref != venue_market_ref:
                raise _GammaStatusUnavailable(
                    f"Gamma market identity mismatch: requested "
                    f"venue_market_ref={venue_market_ref!r} but returned "
                    f"conditionId={returned_ref!r} (foreign/unidentifiable market)"
                )
            status = status_from_gamma_market(market)
        except Exception:
            # Fail closed: ANY fetch/parse/identity failure is UNKNOWN, never a fabricated definite
            # status and never a foreign market's status.
            return ("UNKNOWN", None, None)
        return (status, int(self._clock()), self._epoch)
