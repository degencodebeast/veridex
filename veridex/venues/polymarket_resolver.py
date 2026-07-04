"""Polymarket market resolver (REQ-2D-202, AC-2D-201).

READ-ONLY, OFFLINE-TESTED: turns a market reference for a WC soccer fixture into concrete
Polymarket on-chain identifiers (``condition_id`` + yes/no ``token_id`` + tick size) by
SELECTING the right market out of a Gamma *event* and parsing it. Tests always inject a fake
client returning recorded fixture JSON — no network in tests; only the operator's separate
live resolve step hits the real Gamma API.

Event, not single market (T13b, verified live 2026-07-02 against ``gamma-api.polymarket.com``):
a fixture slug (e.g. ``fifwc-prt-hrv-2026-07-02``) names an EVENT that contains MANY markets —
1X2 is THREE binary Yes/No markets (``"Will Portugal win…"``, ``"Will … end in a draw?"``,
``"Will Croatia win…"``), Totals is an Over/Under ladder (``"… : O/U 2.5"``), plus team-name
spreads (out of scope). So :func:`resolve_market` takes a STRUCTURED ``market_ref`` — one of
``"1X2|home|full"``, ``"1X2|away|full"``, ``"1X2|draw|full"``, ``"OU|<line>|full"`` — and
SELECTS the exact market:

* ``1X2|home`` / ``1X2|away`` → the ``"Will <TEAM> win…"`` market matched by TEAM NAME (the
  fixture's home/away team, normalized — NOT positional). Team names may differ between TxLINE
  and Polymarket ("USA" vs "United States", "Bosnia & Herzegovina" vs "Bosnia and Herzegovina");
  see :func:`_normalize_team`. Ambiguous or absent match → :class:`MarketUnavailable`.
* ``1X2|draw`` → the ``"… end in a draw?"`` market, flagged :attr:`ResolvedMarket.draw_market`
  so a live ``side="draw"`` order maps to that market's YES token (DRAW = YES) via
  :func:`side_to_token`; ``side="draw"`` on any other market fails closed.
* ``OU|<line>`` → the full-match ``"… : O/U <line>"`` market whose numeric line equals
  ``<line>``. O/U totals live on the sibling ``<slug>-more-markets`` event, so the OU lookup
  fetches that event (not the base fixture event) and excludes 2nd-half / team-total markets.

A market_ref without ``"|"`` is treated as a LEGACY direct reference: select the market whose
``slug`` equals ``fixture_hint`` (the pre-T13b behavior, kept backward-usable and fail-closed).

Gamma market shape (verified via Context7 against the official Polymarket OpenAPI spec and
cross-checked against independent references): each market object carries ``conditionId``,
``slug``, ``question``, and three JSON-*encoded-as-string* fields — ``outcomes``,
``outcomePrices``, ``clobTokenIds`` — that must be ``json.loads``'d a SECOND time and are
index-aligned (``clobTokenIds[i]`` is the token for ``outcomes[i]``). Tick size lives in
``orderPriceMinTickSize``.

The vendored CLOB wrapper (``veridex/venues/_vendor/polymarket_clob/client.py``) has no
Gamma/discovery surface — its only market method, ``get_market(condition_id)``, requires a
condition_id you don't have yet, so it can't resolve a human reference. The ``client``
injected here is therefore a small Gamma-shaped duck type
(``async def get_markets(self, **params) -> list[dict]``), independent of the vendored CLOB
client. This module has no LLM SDK imports and is not in the LLM trust path; the default
live Gamma client lazily imports ``httpx`` inside :func:`resolve_market` so importing this
module is offline-safe (like ``sx_bet``).

Cardinal honesty rule (AC-2D-201): an unknown/unavailable/malformed/ambiguous market raises
:class:`MarketUnavailable` — NEVER a fabricated, partially-guessed, or wrong-outcome
:class:`ResolvedMarket`. A wrong selection would route a real order to the WRONG outcome, so
selection fails closed on any ambiguity rather than guess. No default/placeholder ids.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Protocol

from pydantic import BaseModel

GAMMA_BASE_URL = "https://gamma-api.polymarket.com"

# TWO distinct vocabularies — do NOT merge them (a real-money side<->token bug lived in the
# overlap). A market's OUTCOME LABELS and a caller's BET SIDE are different concerns:
#
# * OUTCOME LABELS are the literal strings a Gamma market carries. In every target family the
#   labels are ``["Yes","No"]`` (per-team win / draw binaries) or ``["Over","Under"]`` (O/U);
#   spreads carry team-name labels and fail closed. No target market uses "Home"/"Away" as an
#   OUTCOME label, so those never belong here — :func:`_parse_gamma_market` uses THIS pair.
_OUTCOME_YES_LABELS = frozenset({"yes", "over"})
_OUTCOME_NO_LABELS = frozenset({"no", "under"})
#
# * BET SIDES are what a caller asks to buy. WC 1X2 is THREE per-team Yes/No markets, and each
#   side resolves to ITS OWN "Will <team> win?"/draw market where the BET TEAM is the YES
#   outcome — so home, away AND draw all buy the YES token of their resolved market (``draw``
#   only via :attr:`ResolvedMarket.draw_market`, handled separately). over/yes -> yes token;
#   under/no -> no token. :func:`side_to_token` uses THIS pair. ("away" here is the away-team-
#   WINS bet — NOT the "No" outcome of some market; conflating them inverts a live away order.)
_SIDE_YES_LABELS = frozenset({"yes", "over", "home", "away"})
_SIDE_NO_LABELS = frozenset({"no", "under"})

# Team-name aliases for robust TxLINE<->Polymarket matching. Keys/values are ALREADY
# normalized (lowercased, accent-stripped, "&"->"and", punctuation removed). Only genuine,
# well-known naming differences belong here — never a fuzzy/partial guess (AC-2D-201).
_TEAM_ALIASES: dict[str, str] = {
    "usa": "united states",
    "us": "united states",
    "usmnt": "united states",
    "united states of america": "united states",
    "uae": "united arab emirates",
    "south korea": "korea republic",
    "north korea": "korea dpr",
    "ivory coast": "cote divoire",
    "cote d ivoire": "cote divoire",
    "czech republic": "czechia",
    "cape verde": "cabo verde",
    "turkey": "turkiye",
}

# "Will <TEAM> win…"  — capture the team name up to " win".
_WIN_RE = re.compile(r"^will\s+(.+?)\s+win\b", re.IGNORECASE)
# "… end in a draw?" — the draw market.
_DRAW_RE = re.compile(r"\bend in a draw\b", re.IGNORECASE)
# "… : O/U 2.5" — capture the numeric goal line.
_OU_RE = re.compile(r"\bo\s*/\s*u\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
# Period markers that make an O/U market NON-full-match (1st/2nd half, half-time totals).
_HALF_RE = re.compile(r"\b(?:1st|2nd|first|second)\s+half\b|\bhalf\b|\bh[12]\b", re.IGNORECASE)
# Matchup marker ("vs"/"vs.") — a FULL-MATCH O/U question names both teams ("A vs. B: O/U X");
# a single-team total ("A Team Total: O/U X") does not, so this excludes team totals.
_MATCHUP_RE = re.compile(r"\bvs\.?\b", re.IGNORECASE)


def _normalize_team(name: str) -> str:
    """Normalize a team name for robust, fail-closed matching.

    Lowercases, strips accents, expands ``&`` to ``and``, drops punctuation, collapses
    whitespace, then applies the :data:`_TEAM_ALIASES` table. This absorbs trivial spelling
    differences ("Bosnia & Herzegovina" vs "Bosnia and Herzegovina") and known aliases
    ("USA" -> "United States") while a genuinely different team stays distinct (so a wrong
    fixture fails closed rather than mis-matching).
    """
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = ascii_only.lower().strip().replace("&", " and ").replace("-", " ")
    stripped = re.sub(r"[^a-z0-9 ]", " ", lowered)
    collapsed = re.sub(r"\s+", " ", stripped).strip()
    return _TEAM_ALIASES.get(collapsed, collapsed)


class ResolvedMarket(BaseModel):
    """Concrete Polymarket identifiers resolved from a human market reference.

    Attributes:
        condition_id: The market's on-chain condition ID (from Gamma ``conditionId``).
        token_id_yes: CLOB token ID for the "yes"-side outcome (Yes/Over/Home).
        token_id_no: CLOB token ID for the "no"-side outcome (No/Under/Away).
        tick_size: Minimum price increment (from Gamma ``orderPriceMinTickSize``).
        draw_market: ``True`` when this is the "…end in a draw?" binary market (from a
            ``1X2|draw`` ref), where the YES outcome IS "the match ended in a draw". Only
            then does :func:`side_to_token` map ``side="draw"`` to :attr:`token_id_yes`;
            on any other market ``side="draw"`` fails closed (a wrong outcome loses real
            money). Defaults ``False`` (a non-draw market).
    """

    condition_id: str
    token_id_yes: str
    token_id_no: str
    tick_size: float
    draw_market: bool = False


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
        side: A BET SIDE. ``"yes"``/``"over"``/``"home"``/``"away"`` map to
            :attr:`ResolvedMarket.token_id_yes` — each 1X2 side resolves to its OWN per-team
            "Will <team> win?" market whose YES outcome is that team winning, so ``"away"`` is
            the away-team-WINS bet (NOT a market's "No" outcome). ``"under"``/``"no"`` map to
            :attr:`ResolvedMarket.token_id_no`. Case-insensitive. ``"draw"`` is accepted ONLY
            when :attr:`ResolvedMarket.draw_market` is ``True`` (the "…end in a draw?" binary
            market), where DRAW = YES -> :attr:`token_id_yes`.

    Returns:
        The token ID for *side*.

    Raises:
        ValueError: If *side* is not a recognised alias, or ``side="draw"`` on a market
            that is not the draw-binary market. Never silently picks a side, and never
            mis-maps ``"draw"`` to a non-draw market's YES token (a wrong live outcome).
    """
    normalized = side.strip().lower()
    if normalized == "draw":
        # DRAW is a real venue side ONLY on the draw-binary market, where YES == "draw".
        # On any other market a bare "draw" must fail closed rather than route to its YES.
        if resolved.draw_market:
            return resolved.token_id_yes
        raise ValueError(
            "side 'draw' is only valid on the draw-binary market "
            "(resolved.draw_market is False) — failing closed"
        )
    if normalized in _SIDE_YES_LABELS:
        return resolved.token_id_yes
    if normalized in _SIDE_NO_LABELS:
        return resolved.token_id_no
    raise ValueError(f"Unknown side: {side!r}")


