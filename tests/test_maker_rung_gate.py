from veridex.maker.rung_gate import DataPresence, assign_rung
from veridex.maker.contracts import MakerRungLabel

def test_mids_only_is_r1():
    assert assign_rung(DataPresence(has_mids=True, has_trades=False, has_fill_assumption=False)) == MakerRungLabel("MM-R1")

def test_mids_plus_trades_is_r1_5():
    assert assign_rung(DataPresence(has_mids=True, has_trades=True, has_fill_assumption=False)) == MakerRungLabel("MM-R1.5")

def test_no_mids_is_inconclusive_none():
    assert assign_rung(DataPresence(has_mids=False, has_trades=True, has_fill_assumption=False)) is None

def test_depth_and_cancels_never_upgrade_to_r3_r4():
    p = DataPresence(has_mids=True, has_trades=True, has_fill_assumption=True,
                     has_l2_depth=True, has_cancels=True, has_own_fills=True)
    assert assign_rung(p) == MakerRungLabel("MM-R1.5")   # gate refuses R3/R4 in this lane

def test_fill_assumption_does_not_change_rung():
    a = assign_rung(DataPresence(has_mids=True, has_trades=False, has_fill_assumption=True))
    assert a == MakerRungLabel("MM-R1")
