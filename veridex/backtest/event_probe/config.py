"""E5 sealed ``ProbeConfig`` + config hash + VOID-on-drift + result serializer.

``ProbeConfig`` is the SINGLE, frozen, predeclared superset of every threshold the
probe runs against (CON-002..006/009/010/016). It is the source of truth: E3's
``WindowConfig`` and E4's ``AggConfig`` are rebuilt FROM it (``to_window_config`` /
``to_agg_config``), so the sealed config drives the whole pipeline and there is no
second copy of a default to drift out of sync.

The seal follows the Run-001/Run-002 predeclared pattern (PAT-001):

* ``config_hash()`` -- sha256 over the canonical (sorted-key, compact) JSON dump of
  every field, mirroring ``MarketQualityConfig.filter_config_hash`` exactly (a local
  :func:`_canonical_dump` byte-identical to ``serialize_payload``, inlined so this
  rung-1 package imports nothing from the trust core). Changing ANY threshold after
  results are observed changes this hash -- the CON-014 anti-drift guarantee.
* ``verify_pinned(cfg, expected_hash)`` -- raises ``ProbeVoidError`` when the live
  hash diverges from the committed stamp, and is a pure comparison (no I/O), so the
  E6 runner can VOID BEFORE touching any pack/scores file (PAT-001 / AC-007).
* ``build_sealed_result(...)`` -- serializes the sealed artifact (§4): the pinned
  config + its hash, the verdict + global stats + per-slice, the full per-event
  ``event_records[]`` audit trail (GUD-001), and both tally maps. It builds an
  in-memory dict only -- writing the artifact is the operator-gated E6 ``--seal``
  step, never done here.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict

from veridex.backtest.event_probe.aggregate import AggConfig, ProbeResult
from veridex.backtest.event_probe.compute import EventRecord, WindowConfig
from veridex.strategies.market_quality import DEFAULT_MARKET_QUALITY_CONFIG


def _canonical_dump(payload: Any) -> str:
    """Canonical JSON: sorted keys, compact separators.

    BYTE-IDENTICAL to ``veridex.runtime.evidence.serialize_payload`` -- inlined
    HERE, not imported, so the rung-1 ``event_probe`` package imports NOTHING from
    the trust core (``veridex.runtime.evidence``; CON-012 trust boundary). Because
    it reproduces the exact same dump, ``config_hash()`` is unchanged and stays
    canonicalization-parity with ``MarketQualityConfig.filter_config_hash``.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))

#: Sealed protocol identity (spec §4). Bound alongside the config hash so a result
#: cannot be relabelled under a different protocol without a new stamp (CON-014).
PROTOCOL_ID = "event-fork-probe-v1"

#: The CON-008 observability-floor FAILURE reasons: an event carrying one of these
#: never resolved its windows / had too few states, so it did NOT pass the floor.
#: below_epsilon is NOT here -- a below-epsilon event IS floor-eligible (it resolved
#: its windows; its settled move was merely too small to classify directionally).
_FLOOR_FAIL_REASONS: frozenset[str] = frozenset(
    {"no_pre_tick", "no_imm_tick_60s", "no_settle_tick", "insufficient_odds_states"}
)

#: The §4 CON-014 defaults note carried on every sealed artifact -- states plainly
#: that the pinned thresholds are v1 predeclared, NOT optimized, and that post-hoc
#: tuning is a protocol violation.
PREDECLARED_DEFAULTS_NOTE = (
    "v1 defaults, not optimized; post-hoc threshold changes forbidden (CON-014)."
)


class ProbeVoidError(Exception):
    """Raised when the live config hash diverges from the pinned stamp (PAT-001).

    The E6 runner raises this BEFORE any pack/scores I/O so a drifted config never
    produces a reportable result (CON-014 / AC-007).
    """


