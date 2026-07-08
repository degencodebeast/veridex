"""E5-T1: R1.5 proof-card honesty copy (AC-110).

The NEW behavior under test: when a MM-R1.5 result carries NO trade-aware
diagnostic (``trade_aware_diagnostic is None``), the proof card must render an
explicit ``INSUFFICIENT_DATA`` / "not run" note — never a fabricated value — and
the card must never surface a real-PnL / fill-rate / executable-edge /
"profitable" literal.

The already-built ``trades_not_fills_caveat`` (set at MM-R1.5 in result.py) is
asserted present but is NOT the RED-driving behavior here.
"""

from __future__ import annotations

from veridex.maker.contracts import MakerRungLabel
from veridex.maker.result import MakerArenaResult, render_proof_card


def _r15_result(trade_aware_diagnostic: dict | None = None) -> MakerArenaResult:
    return MakerArenaResult(
        protocol_id="maker-arena-v1",
        config_hash="abc",
        rung=MakerRungLabel("MM-R1.5"),
        fixtures=tuple(range(18)),
        per_agent=[],
        maker_leaderboard=[],
        falsification={"verdict": "INCONCLUSIVE"},
        fixture_universe_n=18,
        excluded_by_reason={},
        trade_aware_diagnostic=trade_aware_diagnostic,
    )


# Literals the R1.5 proof card must NEVER surface — no fill/PnL/edge/profitable claim.
_BLOCKLISTED = ["$", "profit", "pnl of", "fill rate", "executable edge"]


def test_r15_proof_card_insufficient_data_note_and_no_pnl() -> None:
    card = render_proof_card(_r15_result(trade_aware_diagnostic=None))

    # NEW behavior: absent R1.5 diagnostic → an explicit INSUFFICIENT_DATA / "not run" note
    # (never a fabricated 0 or value). This is the RED-driving assertion.
    note = card.trade_aware_diagnostic_note
    assert note is not None
    assert "INSUFFICIENT_DATA" in note
    assert "not run" in note.lower()

    # already-built caveat present (not the RED driver)
    assert card.trades_not_fills_caveat is not None
    assert "not our fills" in card.trades_not_fills_caveat

    # no real-PnL / fill-rate / executable-edge / "profitable" literal anywhere in the card
    blob = card.model_dump_json().lower()
    for lit in _BLOCKLISTED:
        assert lit not in blob, f"blocklisted literal leaked into proof card: {lit!r}"


def test_r15_proof_card_with_diagnostic_omits_insufficient_note() -> None:
    # When the diagnostic IS present the card must NOT fabricate an INSUFFICIENT_DATA note.
    card = render_proof_card(
        _r15_result(trade_aware_diagnostic={"data_state": "OK", "reports": []})
    )
    assert card.trade_aware_diagnostic_note is None
    blob = card.model_dump_json().lower()
    for lit in _BLOCKLISTED:
        assert lit not in blob, f"blocklisted literal leaked into proof card: {lit!r}"
