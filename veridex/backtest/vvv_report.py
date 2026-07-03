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

from collections.abc import Callable
from pathlib import Path
from typing import Any

from veridex.backtest.report import BacktestReport
from veridex.backtest.runner import run_backtest
from veridex.ingest.replay_pack import load_pack_marketstates
from veridex.provenance import EvidenceRung
from veridex.runtime.orchestrator import RunResult
from veridex.runtime.window import RunWindow
from veridex.strategies.value_vs_venue import value_vs_venue_agent, vvv_signal


def _aggregate_estimated_edge(
    pack_dir: Path,
    fixture_id: int,
    *,
    window: RunWindow,
    venue_price_source: Callable[[str], float | None],
    min_edge_bps: int,
) -> int | None:
    """Best estimated executable edge (bps) among the run's evaluated decision opportunities.

    Re-reads the SAME normalized marketstates the run decided on (mirroring ``run_backtest``'s
    pre_match closing-tick split so the closing tick — decided on by no agent — is excluded), then
    prices every (market, side) against the injected venue quote via ``vvv_signal``. Aggregates the
    computable (quote-backed, non-None) estimated edges as their MAX — the best edge the strategy
    could have estimated at the venue over the window. ``None`` only when no opportunity had a quote.
    """
    states = load_pack_marketstates(pack_dir, fixture_id)
    split_closing = window.end_rule == "pre_match" and len(states) >= 2
    decision_states = states[:-1] if split_closing else states

    edges: list[int] = []
    for state in decision_states:
        markets: dict[str, dict[str, Any]] = getattr(state, "markets", {}) or {}
        for market_key in sorted(markets):
            market = markets[market_key]
            if market.get("suspended"):
                continue
            prob_bps = market.get("stable_prob_bps", {})
            if not isinstance(prob_bps, dict):
                continue
            venue_decimal_price = venue_price_source(market_key)
            for side in sorted(prob_bps):
                try:
                    fair_prob_bps = int(prob_bps[side])
                except (TypeError, ValueError):
                    continue
                signal = vvv_signal(fair_prob_bps, venue_decimal_price, min_edge_bps=min_edge_bps)
                estimated = signal["estimated_executable_edge_bps"]
                if estimated is not None:
                    edges.append(estimated)
    return max(edges) if edges else None


async def vvv_report_with_estimated_edge(
    pack_dir: Path,
    fixture_id: int,
    *,
    venue_price_source: Callable[[str], float | None],
    window: RunWindow,
    min_edge_bps: int = 0,
    assumptions: dict[str, Any],
) -> tuple[RunResult, BacktestReport]:
    """Run the VvV agent to a venue-free CLV report, then ATTACH an estimated edge post-build.

    Steps, in trust order:

      1. Build the VvV agent from the injected ``venue_price_source`` + ``min_edge_bps`` and run it
         through :func:`~veridex.backtest.runner.run_backtest` — a PURE, venue-free, CLV-scored
         report (the ranked leaderboard never sees a venue price).
      2. Compute the aggregate estimated executable edge over the run's evaluated opportunities via
         :func:`~veridex.strategies.value_vs_venue.vvv_signal` + ``venue_price_source``.
      3. Attach that edge ONLY via ``report.model_copy(update=...)`` — ``build_backtest_report`` /
         ``run_backtest`` are never modified and never learn the estimated edge exists.

    Args:
        pack_dir: Directory of the self-describing ReplayPack.
        fixture_id: The fixture within the pack to replay.
        venue_price_source: Injected ``Callable[[str], float | None]`` returning the venue DECIMAL
            price for a market_key (the ONLY venue input; never read from sealed evidence).
        window: The coverage window (drives closing behaviour + the report's window id/allowlist).
        min_edge_bps: Minimum estimated executable edge (bps) the VvV agent requires to fire.
        assumptions: The EXPLICIT assumptions the estimated edge is computed under (never implied);
            stored verbatim on the report.

    Returns:
        ``(run_result, report)`` — the sealed run and its report, the report now carrying the
        post-build estimated executable edge + its machine-readable rung + the assumptions.
    """
    agent = value_vs_venue_agent(venue_price_source=venue_price_source, min_edge_bps=min_edge_bps)
    result, report = await run_backtest(pack_dir, fixture_id, [agent], window=window)

    estimated_edge_bps = _aggregate_estimated_edge(
        pack_dir,
        fixture_id,
        window=window,
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