async def resolve_market(
    market_ref: str,
    fixture_hint: str,
    *,
    home_team: str | None = None,
    away_team: str | None = None,
    client: GammaClient | None = None,
) -> ResolvedMarket:
    """Resolve a structured market reference to concrete Polymarket identifiers.

    Fetches the fixture EVENT's market list (the injected client returns the event's
    markets), SELECTS the one market matching *market_ref*, and parses it. A wrong
    selection would route a real order to the wrong outcome, so selection fails closed on
    any ambiguity (AC-2D-201).

    Args:
        market_ref: Structured reference — ``"1X2|home|full"``, ``"1X2|away|full"``,
            ``"1X2|draw|full"``, or ``"OU|<line>|full"``. A ref without ``"|"`` is treated
            as a LEGACY direct reference (select the market whose ``slug`` == *fixture_hint*).
        fixture_hint: The Gamma EVENT ``slug`` to look up (e.g. ``fifwc-prt-hrv-2026-07-02``).
        home_team: Fixture home team name (TxLINE ``Participant`` with ``IsHome`` true), used
            to match the ``"Will <home> win…"`` market for ``1X2|home``. Team-identity params
            are passed by T14/T17/T20 from the TxLINE fixture snapshot.
        away_team: Fixture away team name, used to match the away-team win market.
        client: Injectable Gamma-shaped client. Tests ALWAYS inject a fake returning
            recorded fixture JSON. ``None`` lazily constructs the real live client
            (network, offline-safe to import — only touched when actually called).

    Returns:
        A :class:`ResolvedMarket` with condition_id, both token IDs, and tick_size. For
        ``1X2|draw`` the draw market's YES token is :attr:`ResolvedMarket.token_id_yes` and
        :attr:`ResolvedMarket.draw_market` is ``True`` so :func:`side_to_token` maps a live
        ``side="draw"`` to that YES token (DRAW = YES on the draw-binary market).

    Raises:
        MarketUnavailable: If the client lookup fails; no market matches the ref; the match
            is ambiguous (more than one candidate); *market_ref* has an unknown type; a
            name-matched ref is missing the needed team identity; or the selected market is
            malformed. Never fabricates, guesses, or routes to the wrong outcome.
    """
    if client is None:
        client = _DefaultGammaClient()

    # Route to the RIGHT Gamma event per market TYPE (verified overlap): 1X2 lives on the
    # base fixture event; O/U totals live on the sibling ``<slug>-more-markets`` event. We
    # fetch exactly one event and NEVER fall back across events — resolving OU against the
    # base event (or vice-versa) could route a live order to the wrong outcome.
    lookup_slug = _lookup_slug(market_ref, fixture_hint)

    try:
        raw = await client.get_markets(slug=lookup_slug)
    except Exception as exc:
        raise MarketUnavailable(
            f"Gamma lookup failed for market_ref={market_ref!r} slug={lookup_slug!r}: {exc}"
        ) from exc

    if not isinstance(raw, list):
        raw = [raw]
    markets = [m for m in raw if isinstance(m, dict)]

    if "|" not in market_ref:
        # Legacy direct reference: exact slug match within the returned markets.
        match = next((m for m in markets if m.get("slug") == fixture_hint), None)
        if match is None:
            raise MarketUnavailable(
                f"No Gamma market found for slug={fixture_hint!r} (market_ref={market_ref!r})"
            )
        return _parse_gamma_market(match, market_ref=market_ref, fixture_hint=fixture_hint)

    selected = _select_market(
        markets,
        market_ref=market_ref,
        fixture_hint=lookup_slug,
        home_team=home_team,
        away_team=away_team,
    )
    # A 1X2|draw ref selects the draw-binary market; flag it so side_to_token can map the
    # live side="draw" to the draw market's YES token (DRAW = YES on "…end in a draw?").
    is_draw = _is_draw_ref(market_ref)
    return _parse_gamma_market(
        selected, market_ref=market_ref, fixture_hint=lookup_slug, draw_market=is_draw
    )


