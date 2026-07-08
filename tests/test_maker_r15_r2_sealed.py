"""E9-T1 / E9-T2: sealed runner R2 overlay attach + three-path integration.

E9-T1 wires the report-only MM-R2 overlay into ``run_maker_arena``:

  * an ASSUMPTION-INSTANCE pin (the R2 analog of the E2 artifact-content pin,
    REQ-107): a ``FillAssumptionConfig`` whose ``config_hash()`` differs from the
    ``cfg.fill_assumption_hash`` bound into ``config_hash`` VOIDs BEFORE any
    ``render_r2_suite`` is rendered;
  * a proof-card ``r2_overlay_label`` (REQ-108/AC-110) that carries the quadruple
    honesty label exactly when an R2 bracket is attached, else ``None``;
  * seal discipline: ``seal=False`` writes NOTHING, even with an R2 overlay.

R2 is a report-only overlay: it NEVER changes the rung and NEVER produces an
executable edge (``real_executable_edge_bps`` stays the literal ``None``).

E9-T2 is a three-path integration proving no-artifact -> MM-R1, pinned artifact
-> MM-R1.5, +pinned fill-assumption -> R2 overlay attached + labeled, rung
unchanged by R2.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import veridex.maker.runner as runner_mod
from veridex.maker.config import MakerVoidError, build_maker_run_config
from veridex.maker.r2_bracket import FillAssumptionConfig
from veridex.maker.result import render_proof_card
from veridex.maker.runner import RESULT_PATH
from veridex.maker.trade_artifact import NormalizedTradeRow, recompute_artifact_hash
from veridex.maker.trades import AggressorSide

CP1_18 = (17588229, 17588234, 17588245, 17588325, 17588391, 17588404, 17926593, 18167317,
          18172280, 18172469, 18175918, 18175981, 18175983, 18176123, 18179550, 18179551, 18179759, 18179763)

#: The exact quadruple honesty label the proof card must surface for an R2 overlay.
R2_OVERLAY_LABEL = "REPORT_ONLY / UNCALIBRATED / DECLARED_MODEL_OVERLAY / NOT_A_FILL_PROOF"


def _pinned_cfg():
    return build_maker_run_config(fixture_ids=CP1_18)


def _fill_assumption(**kw) -> FillAssumptionConfig:
    """A minimal valid ex-ante fill-assumption config (deterministic)."""
    base = dict(
        fill_model_id="m1",
        latency_ms=100,
        cross_rule="mid",
        partial_fill_policy="none",
    )
    base.update(kw)
    return FillAssumptionConfig(**base)


def _row(**kw):
    base = dict(ts=1, price=0.5, size=2.0, aggressor_side=AggressorSide.BUY,
                condition_id="0xc", token_id="42", block_number=100, tx_hash="0xabc", log_index=3)
    base.update(kw)
    return NormalizedTradeRow(**base)


def _fake_artifact(rows):
    """A minimal loaded-artifact stand-in exposing only ``.rows``."""
    return SimpleNamespace(rows=tuple(rows))


def _fake_tape():
    # Two markets of CP1_18[0] so scoring produces markouts against each market's
    # own fv series (no cross-market fv borrowing) -- reused from test_maker_runner_r15.
    return [{"ts": 0, "fixture_id": CP1_18[0], "tick_seq": 0,
             "txline_market_key": "1X2_PARTICIPANT_RESULT||", "txline_side": "part1",
             "venue_market_ref": "1X2|home|full", "venue_side": "home",
             "fv": 0.60, "mid": 0.58, "staleness_s": 0},
            {"ts": 0, "fixture_id": CP1_18[0], "tick_seq": 0,
             "txline_market_key": "1X2_PARTICIPANT_RESULT||", "txline_side": "part2",
             "venue_market_ref": "1X2|away|full", "venue_side": "away",
             "fv": 0.30, "mid": 0.28, "staleness_s": 0}]


# --- E9-T1 test 1: assumption-INSTANCE pin VOIDs BEFORE render_r2_suite ------------
# MUTATION-note: deleting the `fill_assumption.config_hash() != cfg.fill_assumption_hash`
# compare in the runner -> the mismatched assumption renders instead of voiding, so this
# test FAILS (no MakerVoidError). build_cp1_maker_tape is spied so, absent the compare,
# the run would fall through to scoring + render_r2_suite rather than raise elsewhere.
def test_r2_assumption_instance_mismatch_voids(monkeypatch):
    render_calls = []
    monkeypatch.setattr(runner_mod, "build_cp1_maker_tape", lambda *a, **k: _fake_tape())
    real_render = runner_mod.render_r2_suite
    monkeypatch.setattr(
        runner_mod, "render_r2_suite",
        lambda *a, **k: render_calls.append("r") or real_render(*a, **k),
    )

    bound = _fill_assumption(latency_ms=100)                 # bound into cfg.fill_assumption_hash
    cfg = build_maker_run_config(fixture_ids=CP1_18, fill_assumption=bound)
    drifted = _fill_assumption(latency_ms=999)               # config_hash() != cfg.fill_assumption_hash
    assert drifted.config_hash() != cfg.fill_assumption_hash

    with pytest.raises(MakerVoidError):
        runner_mod.run_maker_arena(
            cfg, expected_config_hash=cfg.config_hash(),
            fill_assumption=drifted, seal=False,
        )
    assert render_calls == []                                # VOID before ANY R2 render


# --- E9-T1 test 2: proof-card r2_overlay_label present IFF an R2 bracket is attached
def test_r2_overlay_label_on_proof_card(monkeypatch):
    monkeypatch.setattr(runner_mod, "build_cp1_maker_tape", lambda *a, **k: _fake_tape())

    fa = _fill_assumption()
    cfg = build_maker_run_config(fixture_ids=CP1_18, fill_assumption=fa)
    r2_res = runner_mod.run_maker_arena(
        cfg, expected_config_hash=cfg.config_hash(), fill_assumption=fa, seal=False
    )
    assert r2_res.r2_bracket is not None
    assert render_proof_card(r2_res).r2_overlay_label == R2_OVERLAY_LABEL

    # An R1/R1.5 result with NO R2 overlay -> label is None.
    r1_res = runner_mod.run_maker_arena(_pinned_cfg(), seal=False)
    assert r1_res.r2_bracket is None
    assert render_proof_card(r1_res).r2_overlay_label is None


# --- E9-T1 test 3: seal=False writes NOTHING even with an R2 overlay ---------------
def test_seal_false_writes_nothing_with_r2(monkeypatch):
    monkeypatch.setattr(runner_mod, "build_cp1_maker_tape", lambda *a, **k: _fake_tape())
    before = RESULT_PATH.read_text()

    fa = _fill_assumption()
    cfg = build_maker_run_config(fixture_ids=CP1_18, fill_assumption=fa)
    runner_mod.run_maker_arena(
        cfg, expected_config_hash=cfg.config_hash(), fill_assumption=fa, seal=False
    )
    assert RESULT_PATH.read_text() == before                 # seal=False -> RESULT_PATH untouched


# --- E9-T1 test 4: R2 attach is REPORT-ONLY (rung unchanged, edge null) ------------
def test_r2_attached_is_report_only(monkeypatch):
    monkeypatch.setattr(runner_mod, "build_cp1_maker_tape", lambda *a, **k: _fake_tape())

    # Baseline: identical run WITHOUT the fill-assumption overlay.
    base = runner_mod.run_maker_arena(_pinned_cfg(), seal=False)

    fa = _fill_assumption()
    cfg = build_maker_run_config(fixture_ids=CP1_18, fill_assumption=fa)
    r2 = runner_mod.run_maker_arena(
        cfg, expected_config_hash=cfg.config_hash(), fill_assumption=fa, seal=False
    )

    # R2 attaches a labeled overlay but does NOT change the rung.
    assert r2.rung == base.rung
    assert r2.r2_bracket is not None
    assert r2.r2_bracket["label"] == R2_OVERLAY_LABEL
    assert r2.r2_bracket["ranked"] is False
    # No executable edge is ever claimed, and the overlay carries no realized PnL.
    assert r2.real_executable_edge_bps is None
    assert r2.r2_bracket["real_executable_edge_bps"] is None
    assert r2.r2_bracket["realized_pnl"] is None
