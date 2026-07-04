"""S5 — the ValueVsVenue PRODUCER: a real BacktestReport with a POST-BUILD estimated edge.

The trust boundary this module defends is the whole point of S5: the SCORED / RANKED report is
built by :func:`~veridex.backtest.runner.run_backtest` PURELY from the sealed run — venue-blind,
CLV-only (SEC-005). The venue-derived ESTIMATED executable edge is computed SEPARATELY (via
:func:`~veridex.strategies.value_vs_venue.vvv_signal` over the run's evaluated opportunities + the
injected ``venue_price_source``) and attached ONLY AFTERWARDS via ``report.model_copy(update=...)``.

Neither ``run_backtest`` nor ``build_backtest_report`` is modified or fed a venue input — they never
learn the estimated edge exists. This keeps the ranked leaderboard byte-identical to the venue-free
build while the report still surfaces an honest, explicitly-assumed, machine-rung estimated edge.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from veridex.backtest.report import BacktestReport
from veridex.backtest.runner import run_backtest
from veridex.ingest.replay_pack import load_pack_marketstates
from veridex.provenance import EvidenceRung
from veridex.runtime.orchestrator import RunResult
from veridex.runtime.schemas import SportsActionType
from veridex.runtime.window import RunWindow
from veridex.strategies.value_vs_venue import value_vs_venue_agent, vvv_signal
from veridex.venues.venue_price_source import VenuePriceSource, txline_ts_to_venue_seconds


def _estimated_edge_over_fired_picks(
    result: RunResult,
    states_by_tick: dict[int, Any],
    *,
    venue_price_source: VenuePriceSource,
    min_edge_bps: int,
) -> int | None:
    """Estimated executable edge (bps) over the STRATEGY'S ACTUAL FIRED PICKS — never a declined one.

    Reads the agent's real, SEALED decisions from ``result.score_rows`` (the ``FOLLOW_MOMENTUM``
    picks, whose ``raw_prescore.raw_action.params`` carry only the TxLINE-derived ``market_key`` +
    ``side``), re-prices each pick's fair prob — looked up from the SAME tick it fired on
    (``states_by_tick[tick_seq]``) — against the TIME-ALIGNED venue quote from
    ``venue_price_source(fixture_id, market_key, side, ts)`` (that SAME tick's ``fixture_id``/``ts``)
    via ``vvv_signal``, and
    aggregates the computable (quote-backed) edges as their MAX. A pick whose venue price is ``None``
    (no quote) is skipped. Returns ``None`` when the strategy fired NO picks — the honest answer for a
    strategy that took zero positions, NOT the best market opportunity it declined (F2).
    """
    edges: list[int] = []
    for row in result.score_rows:
        raw_action = row.get("raw_prescore", {}).get("raw_action", {})
        action_type = raw_action.get("type")
        # raw_action["type"] may be the SportsActionType enum or its string value — normalize both.
        if getattr(action_type, "value", action_type) != SportsActionType.FOLLOW_MOMENTUM.value:
            continue  # only ACTUAL fired picks — WAIT/abstentions took no position
        params = raw_action.get("params", {})
        market_key = params.get("market_key")
        side = params.get("side")
        tick_seq = row.get("tick_seq")
        state = states_by_tick.get(tick_seq) if isinstance(tick_seq, int) else None
        if market_key is None or side is None or state is None:
            continue
        markets: dict[str, dict[str, Any]] = getattr(state, "markets", {}) or {}
        prob_bps = markets.get(market_key, {}).get("stable_prob_bps", {})
        if not isinstance(prob_bps, dict) or side not in prob_bps:
            continue
        try:
            fair_prob_bps = int(prob_bps[side])
        except (TypeError, ValueError):
            continue
        # Time-aligned venue quote for THIS fired pick's coordinate (same tick's fixture_id + ts).
        # Source is keyed by unix SECONDS; state.ts is unix MILLISECONDS → convert at the seam.
        quote = venue_price_source(
            state.fixture_id, market_key, side, txline_ts_to_venue_seconds(state.ts)
        )
        venue_decimal_price = quote.venue_decimal_price if quote is not None else None
        estimated = vvv_signal(
            fair_prob_bps, venue_decimal_price, min_edge_bps=min_edge_bps
        )["estimated_executable_edge_bps"]
        if estimated is not None:
            edges.append(estimated)
    return max(edges) if edges else None


async def vvv_report_with_estimated_edge(
    pack_dir: Path,
    fixture_id: int,
    *,
    venue_price_source: VenuePriceSource,
    venue_source_id: str,
    window: RunWindow,
    min_edge_bps: int = 0,
    assumptions: dict[str, Any],
) -> tuple[RunResult, BacktestReport]:
    """Run the VvV agent to a venue-free CLV report, then ATTACH an estimated edge post-build.

    Steps, in trust order:

      1. Build the VvV agent from the injected ``venue_price_source`` + ``min_edge_bps`` and run it
         through :func:`~veridex.backtest.runner.run_backtest` — a PURE, venue-free, CLV-scored
         report (the ranked leaderboard never sees a venue price).
      2. Compute the estimated executable edge over the STRATEGY'S ACTUAL FIRED PICKS (read from the
         sealed ``RunResult``) via :func:`~veridex.strategies.value_vs_venue.vvv_signal` +
         ``venue_price_source`` — NOT the best market opportunity the strategy declined (F2). A
         strategy that fired no picks honestly reports ``None`` (no position ⇒ no estimated edge).
      3. Attach that edge ONLY via ``report.model_copy(update=...)`` — ``build_backtest_report`` /
         ``run_backtest`` are never modified and never learn the estimated edge exists.

    Args:
        pack_dir: Directory of the self-describing ReplayPack.
        fixture_id: The fixture within the pack to replay.
        venue_price_source: Injected time-aligned
            :data:`~veridex.venues.venue_price_source.VenuePriceSource` — called with
            ``(fixture_id, market_key, side, ts)`` and returning a
            :class:`~veridex.venues.venue_price_source.TimedVenueQuote` (or ``None``); the ONLY venue
            input, never read from sealed evidence.
        venue_source_id: Stable identity of the venue price source (e.g. the quote/price-history
            artifact hash). Bound into the VvV agent's ``config_hash`` for reproducibility — ties the
            report's reproducible config to the venue source it priced against. Must be non-empty.
        window: The coverage window (drives closing behaviour + the report's window id/allowlist).
        min_edge_bps: Minimum estimated executable edge (bps) the VvV agent requires to fire.
        assumptions: The EXPLICIT assumptions the estimated edge is computed under (never implied);
            stored verbatim on the report. The estimated edge is over the strategy's ACTUAL fired
            picks (F2), not the best available market opportunity.

    Returns:
        ``(run_result, report)`` — the sealed run and its report, the report now carrying the
        post-build estimated executable edge (over the strategy's fired picks) + its machine-readable
        rung + the assumptions.
    """
    agent = value_vs_venue_agent(
        venue_price_source=venue_price_source, venue_source_id=venue_source_id, min_edge_bps=min_edge_bps
    )
    result, report = await run_backtest(pack_dir, fixture_id, [agent], window=window)

    # Map each decision tick to its marketstate so a fired pick's fair prob is read from the SAME
    # tick it fired on (the closing tick is never referenced — the agent decides on no closing tick).
    states_by_tick = {int(state.tick_seq): state for state in load_pack_marketstates(pack_dir, fixture_id)}
    estimated_edge_bps = _estimated_edge_over_fired_picks(
        result,
        states_by_tick,
        venue_price_source=venue_price_source,
        min_edge_bps=min_edge_bps,
    )

    # POST-BUILD attach ONLY — run_backtest/build_backtest_report stay venue-free. The rung is the
    # lower, honest default (backfilled price history): an injected source can't prove a live quote.
    enriched = report.model_copy(
        update={
            "estimated_executable_edge_bps": estimated_edge_bps,
            "estimated_edge_rung": EvidenceRung.BACKFILLED_PRICE_HISTORY.value,
            "estimated_edge_assumptions": assumptions,
        }
    )
    return result, enriched