class ProbeConfig(BaseModel):
    """The single, frozen, predeclared superset of every probe threshold.

    Defaults are the v1 pinned CON values, NOT optimized (CON-014): changing any of
    them changes ``config_hash()``, which the runner VOIDs on. Frozen so a run
    cannot silently mutate a threshold after the hash is computed.
    """

    # frozen -> no post-construction mutation; extra="forbid" -> a construction-time
    # typo (e.g. ``n_min_globl=25``) RAISES instead of silently keeping the default and
    # yielding an identical config_hash (the exact drift the seal exists to prevent).
    model_config = ConfigDict(frozen=True, extra="forbid")

    # window / classifier (CON-002..006)
    pre_window_s: int = 120
    imm_max_s: int = 60
    primary_horizon_s: int = 300
    settle_tol_s: int = 30
    robustness_horizons_s: tuple[int, ...] = (30, 60, 600)
    epsilon: float = 0.05
    min_odds_states: int = 3  # CON-008 per-event observability floor (sealed)
    # slice thresholds (CON-007): the ONLY two numeric knobs the slice tagger reads.
    # Both are v1 PREDECLARED defaults (CON-014), sealed here so a post-hoc change to
    # either moves config_hash() and VOIDs the run rather than silently re-bucketing.
    favorite_prob_cutoff: float = 0.50  # p_pre >= cutoff -> favorite_scorer
    late_match_minute: int = 60  # match_minute >= this -> late (else early)
    # aggregation (CON-009/010)
    n_min_global: int = 30
    n_min_slice: int = 15
    bootstrap_n: int = 10000
    ci_level: float = 0.90
    seed: int = 20260705
    # near-certain band (CON-016): DERIVED from the band E2 (``series.py``) actually
    # consumes, so a market-quality band change moves ``config_hash()`` (VOIDs) rather
    # than leaving the sealed band a decorative literal. Pinned v1 values: 0.05 / 0.95.
    band_lo: float = DEFAULT_MARKET_QUALITY_CONFIG.band_lo
    band_hi: float = DEFAULT_MARKET_QUALITY_CONFIG.band_hi

    def to_window_config(self) -> WindowConfig:
        """Rebuild E3's ``WindowConfig`` from the sealed fields (single source)."""
        return WindowConfig(
            pre_window_s=self.pre_window_s,
            imm_max_s=self.imm_max_s,
            primary_horizon_s=self.primary_horizon_s,
            settle_tol_s=self.settle_tol_s,
            epsilon=self.epsilon,
            robustness_horizons_s=self.robustness_horizons_s,
            min_odds_states=self.min_odds_states,
        )

    def to_agg_config(self) -> AggConfig:
        """Rebuild E4's ``AggConfig`` from the sealed fields (single source)."""
        return AggConfig(
            n_min_global=self.n_min_global,
            n_min_slice=self.n_min_slice,
            bootstrap_n=self.bootstrap_n,
            ci_level=self.ci_level,
            seed=self.seed,
        )

    def config_hash(self) -> str:
        """SHA-256 over the canonically-serialized config (stable, order-independent).

        Mirrors ``MarketQualityConfig.filter_config_hash`` exactly: the same
        canonical dump (``json.dumps(sort_keys=True, separators=(",", ":"))``,
        inlined as :func:`_canonical_dump` to stay off the trust boundary) over
        ``model_dump()``. The ``robustness_horizons_s`` tuple serializes as a JSON
        array, identically to a list, so the dump is deterministic.
        """
        return hashlib.sha256(_canonical_dump(self.model_dump()).encode()).hexdigest()


def verify_pinned(cfg: ProbeConfig, expected_hash: str) -> None:
    """VOID (``ProbeVoidError``) unless ``cfg`` recomputes to ``expected_hash``.

    A pure comparison performing NO I/O, so the E6 runner can call it first and
    fail closed before reading any pack or scores file (PAT-001 / AC-007).
    """
    actual = cfg.config_hash()
    if actual != expected_hash:
        raise ProbeVoidError(
            f"VOID: ProbeConfig hash diverged from the pinned stamp -- expected "
            f"{expected_hash}, got {actual}. The predeclared thresholds changed since "
            "the stamp (CON-014); do NOT report this result."
        )


def _serialize_event_record(record: EventRecord) -> dict[str, Any]:
    """Serialize one ``EventRecord`` to the §4 per-event audit schema (JSON-safe).

    Grid keys are stringified so the artifact is directly JSON-serializable (int
    keys would otherwise be coerced silently on dump) -- mirrors the ``run002_vvv``
    ``{str(k): v}`` treatment of int-keyed maps.
    """
    return {
        "t_e": record.t_e,
        "scoring_side": record.scoring_side,
        "participant": record.participant,
        "p_pre": record.p_pre,
        "p_imm": record.p_imm,
        "p_settle": record.p_settle,
        "delta_imm": record.delta_imm,
        "delta_settle": record.delta_settle,
        "R": record.R,
        "event_class": record.event_class,
        "exclusion_reason": record.exclusion_reason,
        "slice_tags": dict(record.slice_tags),
        "grid": {str(horizon): value for horizon, value in record.grid.items()},
    }