def _ref_kind(market_ref: str) -> str:
    """Return the lowercased TYPE token of a structured ref (``""`` for a legacy ref)."""
    if "|" not in market_ref:
        return ""
    return market_ref.split("|", 1)[0].strip().lower()


def _is_draw_ref(market_ref: str) -> bool:
    """True when *market_ref* is a ``1X2|draw`` reference (selects the draw-binary market)."""
    parts = market_ref.split("|")
    return (
        len(parts) >= 2
        and parts[0].strip().lower() == "1x2"
        and parts[1].strip().lower() == "draw"
    )


def _lookup_slug(market_ref: str, fixture_hint: str) -> str:
    """Return the Gamma EVENT slug to fetch for *market_ref*.

    O/U totals are NOT on the base fixture event — they live on the ``<slug>-more-markets``
    event (verified overlap). 1X2 and legacy refs use the base event slug unchanged.
    """
    if _ref_kind(market_ref) == "ou":
        return f"{fixture_hint}-more-markets"
    return fixture_hint


def _select_market(
    markets: list[dict[str, Any]],
    *,
    market_ref: str,
    fixture_hint: str,
    home_team: str | None,
    away_team: str | None,
) -> dict[str, Any]:
    """Select the one event market matching *market_ref*, or fail closed.

    Parses ``TYPE|param|period`` and dispatches to the per-type matcher. Exactly one
    candidate must match: zero (no such market) or more than one (ambiguous) both raise
    :class:`MarketUnavailable` — never a guessed or partial selection (AC-2D-201).
    """
    parts = market_ref.split("|")
    kind = parts[0].strip().lower()
    param = parts[1].strip() if len(parts) > 1 else ""
    period = parts[2].strip().lower() if len(parts) > 2 else "full"

    # Only full-time markets are verified/supported; other periods fail closed (honest).
    if period not in ("", "full"):
        raise MarketUnavailable(
            f"Unsupported period {period!r} in market_ref={market_ref!r} "
            f"(only full-time supported)"
        )

    if kind == "1x2":
        candidates = _match_1x2(
            markets,
            param=param.lower(),
            market_ref=market_ref,
            home_team=home_team,
            away_team=away_team,
        )
    elif kind == "ou":
        candidates = _match_ou(markets, line=param, market_ref=market_ref)
    else:
        raise MarketUnavailable(
            f"Unknown market_ref type {kind!r} in market_ref={market_ref!r} "
            f"(supported: 1X2, OU)"
        )

    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise MarketUnavailable(
            f"No market in event slug={fixture_hint!r} matched market_ref={market_ref!r}"
        )
    raise MarketUnavailable(
        f"Ambiguous market selection for market_ref={market_ref!r} in event "
        f"slug={fixture_hint!r}: {len(candidates)} markets matched — failing closed"
    )


