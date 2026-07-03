"""M7 (S6) — predeclared multi-fixture evaluation (Task 19).

The S6 evaluation is DECLARED BEFORE it runs (CON-008): a committed :class:`EvalProtocol` pins the
fixtures, the strategy roster, the window + close semantics, and the baseline floor. The runner reads
that committed protocol and reports whatever the ONE pass yields — it never invents a protocol at
runtime, and it never re-ranks or hides a losing number.

Two honesty gates are enforced here:

  * **StaleLine is cadence-gated (AC-009).** ``stale_line_included`` is ``True`` iff the protocol
    asked for ``"stale-line"`` AND the recorded-quote cadence actually backs sub-minute freshness
    (``cadence_ok`` — sourced upstream from :func:`veridex.venues.quote_recorder.cadence_report`).
    A predeclared StaleLine strategy is silently DROPPED, never run, when cadence can't back it.
  * **Every metric carries an evidence rung.** ``per_metric_rung`` attaches one of the five
    :class:`~veridex.provenance.EvidenceRung` labels to every surfaced metric: the CLV-family
    metrics are TxLINE-sealed (``txline-only``); the venue-derived estimated edge (present only when
    the protocol runs ``"value-vs-venue"``) is ``backfilled-price-history``.

``results_by_fixture`` is the producer's output (Task 19b): ``dict[fixture_id, list[row]]`` where each
row is calibration-shaped — ``{"fixture_id", "kind", "market", "action", "clv_bps"}`` — so a row with
``clv_bps is None`` is a null (no closing CLV) and a row whose ``action == "WAIT"`` is an abstention.
Both are counted honestly (never dropped, never scored as 0), and the rows feed a REPORT-ONLY
:class:`~veridex.backtest.calibration.CalibrationReport`.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from veridex.backtest.baselines import BASELINES
from veridex.backtest.calibration import CalibrationReport, build_calibration_report
from veridex.backtest.runner import run_backtest
from veridex.backtest.vvv_report import vvv_report_with_estimated_edge
from veridex.ingest.marketstate import MarketState
from veridex.ingest.replay_pack import load_pack_marketstates
from veridex.provenance import EvidenceRung
from veridex.runtime.orchestrator import RunResult
from veridex.runtime.schemas import AgentAction
from veridex.runtime.window import RunWindow
from veridex.scoring import is_scored
from veridex.strategies.drift import cumulative_drift_agent

#: The strategy-config id that names the (cadence-gated) StaleLine decision strategy (M8).
STALE_LINE_CONFIG = "stale-line"
#: The strategy-config id that names the CumulativeDrift agent (run through ``run_backtest``, S3).
CUMULATIVE_DRIFT_CONFIG = "cumulative-drift"
#: The strategy-config id that names the venue-priced ValueVsVenue strategy (its estimated edge is
#: the one metric surfaced at a venue rung rather than the TxLINE-sealed CLV rung).
VALUE_VS_VENUE_CONFIG = "value-vs-venue"


class EvalProtocol(BaseModel):
    """The predeclared S6 evaluation contract — committed BEFORE the first real run (CON-008).

    Attributes:
        protocol_id: Stable identifier for this committed evaluation.
        fixture_ids: The fixtures the roster is evaluated over.
        strategy_configs: The strategy-config ids in the roster (e.g. ``"cumulative-drift"``,
            ``"value-vs-venue"``, ``"stale-line"``). StaleLine is admitted only when cadence backs it.
        window: The window id every fixture run is scored under.
        close_semantics: The window ``end_rule`` (``"pre_match"`` yields true CLV).
        baselines: The named zero-edge baselines the roster is compared against (never alpha).
        committed_at: When the protocol was committed (ISO-8601) — the pre-run commitment stamp.
    """

    protocol_id: str
    fixture_ids: list[int]
    strategy_configs: list[str]
    window: str
    close_semantics: str
    baselines: list[str]
    committed_at: str


def _flatten(results_by_fixture: dict[int, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """All rows across every fixture, in fixture-id then in-fixture order (deterministic)."""
    rows: list[dict[str, Any]] = []
    for fixture_id in sorted(results_by_fixture):
        rows.extend(results_by_fixture[fixture_id])
    return rows


def _per_metric_rung(protocol: EvalProtocol) -> dict[str, str]:
    """Attach a machine-readable evidence rung to every surfaced metric.

    The CLV-family metrics are derived from TxLINE-sealed evidence (``txline-only``); the estimated
    executable edge is surfaced ONLY when the roster runs ValueVsVenue, and it is a venue-derived
    quantity priced off backfilled price history — so it (and only it) carries the venue rung.
    """
    rung: dict[str, str] = {
        "hit_rate": EvidenceRung.TXLINE_ONLY.value,
        "avg_clv_bps": EvidenceRung.TXLINE_ONLY.value,
        "nulls": EvidenceRung.TXLINE_ONLY.value,
        "abstentions": EvidenceRung.TXLINE_ONLY.value,
        # Key name matches the real CalibrationReport.breadth field so a consumer can join rung→metric.
        "top_match_share_of_net_pct": EvidenceRung.TXLINE_ONLY.value,
    }
    if VALUE_VS_VENUE_CONFIG in protocol.strategy_configs:
        rung["estimated_executable_edge_bps"] = EvidenceRung.BACKFILLED_PRICE_HISTORY.value
    return rung


def run_multi_fixture_evaluation(
    protocol: EvalProtocol,
    *,
    results_by_fixture: dict[int, list[dict[str, Any]]],
    cadence_ok: bool,
) -> dict[str, Any]:
    """Evaluate a committed :class:`EvalProtocol` over already-produced per-fixture results.

    Reports whatever the one predeclared pass yields (CON-008): it never re-ranks, never hides a
    losing number, and never synthesizes a protocol. StaleLine is admitted only when cadence backs
    it (AC-009); every metric carries an evidence rung; nulls (no-CLV rows) and abstentions (WAIT
    rows) are counted honestly; the rows feed a REPORT-ONLY calibration report.

    Args:
        protocol: The committed evaluation contract.
        results_by_fixture: The producer's ``dict[fixture_id, list[row]]`` (Task 19b); each row is
            calibration-shaped ``{"fixture_id", "kind", "market", "action", "clv_bps"}``.
        cadence_ok: Whether the recorded-quote cadence backs sub-minute freshness
            (from :func:`veridex.venues.quote_recorder.cadence_report`) — the StaleLine gate.

    Returns:
        ``{"protocol_id", "per_metric_rung", "nulls", "abstentions", "baselines_included",
        "stale_line_included", "calibration"}``.
    """
    rows = _flatten(results_by_fixture)
    nulls = sum(1 for row in rows if row.get("clv_bps") is None)
    abstentions = sum(1 for row in rows if row.get("action") == "WAIT")

    # AC-009: a predeclared StaleLine strategy is admitted ONLY when cadence actually backs it.
    stale_line_included = (STALE_LINE_CONFIG in protocol.strategy_configs) and cadence_ok

    calibration: CalibrationReport = build_calibration_report(
        rows, provenance=EvidenceRung.TXLINE_ONLY.value
    )

    return {
        "protocol_id": protocol.protocol_id,
        "per_metric_rung": _per_metric_rung(protocol),
        "nulls": nulls,
        "abstentions": abstentions,
        "baselines_included": list(protocol.baselines),
        "stale_line_included": stale_line_included,
        "calibration": calibration,
    }


# ==========================================================================================
# Task 19b — the S6 PRODUCER: run the predeclared roster over REAL packs into results_by_fixture.
# ==========================================================================================

#: The window end rule for an unknown/unspecified ``close_semantics`` — the true-CLV ``pre_match``
#: rule, never a silent mislabel. ``fixed_duration`` needs a ``duration_s`` the protocol does not
#: carry, so a protocol demanding it is under-specified and fails loud inside RunWindow.
_DEFAULT_END_RULE: Literal["pre_match", "fixed_duration", "manual_stop"] = "pre_match"


def _end_rule(close_semantics: str) -> Literal["pre_match", "fixed_duration", "manual_stop"]:
    """Narrow a protocol's ``close_semantics`` string to a RunWindow end rule (unknown → pre_match)."""
    if close_semantics == "fixed_duration":
        return "fixed_duration"
    if close_semantics == "manual_stop":
        return "manual_stop"
    return _DEFAULT_END_RULE


