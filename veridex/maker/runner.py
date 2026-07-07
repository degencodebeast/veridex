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
from pathlib import Path
from typing import Any

from veridex.maker.agents import NaiveMarketMakerAgent, TxLineFairMarketMakerAgent
from veridex.maker.config import (
    MakerRunConfig,
    MakerVoidError,
    verify_pinned,
)
from veridex.maker.falsification import run_falsification_arena
from veridex.maker.leaderboard import rank_makers, window_clv_analog
from veridex.maker.mapping import (
    DEFAULT_MAPPING_PATH,
    load_resolved_market_lookup,
)
from veridex.maker.result import MakerArenaResult
from veridex.maker.rung_gate import DataPresence, assign_rung
from veridex.maker.scorer import aggregate_agent_metrics, score_r1_markout
from veridex.maker.tape import build_cp1_maker_tape

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
    "f74a486cd0ab53d40e6f31b0eef47a88953c8cb3502ea7d326478904f9c1f784"
)

# maker -> veridex -> repo root; the committed pack + venue-frame trees hang off it.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACK_ROOT = _REPO_ROOT / "scripts" / "txline_live" / "packs"
_CP1_FRAMES_ROOT = _REPO_ROOT / "scripts" / "txline_live" / "cp1" / "frames"

#: The ONLY path the sealed artifact is ever written to (and only when ``seal=True``).
RESULT_PATH: Path = _REPO_ROOT / "scripts" / "txline_live" / "cp1" / "maker-arena-result.json"


def _build_ref_at(tape: list[dict[str, Any]]):
    """Build a no-look-ahead reference lookup ``(market_key, side, ts) -> fv | None``.

    Indexes the tape's TxLINE fair value by ``(venue_market_ref, ts)`` and, at query
    time, returns the most-recent fv at or before ``ts`` for that venue ref (a later
    tick is invisible -- no look-ahead). ``side`` does not select the reference: the
    fair value is a per-market series shared by both quote sides. Returns ``None``
    when the market is unknown or no fv exists at/before ``ts`` (never imputed).
    """
    index: dict[str, tuple[list[int], list[float]]] = {}
    by_ref: dict[str, list[tuple[int, float]]] = {}
    for row in tape:
        by_ref.setdefault(row["venue_market_ref"], []).append((row["ts"], row["fv"]))
    for ref, pairs in by_ref.items():
        pairs.sort(key=lambda pair: pair[0])
        index[ref] = ([ts for ts, _ in pairs], [fv for _, fv in pairs])

    def ref_at(market_key: str, side: Any, ts: int) -> float | None:
        entry = index.get(market_key)
        if entry is None:
            return None
        ts_list, fv_list = entry
        pos = bisect.bisect_right(ts_list, ts)
        if pos == 0:
            return None
        return fv_list[pos - 1]

    return ref_at


def _agent_metrics(agent: Any, adapted: list[dict[str, Any]], ref_at, horizons_s):
    """Score one agent over the adapted tape and aggregate its markout metric stack."""
    quote_sets = [
        agent.propose(
            reference_fv={"fv": row["fv"], "suspended": False},
            venue_view={"mid": row["mid"]},
            inventory={},
            params={},
            clock=row["ts"],
        )
        for row in adapted
    ]
    marks, acc = score_r1_markout(quote_sets, ref_at, horizons_s)
    return aggregate_agent_metrics(agent.agent_id, marks, acc)


def run_maker_arena(cfg: MakerRunConfig, *, seal: bool = False) -> MakerArenaResult:
    """Run the sealed MM-R1 maker arena over the real cp1 tape (fail-closed).

    Strict ordering (each step gated by the previous):

    1. :func:`verify_pinned` -- pure, NO I/O; VOIDs on config drift BEFORE any load.
    2. Load the pinned mapping and re-check its recomputed content hash.
    3. Build the real cp1 maker tape (consumes real ReplayPack bytes, ``verify=True``).
    4. Run the naive-vs-candidate falsification arena + per-agent markout metrics.
    5. Assign the data-feasibility rung (mids present, no trades -> MM-R1).
    6. Assemble the :class:`MakerArenaResult`.
    7. Write the sealed artifact ONLY when ``seal=True``.

    Args:
        cfg: The caller-supplied frozen run config (REQUIRED -- no default).
        seal: When ``True``, write the result to :data:`RESULT_PATH`; otherwise
            write nothing and return the result.

    Returns:
        The assembled :class:`MakerArenaResult`.

    Raises:
        MakerVoidError: If the config hash drifted from the pinned stamp, or the
            recomputed mapping content hash diverged from the config's bound value.
    """
    # 1. VERIFY FIRST -- pure, no I/O. A drifted config VOIDs before any byte is read.
    verify_pinned(cfg, MAKER_EXPECTED_CONFIG_HASH)

    # 2. Load the pinned mapping and re-check its recomputed content hash.
    records, recomputed = load_resolved_market_lookup(DEFAULT_MAPPING_PATH)
    if recomputed != cfg.mapping_content_hash:
        raise MakerVoidError(
            "VOID: recomputed mapping content hash diverged from the config's bound "
            f"value -- expected {cfg.mapping_content_hash}, got {recomputed}. Do NOT "
            "report this result."
        )

    # 3. Consume the REAL cp1 ReplayPack bytes (every pack loaded with verify=True).
    tape = build_cp1_maker_tape(
        records, pack_root=_PACK_ROOT, cp1_frames_root=_CP1_FRAMES_ROOT
    )

    # 4. Falsification arena + per-agent markout metrics over the shared tape.
    ref_at = _build_ref_at(tape)
    horizons_s = cfg.markout_horizons_s
    naive = NaiveMarketMakerAgent()
    candidate = TxLineFairMarketMakerAgent()

    # Only ticks carrying a fresh venue mid can be quoted by the venue-anchored naive
    # control; a stale (None) mid is not imputed. Both agents share this same tape.
    adapted = [
        {"ts": row["ts"], "fv": row["fv"], "mid": row["mid"]}
        for row in tape
        if row["mid"] is not None
    ]

    arena = run_falsification_arena(
        tape=adapted,
        naive=naive,
        candidate=candidate,
        ref_at=ref_at,
        horizons_s=horizons_s,
        has_trade_reference=False,
    )

    per_agent = [
        _agent_metrics(naive, adapted, ref_at, horizons_s),
        _agent_metrics(candidate, adapted, ref_at, horizons_s),
    ]
    maker_leaderboard = rank_makers(per_agent)
    top = maker_leaderboard[0]
    wca = window_clv_analog(top["avg_markout_bps"], top["scored"])

    # 5. Rung from data presence alone (mids present, no trades -> MM-R1).
    rung = assign_rung(
        DataPresence(has_mids=True, has_trades=False, has_fill_assumption=False)
    )

    # 6. Assemble the result. real_executable_edge_bps stays None (no edge claim).
    result = MakerArenaResult(
        protocol_id=cfg.protocol_id,
        config_hash=cfg.config_hash(),
        rung=rung,
        fixtures=cfg.fixture_ids,
        per_agent=per_agent,
        maker_leaderboard=maker_leaderboard,
        falsification=arena,
        window_clv_analog=wca,
        fixture_universe_n=len({row["fixture_id"] for row in tape}),
        excluded_by_reason={},
    )

    # 7. Seal path writes ONLY when seal=True.
    if seal:
        RESULT_PATH.write_text(result.model_dump_json())
    return result
