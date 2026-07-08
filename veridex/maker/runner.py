"""Sealed market-maker arena runner (verify-before-I/O, MM-R1).

This is the trust-critical entrypoint for the maker lane. It composes the already
sealed pieces (frozen config, pinned mapping, real cp1 tape, falsification arena,
markout scorer, maker leaderboard) into one :class:`MakerArenaResult` under a
strict fail-closed ordering:

  1. :func:`verify_pinned` is the FIRST statement that can fail -- a pure hash
     comparison performing NO I/O. A drifted config VOIDs here, BEFORE any mapping
     or tape byte is touched (PAT-001).
  2. Only after the verify passes do we load the pinned mapping and re-check its
     recomputed content hash against the value bound into the config.
  3. Only then do we consume the REAL cp1 ReplayPack bytes via
     :func:`build_cp1_maker_tape` (which loads every pack with ``verify=True``).

The runner claims NO executable edge: ``real_executable_edge_bps`` stays ``None``.
It performs NO live network access and imports nothing from any live venue feed --
the mapping and packs are consumed from committed bytes only.

``load_resolved_market_lookup`` and ``build_cp1_maker_tape`` are imported INTO this
module's namespace so a test can monkeypatch ``runner.load_resolved_market_lookup``
/ ``runner.build_cp1_maker_tape`` to prove the ordering without touching real bytes.
"""

from __future__ import annotations

import bisect
from collections.abc import Callable
from pathlib import Path
from typing import Any

from veridex.maker.agents import NaiveMarketMakerAgent, TxLineFairMarketMakerAgent
from veridex.maker.config import (
    MakerRunConfig,
    MakerVoidError,
    verify_pinned,
)
from veridex.maker.contracts import MarketMakerAgent, Side
from veridex.maker.diagnostic import (
    TradeAwareDiagnostic,
    assemble_adverse_selection_report,
    build_convergence_reach,
    gather_near_trade_signals,
)
from veridex.maker.falsification import FalsificationResult, falsify
from veridex.maker.leaderboard import rank_makers, window_clv_analog
from veridex.maker.mapping import (
    DEFAULT_MAPPING_PATH,
    load_resolved_market_lookup,
)
from veridex.maker.r2_bracket import FillAssumptionConfig
from veridex.maker.r2_suite import render_r2_suite
from veridex.maker.result import MakerArenaResult
from veridex.maker.rung_gate import DataPresence, assign_rung
from veridex.maker.scorer import (
    QuoteAccounting,
    QuoteMarkout,
    aggregate_agent_metrics,
    score_r1_markout,
)
from veridex.maker.tape import build_cp1_maker_tape
from veridex.maker.trade_artifact import load_trade_artifact, recompute_artifact_hash

# Imported as a module-level, spyable name so tests can assert the R1.5 artifact-content
# pin VOIDs BEFORE any trade join. The join itself is wired in E4; here it must merely be
# reachable/monkeypatchable and provably never called on a VOID.
from veridex.maker.trades import (  # noqa: F401
    TradePrint,
    join_trades_to_fixture_with_accounting,
)

__all__ = [
    "CP1_18",
    "MAKER_EXPECTED_CONFIG_HASH",
    "RESULT_PATH",
    "run_maker_arena",
]

#: The canonical cp1 fixture universe (n=18, CON-015). Bound into the pinned hash.
CP1_18: tuple[int, ...] = (
    17588229, 17588234, 17588245, 17588325, 17588391, 17588404,
    17926593, 18167317, 18172280, 18172469, 18175918, 18175981,
    18175983, 18176123, 18179550, 18179551, 18179759, 18179763,
)

#: Pinned config-hash stamp: the ``config_hash()`` of the default cp1 maker config.
#: A run whose live config recomputes to anything else VOIDs before any I/O.
MAKER_EXPECTED_CONFIG_HASH: str = (
    "f997d5a8fcb7d7c4cb02048a56bfb7bcdfabc06c6657ea97bf84be43beb16f33"
)

# maker -> veridex -> repo root; the committed pack + venue-frame trees hang off it.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACK_ROOT = _REPO_ROOT / "scripts" / "txline_live" / "packs"
_CP1_FRAMES_ROOT = _REPO_ROOT / "scripts" / "txline_live" / "cp1" / "frames"

#: The ONLY path the sealed artifact is ever written to (and only when ``seal=True``).
RESULT_PATH: Path = _REPO_ROOT / "scripts" / "txline_live" / "cp1" / "maker-arena-result.json"


