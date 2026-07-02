"""Polymarket market resolver tests (REQ-2D-202, AC-2D-201) — TDD.

The resolver is READ-ONLY and OFFLINE-TESTED: it turns a human market reference (a WC
soccer fixture) into concrete Polymarket on-chain identifiers by parsing a RECORDED Gamma
API (``gamma-api.polymarket.com/markets``) response — never a live call in tests.

Gamma response shape verified against the official Polymarket OpenAPI spec
(docs.polymarket.com, fetched via Context7) and cross-checked against two independent
third-party references: a market object carries ``conditionId``, ``slug``, ``question``,
and three JSON-*encoded-as-string* fields — ``outcomes``, ``outcomePrices``,
``clobTokenIds`` — that must be ``json.loads``'d a second time and are index-aligned. Tick
size lives in ``orderPriceMinTickSize``. The vendored CLOB wrapper
(``veridex/venues/_vendor/polymarket_clob/client.py``) has no Gamma/discovery surface at
all (only ``get_market(condition_id)``, which needs a condition_id you don't have yet) —
so the injected ``client`` here is a small Gamma-shaped duck type
(``async def get_markets(self, **params) -> list[dict]``), not the vendored CLOB client.

Cardinal honesty rule (AC-2D-201): an unknown/unavailable/malformed market raises
``MarketUnavailable`` — NEVER a fabricated or partially-guessed ``ResolvedMarket``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from veridex.venues import polymarket_resolver
from veridex.venues.polymarket_resolver import (
    MarketUnavailable,
    ResolvedMarket,
    resolve_market,
    side_to_token,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


class _FakeGammaClient:
    """Offline stand-in for a Gamma-shaped client: returns recorded fixture JSON, no network."""

    def __init__(self, fixture_name: str) -> None:
        self._markets: list[dict] = json.loads((FIXTURES_DIR / fixture_name).read_text())

    async def get_markets(self, **params: object) -> list[dict]:
        return self._markets


# ---------------------------------------------------------------------------
# resolve_market: happy path, parsed from a recorded Gamma payload
# ---------------------------------------------------------------------------


async def test_resolve_market_parses_yes_no_gamma_fixture() -> None:
    """A recorded Yes/No Gamma market resolves to the correct condition_id/token_ids/tick_size."""
    client = _FakeGammaClient("gamma_wc_final.json")

    resolved = await resolve_market(
        "Argentina vs France — 2026 World Cup Final",
        "argentina-wins-2026-world-cup-final",
        client=client,
    )

    assert resolved == ResolvedMarket(
        condition_id="0x3b1f3f5c2e1a4d6b8f0c9e7a5d4b3c2a1f0e9d8c7b6a5f4e3d2c1b0a9f8e7d6c",
        token_id_yes="71234567890123456789012345678901234567890123456789012345678901",
        token_id_no="10987654321098765432109876543210987654321098765432109876543210",
        tick_size=0.01,
    )


async def test_resolve_market_maps_over_under_outcomes() -> None:
    """Over/Under-labelled outcomes map to yes_token/no_token, not just Yes/No labels."""
    client = _FakeGammaClient("gamma_wc_total_goals.json")

    resolved = await resolve_market(
        "Argentina vs France — Over 2.5 goals",
        "argentina-vs-france-2026-final-over-2-5-goals",
        client=client,
    )

    assert resolved.token_id_yes == "55566677788899900011122233344455566677788899900011122233344455"
    assert resolved.token_id_no == "66677788899900011122233344455566677788899900011122233344455566"


# ---------------------------------------------------------------------------
# AC-2D-201 cardinal honesty: unknown/malformed -> MarketUnavailable, never a guess
# ---------------------------------------------------------------------------


async def test_resolve_market_unknown_fixture_raises_market_unavailable() -> None:
    """A fixture_hint matching nothing in the Gamma payload raises MarketUnavailable."""
    client = _FakeGammaClient("gamma_wc_final.json")

    with pytest.raises(MarketUnavailable):
        await resolve_market(
            "Some fixture nobody recorded",
            "no-such-slug-anywhere",
            client=client,
        )


async def test_resolve_market_malformed_market_raises_market_unavailable_not_crash() -> None:
    """A Gamma market missing clobTokenIds raises MarketUnavailable, not a raw exception."""
    client = _FakeGammaClient("gamma_malformed_missing_tokens.json")

    with pytest.raises(MarketUnavailable):
        await resolve_market(
            "Brazil wins 2026 World Cup Final",
            "brazil-wins-2026-world-cup-final",
            client=client,
        )


async def test_resolve_market_empty_gamma_response_raises_market_unavailable() -> None:
    """An empty Gamma markets list (no matches at all) raises MarketUnavailable, not KeyError/IndexError."""

    class _EmptyGammaClient:
        async def get_markets(self, **params: object) -> list[dict]:
            return []

    with pytest.raises(MarketUnavailable):
        await resolve_market("Anything", "anything", client=_EmptyGammaClient())


# ---------------------------------------------------------------------------
# T13b: event -> market SELECTION by structured market_ref + team name.
#
# A fixture slug names an EVENT holding MANY markets (verified live: 3 binary
# 1X2 Yes/No markets + a full O/U goal ladder + team-name spreads). resolve_market
# must pick the EXACT market for the ref -- by TEAM NAME for 1X2 win markets (not
# positional), by "draw" for the draw market, by numeric line for O/U -- or fail
# closed (MarketUnavailable). Never route to the wrong market (AC-2D-201 gates real money).
# ---------------------------------------------------------------------------


async def test_1x2_home_selects_home_team_win_market_by_name() -> None:
    """`1X2|home|full` with home_team=Portugal selects the "Will Portugal win…" market."""
    client = _FakeGammaClient("gamma_event_prt_hrv.json")

    resolved = await resolve_market(
        "1X2|home|full",
        "fifwc-prt-hrv-2026-07-02",
        home_team="Portugal",
        away_team="Croatia",
        client=client,
    )

    assert resolved.condition_id == (
        "0xPRTWIN000000000000000000000000000000000000000000000000000000001"
    )
    assert resolved.token_id_yes == (
        "11100000000000000000000000000000000000000000000000000000000001"
    )
    assert resolved.token_id_no == (
        "11100000000000000000000000000000000000000000000000000000000002"
    )
    assert resolved.tick_size == 0.0025


async def test_1x2_away_selects_away_team_win_market_by_name() -> None:
    """`1X2|away|full` selects the AWAY team's win market (Croatia), by name."""
    client = _FakeGammaClient("gamma_event_prt_hrv.json")

    resolved = await resolve_market(
        "1X2|away|full",
        "fifwc-prt-hrv-2026-07-02",
        home_team="Portugal",
        away_team="Croatia",
        client=client,
    )

    assert resolved.condition_id == (
        "0xHRVWIN000000000000000000000000000000000000000000000000000000001"
    )
    assert resolved.token_id_yes == (
        "33300000000000000000000000000000000000000000000000000000000001"
    )