def _window_allowlist(marketstates: list[MarketState]) -> list[str]:
    """The distinct market-key PREFIXES (the part before ``|``) seen across the fixture's ticks.

    ``RunWindow.market_allowlist`` scores by prefix (e.g. ``"1X2"`` for a ``"1X2||"`` key), so the
    producer derives the allowlist from the ACTUAL replayed markets rather than guessing it.
    """
    prefixes = {
        str(mk).split("|", 1)[0]
        for state in marketstates
        for mk in (getattr(state, "markets", {}) or {})
    }
    return sorted(prefixes)


def _build_window(protocol: EvalProtocol, fixture_id: int, marketstates: list[MarketState]) -> RunWindow:
    """Build the coverage window a fixture's roster is scored under, from the committed protocol.

    ``min_clv_horizon_s`` is 0 so every decision tick is scoreable (the protocol's fixtures are the
    unit of evaluation, not an intra-window pending horizon). An unknown ``close_semantics`` falls
    back to the true-CLV ``pre_match`` rule rather than silently mislabelling the close.
    """
    return RunWindow(
        window_id=protocol.window,
        fixture_id=fixture_id,
        market_allowlist=_window_allowlist(marketstates),
        end_rule=_end_rule(protocol.close_semantics),
        min_clv_horizon_s=0,
    )


