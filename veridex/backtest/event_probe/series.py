"""CON-016 participant 1X2 tracked series for the event-probe backtest.

Builds the in-running, de-vigged fair-probability series for ONE participant's
1X2 win from a fixture's ``MarketState`` stream. This is the raw "tracked" signal
an event-probe compares against goal events (E1) -- it is NOT market-quality
scoring.

v2 seal-coverage: every knob that decides WHAT is measured -- the 1X2 market key,
the in-running phase, and the CON-016 near-certain band -- is read FROM the sealed
:class:`ProbeConfig` passed in, NOT from module constants. This closes the v1 gap
where these lived as module literals outside ``config_hash()`` (so a swap changed
the measurement without VOIDing the seal). v1 tracked the first-half market
``1X2_PARTICIPANT_RESULT|half=1|`` (dead at halftime -> ~62% of goals
unobservable); the v2 default is the full-match ``1X2_PARTICIPANT_RESULT||``.

CON-016 band reuse: the ONLY market-quality rule reused here is the near-certain
band (drop ``prob < band_lo`` or ``prob > band_hi``). The band BOUNDS come from
the sealed config (which derives them from ``MarketQualityConfig``); this module
deliberately does NOT call the eligibility evaluator -- it stays outside the
eligibility filter's trust boundary (tick-count / horizon / mapping rules do not
apply to a raw tracked series).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from veridex.ingest.marketstate import MarketState

if TYPE_CHECKING:
    # Import-cycle guard: ``config`` -> ``compute`` -> ``series`` already exists
    # (compute imports ``TrackedTick``), so importing ``ProbeConfig`` at module top
    # would close a runtime cycle. The series only reads attributes off ``cfg`` at
    # runtime, so a type-only import (annotation as a string via
    # ``from __future__ import annotations``) is sufficient and the values still
    # come FROM the sealed config.
    from veridex.backtest.event_probe.config import ProbeConfig

# Discovery: the full-match 1X2 market surfaces under ``1X2_PARTICIPANT_RESULT||``
# with side tokens ``part1`` / ``draw`` / ``part2`` (home/away is NOT a token here).
# Participant N maps to the ``part{N}`` token -- this participant->token mapping is a
# FEED-SCHEMA decoding fact (not a threshold), so it stays here; the market KEY it
# looks the token up under is the sealed ``cfg.market_1x2_key``.


@dataclass(frozen=True)
class TrackedTick:
    """One kept tick of the participant's de-vigged fair 1X2-win probability.

    ``ts`` is the ``MarketState`` time in seconds; ``prob`` is the de-vigged fair
    probability in [0, 1], read as ``stable_prob_bps[token] / 10000``.
    """

    ts: int
    prob: float


def build_tracked_series(
    states: list[MarketState], participant: int, cfg: ProbeConfig
) -> list[TrackedTick]:
    """Build the in-running de-vigged 1X2-win series for ``participant``.

    Keeps only ticks that are in-running, carry the sealed 1X2 market present and
    un-suspended, and pass the CON-016 near-certain band. Every selection knob --
    the market key (``cfg.market_1x2_key``), the in-running phase
    (``cfg.in_running_phase``), and the band bounds (``cfg.band_lo`` /
    ``cfg.band_hi``) -- is read from the sealed :class:`ProbeConfig`, so a change to
    any of them moves ``config_hash()`` and VOIDs the run rather than silently
    altering what is measured. ``participant`` 1 reads the ``part1`` token and 2
    reads ``part2`` (never inverted).
    """
    token = f"part{participant}"
    series: list[TrackedTick] = []
    for state in states:
        if state.phase != cfg.in_running_phase:
            continue
        market = state.markets.get(cfg.market_1x2_key)
        if market is None or market.get("suspended"):
            continue
        prob_bps = market.get("stable_prob_bps", {}).get(token)
        if prob_bps is None:
            continue
        prob = prob_bps / 10000
        if prob < cfg.band_lo or prob > cfg.band_hi:
            continue
        series.append(TrackedTick(ts=state.ts, prob=prob))
    return series
