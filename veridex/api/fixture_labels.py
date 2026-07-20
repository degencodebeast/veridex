"""Honest, CURATED human labels for pinned fixtures + markets (render-time augmentation only).

The fixture names are TRANSCRIBED from the committed curated capture
``scripts/txline_live/wc-qf-fixtures.json`` — the SAME source
:mod:`veridex.mm_strategy.pmxt_tape` cites for the maker tape's provenance. They are read from
this module (a static, committed transcription), NEVER re-read from ``scripts/`` at runtime and
NEVER re-verified against the live TxLINE fixtures API at render time. They are therefore labelled
**CURATED**, never "verified": only fixture ``18209181`` (France v Morocco) is independently
corroborated in ``pmxt_tape.py``. An UNMAPPED fixture id falls back honestly to ``"Fixture {id}"``.

These labels only AUGMENT the raw ids the UI already shows — they never replace them, and they
never assert a claim the id itself does not already carry.
"""

from __future__ import annotations

#: CURATED (home, away) team pairs, transcribed from the committed WC-QF fixtures capture
#: (``scripts/txline_live/wc-qf-fixtures.json``), the same source ``pmxt_tape.py`` cites. NOT
#: re-verified against the live TxLINE fixtures API at render time — a curated convenience label,
#: never a "verified" claim. Only ``18209181`` is independently corroborated (``pmxt_tape.py``).
FIXTURE_LABELS: dict[int, tuple[str, str]] = {
    18209181: ("France", "Morocco"),
    18218149: ("Spain", "Belgium"),
    18213979: ("Norway", "England"),
    18222446: ("Argentina", "Switzerland"),
}

#: The pinned-market outcome suffixes (``pmxt:{fixture}:{suffix}`` tokens) → their human labels.
_SUFFIX_LABELS: dict[str, str] = {
    "home_win": "Home win",
    "draw": "Draw",
    "away_win": "Away win",
    "over": "Over",
    "under": "Under",
}

#: Directional market keys (the pinned ``market_allowlist`` entries the directional families use)
#: → their human labels.
_MARKET_LABELS: dict[str, str] = {
    "1X2_PARTICIPANT_RESULT": "Match result (1X2)",
    "OVERUNDER_PARTICIPANT_GOALS": "Total goals (O/U)",
}


def fixture_label(fixture_id: int | None) -> str:
    """Return the CURATED "Home v Away" label for a mapped fixture, else an honest fallback.

    Args:
        fixture_id: The pinned fixture id, or ``None`` when no id could be derived.

    Returns:
        ``"France v Morocco"`` for a mapped id; ``"Fixture {id}"`` for an unmapped id (never a
        guessed matchup); ``"Fixture (unknown)"`` when ``fixture_id`` is ``None``.
    """
    if fixture_id is None:
        return "Fixture (unknown)"
    pair = FIXTURE_LABELS.get(fixture_id)
    if pair is None:
        return f"Fixture {fixture_id}"
    home, away = pair
    return f"{home} v {away}"


def market_label(token_or_market: str) -> str:
    """Humanize a pinned market token, honestly returning the raw token when it is not recognized.

    Handles two token shapes:

    * outcome tokens of the EXACT ``pmxt:{numeric_fixture}:{suffix}`` shape (e.g.
      ``pmxt:18209181:home_win`` → ``"Home win"``), keyed off the
      ``home_win``/``draw``/``away_win``/``over``/``under`` suffix; and
    * directional market keys (e.g. ``1X2_PARTICIPANT_RESULT`` → ``"Match result (1X2)"``,
      ``OVERUNDER_PARTICIPANT_GOALS`` → ``"Total goals (O/U)"``).

    The suffix is only humanized for the strict ``pmxt:{numeric}:{suffix}`` shape — an arbitrary
    string that merely ENDS in a known suffix (e.g. ``bogus:home_win``) is NOT a pinned market token
    and passes through unchanged.

    Args:
        token_or_market: A pinned market token or directional market key.

    Returns:
        The human label for a recognized token/market; otherwise the raw ``token_or_market``
        unchanged (an honest passthrough — never a guessed label).
    """
    parts = token_or_market.split(":")
    if len(parts) == 3 and parts[0] == "pmxt" and parts[1].isdigit() and parts[2] in _SUFFIX_LABELS:
        return _SUFFIX_LABELS[parts[2]]
    if token_or_market in _MARKET_LABELS:
        return _MARKET_LABELS[token_or_market]
    return token_or_market
