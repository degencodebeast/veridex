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

from typing import Any

from pydantic import BaseModel

from veridex.backtest.calibration import CalibrationReport, build_calibration_report
from veridex.provenance import EvidenceRung

#: The strategy-config id that names the (cadence-gated) StaleLine decision strategy (M8).
STALE_LINE_CONFIG = "stale-line"
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
        "concentration_top_match_share_pct": EvidenceRung.TXLINE_ONLY.value,
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
