"""CON-016 participant 1X2 tracked series for the event-probe backtest.

Builds the in-running, de-vigged fair-probability series for ONE participant's
1X2 win from a fixture's ``MarketState`` stream. This is the raw "tracked" signal
an event-probe compares against goal events (E1) -- it is NOT market-quality
scoring.

CON-016 band reuse: the ONLY market-quality rule reused here is the near-certain
band (drop ``prob < band_lo`` or ``prob > band_hi``). We reuse ONLY the band
constants from ``MarketQualityConfig`` (via ``DEFAULT_MARKET_QUALITY_CONFIG``) and
deliberately do NOT call the eligibility evaluator -- this module stays outside
the eligibility filter's trust boundary (tick-count / horizon / mapping rules do
not apply to a raw tracked series).
"""

from __future__ import annotations

from dataclasses import dataclass

from veridex.ingest.marketstate import MarketState
from veridex.strategies.market_quality import DEFAULT_MARKET_QUALITY_CONFIG

# Discovery (pack 17588234): MarketState.phase is 0=pre-match, 1=in-running -- see
# veridex/ingest/txline_normalize.py (`phase=1 if in_running else 0`); only {0, 1}
# ever appear in the recorded pack.
_IN_RUNNING_PHASE: int = 1

# Discovery: the 1X2 market surfaces under this exact market_key, with side tokens
# `part1` / `draw` / `part2` (home/away is NOT a token here). Participant N maps to
# the `part{N}` token.
_MARKET_1X2_KEY: str = "1X2_PARTICIPANT_RESULT|half=1|"

# CON-016: reuse ONLY the near-certain band bounds (NOT the eligibility evaluator).
_BAND_LO: float = DEFAULT_MARKET_QUALITY_CONFIG.band_lo
_BAND_HI: float = DEFAULT_MARKET_QUALITY_CONFIG.band_hi


@dataclass(frozen=True)
class TrackedTick:
    """One kept tick of the participant's de-vigged fair 1X2-win probability.

    ``ts`` is the ``MarketState`` time in seconds; ``prob`` is the de-vigged fair
    probability in [0, 1], read as ``stable_prob_bps[token] / 10000``.
    """

    ts: int
    prob: float


def build_tracked_series(states: list[MarketState], participant: int) -> list[TrackedTick]:
    """Build the in-running de-vigged 1X2-win series for ``participant``.

    Keeps only ticks that are in-running, carry the 1X2 market present and
    un-suspended, and pass the CON-016 near-certain band. ``participant`` 1 reads
    the ``part1`` token and 2 reads ``part2`` (never inverted).
    """
    token = f"part{participant}"
    series: list[TrackedTick] = []
    for state in states:
        if state.phase != _IN_RUNNING_PHASE:
            continue
        market = state.markets.get(_MARKET_1X2_KEY)
        if market is None or market.get("suspended"):
            continue
        prob_bps = market.get("stable_prob_bps", {}).get(token)
        if prob_bps is None:
            continue
        prob = prob_bps / 10000
        if prob < _BAND_LO or prob > _BAND_HI:
            continue
        series.append(TrackedTick(ts=state.ts, prob=prob))
    return series