def _rows_from_run(result: RunResult, *, fixture_id: int, kind: str) -> list[dict[str, Any]]:
    """Project a sealed run's ``score_rows`` into calibration-shaped rows (one per decision).

    Reuses the SINGLE-SOURCE-OF-TRUTH :func:`~veridex.scoring.is_scored` predicate: a row carries a
    real ``clv_bps`` only when it is scored, otherwise ``None`` (a WAIT/pending abstention — never a
    fabricated 0). ``action`` normalizes the sealed action type (enum-or-string), and ``market`` is
    the fired pick's ``market_key`` (``"n/a"`` for a no-position row).
    """
    rows: list[dict[str, Any]] = []
    for row in result.score_rows:
        raw_action = row.get("raw_prescore", {}).get("raw_action", {})
        action_type = raw_action.get("type")
        action = getattr(action_type, "value", action_type)
        market = raw_action.get("params", {}).get("market_key") or "n/a"
        clv_bps = row["clv_bps"] if is_scored(row) else None
        rows.append(
            {"fixture_id": fixture_id, "kind": kind, "market": str(market), "action": action, "clv_bps": clv_bps}
        )
    return rows


def _baseline_inputs(marketstates: list[MarketState]) -> dict[str, Any] | None:
    """Extract the inputs the heterogeneous baselines need from a fixture's ticks, or ``None``.

    DRIFT-1: the baselines take DIFFERENT inputs — a ``prices`` series (``no_trade`` / ``threshold_move``
    / ``seeded_random``), a ``fair_probs`` dict (``favorite``), and a horizon. All are derived from the
    first market (sorted) in the LAST tick. Returns ``None`` when no market is present (honest skip).
    """
    if not marketstates:
        return None
    last_markets = getattr(marketstates[-1], "markets", {}) or {}
    if not last_markets:
        return None
    market_key = sorted(last_markets)[0]
    prob_map: dict[str, Any] = last_markets[market_key].get("stable_prob_bps") or {}
    sides = sorted(prob_map)
    if not sides:
        return None
    side0 = sides[0]

    prices: list[float] = []
    for state in marketstates:
        market = (getattr(state, "markets", {}) or {}).get(market_key, {})
        price_map = market.get("stable_price") or {}
        tick_probs = market.get("stable_prob_bps") or {}
        if side0 in price_map:
            prices.append(float(price_map[side0]))
        elif side0 in tick_probs and tick_probs[side0]:
            prices.append(10000.0 / float(tick_probs[side0]))  # decimal price from the fair prob

    fair_probs = {side: float(bps) / 10000.0 for side, bps in prob_map.items()}
    horizon_s = int(getattr(marketstates[-1], "ts", 0)) - int(getattr(marketstates[0], "ts", 0))
    return {"market": market_key, "prices": prices, "fair_probs": fair_probs, "horizon_s": horizon_s}


def _baseline_action(name: str, fn: Callable, inputs: dict[str, Any], *, seed: int) -> AgentAction | None:
    """Call one baseline with ITS OWN signature (DRIFT-1); ``None`` for an unknown baseline shape."""
    prices = inputs["prices"]
    horizon_s = inputs["horizon_s"]
    if name == "no_trade":
        return fn(prices, horizon_s)
    if name == "favorite":
        return fn(inputs["fair_probs"], horizon_s)
    if name == "threshold_move":
        return fn(prices, horizon_s)
    if name == "seeded_random":
        return fn(prices, horizon_s, seed)
    return None  # a baseline whose signature the producer doesn't know — skip honestly, never guess