def _group_ref_at(
    ts_list: list[int], fv_list: list[float]
) -> Callable[[str, Side, int], float | None]:
    """Build a no-look-ahead reference lookup over ONE market's ``(ts, fv)`` series.

    The closure is bound to a single ``(fixture_id, venue_market_ref)`` group's own
    sorted fair-value series, so a quote generated from that market can only ever be
    scored against that same market's future fv -- never another fixture's or another
    venue side's (MM-R1). ``market_key``/``side`` are accepted for the
    :func:`score_r1_markout` calling convention but do NOT select the series: the fair
    value is the per-market series this closure already owns. Returns the most-recent
    fv at or before ``ts`` (a later tick is invisible -- no look-ahead), or ``None``
    when no fv exists at/before ``ts`` (never imputed).
    """

    def ref_at(market_key: str, side: Side, ts: int) -> float | None:
        pos = bisect.bisect_right(ts_list, ts)
        if pos == 0:
            return None
        return fv_list[pos - 1]

    return ref_at


def _group_fv_at(
    ts_list: list[int], fv_list: list[float]
) -> Callable[[int], float | None]:
    """Build a no-look-ahead fv lookup over ONE market's ``(ts, fv)`` series.

    The single-arg ``(ts)`` analog of :func:`_group_ref_at`, used by the trade-aware
    diagnostic so a matched trade's post-trade markout is resolved against ONLY its own
    market's future fv -- never a merged/other-market tape. Returns the most-recent fv at
    or before ``ts`` (a later tick is invisible -- no look-ahead), or ``None`` when no fv
    exists at/before ``ts`` (including an empty series -- never imputed, never borrowed).
    """

    def fv_at(ts: int) -> float | None:
        pos = bisect.bisect_right(ts_list, ts)
        if pos == 0:
            return None
        return fv_list[pos - 1]

    return fv_at


def _score_group(
    agent: MarketMakerAgent,
    rows: list[dict[str, Any]],
    ref_at: Callable[[str, Side, int], float | None],
    horizons_s: tuple[int, ...],
) -> tuple[list[QuoteMarkout], QuoteAccounting]:
    """Score one agent over ONE market group's rows against that group's own fv."""
    quote_sets = [
        agent.propose(
            reference_fv={"fv": row["fv"]},
            venue_view={"mid": row["mid"]},
            inventory={},
            params={},
            clock=row["ts"],
        )
        for row in rows
    ]
    return score_r1_markout(quote_sets, ref_at, horizons_s)


def _row_to_trade_print(row: Any) -> TradePrint:
    """Project a normalized trade row onto its ``TradePrint`` observation fields.

    Only the market-observation fields cross over (``ts, price, size, aggressor_side,
    condition_id, token_id``); the chain-event identity stays on the artifact row. NO
    fill / edge / PnL field is ever introduced — a ``TradePrint`` is a venue trade, never
    a Veridex fill.
    """
    return TradePrint(
        ts=row.ts,
        price=row.price,
        size=row.size,
        aggressor_side=row.aggressor_side,
        condition_id=row.condition_id,
        token_id=row.token_id,
    )


