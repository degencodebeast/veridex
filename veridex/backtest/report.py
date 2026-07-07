"""T15 — BacktestReport: an honest, derived-only report over a sealed RunResult (REQ-2D-303/304).

TRUST-ADJACENT, NOT a trust input. The report is a PURE FUNCTION of a
:class:`~veridex.runtime.orchestrator.RunResult` + its ``score_rows`` (plus config the caller
already chose: the window, the policy envelope, the mode). It re-reads NO venue, NO live feed, NO
LLM — so it can never smuggle a fresh trust claim past the seal (SEC-003: the report is *derived*
evidence, never the evidence itself). No LLM SDK is imported on this path (CON-007 parity).

Two honesty invariants pinned here:

  * **Mode labels never lie (REQ-2D-304).** :func:`mode_ladder_label` is a TOTAL function over the
    ``source_mode × execution_mode`` ladder; an unmapped pair raises rather than guessing. A backtest
    (``replay × paper``) resolves to ``"Backtest"`` and can NEVER read as ``"Live"``/``"Live Guarded"``.
  * **The small-sample flag is additive (AC-2D-302).** ``clv_confidence`` (single-sourced from
    :mod:`veridex.clv_confidence`) FLAGS a small sample; it never reorders the leaderboard or mutates
    a mean. ``report.leaderboard`` is byte-identical to :func:`veridex.scoring.score_run`.

The fake/paper venue (Codex M2) means there is NO real executable edge on this path:
``real_executable_edge_bps`` is ALWAYS ``None`` here — an explicit null, never a fabricated number.
"""

from __future__ import annotations

import hashlib
from typing import Any

from pydantic import BaseModel

from veridex.clv_confidence import clv_confidence
from veridex.policy.envelope import PolicyEnvelope
from veridex.runtime.evidence import serialize_payload
from veridex.runtime.orchestrator import RunResult
from veridex.runtime.window import RunWindow
from veridex.scoring import is_scored, score_run

# --- Mode ladder (REQ-2D-304) ------------------------------------------------
# source_mode × execution_mode → the ONE honest user-facing label. execution_mode ``None`` means
# "replay only, no execution lane"; "paper"/"dry_run"/"live_guarded" mirror
# veridex.competition.models.ExecutionMode. The SAME "paper" execution reads as "Backtest" on a
# replay source and "Live Paper" on a live source — the source disambiguates, so no label overloads.
_MODE_LADDER: dict[tuple[str, str | None], str] = {
    ("replay", None): "Replay",
    ("replay", "paper"): "Backtest",  # replay + scoring + report
    ("live", "paper"): "Live Paper",
    ("live", "dry_run"): "Dry Run",
    ("live", "live_guarded"): "Live Guarded",
}

#: Backtest execution mode: a fake/paper venue (Codex M2) — never real, executable capital.
BACKTEST_EXECUTION_MODE = "paper"

#: The fixed edge thresholds (bps) the threshold-sensitivity sweep reports over. Ascending; the
#: first rung (0) is the NON-NEGATIVE (>=0 bps) edge rung — NOT the full scored set: it already
#: drops negative-CLV picks, so it diverges from the headline avg_clv over EVERY scored pick
#: (count == clv_distribution.count; distinct from sample_size = total decisions) when losers exist.
#: Higher rungs then show how quickly coverage/CLV thin out as the min-edge bar rises.
_THRESHOLD_LADDER_BPS: tuple[int, ...] = (0, 25, 50, 100, 200)


def mode_ladder_label(source_mode: str, execution_mode: str | None) -> str:
    """Resolve the ONE honest user-facing label for a ``source_mode × execution_mode`` pair.

    Total by construction: an unmapped pair raises :class:`ValueError` so a run can never be
    mislabelled by silent fall-through (a replay must never read as "Live", a dry run never as an
    executed order).

    Args:
        source_mode: ``"replay"`` or ``"live"``.
        execution_mode: ``None`` (no execution lane) or ``"paper"``/``"dry_run"``/``"live_guarded"``.

    Returns:
        The user-facing mode label (e.g. ``"Backtest"``).

    Raises:
        ValueError: If ``(source_mode, execution_mode)`` is not a defined rung of the ladder.
    """
    key = (source_mode, execution_mode)
    if key not in _MODE_LADDER:
        raise ValueError(f"no honest mode label for source_mode={source_mode!r} execution_mode={execution_mode!r}")
    return _MODE_LADDER[key]