def _match_1x2(
    markets: list[dict[str, Any]],
    *,
    param: str,
    market_ref: str,
    home_team: str | None,
    away_team: str | None,
) -> list[dict[str, Any]]:
    """Return event markets matching a ``1X2|{home,away,draw}`` ref (0, 1, or many)."""
    if param == "draw":
        return [m for m in markets if _DRAW_RE.search(str(m.get("question", "")))]

    if param == "home":
        team = home_team
    elif param == "away":
        team = away_team
    else:
        raise MarketUnavailable(
            f"Unknown 1X2 side {param!r} in market_ref={market_ref!r} "
            f"(supported: home, away, draw)"
        )

    if not team or not team.strip():
        raise MarketUnavailable(
            f"market_ref={market_ref!r} needs the {param} team identity to match by name, "
            f"but none was provided — failing closed"
        )

    target = _normalize_team(team)
    hits: list[dict[str, Any]] = []
    for market in markets:
        win_match = _WIN_RE.match(str(market.get("question", "")).strip())
        if win_match is not None and _normalize_team(win_match.group(1)) == target:
            hits.append(market)
    return hits


def _match_ou(
    markets: list[dict[str, Any]],
    *,
    line: str,
    market_ref: str,
) -> list[dict[str, Any]]:
    """Return FULL-MATCH event markets whose ``O/U <line>`` equals *line* (0, 1, or many).

    Scope is fixed to full-match (the only supported/verified O/U scope): a candidate must
    carry the numeric line AND be a full-match market — NOT a 1st/2nd-half total and NOT a
    single-team total. Half markets (:data:`_HALF_RE`) are excluded, and the question must
    name the matchup (:data:`_MATCHUP_RE`, "A vs. B"), which team totals ("A Team Total")
    lack. Anything ambiguous surfaces to :func:`_select_market` as >1 hit and fails closed.
    """
    try:
        target_line = float(line)
    except ValueError as exc:
        raise MarketUnavailable(
            f"Malformed O/U line {line!r} in market_ref={market_ref!r}: {exc}"
        ) from exc

    hits: list[dict[str, Any]] = []
    for market in markets:
        question = str(market.get("question", ""))
        line_matches = any(
            abs(float(found.group(1)) - target_line) < 1e-9
            for found in _OU_RE.finditer(question)
        )
        if not line_matches:
            continue
        # Full-match scope only: drop period/half totals and single-team totals (fail closed
        # rather than route a full-match order to a half or team-total outcome).
        if _HALF_RE.search(question) or not _MATCHUP_RE.search(question):
            continue
        hits.append(market)
    return hits


