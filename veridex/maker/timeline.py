"""Report-only event-gate timeline for market-maker quote sets.

This module produces pure display data describing when the maker loop
suspends/cancels, widens, waits, or quotes normally at each tick, keyed to
the suspension window. It feeds no rank key and computes no metric.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from veridex.maker.contracts import TargetQuoteSet
from veridex.maker.state_machine import MakerState


class EventGateTimeline(BaseModel):
    fixture_id: int
    entries: list[dict[str, Any]]


def build_event_gate_timeline(quote_sets: list[TargetQuoteSet]) -> EventGateTimeline:
    fixture_id = quote_sets[0].fixture_id if quote_sets else 0
    entries: list[dict[str, Any]] = []
    prior_quoted = False

    for qs in quote_sets:
        if qs.regime == MakerState.NO_QUOTE.value:
            action = "suspend_cancel" if prior_quoted else "wait"
        elif qs.regime == MakerState.WIDEN.value:
            action = "widen"
        else:
            action = "quote"

        entries.append({"ts": qs.ts, "regime": qs.regime, "action": action})
        prior_quoted = bool(qs.quotes)

    return EventGateTimeline(fixture_id=fixture_id, entries=entries)
