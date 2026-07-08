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
from veridex.maker.falsification import FalsificationResult, falsify
from veridex.maker.leaderboard import rank_makers, window_clv_analog
from veridex.maker.mapping import (
    DEFAULT_MAPPING_PATH,
    load_resolved_market_lookup,
)
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


def run_maker_arena(
    cfg: MakerRunConfig,
    *,
    expected_config_hash: str = MAKER_EXPECTED_CONFIG_HASH,
    trade_artifact_path: Path | None = None,
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
        artifact = load_trade_artifact(trade_artifact_path)
        recomputed = recompute_artifact_hash(list(artifact.rows))
        if recomputed != cfg.trade_artifact_hash:
            raise MakerVoidError(
                "VOID: loaded trade artifact recomputes to "
                f"{recomputed}, which diverges from the predeclared "
                f"cfg.trade_artifact_hash {cfg.trade_artifact_hash}. Do NOT report "
                "this result."
            )
        has_verified_trades = True
    # A `trade_artifact_path` supplied while `cfg.trade_artifact_hash is None` is an
    # UNPINNED artifact: it cannot back an R1.5 claim, so it is never loaded and the
    # run stays MM-R1 (no join, no diagnostic).

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

    # 6. Assemble the result. real_executable_edge_bps stays None (no edge claim).
    result = MakerArenaResult(
        protocol_id=cfg.protocol_id,
        config_hash=cfg.config_hash(),
        rung=rung,
        fixtures=cfg.fixture_ids,
        per_agent=per_agent,
        maker_leaderboard=maker_leaderboard,
        falsification={**falsification.model_dump(), "headline": headline},
        window_clv_analog=wca,
        fixture_universe_n=len({row["fixture_id"] for row in tape}),
        excluded_by_reason={},
    )

    # 7. Seal path writes ONLY when seal=True.
    if seal:
        RESULT_PATH.write_text(result.model_dump_json())
    return result
