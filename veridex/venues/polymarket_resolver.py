"""Polymarket market resolver (REQ-2D-202, AC-2D-201).

READ-ONLY, OFFLINE-TESTED: turns a human market reference (a WC soccer fixture) into
concrete Polymarket on-chain identifiers (``condition_id`` + yes/no ``token_id`` + tick
size) by parsing a Gamma API (``gamma-api.polymarket.com/markets``) response. Tests always
inject a fake client returning recorded fixture JSON — no network in tests; only the
operator's separate live resolve step hits the real Gamma API.

Gamma response shape (verified via Context7 against the official Polymarket OpenAPI spec,
docs.polymarket.com, and cross-checked against independent third-party references): a
market object carries ``conditionId``, ``slug``, ``question``, and three JSON-*encoded-as-
string* fields — ``outcomes``, ``outcomePrices``, ``clobTokenIds`` — that must be
``json.loads``'d a SECOND time and are index-aligned (``clobTokenIds[i]`` is the token for
``outcomes[i]``). Tick size lives in ``orderPriceMinTickSize``.

The vendored CLOB wrapper (``veridex/venues/_vendor/polymarket_clob/client.py``) has no
Gamma/discovery surface — its only market method, ``get_market(condition_id)``, requires a
condition_id you don't have yet, so it can't resolve a human reference. The ``client``
injected here is therefore a small Gamma-shaped duck type
(``async def get_markets(self, **params) -> list[dict]``), independent of the vendored CLOB
client. This module has no LLM SDK imports and is not in the LLM trust path; the default
live Gamma client lazily imports ``httpx`` inside :func:`resolve_market` so importing this
module is offline-safe (like ``sx_bet``).

Cardinal honesty rule (AC-2D-201): an unknown/unavailable/malformed market raises
:class:`MarketUnavailable` — NEVER a fabricated or partially-guessed :class:`ResolvedMarket`.
No default/placeholder ids.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from pydantic import BaseModel

GAMMA_BASE_URL = "https://gamma-api.polymarket.com"

# Outcome labels that map to the "yes" token, per the interface contract (REQ-2D-202):
# over/home/yes -> yes-token; under/away/no -> no-token.
_YES_LABELS = frozenset({"yes", "over", "home"})
_NO_LABELS = frozenset({"no", "under", "away"})


class ResolvedMarket(BaseModel):
    """Concrete Polymarket identifiers resolved from a human market reference.

    Attributes:
        condition_id: The market's on-chain condition ID (from Gamma ``conditionId``).
        token_id_yes: CLOB token ID for the "yes"-side outcome (Yes/Over/Home).
        token_id_no: CLOB token ID for the "no"-side outcome (No/Under/Away).
        tick_size: Minimum price increment (from Gamma ``orderPriceMinTickSize``).
    """

    condition_id: str
    token_id_yes: str
    token_id_no: str
    tick_size: float


class MarketUnavailable(Exception):
    """Raised when a market reference cannot be resolved to real Polymarket identifiers.

    This is the ONLY failure mode for an unknown, unavailable, or malformed market
    (AC-2D-201) — callers must never receive a fabricated or partially-guessed
    :class:`ResolvedMarket`.
    """


class GammaClient(Protocol):
    """Structural protocol for the Gamma-shaped read client :func:`resolve_market` needs.

    Tests inject a fake implementation that returns recorded fixture JSON; the default
    (``client=None``) lazily constructs a real client that hits ``gamma-api.polymarket.com``.
    """

    async def get_markets(self, **params: Any) -> list[dict[str, Any]]:
        """Return Gamma market objects matching *params* (e.g. ``slug=...``)."""
        ...


def side_to_token(resolved: ResolvedMarket, side: str) -> str:
    """Map a bet side to the matching token ID on *resolved*.

    Args:
        resolved: The resolved market to read token IDs from.
        side: One of ``"over"``/``"home"``/``"yes"`` (-> :attr:`ResolvedMarket.token_id_yes`)
            or ``"under"``/``"away"``/``"no"`` (-> :attr:`ResolvedMarket.token_id_no`),
            case-insensitive.

    Returns:
        The token ID for *side*.

    Raises:
        ValueError: If *side* is not a recognised alias. Never silently picks a side.
    """
    normalized = side.strip().lower()
    if normalized in _YES_LABELS:
        return resolved.token_id_yes
    if normalized in _NO_LABELS:
        return resolved.token_id_no
    raise ValueError(f"Unknown side: {side!r}")


async def resolve_market(
    market_ref: str,
    fixture_hint: str,
    *,
    client: GammaClient | None = None,
) -> ResolvedMarket:
    """Resolve a human market reference to concrete Polymarket identifiers.

    Args:
        market_ref: Human-readable market description (e.g. a WC fixture), used only for
            error context — never matched fuzzily against Gamma data (no guessing).
        fixture_hint: The Gamma market ``slug`` to look up.
        client: Injectable Gamma-shaped client. Tests ALWAYS inject a fake returning
            recorded fixture JSON. ``None`` lazily constructs the real live client
            (network, offline-safe to import — only touched when actually called).

    Returns:
        A :class:`ResolvedMarket` with condition_id, both token IDs, and tick_size.

    Raises:
        MarketUnavailable: If no market matches *fixture_hint*, the client lookup fails,
            or the matched market is malformed (missing/mismatched outcomes or token IDs,
            missing condition_id, missing tick size). Never fabricates or guesses.
    """
    if client is None:
        client = _DefaultGammaClient()

    try:
        markets = await client.get_markets(slug=fixture_hint)
    except Exception as exc:
        raise MarketUnavailable(
            f"Gamma lookup failed for market_ref={market_ref!r} slug={fixture_hint!r}: {exc}"
        ) from exc

    if not isinstance(markets, list):
        markets = [markets]

    match = next(
        (m for m in markets if isinstance(m, dict) and m.get("slug") == fixture_hint),
        None,
    )
    if match is None:
        raise MarketUnavailable(
            f"No Gamma market found for slug={fixture_hint!r} (market_ref={market_ref!r})"
        )

    return _parse_gamma_market(match, market_ref=market_ref, fixture_hint=fixture_hint)


def _parse_gamma_market(market: dict[str, Any], *, market_ref: str, fixture_hint: str) -> ResolvedMarket:
    """Parse a single Gamma market object into a :class:`ResolvedMarket`.

    Fails closed: any missing/malformed field raises :class:`MarketUnavailable` rather than
    crashing or guessing (AC-2D-201).
    """
    try:
        condition_id = market["conditionId"]
        outcomes = json.loads(market["outcomes"])
        token_ids = json.loads(market["clobTokenIds"])
        tick_size = float(market["orderPriceMinTickSize"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MarketUnavailable(
            f"Malformed Gamma market for slug={fixture_hint!r} (market_ref={market_ref!r}): {exc}"
        ) from exc

    if not condition_id or not isinstance(condition_id, str):
        raise MarketUnavailable(f"Gamma market slug={fixture_hint!r} has no condition_id")

    if (
        not isinstance(outcomes, list)
        or not isinstance(token_ids, list)
        or not outcomes
        or len(outcomes) != len(token_ids)
    ):
        raise MarketUnavailable(
            f"Gamma market slug={fixture_hint!r} has malformed outcomes/clobTokenIds"
        )

    token_id_yes: str | None = None
    token_id_no: str | None = None
    for outcome, token_id in zip(outcomes, token_ids, strict=True):
        label = str(outcome).strip().lower()
        if label in _YES_LABELS:
            token_id_yes = token_id
        elif label in _NO_LABELS:
            token_id_no = token_id

    if token_id_yes is None or token_id_no is None:
        raise MarketUnavailable(
            f"Gamma market slug={fixture_hint!r} outcomes {outcomes!r} did not map to a yes/no pair"
        )

    return ResolvedMarket(
        condition_id=condition_id,
        token_id_yes=token_id_yes,
        token_id_no=token_id_no,
        tick_size=tick_size,
    )


class _DefaultGammaClient:
    """Live Gamma client used when :func:`resolve_market` is called without an injected client.

    ``httpx`` is imported lazily inside :meth:`get_markets` (not at module scope) so
    importing ``veridex.venues.polymarket_resolver`` stays offline-safe.
    """

    async def get_markets(self, **params: Any) -> list[dict[str, Any]]:
        import httpx

        async with httpx.AsyncClient(base_url=GAMMA_BASE_URL, timeout=10.0) as http:
            response = await http.get("/markets", params=params)
            response.raise_for_status()
            data = response.json()
        return data if isinstance(data, list) else [data]