async def test_1x2_home_selection_is_by_team_name_not_positional() -> None:
    """Making Croatia the HOME team selects the Croatia market (3rd), proving non-positional."""
    client = _FakeGammaClient("gamma_event_prt_hrv.json")

    resolved = await resolve_market(
        "1X2|home|full",
        "fifwc-prt-hrv-2026-07-02",
        home_team="Croatia",
        away_team="Portugal",
        client=client,
    )

    # Croatia's win market, not the first market in the event list.
    assert resolved.condition_id == (
        "0xHRVWIN000000000000000000000000000000000000000000000000000000001"
    )


async def test_1x2_draw_selects_draw_market_yes_token() -> None:
    """`1X2|draw|full` selects the draw market; token_id_yes is the draw market's YES token."""
    client = _FakeGammaClient("gamma_event_prt_hrv.json")

    resolved = await resolve_market(
        "1X2|draw|full",
        "fifwc-prt-hrv-2026-07-02",
        home_team="Portugal",
        away_team="Croatia",
        client=client,
    )

    assert resolved.condition_id == (
        "0xDRAW00000000000000000000000000000000000000000000000000000000001"
    )
    assert resolved.token_id_yes == (
        "22200000000000000000000000000000000000000000000000000000000001"
    )


async def test_1x2_draw_never_passes_draw_to_side_to_token(monkeypatch) -> None:
    """resolve_market must canonicalize draw itself; side_to_token must never see side='draw'."""
    seen: list[str] = []
    real_side_to_token = polymarket_resolver.side_to_token

    def _spy(resolved: ResolvedMarket, side: str) -> str:
        seen.append(side)
        return real_side_to_token(resolved, side)

    monkeypatch.setattr(polymarket_resolver, "side_to_token", _spy)

    client = _FakeGammaClient("gamma_event_prt_hrv.json")
    resolved = await polymarket_resolver.resolve_market(
        "1X2|draw|full",
        "fifwc-prt-hrv-2026-07-02",
        home_team="Portugal",
        away_team="Croatia",
        client=client,
    )

    assert resolved.token_id_yes == (
        "22200000000000000000000000000000000000000000000000000000000001"
    )
    assert "draw" not in [s.strip().lower() for s in seen]


