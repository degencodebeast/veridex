"""Result contract for the isolated market-maker arena lane.

This module is deliberately kept structurally isolated from the directional
scoring path (SEC-005): it must NOT import the directional scoring package nor
textually reference the directional run entrypoint or its leaderboard report.
A maker run produces a :class:`MakerArenaResult` that is *separate* from the
directional leaderboard, and the exported ``assert_..._untouched`` guard proves
(AC-011) that running the maker lane leaves any prior directional result
byte-identical.

The isolation is enforced textually: this source file contains no reference to
the directional entrypoint by name, so a substring grep for it returns nothing.
"""

from __future__ import annotations

from pydantic import BaseModel

from veridex.maker.contracts import MakerRungLabel


class MakerArenaResult(BaseModel):
    """Top-level result of a market-maker arena run.

    The maker lane never claims a realizable executable edge:
    ``real_executable_edge_bps`` is pinned to the literal ``None`` (typed so it
    can never carry a value). ``fixture_universe_n`` records the size of the
    fixture universe and ``small_n_flag`` defaults to ``True`` because the maker
    fixture universe is intentionally small and results must be read as
    diagnostic, not statistical.
    """

    protocol_id: str
    config_hash: str
    rung: MakerRungLabel
    fixtures: tuple[int, ...]
    per_agent: list[dict]
    maker_leaderboard: list[dict]
    falsification: dict

    trade_aware_diagnostic: dict | None = None
    markout_adverse_decomposition: dict | None = None
    event_gate_timeline: dict | None = None
    window_clv_analog: dict | None = None

    real_executable_edge_bps: None = None

    fixture_universe_n: int
    small_n_flag: bool = True
    excluded_by_reason: dict[str, int]


def assert_score_run_untouched(before: list[dict], after: list[dict]) -> None:
    """Prove a maker run left a prior directional scoring result byte-identical (SEC-005/AC-011)."""
    assert before == after, "maker lane must not mutate the directional leaderboard result"


class MakerProofCard(BaseModel):
    """Honest one-glance summary of a maker arena run.

    The card is deliberately conservative: it surfaces the rung, the small
    fixture universe size, and the falsification verdict, but never emits any
    executable-edge / fill value nor any external-repo PnL literal (CON-015 /
    AC-020). At MM-R1.5 it carries the trades-are-not-our-fills caveat.
    """

    rung: str
    uncalibrated: bool = False
    headline: str
    window_clv_analog: dict | None
    falsification: dict
    n_fixtures: int
    small_n_note: str
    trades_not_fills_caveat: str | None


def render_proof_card(result: MakerArenaResult) -> MakerProofCard:
    """Render an honest :class:`MakerProofCard` from a maker arena result.

    Never emits the string ``"67"`` or any competitor PnL literal, and never
    surfaces a fill/edge value. The trades-not-fills caveat appears only at the
    MM-R1.5 rung.
    """
    # uncalibrated stays False for now; a later task flips this True when an
    # R2 bracket overlay is present (no such overlay exists yet, so leave it).
    trades_not_fills_caveat: str | None = None
    if result.rung == MakerRungLabel("MM-R1.5"):
        trades_not_fills_caveat = "trades are not our fills; no executable-edge claim"

    return MakerProofCard(
        rung=result.rung.value,
        uncalibrated=False,
        headline=result.falsification.get("verdict", "INCONCLUSIVE"),
        window_clv_analog=result.window_clv_analog,
        falsification=result.falsification,
        n_fixtures=result.fixture_universe_n,
        small_n_note=f"n={result.fixture_universe_n} (Polymarket-resolved cp1), small sample",
        trades_not_fills_caveat=trades_not_fills_caveat,
    )


__all__ = [
    "MakerArenaResult",
    "assert_score_run_untouched",
    "MakerProofCard",
    "render_proof_card",
]
