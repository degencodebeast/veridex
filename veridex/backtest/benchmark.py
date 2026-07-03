"""Competitor-strategy replication benchmark — Phase 2D Post-2D Tasks 7-8b (SX / M2).

Answers "how would sharpline/sports-workbench have fared on TxLINE data" WITHOUT importing a
single line of competitor code (CON-007: no competitor code on a trust-path module) — only
their published detection *algorithm*, re-expressed as a Veridex config over the existing
`sharp_stats` primitives (`veridex/strategies/sharp_stats.py`). Every scored number in a
`StrategyBenchmarkResult` comes from the injected Veridex `score_fn` seam; this module computes
no CLV/scoring itself, it only decides WHEN a translated detector would have fired.

Competitors are evaluated on rung-1 evidence only — `evidence_rung` is pinned to
`"txline-only"` and rejects anything stronger (REQ-SX-004): a competitor replication is never
allowed to borrow a venue-fill-grade evidence rung it never earned.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, field_validator

from veridex.ingest.marketstate import MarketState
from veridex.ingest.replay_pack import load_pack_marketstates
from veridex.provenance import EvidenceRung
from veridex.runtime.evidence import serialize_payload
from veridex.strategies.sharp_stats import PageHinkley, logit, robust_z

#: Signature of the injected Veridex scoring seam — the ONLY source of scored numbers here.
ScoreFn = Callable[[list[int]], dict[str, Any]]


class CompetitorReplicationConfig(BaseModel):
    """A competitor detector translated into Veridex terms — never the competitor's own code."""

    source_repo: str
    source_strategy: str
    strategy: str
    translated_params: dict[str, float]
    notes: str

    def veridex_config_hash(self) -> str:
        """SHA-256 over the canonically-serialized translated config (stable, order-independent)."""
        payload = {"strategy": self.strategy, "translated_params": self.translated_params}
        return hashlib.sha256(serialize_payload(payload).encode()).hexdigest()


class StrategyBenchmarkResult(BaseModel):
    """Outcome of replaying a translated competitor detector on Veridex-scored TxLINE data.

    Competitors are rung-1 ONLY (REQ-SX-004, CON-007): a `StrategyBenchmarkResult` never
    carries venue-fill-grade evidence — it can only ever claim what a pure TxLINE replay proves.
    """

    benchmark_id: str
    source_strategy: str
    veridex_config_hash: str
    pack_content_hash: str
    evidence_rung: str
    fire_count: int
    scored_count: int
    avg_clv_bps: float | None
    abstain_count: int
    provenance: str

    @field_validator("evidence_rung")
    @classmethod
    def _rung_must_be_txline_only(cls, value: str) -> str:
        if value != EvidenceRung.TXLINE_ONLY.value:
            raise ValueError(
                f"competitor benchmark evidence_rung must be {EvidenceRung.TXLINE_ONLY.value!r} "
                f"(competitors are rung-1 only, REQ-SX-004), got {value!r}"
            )
        return value


def translate_sharpline(params: dict[str, float]) -> CompetitorReplicationConfig:
    """Translate sharpline's z-score + Page-Hinkley detector into a Veridex config (CON-007).

    Re-expresses sharpline's published tuning knobs (`zGate`, `phThresh`, `lambda`, `cooldown`,
    `warmup`) over Veridex's own `sharp_stats` primitives (`robust_z`, `PageHinkley`) — no
    sharpline code is imported or executed, only its algorithm as described by these parameters.
    """
    translated = {
        "z_gate": float(params["zGate"]),
        "ph_delta": float(params["phThresh"]),
        "ph_lambda": float(params["lambda"]),
        "cooldown_ticks": float(params["cooldown"]),
        "warmup_ticks": float(params["warmup"]),
    }
    return CompetitorReplicationConfig(
        source_repo="sharpline",
        source_strategy="sharpline",
        strategy="momentum-sharp",
        translated_params=translated,
        notes=(
            "sharpline's robust z-score gate + Page-Hinkley change-point detector, re-expressed "
            "over Veridex's own sharp_stats primitives (robust_z, PageHinkley) — no sharpline "
            "code imported."
        ),
    )


def translate_threshold(params: dict[str, float]) -> CompetitorReplicationConfig:
    """Translate sports-workbench's flat percent-move-threshold detector into a Veridex config.

    Sibling translator for the M5 baseline lane. Re-expresses a plain "fire when price moves
    more than X%" rule as a Veridex threshold config — no sports-workbench code imported.
    """
    translated = {
        "move_threshold_pct": float(params["moveThreshold"]),
        "cooldown_ticks": float(params.get("cooldown", 0.0)),
    }
    return CompetitorReplicationConfig(
        source_repo="sports-workbench",
        source_strategy="sports-workbench",
        strategy="threshold-move",
        translated_params=translated,
        notes=(
            "sports-workbench's flat percent-move-threshold detector, re-expressed as a Veridex "
            "threshold config — no sports-workbench code imported."
        ),
    )


