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

from typing import Any

from pydantic import BaseModel

from veridex.maker.contracts import MakerRungLabel

#: The quadruple R2 honesty label surfaced on the proof card when an R2 overlay is
#: attached. Kept byte-identical to ``r2_suite.R2_QUADRUPLE_LABEL`` (duplicated here
#: rather than imported so this result contract stays free of the R2 render module);
#: the sealed tests assert the exact string on both sides.
_R2_OVERLAY_LABEL = "REPORT_ONLY / UNCALIBRATED / DECLARED_MODEL_OVERLAY / NOT_A_FILL_PROOF"


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
    per_agent: list[dict[str, Any]]
    maker_leaderboard: list[dict[str, Any]]
    falsification: dict[str, Any]

    trade_aware_diagnostic: dict[str, Any] | None = None
    markout_adverse_decomposition: dict[str, Any] | None = None
    event_gate_timeline: dict[str, Any] | None = None
    window_clv_analog: dict[str, Any] | None = None

    real_executable_edge_bps: None = None

    fixture_universe_n: int
    small_n_flag: bool = True
    excluded_by_reason: dict[str, int]

    r2_bracket: dict[str, Any] | None = None


def assert_score_run_untouched(
    before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> None:
    """Prove a maker run left a prior directional scoring result byte-identical (SEC-005/AC-011)."""
    if before != after:
        raise AssertionError("maker lane must not mutate the directional leaderboard result")


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
    window_clv_analog: dict[str, Any] | None
    falsification: dict[str, Any]
    n_fixtures: int
    small_n_note: str
    trades_not_fills_caveat: str | None
    #: Honest R1.5 diagnostic status. When the trade-aware diagnostic is ABSENT at
    #: MM-R1.5 this carries an explicit ``INSUFFICIENT_DATA`` / "not run" note (never
    #: a fabricated value); it is ``None`` otherwise. It never carries a PnL/edge/fill
    #: value — the card makes no executable-edge or realized-PnL claim.
    trade_aware_diagnostic_note: str | None = None
    #: R2 overlay honesty label (REQ-108/AC-110). Carries the quadruple label
    #: :data:`_R2_OVERLAY_LABEL` exactly when an R2 bracket overlay is attached
    #: (``result.r2_bracket is not None``); ``None`` for a plain R1/R1.5 result. It
    #: is a REPORT-ONLY honesty marker, never a fill/edge claim.
    r2_overlay_label: str | None = None


def render_proof_card(result: MakerArenaResult) -> MakerProofCard:
    """Render an honest :class:`MakerProofCard` from a maker arena result.

    Never emits the string ``"67"`` or any competitor PnL literal, and never
    surfaces a fill/edge value. The trades-not-fills caveat appears only at the
    MM-R1.5 rung.
    """
    # uncalibrated is True exactly when an R2 bracket overlay is attached.
    trades_not_fills_caveat: str | None = None
    trade_aware_diagnostic_note: str | None = None
    if result.rung == MakerRungLabel("MM-R1.5"):
        trades_not_fills_caveat = "trades are not our fills; no executable-edge claim"
        # Honest no-data surfacing: when the trade-aware diagnostic was not produced
        # (no pinned trade artifact / no resolvable trades), say so explicitly rather
        # than fabricating a value. Never emit a fill/edge/PnL literal here.
        if result.trade_aware_diagnostic is None:
            trade_aware_diagnostic_note = (
                "INSUFFICIENT_DATA: trade-aware diagnostic not run "
                "(no pinned trade artifact)"
            )

    return MakerProofCard(
        rung=result.rung.value,
        uncalibrated=result.r2_bracket is not None,
        headline=result.falsification.get("headline") or result.falsification.get(
            "verdict", "INCONCLUSIVE"
        ),
        window_clv_analog=result.window_clv_analog,
        falsification=result.falsification,
        n_fixtures=result.fixture_universe_n,
        small_n_note=f"n={result.fixture_universe_n} (Polymarket-resolved cp1), small sample",
        trades_not_fills_caveat=trades_not_fills_caveat,
        trade_aware_diagnostic_note=trade_aware_diagnostic_note,
        # REQ-108/AC-110: the quadruple honesty label is surfaced exactly when an R2
        # overlay is attached, mirroring `uncalibrated`; None for a plain R1/R1.5 card.
        r2_overlay_label=_R2_OVERLAY_LABEL if result.r2_bracket is not None else None,
    )


__all__ = [
    "MakerArenaResult",
    "MakerProofCard",
    "assert_score_run_untouched",
    "render_proof_card",
]