async def test_ou_selects_market_matching_numeric_line() -> None:
    """`OU|2.5|full` selects the O/U 2.5 market; Over->yes token, Under->no token."""
    client = _FakeGammaClient("gamma_event_prt_hrv.json")

    resolved = await resolve_market(
        "OU|2.5|full",
        "fifwc-prt-hrv-2026-07-02",
        home_team="Portugal",
        away_team="Croatia",
        client=client,
    )

    assert resolved.condition_id == (
        "0xOU25000000000000000000000000000000000000000000000000000000000001"
    )
    assert resolved.token_id_yes == (
        "44400000000000000000000000000000000000000000000000000000000001"
    )
    assert resolved.token_id_no == (
        "44400000000000000000000000000000000000000000000000000000000002"
    )


async def test_ou_different_line_selects_different_market() -> None:
    """`OU|3.5|full` selects the 3.5 market, not 2.5 (numeric line disambiguation)."""
    client = _FakeGammaClient("gamma_event_prt_hrv.json")

    resolved = await resolve_market(
        "OU|3.5|full",
        "fifwc-prt-hrv-2026-07-02",
        home_team="Portugal",
        away_team="Croatia",
        client=client,
    )

    assert resolved.condition_id == (
        "0xOU35000000000000000000000000000000000000000000000000000000000001"
    )


async def test_team_name_alias_matches_usa_to_united_states() -> None:
    """home_team='USA' matches a PM 'Will United States win…' market via normalization/alias."""
    client = _FakeGammaClient("gamma_event_usa_bih.json")

    resolved = await resolve_market(
        "1X2|home|full",
        "fifwc-usa-bih-2026-07-02",
        home_team="USA",
        away_team="Bosnia & Herzegovina",
        client=client,
    )

    assert resolved.condition_id == (
        "0xUSAWIN000000000000000000000000000000000000000000000000000000001"
    )


async def test_team_name_trivial_spelling_diff_matches() -> None:
    """'Bosnia & Herzegovina' (TxLINE) matches 'Bosnia and Herzegovina' (PM): '&' vs 'and'."""
    client = _FakeGammaClient("gamma_event_usa_bih.json")

    resolved = await resolve_market(
        "1X2|away|full",
        "fifwc-usa-bih-2026-07-02",
        home_team="USA",
        away_team="Bosnia & Herzegovina",
        client=client,
    )

    assert resolved.condition_id == (
        "0xBIHWIN000000000000000000000000000000000000000000000000000000001"
    )


