"""E5 sealed ``ProbeConfig`` + config hash + VOID-on-drift + result serializer.

``ProbeConfig`` is the SINGLE, frozen, predeclared superset of every threshold the
probe runs against (CON-002..006/009/010/016). It is the source of truth: E3's
``WindowConfig`` and E4's ``AggConfig`` are rebuilt FROM it (``to_window_config`` /
``to_agg_config``), so the sealed config drives the whole pipeline and there is no
second copy of a default to drift out of sync.

The seal follows the Run-001/Run-002 predeclared pattern (PAT-001):

* ``config_hash()`` -- sha256 over the canonical (sorted-key, compact) JSON dump of
  every field, mirroring ``MarketQualityConfig.filter_config_hash`` exactly (same
  ``serialize_payload`` helper). Changing ANY threshold after results are observed
  changes this hash -- the CON-014 anti-drift guarantee.
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
from typing import Any

from pydantic import BaseModel, ConfigDict

from veridex.backtest.event_probe.aggregate import AggConfig, ProbeResult
from veridex.backtest.event_probe.compute import EventRecord, WindowConfig
from veridex.runtime.evidence import serialize_payload
from veridex.strategies.market_quality import DEFAULT_MARKET_QUALITY_CONFIG

#: Sealed protocol identity (spec §4). Bound alongside the config hash so a result
#: cannot be relabelled under a different protocol without a new stamp (CON-014).
PROTOCOL_ID = "event-fork-probe-v1"


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
        ``serialize_payload`` (``json.dumps(sort_keys=True, separators=(",", ":"))``)
        over ``model_dump()``. The ``robustness_horizons_s`` tuple serializes as a
        JSON array, identically to a list, so the dump is deterministic.
        """
        return hashlib.sha256(serialize_payload(self.model_dump()).encode()).hexdigest()


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


def build_sealed_result(
    cfg: ProbeConfig, result: ProbeResult, records: list[EventRecord]
) -> dict[str, Any]:
    """Serialize the sealed result artifact (§4) -- in memory only, writes NO file.

    Carries the pinned ``config`` + ``config_hash`` (the seal), the ``overall_verdict``
    with global stats and per-slice verdicts (E4), the full per-event
    ``event_records[]`` audit trail (GUD-001 / AC-008), and both tally maps
    (``class_counts`` / ``excluded_by_reason``). Writing this dict to disk is the
    operator-gated E6 ``--seal`` step; this function never performs I/O.
    """
    return {
        "protocol_id": PROTOCOL_ID,
        "config": cfg.model_dump(),
        "config_hash": cfg.config_hash(),
        "overall_verdict": result.overall_verdict,
        "global": {
            "n": result.global_n,
            "median_R": result.global_median_R,
            "ci_low": result.global_ci_low,
            "ci_high": result.global_ci_high,
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
        "excluded_by_reason": dict(result.excluded_by_reason),
    }
