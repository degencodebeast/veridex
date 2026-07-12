"""Shared, pure top-of-book extractor for the live-recorder lane (parity seam).

A single normalization for the venue-published ``best_bid``/``best_ask`` top quote, used by
the pmxt archive puller's DIRECT top-of-book series so that a future R3 increment can adopt the
SAME function on the live WS ``price_change`` path (which also carries per-change
``best_bid``/``best_ask``) and get byte-for-byte parity on mid/spread/sentinel/crossed handling.

Trust discipline:

* **Pure & offline.** No network library and no sealed internals are imported — this module is
  additive and importing it touches nothing. It has no hidden state: the same inputs always
  yield an identical :class:`TopOfBook`.
* **No silent cleaning (fail-open-to-honest).** Missing / null / non-numeric best prices become
  ``status="gap"`` (NEVER a silent zero); a one-sided / empty-side ``"0"`` sentinel is a
  documented ``gap`` (``reason="one_sided"``), NOT an exclusion; a crossed book
  (``best_bid >= best_ask``) is ``status="excluded"`` with the crossed values DISCLOSED but no
  misleading mid/spread computed.

The live WS maintainer's ``_side_matches`` treats ``None``/``""`` and any numeric ``<= 0`` as the
empty-side sentinel; this extractor mirrors that convention so the two paths agree.
"""

from __future__ import annotations

import math
from typing import Any, NamedTuple

# Status discriminants for one extracted top-of-book quote.
STATUS_OK = "ok"
STATUS_GAP = "gap"
STATUS_EXCLUDED = "excluded"


class TopOfBook(NamedTuple):
    """One normalized top-of-book quote extracted from a venue ``best_bid``/``best_ask`` pair.

    ``bid``/``ask`` are the parsed present sides (``None`` when absent / sentinel / non-numeric);
    ``mid``/``spread`` are populated ONLY for an ``ok`` (two-sided, non-crossed) quote. ``status``
    is ``ok`` | ``gap`` | ``excluded``; ``reason`` documents a non-``ok`` status and is ``None``
    when ``ok``.
    """

    bid: float | None
    ask: float | None
    mid: float | None
    spread: float | None
    status: str
    reason: str | None


class _Side(NamedTuple):
    """One parsed side: ``value`` present iff a positive float; ``non_numeric`` flags a parse error."""

    value: float | None
    non_numeric: bool


def _parse_side(raw: Any) -> _Side:
    """Parse one raw best price → present positive float, empty-side sentinel, or a parse error.

    ``None``/``""`` and any numeric ``<= 0`` are the documented empty-side sentinel (``value``
    ``None``, no error). A non-numeric string is a parse error (``non_numeric=True``).
    """
    if raw is None or raw == "":
        return _Side(None, False)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _Side(None, True)
    if not math.isfinite(value):  # nan/inf survive float() and fail-open every guard → parse error
        return _Side(None, True)
    if value <= 0.0:  # venue "0" (or negative) is the empty-side sentinel
        return _Side(None, False)
    return _Side(value, False)


def extract_top_of_book(best_bid: Any, best_ask: Any) -> TopOfBook:
    """Normalize a raw ``(best_bid, best_ask)`` pair into a :class:`TopOfBook` (pure, no I/O).

    Precedence: a non-numeric side → ``gap`` (``reason="non_numeric_best"``); an absent side
    (missing / empty-side sentinel) → ``gap`` (``reason="one_sided"`` if the other side is
    present, else ``"missing_both"``); a crossed book (``bid >= ask``) → ``excluded``
    (``reason="crossed"``, values disclosed, no mid); otherwise ``ok`` with ``mid`` and
    ``spread``.
    """
    bid = _parse_side(best_bid)
    ask = _parse_side(best_ask)

    if bid.non_numeric or ask.non_numeric:
        return TopOfBook(bid.value, ask.value, None, None, STATUS_GAP, "non_numeric_best")

    if bid.value is None or ask.value is None:
        reason = "missing_both" if bid.value is None and ask.value is None else "one_sided"
        return TopOfBook(bid.value, ask.value, None, None, STATUS_GAP, reason)

    if bid.value >= ask.value:  # crossed / locked → disclosed but excluded, never a fake mid
        return TopOfBook(bid.value, ask.value, None, None, STATUS_EXCLUDED, "crossed")

    mid = (bid.value + ask.value) / 2.0
    spread = ask.value - bid.value
    return TopOfBook(bid.value, ask.value, mid, spread, STATUS_OK, None)