def _sharp_detector_fires(series: list[float], params: dict[str, float]) -> list[int]:
    """Replay the translated sharpline detector over `series`; return firing tick indices.

    A tick fires when EITHER the robust z-score of its logit-space move against its own recent
    window exceeds `z_gate`, OR the Page-Hinkley change-point detector confirms a sustained
    level shift — mirroring sharpline's two-signal (sharp-move + confirmed-drift) design. The
    first `warmup_ticks` ticks are skipped (no reference window yet); a fire holds off the next
    `cooldown_ticks` ticks from firing again (sharpline's own cooldown semantics).
    """
    z_gate = params["z_gate"]
    warmup = int(params["warmup_ticks"])
    cooldown = int(params["cooldown_ticks"])
    ph = PageHinkley(delta=params["ph_delta"], lambda_=params["ph_lambda"])

    logits = [logit(p) for p in series]
    fires: list[int] = []
    cooldown_remaining = 0
    for i, value in enumerate(logits):
        direction = ph.update(value)
        if i < warmup:
            continue
        if cooldown_remaining > 0:
            cooldown_remaining -= 1
            continue
        z = robust_z(logits[: i + 1])
        if abs(z) >= z_gate or direction is not None:
            fires.append(i)
            cooldown_remaining = cooldown
    return fires


def _assemble_result(
    config: CompetitorReplicationConfig,
    fires: list[int],
    score_result: dict[str, Any],
    *,
    pack_content_hash: str,
) -> StrategyBenchmarkResult:
    """Assemble the sealed result — every scored field copied verbatim from `score_result`."""
    fire_count = len(fires)
    scored_count = int(score_result.get("scored_count", 0))
    return StrategyBenchmarkResult(
        benchmark_id=f"{config.source_strategy}_{pack_content_hash[:12]}",
        source_strategy=config.source_strategy,
        veridex_config_hash=config.veridex_config_hash(),
        pack_content_hash=pack_content_hash,
        evidence_rung=EvidenceRung.TXLINE_ONLY.value,
        fire_count=fire_count,
        scored_count=scored_count,
        avg_clv_bps=score_result.get("avg_clv_bps"),
        abstain_count=max(fire_count - scored_count, 0),
        provenance=EvidenceRung.TXLINE_ONLY.value,
    )


async def run_strategy_benchmark(
    config: CompetitorReplicationConfig, *, pack: Any, score_fn: ScoreFn
) -> StrategyBenchmarkResult:
    """Replay `config`'s translated detector over `pack.ticks`; score ONLY via `score_fn`.

    This function computes fire indices — WHEN the translated competitor detector would have
    acted — and nothing else. Every scored number (`scored_count`, `avg_clv_bps`, ...) comes
    exclusively from the injected `score_fn` seam (the real Veridex scoring path in production,
    a deterministic fake in tests): the benchmark never computes CLV itself.
    """
    fires = _sharp_detector_fires(list(pack.ticks), config.translated_params)
    score_result = score_fn(fires)
    return _assemble_result(config, fires, score_result, pack_content_hash=pack.content_hash)


def extract_prob_series(marketstates: list[MarketState], market_key: str, side: str) -> list[float]:
    """One market/side's probability series from real `MarketState`s, oldest first.

    Reads `ms.markets[market_key]["stable_prob_bps"][side] / 10000.0` per tick. A tick where the
    market is suspended (`stable_prob_bps` empty) or doesn't carry `side` is SKIPPED, not
    interpolated — the detector only ever sees ticks that genuinely priced.
    """
    series: list[float] = []
    for ms in marketstates:
        market = ms.markets.get(market_key)
        if market is None:
            continue
        prob_bps = market.get("stable_prob_bps") or {}
        if side not in prob_bps:
            continue
        series.append(prob_bps[side] / 10000.0)
    return series


def _pack_content_hash(pack_dir: Path) -> str:
    """Read the pack's stored `content_hash` from its manifest."""
    return str(json.loads((pack_dir / "pack.json").read_text())["content_hash"])


async def benchmark_on_pack(
    config: CompetitorReplicationConfig,
    *,
    pack_dir: Path,
    fixture_id: int,
    market_key: str,
    side: str,
    score_fn: ScoreFn,
) -> StrategyBenchmarkResult:
    """Replay `config`'s translated detector over a REAL loaded ReplayPack (AC-003).

    Loads MarketStates through the same tamper-evident, verified loader the live/backtest paths
    use (`verify=True` — never a hand-faked pack). Scoring happens ONLY via `score_fn`.
    """
    marketstates = load_pack_marketstates(pack_dir, fixture_id, verify=True)
    series = extract_prob_series(marketstates, market_key, side)
    fires = _sharp_detector_fires(series, config.translated_params)
    score_result = score_fn(fires)
    return _assemble_result(config, fires, score_result, pack_content_hash=_pack_content_hash(pack_dir))
