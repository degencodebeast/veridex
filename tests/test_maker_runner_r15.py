"""E2-T2 / E2-T3: the two-layer R1.5 pin verified before any trade I/O.

Layer 1 (config pin) is pure and VOIDs on config/param drift BEFORE any load;
layer 2 (artifact-content pin) loads the artifact but VOIDs BEFORE any trade join
when the loaded bytes do not recompute to the predeclared ``cfg.trade_artifact_hash``.
The artifact SOURCE is the EXPLICIT ``trade_artifact_path`` arg (never a mutable
default disk path): ``cfg.trade_artifact_hash`` is the identity, the path is only the
source of bytes.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

import veridex.maker.runner as runner_mod
from veridex.maker.config import MakerVoidError, build_maker_run_config
from veridex.maker.trade_artifact import NormalizedTradeRow, recompute_artifact_hash
from veridex.maker.trades import AggressorSide

CP1_18 = (17588229, 17588234, 17588245, 17588325, 17588391, 17588404, 17926593, 18167317,
          18172280, 18172469, 18175918, 18175981, 18175983, 18176123, 18179550, 18179551, 18179759, 18179763)


def _pinned_cfg():
    return build_maker_run_config(fixture_ids=CP1_18)


def _row(**kw):
    base = dict(ts=1, price=0.5, size=2.0, aggressor_side=AggressorSide.BUY,
                condition_id="0xc", token_id="42", block_number=100, tx_hash="0xabc", log_index=3)
    base.update(kw)
    return NormalizedTradeRow(**base)


def _fake_artifact(rows):
    """A minimal loaded-artifact stand-in exposing only ``.rows`` (what E2 reads)."""
    return SimpleNamespace(rows=tuple(rows))


def _fake_tape():
    return [{"ts": 0, "fixture_id": CP1_18[0], "tick_seq": 0,
             "txline_market_key": "1X2_PARTICIPANT_RESULT||", "txline_side": "part1",
             "venue_market_ref": "1X2|home|full", "venue_side": "home",
             "fv": 0.60, "mid": 0.58, "staleness_s": 0}]


# --- E2-T2 test A: config pin (layer 1) VOIDs before ANY artifact load -----------
def test_config_drift_voids_before_artifact_load(monkeypatch):
    loads = []
    monkeypatch.setattr(runner_mod, "load_trade_artifact", lambda *a, **k: loads.append("x"))
    cfg = _pinned_cfg()  # valid R1 cfg (no artifact)
    with pytest.raises(MakerVoidError):
        runner_mod.run_maker_arena(cfg, expected_config_hash="WRONGSTAMP", seal=False)
    assert loads == []   # config VOID happened before ANY artifact load


# --- E2-T2 test B: artifact-content pin (layer 2) VOIDs before ANY trade I/O -------
# CANNOT be false-green: the config gate PASSES (expected == cfg.config_hash()), so the
# only possible VOID source is the artifact-content branch. The spy targets the REAL
# trade-I/O entrypoint the happy R1.5 path reaches AFTER the pins -- `build_cp1_maker_tape`
# (step 3, which consumes real ReplayPack bytes). Spying it (rather than the E4-only
# `join_trades_to_fixture_with_accounting`, which the runner never calls yet) makes the
# ordering assertion LOAD-BEARING: neutralize the artifact-content compare and the run
# falls through to step 3, so the spy WOULD fire.
def test_artifact_content_mismatch_voids_before_join(monkeypatch):
    tape_calls = []
    monkeypatch.setattr(runner_mod, "build_cp1_maker_tape",
                        lambda *a, **k: tape_calls.append("t") or _fake_tape())

    class _FakeArt:  # exposes .artifact_hash for the build binding
        artifact_hash = "H_PINNED_DIFFERENT"

    cfg = build_maker_run_config(fixture_ids=CP1_18, trade_artifact=_FakeArt())
    loaded = _fake_artifact(rows=[_row()])  # recompute_artifact_hash(rows) == H_real != H_PINNED_DIFFERENT
    monkeypatch.setattr(runner_mod, "load_trade_artifact", lambda *a, **k: loaded)
    with pytest.raises(MakerVoidError):
        runner_mod.run_maker_arena(cfg, expected_config_hash=cfg.config_hash(),
                                   trade_artifact_path=Path("pinned.json"), seal=False)
    assert tape_calls == []   # VOID originated in the artifact-content branch, before any trade I/O


# --- E2-T2 source-contract tests --------------------------------------------------
def test_pinned_hash_but_no_path_is_insufficient_data(monkeypatch):
    # cfg.trade_artifact_hash set, but no trade_artifact_path -> the predeclared artifact
    # bytes cannot be verified -> VOID, no load, no join.
    loads, joins = [], []
    monkeypatch.setattr(runner_mod, "load_trade_artifact", lambda *a, **k: loads.append("x"))
    monkeypatch.setattr(runner_mod, "join_trades_to_fixture_with_accounting",
                        lambda *a, **k: joins.append("j") or ({}, 0))

    class _Art:
        artifact_hash = recompute_artifact_hash([_row()])

    cfg = build_maker_run_config(fixture_ids=CP1_18, trade_artifact=_Art())
    with pytest.raises(MakerVoidError):
        runner_mod.run_maker_arena(cfg, expected_config_hash=cfg.config_hash(),
                                   trade_artifact_path=None, seal=False)
    assert loads == [] and joins == []


def test_path_without_pin_cannot_claim_r15(monkeypatch):
    # trade_artifact_path supplied but cfg.trade_artifact_hash is None -> the unpinned
    # artifact cannot back an R1.5 claim: rung stays MM-R1, no load, diagnostic None.
    loads = []
    monkeypatch.setattr(runner_mod, "load_trade_artifact", lambda *a, **k: loads.append("x"))
    monkeypatch.setattr(runner_mod, "build_cp1_maker_tape", lambda *a, **k: _fake_tape())
    res = runner_mod.run_maker_arena(_pinned_cfg(), trade_artifact_path=Path("ignored.json"), seal=False)
    assert "R1" in str(res.rung) and "R1_5" not in str(res.rung)
    assert res.trade_aware_diagnostic is None
    assert loads == []   # an unpinned path is never loaded


def test_loader_receives_exact_supplied_path_after_config_pin(monkeypatch):
    order = []
    recorded_paths = []
    real_verify = runner_mod.verify_pinned
    monkeypatch.setattr(runner_mod, "verify_pinned",
                        lambda *a, **k: order.append("verify") or real_verify(*a, **k))

    def _spy_load(path, *a, **k):
        order.append("load")
        recorded_paths.append(path)
        return _fake_artifact(rows=[_row()])

    monkeypatch.setattr(runner_mod, "load_trade_artifact", _spy_load)
    monkeypatch.setattr(runner_mod, "build_cp1_maker_tape", lambda *a, **k: _fake_tape())

    class _Art:
        artifact_hash = recompute_artifact_hash([_row()])

    cfg = build_maker_run_config(fixture_ids=CP1_18, trade_artifact=_Art())
    supplied = Path("pinned.json")
    runner_mod.run_maker_arena(cfg, expected_config_hash=cfg.config_hash(),
                               trade_artifact_path=supplied, seal=False)
    assert recorded_paths == [supplied]              # loader got the EXACT supplied path
    assert order.index("verify") < order.index("load")  # loaded ONLY after config pin passed


# --- E2-T3: both pins matching -> claimable MM-R1.5; stamping-only insufficient ----
def test_r15_claim_requires_both_pins(monkeypatch):
    class _Art:
        artifact_hash = recompute_artifact_hash([_row()])  # pin == what the artifact recomputes to

    cfg = build_maker_run_config(fixture_ids=CP1_18, trade_artifact=_Art())
    monkeypatch.setattr(runner_mod, "load_trade_artifact", lambda *a, **k: _fake_artifact(rows=[_row()]))
    monkeypatch.setattr(runner_mod, "build_cp1_maker_tape", lambda *a, **k: _fake_tape())  # keep fast
    res = runner_mod.run_maker_arena(cfg, expected_config_hash=cfg.config_hash(),
                                     trade_artifact_path=Path("pinned.json"), seal=False)
    assert "R1_5" in str(res.rung) and res.real_executable_edge_bps is None
    r1 = runner_mod.run_maker_arena(_pinned_cfg(), seal=False)  # no artifact -> MM-R1
    assert "R1" in str(r1.rung) and "R1_5" not in str(r1.rung)