async def test_1x2_no_matching_team_fails_closed() -> None:
    """A home_team present in neither market (France) fails closed, never a guess."""
    client = _FakeGammaClient("gamma_event_prt_hrv.json")

    with pytest.raises(MarketUnavailable):
        await resolve_market(
            "1X2|home|full",
            "fifwc-prt-hrv-2026-07-02",
            home_team="France",
            away_team="Croatia",
            client=client,
        )


async def test_1x2_home_without_team_identity_fails_closed() -> None:
    """`1X2|home` with no home_team cannot be matched by name -> MarketUnavailable."""
    client = _FakeGammaClient("gamma_event_prt_hrv.json")

    with pytest.raises(MarketUnavailable):
        await resolve_market(
            "1X2|home|full",
            "fifwc-prt-hrv-2026-07-02",
            client=client,
        )


async def test_1x2_ambiguous_team_match_fails_closed() -> None:
    """Two markets matching the same team is ambiguous -> MarketUnavailable, never a guess."""
    client = _FakeGammaClient("gamma_event_ambiguous.json")

    with pytest.raises(MarketUnavailable):
        await resolve_market(
            "1X2|home|full",
            "fifwc-prt-hrv-2026-07-02",
            home_team="Portugal",
            away_team="Croatia",
            client=client,
        )


async def test_ou_no_matching_line_fails_closed() -> None:
    """An O/U line with no market in the event (9.5) fails closed."""
    client = _FakeGammaClient("gamma_event_prt_hrv.json")

    with pytest.raises(MarketUnavailable):
        await resolve_market(
            "OU|9.5|full",
            "fifwc-prt-hrv-2026-07-02",
            home_team="Portugal",
            away_team="Croatia",
            client=client,
        )


async def test_unknown_market_ref_type_fails_closed() -> None:
    """An unsupported market_ref type (BTTS) fails closed, never mis-selects."""
    client = _FakeGammaClient("gamma_event_prt_hrv.json")

    with pytest.raises(MarketUnavailable):
        await resolve_market(
            "BTTS|yes|full",
            "fifwc-prt-hrv-2026-07-02",
            home_team="Portugal",
            away_team="Croatia",
            client=client,
        )


async def test_1x2_home_does_not_select_spread_market() -> None:
    """A 1X2|home ref must never resolve to the team-name spread market (guarded twice)."""
    client = _FakeGammaClient("gamma_event_prt_hrv.json")

    resolved = await resolve_market(
        "1X2|home|full",
        "fifwc-prt-hrv-2026-07-02",
        home_team="Portugal",
        away_team="Croatia",
        client=client,
    )

    assert resolved.condition_id != (
        "0xSPREAD00000000000000000000000000000000000000000000000000000000001"
    )


# ---------------------------------------------------------------------------
# side_to_token: full alias mapping, unknown side -> ValueError (no silent fallback)
# ---------------------------------------------------------------------------


@pytest.fixture
def resolved_market() -> ResolvedMarket:
    return ResolvedMarket(
        condition_id="0xcond",
        token_id_yes="tok-yes",
        token_id_no="tok-no",
        tick_size=0.01,
    )


@pytest.mark.parametrize("side", ["over", "home", "yes", "Over", "HOME", "Yes"])
def test_side_to_token_yes_aliases(resolved_market: ResolvedMarket, side: str) -> None:
    assert side_to_token(resolved_market, side) == "tok-yes"


@pytest.mark.parametrize("side", ["under", "away", "no", "Under", "AWAY", "No"])
def test_side_to_token_no_aliases(resolved_market: ResolvedMarket, side: str) -> None:
    assert side_to_token(resolved_market, side) == "tok-no"


def test_side_to_token_unknown_side_raises_value_error(resolved_market: ResolvedMarket) -> None:
    with pytest.raises(ValueError):
        side_to_token(resolved_market, "draw")