class BacktestAssumptions(BaseModel):
    """The EXPLICIT assumptions a BacktestReport is computed under (never implied).

    Attributes:
        slippage_bps: Assumed execution slippage — ``0`` on the paper/replay path (no fills).
        costs_bps: Assumed round-trip costs/fees — ``0`` on the paper/replay path.
        quote_freshness_s: Max quote age assumed; ``None`` on replay (ticks are recorded evidence,
            not live quotes with a staleness budget).
        execution_mode: The venue execution mode these assumptions describe (``"paper"``).
    """

    slippage_bps: int
    costs_bps: int
    quote_freshness_s: int | None
    execution_mode: str


class ClvDistribution(BaseModel):
    """The distribution of scored (true-CLV) values behind ``avg_clv`` — reported, never hidden.

    Attributes:
        count: Number of scored CLV observations.
        min: Smallest scored CLV in bps (``None`` when empty).
        max: Largest scored CLV in bps (``None`` when empty).
        mean: Mean scored CLV in bps (``None`` when empty) — equals ``avg_clv``.
        values: The scored CLV values in ascending order (deterministic).
    """

    count: int
    min: int | None
    max: int | None
    mean: float | None
    values: list[int]


class ThresholdSensitivityPoint(BaseModel):
    """One rung of the min-edge sensitivity sweep: coverage + realized CLV surviving a threshold.

    Attributes:
        threshold_bps: The minimum recomputed edge (bps) required to keep a scored action.
        scored_count: How many scored actions clear ``threshold_bps``.
        avg_clv: Mean realized CLV (bps) of the surviving actions (``None`` when none survive).
    """

    threshold_bps: int
    scored_count: int
    avg_clv: float | None