def _parse_gamma_market(
    market: dict[str, Any],
    *,
    market_ref: str,
    fixture_hint: str,
    draw_market: bool = False,
) -> ResolvedMarket:
    """Parse a single Gamma market object into a :class:`ResolvedMarket`.

    Fails closed: any missing/malformed field raises :class:`MarketUnavailable` rather than
    crashing or guessing (AC-2D-201). *draw_market* flags the draw-binary market (a
    ``1X2|draw`` selection) so :func:`side_to_token` can map ``side="draw"`` to its YES token.
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
        if label in _OUTCOME_YES_LABELS:
            token_id_yes = token_id
        elif label in _OUTCOME_NO_LABELS:
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
        draw_market=draw_market,
    )


class _DefaultGammaClient:
    """Live Gamma client used when :func:`resolve_market` is called without an injected client.

    Fetches the ``/events`` endpoint by slug and returns the event's ``markets`` FLATTENED —
    the same flat ``list[market]`` shape the injected test client returns. This is the live
    ground truth (verified 2026-07-02): a fixture slug names an EVENT whose ``markets`` list
    holds the 3 binary 1X2 Yes/No markets. The per-market ``slug`` carries a ``-<team>``/
    ``-draw`` suffix, so ``/markets?slug=<event-slug>`` returns ``[]`` and never surfaces the
    1X2 markets — the event must be fetched and its ``markets`` unwrapped instead.

    ``httpx`` is imported lazily inside :meth:`get_markets` (not at module scope) so
    importing ``veridex.venues.polymarket_resolver`` stays offline-safe.
    """

    async def get_markets(self, **params: Any) -> list[dict[str, Any]]:
        import httpx

        async with httpx.AsyncClient(base_url=GAMMA_BASE_URL, timeout=10.0) as http:
            response = await http.get("/events", params=params)
            response.raise_for_status()
            data = response.json()
        events = data if isinstance(data, list) else [data]
        markets: list[dict[str, Any]] = []
        for event in events:
            if not isinstance(event, dict):
                continue
            event_markets = event.get("markets")
            if isinstance(event_markets, list):
                markets.extend(m for m in event_markets if isinstance(m, dict))
        return markets
