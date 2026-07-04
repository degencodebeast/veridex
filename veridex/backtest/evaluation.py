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

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from veridex.backtest.baseline_agents import baseline_agent
from veridex.backtest.baselines import BASELINES
from veridex.backtest.calibration import CalibrationReport, build_calibration_report
from veridex.backtest.market_filter import (
    EligibleMarketManifest,
    build_eligible_market_manifest,
)
from veridex.backtest.pre_match import plan_pre_match_backtest
from veridex.backtest.runner import run_backtest
from veridex.backtest.venue_behavior_report import (
    HEADLINE,
    VenueBehaviorRow,
    VenueDecision,
)
from veridex.backtest.vvv_report import vvv_report_with_estimated_edge
from veridex.ingest.marketstate import MarketState
from veridex.ingest.replay_pack import load_pack_marketstates
from veridex.provenance import EvidenceRung
from veridex.runtime.orchestrator import RunResult
from veridex.runtime.schemas import SportsActionType
from veridex.runtime.window import RunWindow
from veridex.scoring import is_scored
from veridex.strategies.drift import cumulative_drift_agent
from veridex.strategies.market_quality import MarketQualityConfig
from veridex.strategies.value_vs_venue import vvv_signal
from veridex.venues.venue_price_source import (
    TimedVenueQuote,
    VenuePriceSource,
    txline_ts_to_venue_seconds,
)

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

    # FU-3: the drift-vs-baseline comparison surface — drift's scored-CLV distribution alongside each
    # named baseline's. StaleLine is excluded (it is not produced here — cadence-gated separately); any
    # named kind that produced no rows is dropped by the builder, so no fabricated empty bucket appears.
    # HONESTY (entry asymmetry): favorite/threshold_move enter at the EARLIEST usable tick (full
    # pre-kickoff CLV runway), while drift enters gated-late (min_tick_count/min_horizon), so the
    # head-to-head structurally FAVORS the baseline — the bias runs ADVERSE to drift, never flattering it.
    comparison_kinds = [
        config for config in protocol.strategy_configs if config != STALE_LINE_CONFIG
    ] + list(protocol.baselines)
    calibration: CalibrationReport = build_calibration_report(
        rows, provenance=EvidenceRung.TXLINE_ONLY.value, comparison_kinds=comparison_kinds
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


def _rows_from_run(
    result: RunResult, *, fixture_id: int, kind: str, agent_id: str | None = None
) -> list[dict[str, Any]]:
    """Project a sealed run's ``score_rows`` into calibration-shaped rows (one per decision).

    Reuses the SINGLE-SOURCE-OF-TRUTH :func:`~veridex.scoring.is_scored` predicate: a row carries a
    real ``clv_bps`` only when it is scored, otherwise ``None`` (a WAIT/pending abstention — never a
    fabricated 0). ``action`` normalizes the sealed action type (enum-or-string), and ``market`` is
    the fired pick's ``market_key`` (``"n/a"`` for a no-position row).

    ``agent_id`` (FU-3) filters a multi-agent run's rows to one participant — the baselines share ONE
    ``run_backtest`` (head-to-head on identical ticks), so each is projected back to its own ``kind``.
    """
    rows: list[dict[str, Any]] = []
    for row in result.score_rows:
        if agent_id is not None and row.get("agent_id") != agent_id:
            continue
        raw_action = row.get("raw_prescore", {}).get("raw_action", {})
        action_type = raw_action.get("type")
        action = getattr(action_type, "value", action_type)
        market = raw_action.get("params", {}).get("market_key") or "n/a"
        clv_bps = row["clv_bps"] if is_scored(row) else None
        rows.append(
            {"fixture_id": fixture_id, "kind": kind, "market": str(market), "action": action, "clv_bps": clv_bps}
        )
    return rows


def _pre_match_close_ts(marketstates: list[MarketState], window: RunWindow) -> int:
    """The CON-040 per-market close ts used ONLY for the report-only ``time_to_close_s`` (C-6, C-5).

    For a ``pre_match`` window the close is the folded last-pre-kickoff snapshot from
    :func:`~veridex.backtest.pre_match.plan_pre_match_backtest` (the SAME close ``run_backtest`` seals
    against). When no true pre-match close exists (degrade) or the window is not ``pre_match``, fall
    back to the last observed tick ts — this ts feeds ONLY the descriptive time-to-close bucket, never
    scoring or the sealed evidence.
    """
    if window.end_rule == "pre_match":
        plan = plan_pre_match_backtest(marketstates)
        if plan.closing_state is not None:
            return int(plan.closing_state.ts)
    return max((int(state.ts) for state in marketstates), default=0)


def _first_matched_quote(
    state: MarketState, venue_price_source: VenuePriceSource
) -> TimedVenueQuote | None:
    """The first time-aligned quote over the VvV agent's OWN market/side iteration on this tick.

    Mirrors :func:`~veridex.strategies.value_vs_venue.value_vs_venue_agent`'s decide loop (sorted
    markets, skip suspended / non-dict prob maps, sorted sides) so "could this tick be priced?" is
    answered over exactly the coordinates the agent evaluated — the honest ``quote_matched`` signal for
    a WAIT tick (CON-012). Returns ``None`` when no side had a quote under the source's freshness bound.
    """
    markets: dict[str, dict[str, Any]] = getattr(state, "markets", {}) or {}
    for market_key in sorted(markets):
        market = markets[market_key]
        if market.get("suspended"):
            continue
        prob_bps = market.get("stable_prob_bps", {})
        if not isinstance(prob_bps, dict):
            continue
        for side in sorted(prob_bps):
            # Source is keyed by unix SECONDS; state.ts is unix MILLISECONDS → convert at the seam.
            quote = venue_price_source(
                state.fixture_id, market_key, side, txline_ts_to_venue_seconds(state.ts)
            )
            if quote is not None:
                return quote
    return None


def _collect_vvv_venue_behavior(
    result: RunResult,
    states_by_tick: dict[int, MarketState],
    *,
    venue_price_source: VenuePriceSource,
    close_ts: int,
    coverage_class: str,
) -> tuple[list[VenueBehaviorRow], list[VenueDecision]]:
    """Collect the VvV decision opportunities + fired-pick behavior rows for the C-5 report (C-6).

    One :class:`~veridex.backtest.venue_behavior_report.VenueDecision` is recorded for EVERY decision
    tick (CON-012: decision coverage != fixture coverage), including WAIT ticks where the source had no
    quote (``quote_matched=False``). A fired pick's decision reads the venue quote for its OWN
    (market_key, side) coordinate, so ``quote_matched=True`` ALWAYS carries the used mid's
    ``staleness_s`` (6b — else the C-5 model raises); a WAIT tick's coverage is the first quote over the
    agent's iteration. Fired picks additionally yield a :class:`VenueBehaviorRow` carrying the RAW
    estimated edge (haircut applied at report time only) — the venue numbers live here, never in the
    sealed run.
    """
    decisions: list[VenueDecision] = []
    rows: list[VenueBehaviorRow] = []
    for row in result.score_rows:
        tick_seq = row.get("tick_seq")
        state = states_by_tick.get(tick_seq) if isinstance(tick_seq, int) else None
        if state is None:
            continue
        raw_action = row.get("raw_prescore", {}).get("raw_action", {})
        action_type = raw_action.get("type")
        fired = getattr(action_type, "value", action_type) == SportsActionType.FOLLOW_MOMENTUM.value
        if not fired:
            quote = _first_matched_quote(state, venue_price_source)
            decisions.append(
                VenueDecision(
                    fired=False,
                    quote_matched=quote is not None,
                    staleness_s=quote.staleness_s if quote is not None else None,
                )
            )
            continue

        # A fired pick prices against its OWN sealed (market_key, side) at the SAME tick it fired on.
        params = raw_action.get("params", {})
        market_key = params.get("market_key")
        side = params.get("side")
        # Source is keyed by unix SECONDS; state.ts is unix MILLISECONDS → convert at the seam.
        quote = (
            venue_price_source(
                state.fixture_id, market_key, side, txline_ts_to_venue_seconds(state.ts)
            )
            if market_key is not None and side is not None
            else None
        )
        decisions.append(
            VenueDecision(
                fired=True,
                quote_matched=quote is not None,
                staleness_s=quote.staleness_s if quote is not None else None,
            )
        )
        if quote is None:
            continue
        prob_bps = (getattr(state, "markets", {}) or {}).get(market_key, {}).get("stable_prob_bps", {})
        if not isinstance(prob_bps, dict) or side not in prob_bps:
            continue
        try:
            fair_prob_bps = int(prob_bps[side])
        except (TypeError, ValueError):
            continue
        estimated = vvv_signal(fair_prob_bps, quote.venue_decimal_price)["estimated_executable_edge_bps"]
        if estimated is None:
            continue
        rows.append(
            VenueBehaviorRow(
                side=str(side),
                fair_prob=fair_prob_bps / 10_000.0,
                venue_decimal_price=quote.venue_decimal_price,
                staleness_s=quote.staleness_s,
                time_to_close_s=max(0, close_ts - int(state.ts)),
                estimated_edge_bps=estimated,
                coverage_class=coverage_class,
            )
        )
    return rows, decisions


async def produce_results_by_fixture(
    protocol: EvalProtocol,
    *,
    packs: dict[int, Path],
    venue_price_source: VenuePriceSource | None = None,
    venue_source_id: str | None = None,
    market_quality_config: MarketQualityConfig | None = None,
    manifest_sink: dict[int, EligibleMarketManifest] | None = None,
    venue_decision_sink: dict[int, list[VenueDecision]] | None = None,
    venue_behavior_row_sink: dict[int, list[VenueBehaviorRow]] | None = None,
    venue_coverage_class_by_fixture: dict[int, str] | None = None,
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
      * each named baseline → wrapped as an Agent (:func:`~veridex.backtest.baseline_agents.baseline_agent`)
        and run through :func:`~veridex.backtest.runner.run_backtest` — the SAME scored path drift uses, so
        a fired baseline pick is SCORED by the law against the per-market CON-040 kickoff close (FU-3), not
        a hardcoded null. The baselines share ONE run (head-to-head on identical ticks) and their rows are
        split back to per-baseline ``kind``; ``no_trade`` always WAITs (stays null) and a pick with no
        valid close FAILS CLOSED (degrades to WINDOW CLV → unscored), never a fabricated CLV.

    No scoring logic is duplicated: EVERY strategy AND baseline row comes from the SAME sealed
    ``RunResult.score_rows`` ``run_backtest`` produces — the law owns CLV.

    Args:
        protocol: The committed evaluation contract (its fixtures/roster/baselines drive the run).
        packs: ``{fixture_id: pack_dir}`` — the self-describing ReplayPack for each protocol fixture.
        venue_price_source: Optional injected venue DECIMAL-price source; when ``None`` the
            ValueVsVenue strategy is skipped for every fixture.
        venue_source_id: The EXPLICIT, distinct identity of that venue source (in production, the
            content_hash of the quote / price-history artifact), bound into the VvV ``config_hash``
            for reproducibility. Required for VvV: a missing/empty value skips VvV (fail closed). The
            producer never derives it from ``venue_price_source``.
        market_quality_config: FU-2 OPT-IN eligibility gate. ``None`` (default) ⇒ UNFILTERED — every
            market enters the scored universe, byte-identical to the pre-FU-2 behavior (no manifest is
            emitted). When provided, the M1 filter (:func:`~veridex.strategies.market_quality.evaluate_market_quality`)
            builds a per-fixture ELIGIBLE allowlist over the pinned config BEFORE scoring, and that
            SAME allowlist is applied to drift AND baselines (identical eligible universe). It changes
            WHICH markets are scored, never HOW CLV is computed. A fixture with ZERO eligible markets is
            a NAMED skip (empty rows) recorded in its manifest — never a silent empty. (The venue-rung
            ValueVsVenue path is intentionally NOT gated here: it is a separate strategy priced off
            backfilled venue data, outside the drift-vs-baseline apples-to-apples comparison.)
        manifest_sink: Optional ``{fixture_id: EligibleMarketManifest}`` collector. When
            ``market_quality_config`` is provided, the producer populates it with the eligible-market
            manifest for each fixture (``filter_config_hash`` + eligible/excluded market_keys + reasons
            + counts + the ``zero_eligible`` named-skip flag), so a Run-001 protocol can pin the exact
            scored market universe. Untouched on the unfiltered (``None``-config) path.
        venue_decision_sink: Optional ``{fixture_id: [VenueDecision, ...]}`` collector (C-6). When
            provided AND the VvV strategy runs, the producer records ONE decision per VvV decision tick
            — ``fired`` / ``quote_matched`` (did the time-aligned source have a quote) / ``staleness_s``
            (the used mid's age on a match) — over ALL ticks (CON-012), the ``decision_quote_coverage``
            input for the C-5 report. Every ``quote_matched=True`` decision carries a non-None
            ``staleness_s`` (6b). Untouched when the VvV strategy is not run.
        venue_behavior_row_sink: Optional ``{fixture_id: [VenueBehaviorRow, ...]}`` collector (C-6):
            the FIRED-pick rows (side / fair_prob / venue mid / staleness / raw estimated edge /
            time-to-close / coverage_class) the C-5 report slices over. Venue numbers live ONLY here
            (report layer), never in the sealed run.
        venue_coverage_class_by_fixture: Optional ``{fixture_id: "headline"|"diagnostic-partial"}``.
            The coverage class stamped on that fixture's behavior rows (default ``"headline"``); a
            Run-002 headline-only universe leaves it defaulted.

    Returns:
        ``{fixture_id: [row, ...]}`` where each row is calibration-shaped
        (``{"fixture_id", "kind", "market", "action", "clv_bps"}``) — the exact input
        :func:`run_multi_fixture_evaluation` consumes.
    """
    # FU-2 fail-loud (review Finding A): the manifest is the "no filter claim without it" artifact, so
    # filtering with nowhere to retain the eligible/excluded universe (incl. a named zero-eligible skip)
    # is a footgun — refuse it rather than silently drop the record.
    if market_quality_config is not None and manifest_sink is None:
        raise ValueError(
            "market_quality_config was provided without a manifest_sink: the eligible-market manifest is "
            "the required FU-2 artifact (no filter claim without it) — pass a manifest_sink dict to retain it."
        )

    results: dict[int, list[dict[str, Any]]] = {}
    for fixture_id in protocol.fixture_ids:
        pack_dir = packs[fixture_id]
        marketstates = load_pack_marketstates(pack_dir, fixture_id)
        window = _build_window(protocol, fixture_id, marketstates)

        # FU-2 eligibility gate (opt-in): build the ELIGIBLE-MARKET allowlist + manifest BEFORE scoring,
        # then feed the SAME allowlist to drift AND baselines so the comparison stays apples-to-apples.
        # None-config leaves ``market_key_allowlist`` as ``None`` — run_backtest filters nothing (unchanged).
        market_key_allowlist: list[str] | None = None
        if market_quality_config is not None:
            manifest = build_eligible_market_manifest(fixture_id, marketstates, market_quality_config)
            if manifest_sink is not None:
                manifest_sink[fixture_id] = manifest
            market_key_allowlist = manifest.eligible
            if manifest.zero_eligible:
                # No silent zero: ZERO eligible markets is a NAMED skip in the manifest (recorded above),
                # surfaced here as an explicit empty result — the fixture is not scored on a degenerate universe.
                results[fixture_id] = []
                continue

        rows: list[dict[str, Any]] = []

        for config in protocol.strategy_configs:
            if config == CUMULATIVE_DRIFT_CONFIG:
                result, _ = await run_backtest(
                    pack_dir, fixture_id, [cumulative_drift_agent()], window=window,
                    market_key_allowlist=market_key_allowlist,
                )
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
                # C-6: collect the VvV decision opportunities (decision_quote_coverage, CON-012) + the
                # fired-pick behavior rows for the C-5 VenueBehaviorReport. Opt-in via the sinks; the
                # venue numbers live ONLY in these report-layer artifacts, never in the sealed `result`.
                if venue_decision_sink is not None or venue_behavior_row_sink is not None:
                    states_by_tick = {int(state.tick_seq): state for state in marketstates}
                    coverage_class = (venue_coverage_class_by_fixture or {}).get(fixture_id, HEADLINE)
                    behavior_rows, decisions = _collect_vvv_venue_behavior(
                        result,
                        states_by_tick,
                        venue_price_source=venue_price_source,
                        close_ts=_pre_match_close_ts(marketstates, window),
                        coverage_class=coverage_class,
                    )
                    if venue_behavior_row_sink is not None:
                        venue_behavior_row_sink[fixture_id] = behavior_rows
                    if venue_decision_sink is not None:
                        venue_decision_sink[fixture_id] = decisions
            # STALE_LINE_CONFIG (cadence-gated in the evaluation) and any unknown config: skip here.

        # FU-3: each acting baseline is now SCORED through the SAME path drift uses — wrapped as an Agent
        # (baseline_agent) and run through run_backtest, so its fired pick gets a real clv_bps recomputed
        # by the law against the per-market CON-040 kickoff close (D2), never a hardcoded None. All named
        # baselines share ONE run (head-to-head on identical ticks, the CompetitionRun contract); their
        # rows are split back to per-baseline ``kind`` by agent_id. no_trade always WAITs → its rows stay
        # unscored/null; a fired pick with no valid close FAILS CLOSED inside run_backtest (degrades to
        # WINDOW CLV → is_scored False → clv_bps None), never a fabricated CLV.
        baseline_names = [name for name in protocol.baselines if name in BASELINES]
        if baseline_names:
            baseline_run, _ = await run_backtest(
                pack_dir,
                fixture_id,
                [baseline_agent(name, seed=fixture_id) for name in baseline_names],
                window=window,
                market_key_allowlist=market_key_allowlist,
            )
            for name in baseline_names:
                rows.extend(_rows_from_run(baseline_run, fixture_id=fixture_id, kind=name, agent_id=name))

        results[fixture_id] = rows
    return results