class BacktestReport(BaseModel):
    """The honest, derived-only report for one backtest run (§4.4 field set — all present).

    Every field is a pure projection of a sealed ``RunResult`` + ``score_rows`` and the caller's
    chosen config. ``generated_ts`` is the ONLY wall-clock field (excluded from determinism).
    """

    # --- lineage (tamper-evident) -------------------------------------------
    pack_id: str
    content_hash: str
    run_id: str
    evidence_hash: str

    # --- config -------------------------------------------------------------
    window_id: str
    config_hash: str
    market_universe: list[str]
    source_mode: str
    execution_mode: str
    mode_label: str

    # --- sample size + CLV confidence (AC-2D-302) ---------------------------
    #: TOTAL decisions evaluated (WAIT-inclusive) — the ``law_valid_rate`` denominator, NOT the
    #: scored-pick count (that is ``clv_distribution.count``). ``clv_confidence`` is keyed off the
    #: SCORED count, so ``sample_size`` and ``clv_confidence`` describe DIFFERENT populations: a
    #: surface must not render ``sample_size`` next to ``clv_confidence`` as if it were the tier's basis.
    sample_size: int
    #: LAW-acceptance count (``valid`` is True, INCLUDING valid WAIT abstentions) — feeds ``law_valid_rate``.
    valid_count: int
    #: CLV confidence tier — keyed off the SCORED-pick count (``clv_distribution.count``), NOT ``sample_size``.
    clv_confidence: str
    low_sample_warning: str | None

    # --- CLV + honest PnL proxy ---------------------------------------------
    avg_clv: float | None
    clv_distribution: ClvDistribution
    sim_pnl: int | None
    #: Real venue-executable edge — ALWAYS None on the paper/replay path (fake venue, Codex M2).
    real_executable_edge_bps: int | None
    #: ESTIMATED venue-executable edge (bps) — a VENUE-DERIVED, EXPLANATORY quantity. DISTINCT from
    #: ``real_executable_edge_bps`` (which needs a live fill and stays None): this is what the
    #: de-margined fair probability WOULD have earned at a recorded/backfilled venue price. It is
    #: attached ONLY AFTER the pure venue-free build (via ``model_copy``); ``build_backtest_report``
    #: NEVER sets it, so the builder stays venue-blind. NEVER a ranked axis (CLV alone ranks — SEC-005).
    estimated_executable_edge_bps: int | None = None
    #: Machine-readable evidence rung the estimated edge was priced from (e.g.
    #: ``"backfilled-price-history"`` / ``"recorded-live-quote"`` — a ``veridex.provenance.EvidenceRung``
    #: value). ``None`` until a producer attaches an estimated edge.
    estimated_edge_rung: str | None = None
    #: The EXPLICIT assumptions the estimated edge was computed under (e.g. ``no_interpolation``).
    #: ``None`` until a producer attaches an estimated edge.
    estimated_edge_assumptions: dict[str, Any] | None = None

    # --- sensitivity + operational rates ------------------------------------
    threshold_sensitivity: list[ThresholdSensitivityPoint]
    stale_rejected_quote_rate: float
    #: PASS fraction of the operator POLICY ENVELOPE (§4.4). ``None`` until a real policy-envelope
    #: evaluation actually backs it — the backtest lane does NOT yet run the execution envelope
    #: (that wiring arrives in M4/T17), so this is honestly null here rather than a law-validity
    #: number wearing a policy name. LAW-acceptance lives under ``law_valid_rate`` instead.
    policy_pass_fail_rate: float | None
    #: LAW-acceptance pass fraction (valid decisions / total decisions) — the honest name for what
    #: the replay law actually verifies. Distinct from the (policy-envelope) ``policy_pass_fail_rate``.
    law_valid_rate: float

    #: REPORT-ONLY odds input-proof stamp ("verify-before-seal", input-proof axis): a summary of
    #: whether TxLINE returned a valid two-tier Merkle inclusion proof for each odds message that fed
    #: this run (see :mod:`veridex.ingest.odds_proof`). Attached POST-BUILD via ``model_copy``
    #: (:func:`veridex.ingest.odds_proof.attach_odds_proof_status`); ``build_backtest_report`` NEVER
    #: sets it and it is NOT bound into ``config_hash`` / ``evidence_hash`` — adding it CANNOT perturb
    #: an existing sealed hash. HONEST claim only: "TxLINE returned valid inclusion proofs for N/M" —
    #: NOT independently verified against the on-chain root (a future Tier-2 recompute). ``None`` until
    #: a producer attaches it. NEVER a ranked axis (SEC-005).
    odds_proof_status: dict[str, Any] | None = None

    # --- close provenance / honest degrade marker (D2) ----------------------
    #: NON-sealed, human-readable provenance for the pre_match close (the report analog of the live
    #: runner's ``ops`` markers). ``None`` for a clean full-match ``pre_match`` (verified kickoff +
    #: complete per-market CON-040 close). Non-None either (a) NOTES a pre-match-only pack (no verified
    #: kickoff — still true CLV) or (b) NAMES a fail-closed DEGRADE (all-in-running / incomplete close),
    #: in which case no row carries true ``clv_bps`` (the run finalized on window CLV) and ``avg_clv`` is
    #: ``None``. Never part of the sealed evidence — a derived, explanatory marker only.
    closing_note: str | None = None

    # --- explicit assumptions + the untouched score stack -------------------
    assumptions: BacktestAssumptions
    leaderboard: list[dict[str, Any]]

    # --- wall-clock (excluded from determinism comparisons) -----------------
    generated_ts: int


def _scored_clv_values(score_rows: list[dict[str, Any]]) -> list[int]:
    """The scored TRUE-CLV values (bps), ascending. Window CLV is deliberately excluded — only a
    ``pre_match`` reconstructed close yields true CLV, and window CLV is named distinctly elsewhere
    (DEC-2D-1); relabelling it as ``avg_clv`` would be a lie."""
    return sorted(row["clv_bps"] for row in score_rows if is_scored(row))


