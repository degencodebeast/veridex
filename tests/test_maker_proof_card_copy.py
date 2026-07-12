from veridex.maker.result import MakerArenaResult, render_proof_card
from veridex.maker.contracts import MakerRungLabel

def _result(rung, r2_bracket=None):
    return MakerArenaResult(protocol_id="maker-arena-v1", config_hash="abc", rung=rung,
        fixtures=tuple(range(18)), per_agent=[], maker_leaderboard=[], falsification={"verdict": "SEPARATED"},
        fixture_universe_n=18, excluded_by_reason={}, r2_bracket=r2_bracket)

# competitor demo-PnL phrasings the doctrine treats as context-only, never in our proof (AC-007)
_BLOCKLISTED_PNL = ["$", "profit of", "pnl of", "roi of", "% return"]

def test_proof_card_renders_no_external_pnl_literal():
    card_json = render_proof_card(_result(MakerRungLabel("MM-R1"))).model_dump_json().lower()
    for lit in _BLOCKLISTED_PNL:
        assert lit not in card_json

def test_uncalibrated_label_renders_when_r2_present():
    card = render_proof_card(_result(MakerRungLabel("MM-R1"),
        r2_bracket={"label": "UNCALIBRATED / declared model overlay", "ranked": False}))
    assert card.uncalibrated is True

def test_no_r2_means_not_uncalibrated():
    card = render_proof_card(_result(MakerRungLabel("MM-R1")))
    assert card.uncalibrated is False
