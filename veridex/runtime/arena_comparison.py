"""II-9 — the checkpointed arena comparison: deterministic Drift vs LLM-Drift, fairly and honestly.

The rules-vs-reasoning claim is only defensible when BOTH contestants decide at the SAME pinned
checkpoints from the SAME shared snapshot, and the comparison report never launders a bare average
CLV as its headline (addendum §3, lines 78-83). This module is the ARENA DRIVER that makes that so.
It is purely ADDITIVE — it wraps the existing pieces without mutating any of them:

  * ONE shared projector (:class:`ArenaSharedProjector`) does the drift-feature projection ONCE per
    tick — delegating the MATH to II-7's pure :func:`~veridex.strategies.drift_features.drift_features`
    — and hands the IDENTICAL :class:`~veridex.strategies.drift_features.DriftFeatureSnapshot` (same
    ``evidence_hash``) to BOTH contestants. Neither contestant projects independently, so they
    cannot diverge on evidence.
  * A SNAPSHOT-TS clock (:class:`SnapshotClock`) drives the checkpoint cadence and evidence-age from
    the replay's own timestamps, so the arena replay is deterministic.
  * II-8's :class:`~veridex.runtime.llm_checkpoint.CheckpointPolicy` pins the checkpoint grid and its
    :class:`~veridex.runtime.llm_checkpoint.InflightGuard` preserves the one-physical-call-in-flight
    invariant VERBATIM (the exact class — not a re-implementation). LLM-Drift's own prompt builder and
    trust-boundary revalidation are CONSUMED from :mod:`veridex.strategies.llm_drift`, never forked.
  * det-Drift is run in ARENA MODE by a thin WRAP (:func:`_det_decide_from_snapshot`) that applies
    :class:`~veridex.strategies.drift.CumulativeDriftStrategy`'s gate order to the shared snapshot.
    The standalone per-tick :meth:`CumulativeDriftStrategy.decide` path is NEVER touched — outside the
    arena the template still decides per-tick, byte-identical (a different, disclosed mode).

The honest report (:class:`ArenaComparisonReport`) carries eligible checkpoints, per-contestant
actions-vs-WAITs, scoreable decisions, fixture count, and a clustered-uncertainty caveat — and, when
the two contestants did NOT actually share their decision opportunities, it DROPS the
identical-opportunity claim (the flag flips) rather than silently averaging.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from veridex.ingest.marketstate import MarketState
from veridex.runtime.llm_checkpoint import (
    AWAITING_CONFIRMATION,
    COMPLETED_FRESH,
    COMPLETED_STALE,
    CONFIRMED_TERMINATED,
    FAILED,
    IN_FLIGHT,
    CheckpointPolicy,
    InflightGuard,
    ServiceOutcome,
)
from veridex.runtime.schemas import AgentAction, SportsActionType
from veridex.strategies.drift_features import (
    DriftFeatureParams,
    DriftFeatureSnapshot,
    drift_features,
)
from veridex.strategies.llm_drift import (
    ModelLauncher,
    _revalidate_action,
    _wait,
    build_drift_decision_prompt,
)
from veridex.strategies.sharp_stats import logit

#: Stable contestant identifiers (folded into every emitted event and the report rows).
DET_DRIFT_CONTESTANT = "det-drift"
LLM_DRIFT_CONTESTANT = "llm-drift"


# ---------------------------------------------------------------------------
# The snapshot-ts clock — deterministic replay time for cadence + evidence-age
# ---------------------------------------------------------------------------


class SnapshotClock:
    """A clock whose reading is the CURRENT tick's snapshot timestamp (deterministic replay time).

    Driving cadence and evidence-age from the replay's own ``ts`` (rather than wall-clock) is what
    makes the arena replay reproducible: the SAME tape always yields the SAME checkpoint grid and the
    SAME staleness decisions for BOTH contestants. Injected into :class:`InflightGuard` exactly where
    production would pass ``time.monotonic``.
    """

    def __init__(self, t0: float = 0.0) -> None:
        self._now = float(t0)

    def __call__(self) -> float:
        return self._now

    def set(self, ts: float) -> None:
        """Pin the clock to this tick's snapshot timestamp (called once per tick by the arena)."""
        self._now = float(ts)