def _merge_excluded(
    compute_excluded: dict[str, int], extraction_excluded: dict[str, int] | None
) -> dict[str, int]:
    """Merge extraction excludes (E1) into the compute excludes (E3) by reason.

    §4 requires ONE ``excluded_by_reason`` map spanning both stages, so the
    extraction rejects (``decreasing_score`` / ``ambiguous_delta`` / ``unparseable``)
    appear alongside the compute reasons (``no_pre_tick`` ...) rather than being
    dropped on the floor before aggregation ever sees them.
    """
    merged = dict(compute_excluded)
    for reason, count in (extraction_excluded or {}).items():
        merged[reason] = merged.get(reason, 0) + count
    return merged


def build_sealed_result(
    cfg: ProbeConfig,
    result: ProbeResult,
    records: list[EventRecord],
    *,
    fixtures: Sequence[int] = (),
    total_goal_events: int = 0,
    extraction_excluded: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Serialize the sealed result artifact (§4) -- in memory only, writes NO file.

    Carries the pinned ``config`` + ``config_hash`` (the seal), the top-level
    ``verdict`` plus the ``global`` block (directional stats, ``raw_delta_*_median``,
    and a nested ``verdict``) and per-slice verdicts (E4), the full per-event
    ``event_records[]`` audit trail (GUD-001 / AC-008), the §4 top-level counts
    (``fixtures`` / ``total_goal_events`` / ``eligible_events``), the CON-014 defaults
    note, and both tally maps -- with the per-fixture extraction excludes merged into
    ``excluded_by_reason``. Writing this dict to disk is the operator-gated E6
    ``--seal`` step; no I/O here.
    """
    # Eligible == directional set: R is set ONLY for LAG/OVERSHOOT/REVERSAL events
    # (every NO-SIGNAL / excluded record carries R=None), so ``R is not None`` is the
    # class gate. Raw-delta medians are reported over exactly this set (GUD-001), so
    # a below-epsilon ratio artifact can never leak into the headline move sizes.
    eligible = [rec for rec in records if rec.R is not None]
    raw_delta_imm = [rec.delta_imm for rec in eligible if rec.delta_imm is not None]
    raw_delta_settle = [rec.delta_settle for rec in eligible if rec.delta_settle is not None]

    # eligible_events = events that PASSED the CON-008 observability floor =
    # total_goal_events - (the four floor-FAIL reasons). This is a SUPERSET of the
    # directional set (result.global_n): a below_epsilon event passed the floor but
    # carries no R, so it counts here yet not in the directional CI.
    eligible_events = sum(
        1 for rec in records if rec.exclusion_reason not in _FLOOR_FAIL_REASONS
    )

    return {
        "protocol_id": PROTOCOL_ID,
        "config": cfg.model_dump(),
        "config_hash": cfg.config_hash(),
        "fixtures": list(fixtures),
        "total_goal_events": total_goal_events,
        "eligible_events": eligible_events,
        # §4 nests the directional stats, raw-delta medians, and verdict INSIDE the
        # `global` block; a single top-level `verdict` mirrors it (no redundant
        # top-level `overall_verdict`, which §4 does not carry).
        "verdict": result.overall_verdict,
        "predeclared_defaults_note": PREDECLARED_DEFAULTS_NOTE,
        "global": {
            "n": result.global_n,
            "median_R": result.global_median_R,
            "ci_low": result.global_ci_low,
            "ci_high": result.global_ci_high,
            "raw_delta_imm_median": (
                statistics.median(raw_delta_imm) if raw_delta_imm else None
            ),
            "raw_delta_settle_median": (
                statistics.median(raw_delta_settle) if raw_delta_settle else None
            ),
            "verdict": result.overall_verdict,
        },
        "per_slice": [
            {
                "slice": sv.slice,
                "n": sv.n,
                "median_R": sv.median_R,
                "ci_low": sv.ci_low,
                "ci_high": sv.ci_high,
                "verdict": sv.verdict,
            }
            for sv in result.per_slice
        ],
        "event_records": [_serialize_event_record(record) for record in records],
        "class_counts": dict(result.class_counts),
        "excluded_by_reason": _merge_excluded(
            result.excluded_by_reason, extraction_excluded
        ),
    }
