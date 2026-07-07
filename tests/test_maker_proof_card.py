from veridex.maker.result import MakerArenaResult, render_proof_card
from veridex.maker.contracts import MakerRungLabel

def _result(rung):
    return MakerArenaResult(protocol_id="maker-arena-v1", config_hash="abc", rung=rung,
        fixtures=tuple(range(18)), per_agent=[], maker_leaderboard=[], falsification={"verdict": "SEPARATED"},
        fixture_universe_n=18, excluded_by_reason={})

def test_card_shows_n18_and_never_67():
    card = render_proof_card(_result(MakerRungLabel("MM-R1")))
    assert card.n_fixtures == 18 and "18" in card.small_n_note
    assert "67" not in card.model_dump_json()

def test_r1_5_card_carries_trades_not_fills_caveat():
    card = render_proof_card(_result(MakerRungLabel("MM-R1.5")))
    assert "not our fills" in card.trades_not_fills_caveat

def test_r1_card_has_no_trades_caveat():
    card = render_proof_card(_result(MakerRungLabel("MM-R1")))
    assert card.trades_not_fills_caveat is None
