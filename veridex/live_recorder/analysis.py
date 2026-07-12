"""E8 post-session analysis / report for the live-recorder lane (MM-R3, milestone E8).

Reads a SEALED session back via :func:`~veridex.live_recorder.replay.read_session` and
produces an :class:`AnalysisResult` covering cadence, lead-lag, and queue-jump — all
gap-excluded and OBSERVATION-only. This module CONSUMES already-built, already-verified
primitives and reimplements no trust logic:

* :func:`~veridex.live_recorder.replay.iter_change_series` (E3) — the gap-crossing
  exclusion (AC-008) itself lives there; every FV series here is built ON TOP OF its
  consecutive, never-gap-crossing pairs (:func:`_fv_segments_by_market`
  reconstructs the maximal gap-safe chains from those pairs).
* :func:`scripts.maker.leadlag_probe.compress_to_change_events` /
  :func:`~scripts.maker.leadlag_probe.run_leadlag_probe` /
  :func:`~scripts.maker.leadlag_probe.render_markdown` (committed cadence + lead-lag
  primitives, consumed UNCHANGED).
* :func:`~veridex.live_recorder.executability.derive_queue_jump` (E5) — queue-jump
  derivation, keyed off the recorder's own ``recv_ts`` clock.

Trust boundaries enforced here (the whole point of E8):

* **Gaps excluded from analysis (AC-008).** Every per-market series — cadence AND
  lead-lag — is built from :func:`_fv_segments_by_market`, which reconstructs the
  maximal event chains that :func:`iter_change_series` never spliced across a gap; a
  lead-lag join window is bounded to one such chain's own timestamp range, so a
  book event recorded inside a gap can never enter either series.
* **COUNTERFACTUAL / observation only (EXE-003, CON-004, GUD-001).**
  :func:`render_session_report` contains NO fill / fill-rate / realized-PnL / rank /
  "profitable" / "executable edge" claim. The only claims rendered are:
  observed-size-at-price-at-T (COUNTERFACTUAL), cadence (gap-excluded), no-look-ahead
  replay-reproduced evidence, and R4-prerequisite met/not-met status. When the
  lead-lag evidence does not support a lead, the report says so HONESTLY — it does
  NOT splice in :func:`~scripts.maker.leadlag_probe.render_markdown`'s fixed "FV
  LEADS" narrative unless the probe's own verdict actually says so.

This module itself imports nothing from ``veridex.scoring`` or ``veridex.maker`` directly
(only the committed ``scripts.maker.leadlag_probe`` analysis script, which is not a trust
module — see its own docstring). No network. No secret logged.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.maker.leadlag_probe import (
    HEADLINE_THRESHOLD_BPS,
    ProbeResult,
    compress_to_change_events,
    render_markdown,
    run_leadlag_probe,
)
from veridex.live_recorder.contracts import LiveRecorderSessionMeta, VenueBookSnapshotEvent
from veridex.live_recorder.executability import (
    QueueJumpDecision,
    _BookEventLike,
    derive_queue_jump,
)
from veridex.live_recorder.replay import iter_change_series, read_session, replay_reproduces

__all__ = [
    "MarketKey",
    "CadenceSummary",
    "QueueJumpObservation",
    "AnalysisResult",
    "analyze_session",
    "render_session_report",
]

#: A ``(fixture_id, market_ref)`` market key, mirroring ``iter_change_series``'s grouping.
MarketKey = tuple[int, str]


@dataclass(frozen=True)
class CadenceSummary:
    """Per-market FV cadence, computed only over gap-safe (never gap-crossing) chains.

    ``n_gap_safe_changes`` is the count of :func:`compress_to_change_events` change events
    across all gap-safe chains for the market (an FV VALUE-change count, never a fill or
    edge measure). ``mean_interval_ms``/``median_interval_ms`` are ``None`` when fewer than
    two consecutive gap-safe FV events exist for the market (never a fabricated number).
    """

    key: MarketKey
    n_fv_events: int
    n_gap_safe_changes: int
    mean_interval_ms: float | None
    median_interval_ms: float | None


@dataclass(frozen=True)
class QueueJumpObservation:
    """One decision's DERIVED, COUNTERFACTUAL queue-jump outcome (never a fill).

    Carries exactly the fields :func:`~veridex.live_recorder.executability.derive_queue_jump`
    produces — no fill-probability / queue-simulation field is ever added here.
    """

    decision_id: str
    outbid_within_ms: int | None
    stepped_ahead_count: int


@dataclass(frozen=True)
class AnalysisResult:
    """Full session analysis: cadence, lead-lag, and queue-jump — all observation-only.

    ``replay_reproduced`` is the byte-determinism check
    (:func:`~veridex.live_recorder.replay.replay_reproduces`); ``r3_replay_prereq_met`` is
    an honest, closed-form boolean for the R3-LOCAL replay/cadence prerequisite — R4 gate 1
    of 4 ONLY (sealed + replay-reproduced + at least one gap-safe FV change observed). It is
    NEVER a fill/edge/rank claim, and it is NEVER an R4-readiness claim: R4 go/no-go requires
    three further, independent gates NOT evaluated here (live FV lead confirmed; make-vs-take
    EV positive under Rose 4× fee stress; guarded-live safety wiring).
    """

    session_meta: LiveRecorderSessionMeta
    n_events: int
    n_gaps: int
    fixture_ids: tuple[int, ...]
    cadence_by_market: tuple[CadenceSummary, ...]
    leadlag: ProbeResult
    queue_jump: tuple[QueueJumpObservation, ...]
    replay_reproduced: bool
    r3_replay_prereq_met: bool


def _fv_segments_by_market(
    fv_events: list[dict[str, Any]], gaps: list[dict[str, Any]]
) -> dict[MarketKey, list[list[dict[str, Any]]]]:
    """Reconstruct maximal, never-gap-crossing FV event chains per market.

    ``iter_change_series`` yields every consecutive ``(prev, curr)`` pair EXCEPT the ones
    whose interval crosses a recorded gap; a chain therefore breaks exactly where a pair was
    excluded. Walking the pairs in order and starting a new segment whenever the next pair's
    ``prev`` is not the running segment's last event reconstructs those chains WITHOUT
    reimplementing any gap-crossing logic (that logic lives solely in ``iter_change_series``).
    """
    by_key: dict[MarketKey, list[list[dict[str, Any]]]] = {}
    for key, prev, curr in iter_change_series(fv_events, gaps):
        segments = by_key.setdefault(key, [])
        if segments and segments[-1][-1]["sequence_no"] == prev["sequence_no"]:
            segments[-1].append(curr)
        else:
            segments.append([prev, curr])
    return by_key


def _cadence_summaries(
    fv_events: list[dict[str, Any]],
    fv_segments_by_key: dict[MarketKey, list[list[dict[str, Any]]]],
) -> list[CadenceSummary]:
    """Per-market cadence: FV value-change count (via ``compress_to_change_events``) and
    interval timing, computed ONLY within each gap-safe chain (never across a chain break)."""
    n_fv_by_key: dict[MarketKey, int] = {}
    for event in fv_events:
        if "fixture_id" in event and "market_ref" in event:
            key = (event["fixture_id"], event["market_ref"])
            n_fv_by_key[key] = n_fv_by_key.get(key, 0) + 1

    summaries: list[CadenceSummary] = []
    for key in sorted(n_fv_by_key, key=str):
        segments = fv_segments_by_key.get(key, [])
        n_changes = 0
        intervals: list[int] = []
        for segment in segments:
            ts = [event["recv_ts"] for event in segment]
            fv = [event["fv"] for event in segment]
            # mid=fv here counts FV VALUE-change events (a cadence measure), reusing the
            # primitive rather than reimplementing its change-detection logic.
            n_changes += len(compress_to_change_events(ts, fv, fv))
            intervals.extend(
                curr["recv_ts"] - prev["recv_ts"]
                for prev, curr in zip(segment, segment[1:], strict=False)
            )
        summaries.append(
            CadenceSummary(
                key=key,
                n_fv_events=n_fv_by_key[key],
                n_gap_safe_changes=n_changes,
                mean_interval_ms=(sum(intervals) / len(intervals)) if intervals else None,
                median_interval_ms=statistics.median(intervals) if intervals else None,
            )
        )
    return summaries


def _venue_mid(book_event: dict[str, Any]) -> float | None:
    """Best-bid/best-ask midpoint from a recorded book event's levels, or ``None``.

    An empty/missing side yields ``None`` (never imputed) — mirrors the honesty discipline
    of ``veridex.live_recorder.executability._best_price``.
    """
    bids = book_event.get("bids") or []
    asks = book_event.get("asks") or []
    best_bid = max((level["price"] for level in bids), default=None)
    best_ask = min((level["price"] for level in asks), default=None)
    if best_bid is None or best_ask is None:
        return None
    return (best_bid + best_ask) / 2.0


def _leadlag_series_by_market(
    fv_segments_by_key: dict[MarketKey, list[list[dict[str, Any]]]],
    book_events: list[dict[str, Any]],
) -> dict[tuple[Any, ...], tuple[list[int], list[float], list[float]]]:
    """Build ``(ts, fv, mid)`` lead-lag input series, one per gap-safe FV chain.

    A book event only enters a chain's series if its ``recv_ts`` falls WITHIN that chain's
    own ``[first, last]`` FV timestamp span — a book event recorded during a gap (between
    two chains) is therefore excluded from BOTH neighboring chains, never spliced across the
    gap it fell inside. Each chain gets its own key (``fixture_id, market_ref, segment_idx``)
    so ``run_leadlag_probe`` never compresses a change event across a chain break.
    """
    book_by_ref: dict[str, list[dict[str, Any]]] = {}
    for book_event in book_events:
        ref = book_event.get("venue_market_ref")
        if ref is None:
            continue
        book_by_ref.setdefault(ref, []).append(book_event)
    for books in book_by_ref.values():
        books.sort(key=lambda event: (event["recv_ts"], event["sequence_no"]))

    series: dict[tuple[Any, ...], tuple[list[int], list[float], list[float]]] = {}
    for (fixture_id, market_ref), segments in fv_segments_by_key.items():
        book_list = book_by_ref.get(market_ref, [])
        for segment_idx, segment in enumerate(segments):
            lo, hi = segment[0]["recv_ts"], segment[-1]["recv_ts"]
            windowed_books = [b for b in book_list if lo <= b["recv_ts"] <= hi]
            merged: list[tuple[int, int, str, dict[str, Any]]] = [
                (event["recv_ts"], event["sequence_no"], "fv", event) for event in segment
            ] + [(book["recv_ts"], book["sequence_no"], "book", book) for book in windowed_books]
            merged.sort(key=lambda item: (item[0], item[1]))

            ts: list[int] = []
            fv_vals: list[float] = []
            mid_vals: list[float] = []
            cur_fv: float | None = None
            cur_mid: float | None = None
            for t, _seq, kind, event in merged:
                if kind == "fv":
                    cur_fv = event["fv"]
                else:
                    cur_mid = _venue_mid(event)
                if cur_fv is not None and cur_mid is not None:
                    ts.append(t)
                    fv_vals.append(cur_fv)
                    mid_vals.append(cur_mid)
            if len(mid_vals) >= 2:
                series[(fixture_id, market_ref, segment_idx)] = (ts, fv_vals, mid_vals)
    return series


def _queue_jump_observations(
    events: list[dict[str, Any]], book_events: list[dict[str, Any]]
) -> tuple[QueueJumpObservation, ...]:
    """Derive one :class:`QueueJumpObservation` per recorded ``QuoteIntentEvent``.

    Delegates entirely to :func:`~veridex.live_recorder.executability.derive_queue_jump`
    (keyed off the recorder's own ``recv_ts`` clock) — this function only assembles the
    inputs and never computes an outcome itself.
    """
    intents = [event for event in events if event.get("event_type") == "QuoteIntentEvent"]
    if not intents:
        return ()
    book_models = [VenueBookSnapshotEvent.model_validate(b) for b in book_events]
    observations: list[QueueJumpObservation] = []
    for intent in sorted(intents, key=lambda event: (event["recv_ts"], event["sequence_no"])):
        decision = QueueJumpDecision(
            decision_id=intent["decision_id"],
            side=intent["side"],
            native_price=intent["native_price"],
            recv_ts=intent["recv_ts"],
        )
        subsequent: list[_BookEventLike] = [
            book for book in book_models if book.recv_ts >= intent["recv_ts"]
        ]
        derivation = derive_queue_jump(decision, subsequent)
        observations.append(
            QueueJumpObservation(
                decision_id=derivation.decision_id,
                outbid_within_ms=derivation.outbid_within_ms,
                stepped_ahead_count=derivation.stepped_ahead_count,
            )
        )
    return tuple(observations)


def analyze_session(path: str | Path) -> AnalysisResult:
    """Read a sealed session and produce its gap-excluded, observation-only analysis.

    Groups by ``(fixture_id, market_ref)``, NEVER letting a cadence or lead-lag series cross
    a recorded gap (AC-008; see :func:`_fv_segments_by_market`), and derives queue-jump from
    the recorded book stream. Every executability-adjacent number is COUNTERFACTUAL; this
    function produces no fill / PnL / rank field.
    """
    meta, events, gaps = read_session(path)

    fv_events = [event for event in events if event.get("event_type") == "FairValueEvent"]
    book_events = [
        event for event in events if event.get("event_type") == "VenueBookSnapshotEvent"
    ]

    fv_segments_by_key = _fv_segments_by_market(fv_events, gaps)
    cadence_by_market = _cadence_summaries(fv_events, fv_segments_by_key)
    leadlag_series = _leadlag_series_by_market(fv_segments_by_key, book_events)
    leadlag = run_leadlag_probe(leadlag_series)
    queue_jump = _queue_jump_observations(events, book_events)

    replay_reproduced = replay_reproduces(path)
    # R3-LOCAL replay/cadence prerequisite — R4 gate 1 of 4 ONLY. This asserts nothing about
    # the other three R4 gates (live FV lead confirmed / make-vs-take EV positive under Rose
    # 4× fee stress / guarded-live safety wiring), which R3 does not and must not evaluate.
    r3_replay_prereq_met = (
        replay_reproduced
        and meta.ended_ts is not None
        and bool(meta.fixture_ids)
        and any(summary.n_gap_safe_changes > 0 for summary in cadence_by_market)
    )

    return AnalysisResult(
        session_meta=meta,
        n_events=len(events),
        n_gaps=len(gaps),
        fixture_ids=meta.fixture_ids,
        cadence_by_market=tuple(cadence_by_market),
        leadlag=leadlag,
        queue_jump=queue_jump,
        replay_reproduced=replay_reproduced,
        r3_replay_prereq_met=r3_replay_prereq_met,
    )


def _fmt_optional(value: float | None) -> str:
    return f"{value:.1f}" if value is not None else "n/a"


def render_session_report(result: AnalysisResult) -> str:
    """Render *result* as an observation-only Markdown report.

    Every executability-adjacent reference is labeled ``COUNTERFACTUAL``; the only claims
    made are observed-size-at-price-at-T, gap-excluded cadence, no-look-ahead
    replay-reproduced evidence, and the R3-LOCAL replay/cadence prerequisite (R4 gate 1 of 4)
    met/not-met status. The report NEVER claims "R4 prerequisites met": R4 go/no-go needs
    three further gates (live FV lead confirmed / make-vs-take EV positive under Rose 4× fee
    stress / guarded-live safety wiring) that are NOT evaluated here. When the lead-lag
    probe's own verdict is NOT an actual lead, the full narrative from
    :func:`~scripts.maker.leadlag_probe.render_markdown` (whose callout text is fixed to
    describe a confirmed lead) is deliberately NOT embedded — this section instead states
    the honest verdict directly, with no overclaim.
    """
    lines: list[str] = []
    lines.append("# Live-recorder session analysis (observation only, COUNTERFACTUAL)")
    lines.append("")
    lines.append(
        "This report is built from a sealed, no-look-ahead-replay-reproduced session. It "
        "makes ONLY the following claims: observed-size-at-price-at-T (COUNTERFACTUAL), "
        "gap-excluded cadence, no-look-ahead replay-reproduced evidence, and the R3-local "
        "replay/cadence prerequisite (R4 gate 1 of 4) met/not-met status. It does NOT claim "
        "R4 readiness."
    )
    lines.append("")
    lines.append(f"- session_ts: `{result.session_meta.session_ts}`")
    lines.append(f"- fixture_ids: `{result.fixture_ids}`")
    lines.append(f"- events recorded: {result.n_events}  gaps: {result.n_gaps}")
    lines.append(f"- no-look-ahead replay-reproduced: {result.replay_reproduced}")
    r3_gate_status = "met" if result.r3_replay_prereq_met else "not met"
    lines.append(
        f"- R3 replay/cadence prerequisite (R4 gate 1 of 4): {r3_gate_status}"
    )
    lines.append(
        "- R4 gates 2-4 (live FV-lead confirmed / make-vs-take EV positive under Rose 4x / "
        "guarded-live safety wiring): NOT evaluated -- declared-gated, not run."
    )
    lines.append("")

    lines.append("## Cadence (gap-excluded FV value-change series)")
    lines.append("")
    if not result.cadence_by_market:
        lines.append("No FV events recorded in this session.")
    else:
        lines.append(
            "| market (fixture_id, market_ref) | fv events | gap-safe changes | "
            "mean interval (ms) | median interval (ms) |"
        )
        lines.append("|---|---|---|---|---|")
        for summary in result.cadence_by_market:
            lines.append(
                f"| {summary.key} | {summary.n_fv_events} | {summary.n_gap_safe_changes} | "
                f"{_fmt_optional(summary.mean_interval_ms)} | "
                f"{_fmt_optional(summary.median_interval_ms)} |"
            )
    lines.append("")

    lines.append("## Lead-lag (COUNTERFACTUAL data-freshness observation, gap-excluded)")
    lines.append("")
    if result.leadlag.verdict.startswith("FV LEADS"):
        # Only splice in the full narrative when the probe's own verdict actually says so —
        # its callout text is fixed and would overclaim a lead if reused unconditionally.
        lines.append(render_markdown(result.leadlag))
    else:
        headline = next(
            (
                agg
                for agg in result.leadlag.aggregates
                if agg.threshold_bps == HEADLINE_THRESHOLD_BPS
            ),
            None,
        )
        lines.append(f"VERDICT (honest, no overclaim): {result.leadlag.verdict}")
        lines.append("")
        lines.append(
            "No confirmed lead in this session's gap-safe evidence. This is stated "
            "honestly, not embellished."
        )
        if headline is not None:
            lines.append("")
            lines.append(
                f"- headline (50 bps) NEXT hit rate: {headline.next_rate!r} "
                f"(n={headline.next_n})"
            )
    lines.append("")

    lines.append(
        "## Queue-jump (COUNTERFACTUAL — derived from the recorded post-decision book "
        "stream only)"
    )
    lines.append("")
    if not result.queue_jump:
        lines.append("No decision-time quote intents recorded in this session.")
    else:
        lines.append("| decision_id | outbid_within_ms | stepped_ahead_count |")
        lines.append("|---|---|---|")
        for observation in result.queue_jump:
            outbid = (
                observation.outbid_within_ms
                if observation.outbid_within_ms is not None
                else "n/a"
            )
            lines.append(
                f"| {observation.decision_id} | {outbid} | {observation.stepped_ahead_count} |"
            )
    lines.append("")

    return "\n".join(lines)