# ---------------------------------------------------------------------------
# The ONE shared projector — projects once per tick, feeds BOTH contestants
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SharedProjection:
    """The single per-tick projection both contestants consume — snapshot + the side it names.

    ``snapshot`` is the exact :class:`DriftFeatureSnapshot` (its ``evidence_hash`` is the shared
    evidence coordinate). ``market_key`` / ``side`` name the strongest-drift side so the det-Drift
    action can carry the same target the standalone template would (UX metadata only).
    """

    snapshot: DriftFeatureSnapshot
    market_key: str
    side: str


class ArenaSharedProjector:
    """Accumulates per-``(market, side)`` logit series and projects the strongest-drift side ONCE.

    The drift MATH is NEVER re-implemented here — it delegates entirely to II-7's shared
    :func:`~veridex.strategies.drift_features.drift_features`. This class only does the bookkeeping the
    pure projector needs (which observations belong to which side, and the first-observation ts) and
    selects the strongest RISING side (ties broken by ``(market_key, side)`` ascending) — mirroring
    :class:`~veridex.strategies.llm_drift.DefaultDriftProjector` so the fed snapshot is exactly what a
    drift contestant would see. ``call_count`` proves it runs ONCE per tick (not once per contestant).
    """

    def __init__(self, *, ewma_slope_alpha: float = 0.2, close_quality_required: bool = True) -> None:
        self._ewma_slope_alpha = ewma_slope_alpha
        self._close_quality_required = close_quality_required
        self._logits: dict[tuple[str, str], list[float]] = {}
        self._ts_first: dict[tuple[str, str], int] = {}
        self.call_count = 0

    def project(self, market_state: MarketState) -> SharedProjection | None:
        """Fold this tick into per-side state and return the strongest-drift projection (or ``None``)."""
        self.call_count += 1
        markets: dict[str, dict[str, Any]] = getattr(market_state, "markets", {}) or {}
        ts = int(getattr(market_state, "ts", 0))
        best: SharedProjection | None = None
        for market_key in sorted(markets):
            market = markets[market_key]
            if self._close_quality_required and market.get("suspended"):
                continue
            prob_bps = market.get("stable_prob_bps", {})
            if not isinstance(prob_bps, dict):
                continue
            for side in sorted(prob_bps):
                try:
                    bps = int(prob_bps[side])
                except (TypeError, ValueError):
                    continue
                key = (market_key, side)
                series = self._logits.setdefault(key, [])
                series.append(logit(bps / 10000.0))
                self._ts_first.setdefault(key, ts)
                snap = drift_features(
                    series, self._ts_first[key], ts, DriftFeatureParams(ewma_slope_alpha=self._ewma_slope_alpha)
                )
                # Strongest RISING cumulative drift wins; ties broken by (market_key, side) ASCENDING
                # (smallest key), MIRRORING standalone det-Drift (drift.py) + DefaultDriftProjector
                # (llm_drift.py) so arena det-Drift targets the SAME tied market as standalone.
                if best is None or (snap.cum_logit_drift, (best.market_key, best.side)) > (
                    best.snapshot.cum_logit_drift, key
                ):
                    best = SharedProjection(snapshot=snap, market_key=market_key, side=side)
        return best


# ---------------------------------------------------------------------------
# det-Drift ARENA WRAP — apply the CumulativeDriftStrategy gate order to the shared snapshot
# ---------------------------------------------------------------------------


@dataclass
class _DetGateConfig:
    """The det-Drift decision thresholds (mirrors ``CumulativeDriftStrategy.__init__`` defaults)."""

    cum_drift_logit_min: float = 0.15
    trend_strength_min: float = 0.5
    min_tick_count: int = 20
    min_horizon_s: int = 600
    cooldown_ticks: int = 5