def _threshold_sensitivity(scored_clv: list[int]) -> list[ThresholdSensitivityPoint]:
    """Sweep the fixed min-edge ladder. Phase 1 has ``edge_bps == clv_bps`` (no independent fair
    value), so thresholding on edge is thresholding on the scored CLV itself: each rung reports how
    many picks cleared the bar (``clv >= threshold``) and the mean CLV they realized. Rung 0 is the
    NON-NEGATIVE (>=0 bps) rung — it already excludes losers, so it is deliberately NOT the headline
    ``avg_clv``, which covers every scored pick (winners and losers alike; that scored-pick count is
    ``clv_distribution.count``). ``sample_size`` is a DIFFERENT quantity — total decisions evaluated
    (WAIT-inclusive), the ``law_valid_rate`` denominator, NOT the scored-pick count."""
    points: list[ThresholdSensitivityPoint] = []
    for threshold in _THRESHOLD_LADDER_BPS:
        survivors = [clv for clv in scored_clv if clv >= threshold]
        avg = (sum(survivors) / len(survivors)) if survivors else None
        points.append(ThresholdSensitivityPoint(threshold_bps=threshold, scored_count=len(survivors), avg_clv=avg))
    return points


def _stale_rejected_quote_rate(score_rows: list[dict[str, Any]]) -> float:
    """Fraction of decisions rejected for a STALE quote. Derived purely from law ``reason`` codes;
    the replay law emits no staleness reason, so a pure replay honestly reports ``0.0``."""
    if not score_rows:
        return 0.0
    stale = sum(1 for row in score_rows if str(row.get("reason", "")).startswith("stale"))
    return stale / len(score_rows)


def _config_hash(
    run: RunResult,
    *,
    window: RunWindow,
    source_mode: str,
    execution_mode: str,
    policy_envelope: PolicyEnvelope | None,
    assumptions: BacktestAssumptions,
) -> str:
    """Deterministic SHA-256 over the backtest CONFIGURATION (reproducibility fingerprint).

    Binds the window, the market universe, the mode, the policy commitment, the agents' pinned
    config hashes (read out of ``score_rows`` — the run's own evidence), and the assumptions. Uses
    the ONE canonical serializer so it is byte-stable across processes, exactly like the rest of the
    evidence/prescore chain.
    """
    agent_config_hashes = sorted(
        {row.get("raw_prescore", {}).get("model_prompt_config_hash", "") for row in run.score_rows}
    )
    payload = {
        "window": window.model_dump(),
        "market_universe": window.market_allowlist,
        "source_mode": source_mode,
        "execution_mode": execution_mode,
        "policy_hash": policy_envelope.policy_hash() if policy_envelope is not None else None,
        "agent_config_hashes": agent_config_hashes,
        "assumptions": assumptions.model_dump(),
    }
    return hashlib.sha256(serialize_payload(payload).encode("utf-8")).hexdigest()