async def produce_results_by_fixture(
    protocol: EvalProtocol,
    *,
    packs: dict[int, Path],
    venue_price_source: Callable[[str], float | None] | None = None,
    venue_source_id: str | None = None,
) -> dict[int, list[dict[str, Any]]]:
    """Run the committed roster over each fixture's REAL pack into the ``results_by_fixture`` dict.

    For every ``fixture_id`` in ``protocol.fixture_ids`` the producer replays that fixture's pack and:

      * ``"cumulative-drift"`` → :func:`~veridex.backtest.runner.run_backtest` (the S3 real path).
      * ``"value-vs-venue"`` → :func:`~veridex.backtest.vvv_report.vvv_report_with_estimated_edge`
        ONLY when BOTH a ``venue_price_source`` AND a distinct, non-empty ``venue_source_id`` are
        supplied (DRIFT-2 / Codex M7). The producer NEVER synthesizes the identity from the callable
        — a low-entropy tag would re-open the M6 reproducibility gap — so a missing/empty
        ``venue_source_id`` FAILS CLOSED: the strategy is HONESTLY SKIPPED (no row, no faked price).
      * ``"stale-line"`` → NOT run here — it is cadence-gated inside
        :func:`run_multi_fixture_evaluation` (AC-009).
      * each named baseline → dispatched DIRECTLY with its own heterogeneous signature (DRIFT-1),
        one row per fixture; a baseline whose inputs can't be derived is skipped, never faked.

    No scoring logic is duplicated: the strategy rows come from the SAME sealed
    ``RunResult.score_rows`` those real paths produce.

    Args:
        protocol: The committed evaluation contract (its fixtures/roster/baselines drive the run).
        packs: ``{fixture_id: pack_dir}`` — the self-describing ReplayPack for each protocol fixture.
        venue_price_source: Optional injected venue DECIMAL-price source; when ``None`` the
            ValueVsVenue strategy is skipped for every fixture.
        venue_source_id: The EXPLICIT, distinct identity of that venue source (in production, the
            content_hash of the quote / price-history artifact), bound into the VvV ``config_hash``
            for reproducibility. Required for VvV: a missing/empty value skips VvV (fail closed). The
            producer never derives it from ``venue_price_source``.

    Returns:
        ``{fixture_id: [row, ...]}`` where each row is calibration-shaped
        (``{"fixture_id", "kind", "market", "action", "clv_bps"}``) — the exact input
        :func:`run_multi_fixture_evaluation` consumes.
    """
    results: dict[int, list[dict[str, Any]]] = {}
    for fixture_id in protocol.fixture_ids:
        pack_dir = packs[fixture_id]
        marketstates = load_pack_marketstates(pack_dir, fixture_id)
        window = _build_window(protocol, fixture_id, marketstates)
        rows: list[dict[str, Any]] = []

        for config in protocol.strategy_configs:
            if config == CUMULATIVE_DRIFT_CONFIG:
                result, _ = await run_backtest(pack_dir, fixture_id, [cumulative_drift_agent()], window=window)
                rows.extend(_rows_from_run(result, fixture_id=fixture_id, kind=config))
            elif config == VALUE_VS_VENUE_CONFIG:
                # DRIFT-2 / Codex M7 — FAIL CLOSED: run VvV only when a venue source AND a DISTINCT
                # explicit ``venue_source_id`` are BOTH supplied. The producer NEVER synthesizes the
                # identity (e.g. from a callable's ``__name__``): a low-entropy tag would let two
                # different sources share a config_hash while sealing different actions — the exact
                # M6 reproducibility gap. No explicit identity ⇒ honest skip, never a derived one.
                if venue_price_source is None or not venue_source_id:
                    continue
                result, _ = await vvv_report_with_estimated_edge(
                    pack_dir,
                    fixture_id,
                    venue_price_source=venue_price_source,
                    venue_source_id=venue_source_id,
                    window=window,
                    min_edge_bps=0,
                    assumptions={"no_interpolation": True, "source": "s6-producer"},
                )
                rows.extend(_rows_from_run(result, fixture_id=fixture_id, kind=config))
            # STALE_LINE_CONFIG (cadence-gated in the evaluation) and any unknown config: skip here.

        baseline_inputs = _baseline_inputs(marketstates)
        for name in protocol.baselines:
            fn = BASELINES.get(name)
            if fn is None or baseline_inputs is None:
                continue  # unknown baseline / no derivable inputs — honest skip (never a faked row)
            action = _baseline_action(name, fn, baseline_inputs, seed=fixture_id)
            if action is None:
                continue
            rows.append(
                {
                    "fixture_id": fixture_id,
                    "kind": name,
                    "market": str(baseline_inputs["market"]),
                    "action": action.type.value,
                    "clv_bps": None,  # baselines are called directly — no law-scored CLV to report
                }
            )

        results[fixture_id] = rows
    return results