def _det_decide_from_snapshot(cfg: _DetGateConfig, proj: SharedProjection) -> AgentAction:
    """Apply det-Drift's gate order to the SHARED snapshot — the arena-mode WRAP of the template.

    This mirrors :meth:`~veridex.strategies.drift.CumulativeDriftStrategy._score_side` (drift.py
    lines 118-127) VERBATIM in order and comparison, but reads the ALREADY-PROJECTED shared snapshot
    instead of re-projecting — so det-Drift decides from the IDENTICAL evidence LLM-Drift sees. The
    standalone per-tick path in ``drift.py`` is untouched; this is a separate arena-mode decision.

    Cooldown is enforced by the caller (checkpoint-index based); here we only apply the feature gates.
    """
    snap = proj.snapshot
    # --- gates, cheap -> expensive (same order as CumulativeDriftStrategy._score_side) ---
    if snap.tick_count < cfg.min_tick_count:
        return _wait()  # thin-data guard: not enough observations yet
    if snap.horizon_s < cfg.min_horizon_s:
        return _wait()  # thin-data guard: observation horizon too short
    if snap.cum_logit_drift < cfg.cum_drift_logit_min:
        return _wait()  # proposer follows RISING sides only; drift too small (or falling)
    if snap.tick_count < 2:
        return _wait()  # no per-tick direction yet ⇒ no trend to confirm
    if snap.trend_strength < cfg.trend_strength_min:
        return _wait()  # not a smooth, sustained trend
    return AgentAction(
        type=SportsActionType.FOLLOW_MOMENTUM,
        params={
            "market_key": proj.market_key,
            "side": proj.side,
            # UNTRUSTED UX metadata (gate 1) — never scored by the law:
            "reason": f"cumulative drift +{snap.cum_logit_drift:.3f} logit, sustained trend",
            "claimed_edge_bps": int(round(snap.cum_logit_drift * 100)),
        },
    )


# ---------------------------------------------------------------------------
# Emitted events — the audit trail the report (and the tests) read
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArenaCheckpointEvent:
    """One emitted event in the arena's audit trail (one per contestant per relevant tick).

    ``kind`` is one of ``"opportunity"`` (a checkpoint was OFFERED, evidence present),
    ``"launched"``/``"skipped_inflight"`` (LLM in-flight bookkeeping), or ``"decision"`` (a scored
    action or WAIT was produced). ``checkpoint_ts`` + ``evidence_hash`` are the shared coordinate.
    """

    contestant: str
    kind: str
    checkpoint_ts: int
    evidence_hash: str
    action_type: str | None = None
    market_key: str | None = None
    side: str | None = None