def build_backtest_report(
    run: RunResult,
    *,
    window: RunWindow,
    pack_id: str,
    content_hash: str,
    source_mode: str,
    execution_mode: str = BACKTEST_EXECUTION_MODE,
    policy_envelope: PolicyEnvelope | None = None,
    assumptions: BacktestAssumptions | None = None,
    generated_ts: int | None = None,
) -> BacktestReport:
    """Derive a :class:`BacktestReport` PURELY from a sealed ``RunResult`` + config — no new inputs.

    Reads ONLY ``run`` (its ``score_rows`` / ``evidence_hash`` / ``run_id``) plus the caller's
    already-chosen config (window, mode, policy, lineage). No venue, no live feed, no LLM. The
    small-sample flag is additive: ``leaderboard`` is the UNTOUCHED :func:`score_run` stack.

    Args:
        run: The sealed run to report on.
        window: The coverage window whose id + allowlist frame the report.
        pack_id: The source pack's identifier (tamper-evident lineage).
        content_hash: The source pack's content hash (tamper-evident lineage).
        source_mode: ``"replay"`` for a backtest (carried onto the mode label).
        execution_mode: Venue execution mode — defaults to the paper backtest venue.
        policy_envelope: Optional operator envelope folded into ``config_hash`` (never re-evaluated
            against a live venue here).
        assumptions: Optional explicit assumptions block; defaults to the paper/replay assumptions.
        generated_ts: Optional wall-clock stamp (injected by the runner; the ONLY non-deterministic
            field). Defaults to ``0`` so a bare call stays deterministic.

    Returns:
        The fully-populated, honest :class:`BacktestReport`.
    """
    resolved_assumptions = assumptions or BacktestAssumptions(
        slippage_bps=0, costs_bps=0, quote_freshness_s=None, execution_mode=execution_mode
    )

    score_rows = run.score_rows
    sample_size = len(score_rows)
    # valid_count is LAW-ACCEPTANCE (valid is True), INCLUDING valid WAIT abstentions. It feeds
    # law_valid_rate below — a legitimate, DISTINCT metric — but it is NOT the CLV confidence source.
    valid_count = sum(1 for row in score_rows if row.get("valid") is True)

    scored_clv = _scored_clv_values(score_rows)
    # CLV confidence keys off the SCORED sample (actual picks carrying a real CLV), NEVER valid_count:
    # a run can be law-valid on thousands of WAIT abstentions yet score ZERO picks, and a zero-scored
    # run MUST read "low" confidence, never "high" (honesty — the tier reflects CLV coverage, not
    # acceptance). Keying off valid_count here was an overclaim; the scored count is the honest source.
    confidence = clv_confidence(len(scored_clv))
    avg_clv = (sum(scored_clv) / len(scored_clv)) if scored_clv else None
    distribution = ClvDistribution(
        count=len(scored_clv),
        min=min(scored_clv) if scored_clv else None,
        max=max(scored_clv) if scored_clv else None,
        mean=avg_clv,
        values=scored_clv,
    )

    low_sample_warning = (
        f"Only {len(scored_clv)} scored picks (< 10): CLV confidence is LOW — "
        "treat the ranking as indicative, not conclusive."
        if confidence["low_sample"]
        else None
    )
    # law_valid_rate is the LAW-acceptance PASS rate over all decisions (valid / total). The
    # POLICY-envelope pass/fail rate stays None until a real envelope evaluation backs it (M4/T17) —
    # the backtest lane does not run the execution policy envelope, so naming law-validity as a
    # "policy" rate would overclaim (Codex M3). policy_envelope here only feeds config_hash.
    law_valid_rate = (valid_count / sample_size) if sample_size else 0.0

    return BacktestReport(
        pack_id=pack_id,
        content_hash=content_hash,
        run_id=run.run_id,
        evidence_hash=run.evidence_hash,
        window_id=window.window_id,
        config_hash=_config_hash(
            run,
            window=window,
            source_mode=source_mode,
            execution_mode=execution_mode,
            policy_envelope=policy_envelope,
            assumptions=resolved_assumptions,
        ),
        market_universe=window.market_allowlist,
        source_mode=source_mode,
        execution_mode=execution_mode,
        mode_label=mode_ladder_label(source_mode, execution_mode),
        sample_size=sample_size,
        valid_count=valid_count,
        clv_confidence=confidence["clv_confidence"],
        low_sample_warning=low_sample_warning,
        avg_clv=avg_clv,
        clv_distribution=distribution,
        # sim_pnl is the closing-referenced flat-stake CLV PROXY (honest as a proxy, not a real fill);
        # null when nothing scored. NOT real executable edge — that stays None on the paper venue.
        sim_pnl=sum(scored_clv) if scored_clv else None,
        real_executable_edge_bps=None,
        threshold_sensitivity=_threshold_sensitivity(scored_clv),
        stale_rejected_quote_rate=_stale_rejected_quote_rate(score_rows),
        policy_pass_fail_rate=None,  # no real policy-envelope evaluation on this path yet (M4/T17)
        law_valid_rate=law_valid_rate,
        assumptions=resolved_assumptions,
        leaderboard=score_run(run),
        generated_ts=generated_ts if generated_ts is not None else 0,
    )