def _build_trade_aware_diagnostic(
    *,
    artifact: Any,
    records: list[Any],
    cfg: MakerRunConfig,
    tape: list[dict[str, Any]],
    agents: tuple[MarketMakerAgent, ...],
    quality_by: dict[str, list[int]],
    falsification: FalsificationResult,
) -> TradeAwareDiagnostic:
    """Join the verified artifact's trades to fixtures and build the R1.5 diagnostic.

    FULL ACCOUNTING (AC-102): each trade is joined to exactly one fixture-market XOR
    counted as unmatched, with NO silent drop — a trade matched under one fixture is
    removed from the pool so it can never be re-counted as unmatched under another
    (``rows_total == rows_matched + rows_unmatched``). The join key is the pinned mapping
    ``(condition_id, token_id)`` only (never a live lookup).

    The trades are an independent DIAGNOSTIC reference: no fill / fill-rate / spread-capture
    / PnL / executable-edge value is produced, and ``real_executable_edge_bps`` stays
    ``None`` on every per-agent report.
    """
    trade_prints = [_row_to_trade_print(row) for row in artifact.rows]

    # Full-accounting join across all fixtures: matched trades are removed from the pool
    # so a trade matched under fixture A is never re-counted as unmatched under fixture B.
    # Each matched trade is bucketed under its OWN (fixture_id, market_ref) so its markout
    # can later be measured against that market's fair value alone (never a pooled tape).
    remaining: list[TradePrint] = list(trade_prints)
    matched_by_market: dict[tuple[int, str], list[TradePrint]] = {}
    for fixture_id in cfg.fixture_ids:
        joined, _unmatched = join_trades_to_fixture_with_accounting(
            remaining, records, fixture_id
        )
        matched_here: list[TradePrint] = []
        for market_ref, group in joined.items():
            matched_by_market.setdefault((fixture_id, market_ref), []).extend(group)
            matched_here.extend(group)
        matched_ids = {id(trade) for trade in matched_here}
        remaining = [trade for trade in remaining if id(trade) not in matched_ids]
    rows_total = len(trade_prints)
    rows_matched = sum(len(group) for group in matched_by_market.values())
    rows_unmatched = len(remaining)

    # No-look-ahead fv lookup PER (fixture_id, venue_market_ref) market. Building ONE fv_at
    # over the merged tape would let a matched trade's post-trade markout resolve against a
    # DIFFERENT market's fair value (the same cross-market FV-pooling class fixed in the R1
    # scoring path via `_group_ref_at`). So each market owns its own sorted fv series and a
    # matched trade is only ever marked out against its own market's future fv.
    fv_rows_by_market: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for row in tape:
        key = (row["fixture_id"], row["venue_market_ref"])
        fv_rows_by_market.setdefault(key, []).append(row)
    fv_series_by_market: dict[tuple[int, str], tuple[list[int], list[float]]] = {}
    for key, market_rows in fv_rows_by_market.items():
        market_rows.sort(key=lambda row: row["ts"])
        fv_series_by_market[key] = (
            [row["ts"] for row in market_rows],
            [row["fv"] for row in market_rows],
        )

    all_sorted = sorted(tape, key=lambda row: row["ts"])
    fv_vals = [row["fv"] for row in all_sorted]

    # The quote reference the venue trades are measured around: the median fresh venue mid
    # (fall back to the median fv when no mid is fresh). Size is NEVER used.
    mids = [row["mid"] for row in tape if row["mid"] is not None]
    if mids:
        quote_price = sorted(mids)[len(mids) // 2]
    else:
        quote_price = sorted(fv_vals)[len(fv_vals) // 2] if fv_vals else 0.5

    # Per-agent toxicity loss (bps) as a non-negative MEAN magnitude derived from the
    # pooled markout QUALITY (which is <= 0). This is a comparison anchor, NOT a
    # PnL/fill. It MUST be a mean (not a cumulative sum): the lane's gated toxicity
    # axis is `scorer.avg_toxicity_loss_bps`, itself a mean of max(0, -markout) over
    # scored marks, and `candidate_vs_naive_toxicity_delta_bps_diagnostic` (below) must
    # stay on that same per-quote-scale axis rather than scaling with quote count.
    # `None` when an agent has zero scored quotes (never fabricate a mean from nothing).
    horizons_s = cfg.markout_horizons_s
    loss_by: dict[str, int | None] = {
        a.agent_id: (
            round(sum(-q for q in qs) / len(qs)) if (qs := quality_by.get(a.agent_id, [])) else None
        )
        for a in agents
    }
    agent_ids = [a.agent_id for a in agents]
    candidate_loss = loss_by[agent_ids[-1]] if agent_ids else None
    naive_loss = loss_by[agent_ids[0]] if agent_ids else None

    # Gather each market's near-quote trade signals against THAT market's own fv series,
    # then POOL them (mirrors the R1 scoring path: per-market first, pool after). A market
    # whose trades matched but that has no fv series in the tape contributes nothing (its
    # fv_at yields None) rather than borrowing another market's fv.
    pooled_signs: list[float] = []
    pooled_contributions: list[float] = []
    pooled_near = 0
    pooled_total = 0
    for market_key, market_trades in matched_by_market.items():
        # A matched market with no fv series in the tape gets an empty series -> its fv_at
        # yields None for every trade (nothing resolves), never another market's fv.
        ts_list, fv_list = fv_series_by_market.get(market_key, ([], []))
        market_fv_at = _group_fv_at(ts_list, fv_list)
        signs, contributions, near, total = gather_near_trade_signals(
            market_trades, market_fv_at, quote_price
        )
        pooled_signs.extend(signs)
        pooled_contributions.extend(contributions)
        pooled_near += near
        pooled_total += total

    per_agent: dict[str, Any] = {}
    for agent in agents:
        per_agent[agent.agent_id] = assemble_adverse_selection_report(
            pooled_signs,
            pooled_contributions,
            pooled_near,
            pooled_total,
            candidate_toxicity_loss_bps=candidate_loss,
            naive_toxicity_loss_bps=naive_loss,
            falsification_verdict=falsification.verdict,
        )

    # Basis-adjusted convergence over the mid-present rows (reach on the RESIDUAL only).
    mid_rows = [row for row in all_sorted if row["mid"] is not None]
    convergence = None
    if mid_rows:
        convergence = build_convergence_reach(
            txline_fv=[row["fv"] for row in mid_rows],
            venue_native=[row["mid"] for row in mid_rows],
            reach_horizon_s=horizons_s[0] if horizons_s else 60,
        )

    usable = (
        rows_matched > 0
        or (convergence is not None and convergence.residual_reach_fraction is not None)
        or any(
            report.independent_reference_verdict != "INSUFFICIENT_DATA"
            for report in per_agent.values()
        )
    )
    data_state = "OK" if usable else "INSUFFICIENT_DATA"

    return TradeAwareDiagnostic(
        data_state=data_state,
        artifact_hash=cfg.trade_artifact_hash,
        rows_total=rows_total,
        rows_matched=rows_matched,
        rows_unmatched=rows_unmatched,
        per_agent=per_agent,
        convergence=convergence,
        excluded_by_reason={},
    )


def run_maker_arena(
    cfg: MakerRunConfig,
    *,
    expected_config_hash: str = MAKER_EXPECTED_CONFIG_HASH,
    trade_artifact_path: Path | None = None,
    fill_assumption: FillAssumptionConfig | None = None,
    seal: bool = False,
) -> MakerArenaResult:
    """Run the sealed maker arena over the real cp1 tape (fail-closed).

    Strict ordering (each step gated by the previous):

    1. :func:`verify_pinned` -- pure, NO I/O; VOIDs on config drift BEFORE any load.
       (R1 uses the default :data:`MAKER_EXPECTED_CONFIG_HASH`; R1.5 passes its
       operator-predeclared per-run hash via ``expected_config_hash``.)
    2. Load the pinned mapping and re-check its recomputed content hash.
    2b. Cross-check the config's fixture universe against the mapping's fixtures.
    2c. TWO-LAYER R1.5 pin: when ``cfg.trade_artifact_hash`` is predeclared, load the
        artifact from the EXPLICIT ``trade_artifact_path`` and VOID unless the loaded
        rows recompute to that hash -- BEFORE any trade join / diagnostic / scoring.
    3. Build the real cp1 maker tape (consumes real ReplayPack bytes, ``verify=True``).
    4. Score per ``(fixture_id, venue_market_ref)`` market (each quote marked out only
       against its OWN market's future fv), pool toxicity quality across markets, and
       run the naive-vs-candidate falsification on the pooled quality.
    5. Assign the data-feasibility rung (mids present; trades present + verified ->
       MM-R1.5, else MM-R1).
    6. Assemble the :class:`MakerArenaResult`.
    7. Write the sealed artifact ONLY when ``seal=True``.

    Args:
        cfg: The caller-supplied frozen run config (REQUIRED -- no default).
        expected_config_hash: The predeclared config-hash the run is pinned to. For
            the R1 lane this defaults to :data:`MAKER_EXPECTED_CONFIG_HASH`; the R1.5
            lane supplies its operator-predeclared per-run hash. Layer-1 pin.
        trade_artifact_path: The EXPLICIT source of the trade-artifact bytes -- never a
            mutable default disk path / "latest artifact". ``cfg.trade_artifact_hash``
            is the artifact IDENTITY; this arg is only the SOURCE of bytes. Required
            (and verified) only when ``cfg.trade_artifact_hash`` is set.
        fill_assumption: Optional pinned ex-ante fill-assumption instance. When
            supplied, its ``config_hash()`` MUST equal ``cfg.fill_assumption_hash``
            (the R2 analog of the artifact-content pin, REQ-107) or the run VOIDs
            BEFORE any overlay is rendered; on a match a report-only
            :func:`render_r2_suite` overlay is attached to ``r2_bracket``. The
            overlay NEVER changes the rung and NEVER produces an executable edge.
        seal: When ``True``, write the result to :data:`RESULT_PATH`; otherwise
            write nothing and return the result.

    Returns:
        The assembled :class:`MakerArenaResult`.

    Raises:
        MakerVoidError: If the config hash drifted from the pinned stamp; the
            recomputed mapping content hash diverged from the config's bound value;
            a predeclared ``trade_artifact_hash`` has no supplied path; or the loaded
            artifact's rows do not recompute to the predeclared hash.
    """
    # 1. VERIFY FIRST -- pure, no I/O. A drifted config VOIDs before any byte is read.
    verify_pinned(cfg, expected_config_hash)

    # 2. Load the pinned mapping and re-check its recomputed content hash.
    records, recomputed = load_resolved_market_lookup(DEFAULT_MAPPING_PATH)
    if recomputed != cfg.mapping_content_hash:
        raise MakerVoidError(
            "VOID: recomputed mapping content hash diverged from the config's bound "
            f"value -- expected {cfg.mapping_content_hash}, got {recomputed}. Do NOT "
            "report this result."
        )

    # 2b. Cross-check the config's fixture universe against the pinned mapping's
    # fixtures. Even if someone re-pinned the config hash AND the mapping to
    # mutually inconsistent 18-sets, this VOIDs rather than silently emitting a
    # result whose `fixtures` disagree with the consumed tape (canonical-universe
    # self-consistency, Codex M1 watch item).
    mapping_fixtures = {record.fixture_id for record in records}
    if set(cfg.fixture_ids) != mapping_fixtures:
        symmetric_difference = sorted(set(cfg.fixture_ids) ^ mapping_fixtures)
        raise MakerVoidError(
            "cfg fixture universe disagrees with the pinned mapping's fixtures "
            f"(cfg has {len(cfg.fixture_ids)}, mapping has {len(mapping_fixtures)}; "
            f"symmetric difference {symmetric_difference})"
        )

    # 2c. TWO-LAYER R1.5 PIN, layer 2 (artifact-content). The layer-1 config pin
    # (step 1) already froze `cfg.trade_artifact_hash` as the artifact IDENTITY; here
    # we verify the loaded BYTES recompute to that identity BEFORE any trade join /
    # diagnostic / scoring can consume them (CON-111, AC-118). The artifact SOURCE is
    # the EXPLICIT `trade_artifact_path` arg -- never a mutable default disk path.
    has_verified_trades = False
    if cfg.trade_artifact_hash is not None:
        if trade_artifact_path is None:
            # Predeclared artifact identity but no supplied bytes -> INSUFFICIENT_DATA:
            # the pinned artifact cannot be verified, so no loader call, no join.
            raise MakerVoidError(
                "VOID: cfg.trade_artifact_hash is predeclared but no "
                "trade_artifact_path was supplied -- INSUFFICIENT_DATA: the pinned "
                "artifact bytes cannot be verified. Do NOT report this result."
            )
        try:
            artifact = load_trade_artifact(trade_artifact_path)
        except OSError as exc:
            # Fail CLOSED, uniform VOID surface: a missing / unreadable artifact file
            # must join the MakerVoidError VOID-everywhere boundary -- never leak a raw
            # FileNotFoundError / OSError past the trust surface.
            raise MakerVoidError(
                "VOID: the predeclared trade artifact could not be read from "
                f"{trade_artifact_path!r} ({type(exc).__name__}: {exc}) -- "
                "INSUFFICIENT_DATA. Do NOT report this result."
            ) from exc
        # Named distinctly from the step-2 mapping `recomputed` above so the two trust
        # hashes never shadow each other on the trust path.
        recomputed_artifact_hash = recompute_artifact_hash(list(artifact.rows))
        if recomputed_artifact_hash != cfg.trade_artifact_hash:
            raise MakerVoidError(
                "VOID: loaded trade artifact recomputes to "
                f"{recomputed_artifact_hash}, which diverges from the predeclared "
                f"cfg.trade_artifact_hash {cfg.trade_artifact_hash}. Do NOT report "
                "this result."
            )
        has_verified_trades = True
    # A `trade_artifact_path` supplied while `cfg.trade_artifact_hash is None` is an
    # UNPINNED artifact: it cannot back an R1.5 claim, so it is never loaded and the
    # run stays MM-R1 (no join, no diagnostic).

    # 2d. R2 ASSUMPTION-INSTANCE PIN (REQ-107) -- the R2 analog of the layer-2
    # artifact-content pin. `cfg.fill_assumption_hash` binds only the HASH of the pinned
    # ex-ante assumption into `config_hash`; the runner still receives the assumption
    # INSTANCE separately, so an overlay rendered under an instance DIFFERENT from the one
    # bound into the hash is exactly the drift REQ-107 forbids. Re-verify the instance here
    # -- BEFORE any `render_r2_suite` -- and VOID on mismatch. (Deleting this compare lets a
    # drifted assumption render an overlay, which `test_r2_assumption_instance_mismatch_voids`
    # catches.)
    if fill_assumption is not None:
        recomputed_fill_assumption_hash = fill_assumption.config_hash()
        if recomputed_fill_assumption_hash != cfg.fill_assumption_hash:
            raise MakerVoidError(
                "VOID: supplied fill_assumption recomputes to "
                f"{recomputed_fill_assumption_hash}, which diverges from the "
                f"predeclared cfg.fill_assumption_hash {cfg.fill_assumption_hash}. An R2 "
                "overlay rendered under a DIFFERENT assumption than the one bound into "
                "config_hash is forbidden drift. Do NOT report this result."
            )

    # 3. Consume the REAL cp1 ReplayPack bytes (every pack loaded with verify=True).
    tape = build_cp1_maker_tape(
        records, pack_root=_PACK_ROOT, cp1_frames_root=_CP1_FRAMES_ROOT
    )

    # 4. Per-market scoring (MM-R1): each quote is scored ONLY against its own
    # (fixture_id, venue_market_ref) market's future TxLINE fv. The tape is grouped
    # by that key so draw/away quotes are never marked out against the home fv, and
    # so distinct fixtures that happen to share a venue_market_ref (e.g. every home
    # market is "1X2|home|full") never pool into one ts-sorted series where
    # ref_at(ts) could return a DIFFERENT match's fair value. Toxicity quality is
    # pooled across markets only AFTER each quote has been scored in-market.
    horizons_s = cfg.markout_horizons_s
    naive = NaiveMarketMakerAgent()
    candidate = TxLineFairMarketMakerAgent()
    agents = (naive, candidate)

    groups: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for row in tape:
        groups.setdefault((row["fixture_id"], row["venue_market_ref"]), []).append(row)

    marks_by: dict[str, list[QuoteMarkout]] = {a.agent_id: [] for a in agents}
    quality_by: dict[str, list[int]] = {a.agent_id: [] for a in agents}
    scored_by: dict[str, int] = {a.agent_id: 0 for a in agents}
    abstained_by: dict[str, int] = {a.agent_id: 0 for a in agents}

    for rows in groups.values():
        # Only ticks carrying a fresh venue mid can be quoted by the venue-anchored
        # naive control; a stale (None) mid is not imputed -- so QUOTE GENERATION runs
        # over the mid-present ("live") rows only.
        live = sorted(
            (row for row in rows if row["mid"] is not None), key=lambda row: row["ts"]
        )
        if not live:
            continue
        # The markout REFERENCE fv series, however, is the FULL observed TxLINE fv for
        # THIS market -- fv is observed at EVERY tick, independent of venue-mid freshness.
        # Building ref_at from `live` alone would drop the real future fv of any tick whose
        # venue mid is stale (mid=None), silently falling ref_at(ts+h) back to an OLDER fv
        # and corrupting the forward markout (Codex M8). So ref_at spans ALL group rows.
        # This stays a single (fixture_id, venue_market_ref) group's own series -- no
        # cross-market leakage; the grouping key is unchanged.
        all_sorted = sorted(rows, key=lambda row: row["ts"])
        ref_at = _group_ref_at(
            [row["ts"] for row in all_sorted], [row["fv"] for row in all_sorted]
        )
        for agent in agents:
            marks, acc = _score_group(agent, live, ref_at, horizons_s)
            marks_by[agent.agent_id].extend(marks)
            quality_by[agent.agent_id].extend(-max(0, -m.markout_bps) for m in marks)
            scored_by[agent.agent_id] += acc.scored
            abstained_by[agent.agent_id] += acc.abstained

    naive_quality = quality_by[naive.agent_id]
    cand_quality = quality_by[candidate.agent_id]
    if naive_quality and cand_quality:
        falsification = falsify(naive_quality, cand_quality)
        headline = (
            "SEPARATED_QUOTE_QUALITY"
            if falsification.verdict == "SEPARATED"
            else "INCONCLUSIVE"
        )
    else:
        # Degenerate/all-abstain tape: falsify would raise on an empty sample. Fail
        # to an honest INCONCLUSIVE verdict rather than crash the sealed run.
        falsification = FalsificationResult(
            delta_bps=0, ci_low_bps=0, ci_high_bps=0, verdict="INCONCLUSIVE"
        )
        headline = "INCONCLUSIVE"

    per_agent = [
        aggregate_agent_metrics(
            naive.agent_id,
            marks_by[naive.agent_id],
            QuoteAccounting(
                scored=scored_by[naive.agent_id],
                abstained=abstained_by[naive.agent_id],
                excluded={},
            ),
        ),
        aggregate_agent_metrics(
            candidate.agent_id,
            marks_by[candidate.agent_id],
            QuoteAccounting(
                scored=scored_by[candidate.agent_id],
                abstained=abstained_by[candidate.agent_id],
                excluded={},
            ),
        ),
    ]
    maker_leaderboard = rank_makers(per_agent)
    top = maker_leaderboard[0]
    wca = window_clv_analog(top["avg_markout_bps"], top["scored"])

    # 5. Rung from data presence alone (mids present; a verified trade artifact ->
    # MM-R1.5 via the EXISTING gate, else MM-R1). No parallel rung path is invented.
    rung = assign_rung(
        DataPresence(
            has_mids=True,
            has_trades=has_verified_trades,
            has_fill_assumption=False,
        )
    )

    # 5b. R1.5 real-artifact join + trade-aware diagnostic. Only when the artifact-content
    # pin verified (both pins passed): join the artifact's trades to fixtures under FULL
    # accounting and build the per-agent no-fill diagnostic. Absent a verified artifact the
    # diagnostic stays None and the run remains MM-R1 (no join, no fabricated value).
    trade_aware_diagnostic: dict[str, Any] | None = None
    if has_verified_trades:
        trade_aware_diagnostic = _build_trade_aware_diagnostic(
            artifact=artifact,
            records=records,
            cfg=cfg,
            tape=tape,
            agents=agents,
            quality_by=quality_by,
            falsification=falsification,
        ).model_dump()

    # 5c. R2 REPORT-ONLY OVERLAY (REQ-107/108). When a pinned ex-ante fill assumption was
    # supplied (and passed the 2d instance pin), attach a quadruple-labeled
    # `render_r2_suite` overlay driven ONLY by the pinned assumption + report-only markout
    # inputs. It is a DECLARED MODEL OVERLAY: it does NOT touch the rung (computed above
    # from data presence alone) and produces NO executable edge / fill value. The markout
    # inputs are the observed per-quote markout_bps pooled across agents (a report-only
    # input, never a fill); an empty degenerate tape falls back to a neutral [0] so the
    # overlay renders without fabricating a value.
    r2_bracket: dict[str, Any] | None = None
    if fill_assumption is not None:
        r2_markouts = [
            mark.markout_bps for agent in agents for mark in marks_by[agent.agent_id]
        ] or [0]
        r2_bracket = render_r2_suite(r2_markouts, fill_assumption).model_dump()

    # 6. Assemble the result. real_executable_edge_bps stays None (no edge claim).
    # For E2 the verified `trade_artifact_hash` is recorded transitively via the
    # `config_hash` binding (the pin is frozen INTO `cfg.config_hash()`), so the result
    # is already bound to the exact artifact identity; a dedicated result-level
    # `trade_artifact_hash` field is surfaced later in E5-T2.
    result = MakerArenaResult(
        protocol_id=cfg.protocol_id,
        config_hash=cfg.config_hash(),
        rung=rung,
        fixtures=cfg.fixture_ids,
        per_agent=per_agent,
        maker_leaderboard=maker_leaderboard,
        falsification={**falsification.model_dump(), "headline": headline},
        trade_aware_diagnostic=trade_aware_diagnostic,
        window_clv_analog=wca,
        fixture_universe_n=len({row["fixture_id"] for row in tape}),
        excluded_by_reason={},
        r2_bracket=r2_bracket,
    )

    # 7. Seal path writes ONLY when seal=True.
    if seal:
        RESULT_PATH.write_text(result.model_dump_json())
    return result