# ---------------------------------------------------------------------------
# The honest comparison report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArenaComparisonReport:
    """The HONEST comparison payload — never a bare average CLV (addendum §3).

    Attributes:
        identical_opportunities: ``True`` only when BOTH contestants were offered the exact same
            ``(checkpoint_ts, evidence_hash)`` sequence; otherwise the claim is DROPPED.
        eligible_checkpoints: The count of shared eligible checkpoints when identical, else ``0``.
        fixture_count: Number of distinct fixtures/windows compared in this run.
        contestants: Per-contestant tallies ``{actions, waits, scoreable_decisions, eligible_checkpoints}``
            — ``actions`` counts EVERY non-WAIT emission; ``scoreable_decisions`` is the AUTHORITATIVE
            subset (only law-valid, targeted non-WAITs), so the two are a real, distinct pair.
        scoreable_decisions: Total AUTHORITATIVE scoreable decisions across both contestants — only
            law-valid non-WAIT actions that name a real ``(market_key, side)`` target (a bare non-WAIT
            with no market/side is NOT scoreable), never a raw ``action_type != WAIT`` count.
        clustered_uncertainty: An honesty caveat — decisions cluster on few checkpoints/markets, so
            CLV samples are correlated, NOT independent (why no bare average is reported).
        shared_checkpoints: The shared ``(checkpoint_ts, evidence_hash)`` sequence when identical, else ``[]``.
        notes: Human-readable disclosures (e.g. the dropped-claim reason).
    """

    identical_opportunities: bool
    eligible_checkpoints: int
    fixture_count: int
    contestants: dict[str, dict[str, int]]
    scoreable_decisions: int
    clustered_uncertainty: dict[str, Any]
    shared_checkpoints: list[tuple[int, str]]
    notes: list[str] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        """Render a JSON-serializable payload (tuples → lists) — the run-report/leaderboard shape."""
        return {
            "identical_opportunities": self.identical_opportunities,
            "eligible_checkpoints": self.eligible_checkpoints,
            "fixture_count": self.fixture_count,
            "contestants": {cid: dict(row) for cid, row in self.contestants.items()},
            "scoreable_decisions": self.scoreable_decisions,
            "clustered_uncertainty": dict(self.clustered_uncertainty),
            "shared_checkpoints": [[ts, h] for (ts, h) in self.shared_checkpoints],
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# The arena driver
# ---------------------------------------------------------------------------


class ArenaComparison:
    """Drives det-Drift AND LLM-Drift at the SAME pinned checkpoints from the SAME shared snapshot.

    One instance per comparison run (stateful across ticks). Each :meth:`step` projects the tape ONCE
    (the shared projector), then offers that identical snapshot to both contestants under their pinned
    checkpoint policies — recording an audit event per contestant. The report is assembled from those
    emitted events, so the fairness properties are provable from the trail, not asserted by fiat.
    """

    def __init__(
        self,
        *,
        model: ModelLauncher,
        det_policy: CheckpointPolicy,
        llm_policy: CheckpointPolicy | None = None,
        projector: ArenaSharedProjector | None = None,
        clock: SnapshotClock | None = None,
        cum_drift_logit_min: float = 0.15,
        ewma_slope_alpha: float = 0.2,
        trend_strength_min: float = 0.5,
        min_tick_count: int = 20,
        min_horizon_s: int = 600,
        close_quality_required: bool = True,
        cooldown_ticks: int = 5,
    ) -> None:
        self._model = model
        self._det_policy = det_policy
        self._llm_policy = llm_policy if llm_policy is not None else det_policy
        self.projector = projector if projector is not None else ArenaSharedProjector(
            ewma_slope_alpha=ewma_slope_alpha, close_quality_required=close_quality_required
        )
        self._clock = clock if clock is not None else SnapshotClock()
        self._det_cfg = _DetGateConfig(
            cum_drift_logit_min=cum_drift_logit_min,
            trend_strength_min=trend_strength_min,
            min_tick_count=min_tick_count,
            min_horizon_s=min_horizon_s,
            cooldown_ticks=cooldown_ticks,
        )

        # det-Drift arena-mode checkpoint state (its own grid + cooldown clock).
        self._det_last_cp_at: float | None = None
        self._det_last_snapshot: DriftFeatureSnapshot | None = None
        self._det_cp_index = -1
        self._det_last_fire_index = -(10**9)

        # LLM-Drift checkpoint state — the exact §3 one-in-flight guard (VERBATIM, not re-implemented).
        self._guard = InflightGuard(evidence_age_limit_s=self._llm_policy.evidence_age_limit_s, clock=self._clock)
        self._llm_last_cp_at: float | None = None
        self._llm_last_snapshot: DriftFeatureSnapshot | None = None

        self.events: list[ArenaCheckpointEvent] = []
        self._fixture_ids: set[int] = set()

    # --- per-tick drive ----------------------------------------------------

    async def step(self, market_state: MarketState) -> None:
        """Advance BOTH contestants one tick from ONE shared projection (records audit events)."""
        ts = int(getattr(market_state, "ts", 0))
        self._clock.set(ts)
        self._fixture_ids.add(int(getattr(market_state, "fixture_id", 0)))

        proj = self.projector.project(market_state)  # THE ONE shared projection this tick
        snapshot = proj.snapshot if proj is not None else None

        self._step_det(ts, proj, snapshot)
        self._step_llm(ts, proj, snapshot)

    def _step_det(self, ts: int, proj: SharedProjection | None, snapshot: DriftFeatureSnapshot | None) -> None:
        is_cp = self._det_policy.is_checkpoint(
            now=ts,
            last_checkpoint_at=self._det_last_cp_at,
            snapshot=snapshot,
            last_snapshot=self._det_last_snapshot,
        )
        self._det_last_snapshot = snapshot
        if not is_cp:
            return
        # A checkpoint OPENED — the grid advances even when there is no snapshot yet (thin data).
        self._det_last_cp_at = ts
        if proj is None or snapshot is None:
            return
        self._det_cp_index += 1
        self.events.append(
            ArenaCheckpointEvent(
                contestant=DET_DRIFT_CONTESTANT, kind="opportunity", checkpoint_ts=ts,
                evidence_hash=snapshot.evidence_hash,
            )
        )
        action = _det_decide_from_snapshot(self._det_cfg, proj)
        # Cooldown gate (checkpoint-index based — the arena-mode analogue of the per-tick cooldown).
        if action.type != SportsActionType.WAIT and (
            self._det_cp_index - self._det_last_fire_index <= self._det_cfg.cooldown_ticks
        ):
            action = _wait()
        if action.type != SportsActionType.WAIT:
            self._det_last_fire_index = self._det_cp_index
        self.events.append(
            ArenaCheckpointEvent(
                contestant=DET_DRIFT_CONTESTANT, kind="decision", checkpoint_ts=ts,
                evidence_hash=snapshot.evidence_hash, action_type=action.type.value,
                market_key=action.params.get("market_key"), side=action.params.get("side"),
            )
        )

    def _step_llm(self, ts: int, proj: SharedProjection | None, snapshot: DriftFeatureSnapshot | None) -> None:
        is_cp = self._llm_policy.is_checkpoint(
            now=ts,
            last_checkpoint_at=self._llm_last_cp_at,
            snapshot=snapshot,
            last_snapshot=self._llm_last_snapshot,
        )
        self._llm_last_snapshot = snapshot
        if is_cp:
            self._llm_last_cp_at = ts
            if snapshot is not None:
                self.events.append(
                    ArenaCheckpointEvent(
                        contestant=LLM_DRIFT_CONTESTANT, kind="opportunity", checkpoint_ts=ts,
                        evidence_hash=snapshot.evidence_hash,
                    )
                )

        # SERVICE the in-flight call first (accept-fresh / drop-stale / cancel-on-expiry / fail-closed),
        # then LAUNCH only if the slot is free AND this tick opens a checkpoint (the §3 order).
        outcome = self._guard.service()
        if outcome.status == COMPLETED_FRESH:
            self._emit_llm_decision(ts, outcome)
            return
        if outcome.status in (IN_FLIGHT, AWAITING_CONFIRMATION):
            if is_cp and snapshot is not None:
                self.events.append(
                    ArenaCheckpointEvent(
                        contestant=LLM_DRIFT_CONTESTANT, kind="skipped_inflight",
                        checkpoint_ts=ts, evidence_hash=self._guard.evidence_coordinate() or "",
                    )
                )
            return
        if outcome.status in (COMPLETED_STALE, FAILED, CONFIRMED_TERMINATED):
            return  # terminal-this-tick: the slot is free but the resolving tick is consumed
        # IDLE and nothing resolved → launch iff this tick opens a checkpoint and we have evidence.
        if is_cp and snapshot is not None:
            prompt = build_drift_decision_prompt(snapshot)  # hashed pre-call (snapshot.evidence_hash)
            try:
                handle = self._model.launch(prompt)
            except Exception:
                return  # fail-closed: a launch failure must not kill the loop
            self._guard.launch(handle, evidence_hash=snapshot.evidence_hash)
            self.events.append(
                ArenaCheckpointEvent(
                    contestant=LLM_DRIFT_CONTESTANT, kind="launched",
                    checkpoint_ts=ts, evidence_hash=snapshot.evidence_hash,
                )
            )

    def _emit_llm_decision(self, ts: int, outcome: ServiceOutcome) -> None:
        """Emit the ONE scored LLM decision for a freshly-completed call (typed-revalidated, fail-closed).

        Shared by the per-tick service path (:meth:`_step_llm`) and the end-of-tape :meth:`drain`, so a
        call that completes fresh ON the final checkpoint is emitted with the SAME typed-revalidation
        and fail-closed-WAIT discipline as one that completes mid-tape.
        """
        try:
            action = _revalidate_action(outcome.raw)
        except Exception:
            action = _wait()  # fail-closed: malformed / over-powered output → WAIT
        self.events.append(
            ArenaCheckpointEvent(
                contestant=LLM_DRIFT_CONTESTANT, kind="decision",
                checkpoint_ts=ts, evidence_hash=outcome.evidence_hash or "",
                action_type=action.type.value,
                market_key=action.params.get("market_key"), side=action.params.get("side"),
            )
        )

    @property
    def guard_busy(self) -> bool:
        """Whether an LLM physical call still occupies the single in-flight slot (drain check)."""
        return self._guard.busy

    async def drain(self, *, max_yields: int = 256) -> None:
        """End-of-tape: settle the final in-flight LLM call to an AUDITED terminal outcome.

        A production launcher backs each model call with an ``asyncio.Task`` that advances ONLY when
        the event loop runs. The replay loop yields once per tick so an in-flight call is serviced on a
        following tick, but a call LAUNCHED on the FINAL checkpoint has no following tick. This gives
        that last call a bounded chance to complete under the (frozen snapshot-ts) clock — emitting its
        decision if it lands fresh — and otherwise deterministically CANCELS it via the guard's OWN
        expiry machinery and confirms termination, so every physical call reaches an audited terminal
        outcome (completed-fresh / completed-stale / failed / confirmed-terminated) rather than being
        left silently in flight. One-in-flight + expiry-confirmation are preserved (the guard is the
        SAME §3 machine); no new projection is taken (the tape is exhausted).
        """
        if not self._guard.busy:
            return
        ts = int(self._clock())
        # Phase 1 — let a launched-but-unfinished task finish under the snapshot-ts clock.
        for _ in range(max_yields):
            await asyncio.sleep(0)
            outcome = self._guard.service()
            if outcome.status == COMPLETED_FRESH:
                self._emit_llm_decision(ts, outcome)
                return
            if outcome.status in (COMPLETED_STALE, FAILED, CONFIRMED_TERMINATED):
                return  # terminal, non-fresh: audited, nothing to emit
            if not self._guard.busy:
                return
        # Phase 2 — still in flight after the bounded window: force expiry via the guard's own cancel
        # path (advance the clock past the pinned evidence-age limit), then confirm termination.
        self._clock.set(float(ts) + self._llm_policy.evidence_age_limit_s + 1.0)
        for _ in range(max_yields):
            outcome = self._guard.service()
            if outcome.status in (CONFIRMED_TERMINATED, COMPLETED_STALE, FAILED) or not self._guard.busy:
                return
            await asyncio.sleep(0)

    # --- audit-trail readers ----------------------------------------------

    def opportunities(self, contestant: str) -> list[tuple[int, str]]:
        """The ``(checkpoint_ts, evidence_hash)`` sequence this contestant was OFFERED (from events)."""
        return [
            (e.checkpoint_ts, e.evidence_hash)
            for e in self.events
            if e.contestant == contestant and e.kind == "opportunity"
        ]

    @staticmethod
    def _is_non_wait(e: ArenaCheckpointEvent) -> bool:
        """A decision event that emitted a NON-WAIT action (regardless of whether it is scoreable)."""
        return e.kind == "decision" and e.action_type != SportsActionType.WAIT.value

    @staticmethod
    def _is_scoreable(e: ArenaCheckpointEvent) -> bool:
        """Whether a decision event is a SCOREABLE decision — AUTHORITATIVE, not a bare non-WAIT count.

        A non-WAIT action is scoreable ONLY when it names a valid ``(market_key, side)`` target the law
        can actually score against a closing snapshot. A schema-valid but law-invalid non-WAIT (empty
        params / missing market or side) is NOT scoreable — counting it would overstate a contestant's
        usable actions and collapse the required actions-vs-scoreable distinction (addendum §3).
        """
        return (
            e.kind == "decision"
            and e.action_type != SportsActionType.WAIT.value
            and bool(e.market_key)
            and bool(e.side)
        )

    def _tallies(self, contestant: str) -> dict[str, int]:
        eligible = len(self.opportunities(contestant))
        actions = sum(1 for e in self.events if e.contestant == contestant and self._is_non_wait(e))
        scoreable = sum(1 for e in self.events if e.contestant == contestant and self._is_scoreable(e))
        return {
            "actions": actions,  # every non-WAIT emission (may include law-invalid ones)
            "waits": max(eligible - actions, 0),
            "scoreable_decisions": scoreable,  # AUTHORITATIVE: only law-valid, targeted non-WAITs
            "eligible_checkpoints": eligible,
        }

    def report(self) -> ArenaComparisonReport:
        """Assemble the HONEST comparison report from the emitted audit trail."""
        det_opps = self.opportunities(DET_DRIFT_CONTESTANT)
        llm_opps = self.opportunities(LLM_DRIFT_CONTESTANT)
        identical = det_opps == llm_opps and bool(det_opps)

        det_tally = self._tallies(DET_DRIFT_CONTESTANT)
        llm_tally = self._tallies(LLM_DRIFT_CONTESTANT)
        contestants = {DET_DRIFT_CONTESTANT: det_tally, LLM_DRIFT_CONTESTANT: llm_tally}
        scoreable = det_tally["scoreable_decisions"] + llm_tally["scoreable_decisions"]

        # Clustered-uncertainty honesty caveat: decisions cluster on few checkpoints/markets, so the
        # CLV samples are CORRELATED — an average across them is not an independent-sample mean. Scoped
        # to AUTHORITATIVE scoreable decisions (law-valid targets), consistent with ``scoreable`` above.
        distinct_markets = {e.market_key for e in self.events if self._is_scoreable(e)}
        distinct_checkpoints = {e.checkpoint_ts for e in self.events if self._is_scoreable(e)}
        clustered_uncertainty = {
            "scoreable_decisions": scoreable,
            "distinct_markets": len(distinct_markets),
            "distinct_checkpoints": len(distinct_checkpoints),
            "note": (
                "CLV decisions cluster on few checkpoints/markets; treat as correlated samples, not "
                "independent draws — NO bare average CLV is reported as a headline."
            ),
        }

        notes: list[str] = []
        if not identical:
            notes.append(
                "identical-opportunity claim DROPPED: contestants did not share the same "
                "(checkpoint_ts, evidence_hash) sequence — comparison is per-contestant, never averaged."
            )

        return ArenaComparisonReport(
            identical_opportunities=identical,
            eligible_checkpoints=len(det_opps) if identical else 0,
            fixture_count=len(self._fixture_ids),
            contestants=contestants,
            scoreable_decisions=scoreable,
            clustered_uncertainty=clustered_uncertainty,
            shared_checkpoints=list(det_opps) if identical else [],
            notes=notes,
        )


# ---------------------------------------------------------------------------
# Convenience runner
# ---------------------------------------------------------------------------


async def run_arena_comparison(
    marketstates: list[MarketState],
    *,
    model: ModelLauncher,
    det_policy: CheckpointPolicy,
    llm_policy: CheckpointPolicy | None = None,
    projector: ArenaSharedProjector | None = None,
    clock: SnapshotClock | None = None,
    **det_kwargs: Any,
) -> ArenaComparison:
    """Drive a full arena comparison over a tape and return the completed :class:`ArenaComparison`.

    Args:
        marketstates: The ordered replay tape (identical inputs for both contestants).
        model: The injectable LLM launcher seam (a fake in tests; the Agno launcher in production).
        det_policy: The pinned checkpoint policy for det-Drift.
        llm_policy: The pinned checkpoint policy for LLM-Drift; defaults to ``det_policy`` (the fair,
            identical-grid case). A DIFFERENT policy models contestants that do NOT share opportunities.
        projector: The shared projector; a fresh :class:`ArenaSharedProjector` when ``None``.
        clock: The snapshot-ts clock; a fresh :class:`SnapshotClock` when ``None``.
        **det_kwargs: det-Drift gate thresholds (mirrors ``CumulativeDriftStrategy`` kwargs).

    Returns:
        The completed :class:`ArenaComparison` (call :meth:`ArenaComparison.report` for the payload).
    """
    arena = ArenaComparison(
        model=model, det_policy=det_policy, llm_policy=llm_policy, projector=projector, clock=clock, **det_kwargs
    )
    for market_state in marketstates:
        await arena.step(market_state)
        # Yield so a production launcher's asyncio.Task can progress before the next tick's service —
        # a call launched this tick can then complete/settle under the snapshot-ts clock (not left in
        # flight because the replay loop never released the event loop).
        await asyncio.sleep(0)
    # Deterministically settle/cancel the final in-flight call so every physical call is audited.
    await arena.drain()
    return arena


ProjectorFn = Callable[[MarketState], SharedProjection | None]
