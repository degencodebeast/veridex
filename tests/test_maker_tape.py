from pathlib import Path
from veridex.maker.tape import build_cp1_maker_tape
from veridex.maker.mapping import load_resolved_market_lookup, DEFAULT_MAPPING_PATH

REPO = Path(__file__).resolve().parents[1]
PACK_ROOT = REPO / "scripts" / "txline_live" / "packs"
FRAMES_ROOT = REPO / "scripts" / "txline_live" / "cp1" / "frames"

def test_tape_bridges_txline_coords_to_venue_record_on_real_pack():
    records, _ = load_resolved_market_lookup(DEFAULT_MAPPING_PATH)
    tape = build_cp1_maker_tape(records, pack_root=PACK_ROOT, cp1_frames_root=FRAMES_ROOT)
    assert tape, "expected non-empty tape from real cp1 packs"
    # the home side must come from the REAL TxLINE coordinate part1, NOT the venue key/side
    home_rows = [r for r in tape if r["venue_side"] == "home"]
    assert home_rows, "expected home-side rows"
    for r in home_rows:
        assert r["txline_market_key"] == "1X2_PARTICIPANT_RESULT||"
        assert r["txline_side"] == "part1"          # home ← part1 (NOT "home")
        assert r["venue_market_ref"].startswith("1X2|home")
        assert 0.0 <= r["fv"] <= 1.0
    # canonical universe: exactly the 18 cp1 fixtures, nothing else (Codex M1 watch item)
    assert len({r["fixture_id"] for r in tape}) == 18

def test_tape_never_reads_venue_key_from_marketstate():
    # a MarketState has NO "1X2|home|full" key; the builder must read the TxLINE key. Assert the source
    # does not attempt the venue key against the marketstate.
    import inspect, veridex.maker.tape as tape_mod
    src = inspect.getsource(tape_mod)
    assert 'markets["1X2|home|full"]' not in src and "markets['1X2|home|full']" not in src
    assert "1X2_PARTICIPANT_RESULT||" in src   # reads the real TxLINE key
