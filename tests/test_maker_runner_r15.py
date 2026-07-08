"""E2-T2 / E2-T3: the two-layer R1.5 pin verified before any trade I/O.

Layer 1 (config pin) is pure and VOIDs on config/param drift BEFORE any load;
layer 2 (artifact-content pin) loads the artifact but VOIDs BEFORE any trade join
when the loaded bytes do not recompute to the predeclared ``cfg.trade_artifact_hash``.
The artifact SOURCE is the EXPLICIT ``trade_artifact_path`` arg (never a mutable
default disk path): ``cfg.trade_artifact_hash`` is the identity, the path is only the
source of bytes.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import veridex.maker.runner as runner_mod
from veridex.maker.config import MakerVoidError, build_maker_run_config
from veridex.maker.diagnostic import FORBIDDEN_FILL_FIELDS
from veridex.maker.trade_artifact import NormalizedTradeRow, recompute_artifact_hash
from veridex.maker.trades import AggressorSide

CP1_18 = (17588229, 17588234, 17588245, 17588325, 17588391, 17588404, 17926593, 18167317,
          18172280, 18172469, 18175918, 18175981, 18175983, 18176123, 18179550, 18179551, 18179759, 18179763)

# Descriptive manifest for build_trade_artifact (carries no operator secret); the
# count / hash / mapping-pin / rows fields are computed by the builder.
_MANIFEST_META = dict(
    raw_artifact_hash=None, schema_version="v1", decoder_version="d1", decoder_commit=None,
    source="polymarket_ctf_exchange_v2_orderfilled", chain_id=137, contract_address="0xe11...",
    event_signature="OrderFilled(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)",
    from_block=1, to_block=2, reorg_buffer_confs=20, capture_ts=1, capture_tool_id="t1",
    provider_id="hs-prod", token_supplied_externally=True, fixture_count=18, side_count=54,
    cleanroom_attestation="clean-room; no GPL copied",
)


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
    # Two markets of CP1_18[0] so a trade matched to EITHER market (home or away) has its
    # own per-market fv series to be marked out against (no cross-market fv borrowing).
    return [{"ts": 0, "fixture_id": CP1_18[0], "tick_seq": 0,
             "txline_market_key": "1X2_PARTICIPANT_RESULT||", "txline_side": "part1",
             "venue_market_ref": "1X2|home|full", "venue_side": "home",
             "fv": 0.60, "mid": 0.58, "staleness_s": 0},
            {"ts": 0, "fixture_id": CP1_18[0], "tick_seq": 0,
             "txline_market_key": "1X2_PARTICIPANT_RESULT||", "txline_side": "part2",
             "venue_market_ref": "1X2|away|full", "venue_side": "away",
             "fv": 0.30, "mid": 0.28, "staleness_s": 0}]


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


# --- E2-T2 test C: a missing/unreadable artifact path fails CLOSED as MakerVoidError
# The predeclared artifact identity is set, but the supplied path does not exist. The
# real loader would raise FileNotFoundError; the runner must translate that into the
# uniform MakerVoidError fail-closed surface and NEVER reach the tape/trade I/O.
def test_missing_artifact_path_voids(monkeypatch):
    tape_calls = []
    monkeypatch.setattr(runner_mod, "build_cp1_maker_tape",
                        lambda *a, **k: tape_calls.append("t") or _fake_tape())

    class _Art:
        artifact_hash = recompute_artifact_hash([_row()])

    cfg = build_maker_run_config(fixture_ids=CP1_18, trade_artifact=_Art())
    missing = Path("does-not-exist-trade-artifact-9f3c1e.json")
    with pytest.raises(MakerVoidError):
        runner_mod.run_maker_arena(cfg, expected_config_hash=cfg.config_hash(),
                                   trade_artifact_path=missing, seal=False)
    assert tape_calls == []   # VOID before any trade I/O


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


# --- E4-T4: real-artifact join -> trade-aware diagnostic, no-fill, full accounting ----
def test_runner_r15_joins_real_artifact_and_stays_no_fill(monkeypatch):
    class _Art:
        artifact_hash = recompute_artifact_hash([_row()])  # pin == what the artifact recomputes to

    cfg = build_maker_run_config(fixture_ids=CP1_18, trade_artifact=_Art())
    monkeypatch.setattr(runner_mod, "load_trade_artifact", lambda *a, **k: _fake_artifact(rows=[_row()]))
    monkeypatch.setattr(runner_mod, "build_cp1_maker_tape", lambda *a, **k: _fake_tape())
    res = runner_mod.run_maker_arena(cfg, expected_config_hash=cfg.config_hash(),
                                     trade_artifact_path=Path("pinned.json"), seal=False)

    assert "R1_5" in str(res.rung)                 # verified trade artifact -> MM-R1.5
    diagnostic = res.trade_aware_diagnostic
    assert diagnostic is not None                  # the join produced a diagnostic container

    # FULL ACCOUNTING: every trade grouped-or-unmatched, no silent drop.
    assert diagnostic["rows_total"] == 1
    assert diagnostic["rows_total"] == diagnostic["rows_matched"] + diagnostic["rows_unmatched"]
    # both agents carry a report; convergence built on the residual (basis reported).
    assert set(diagnostic["per_agent"]) == {"naive-mm", "txline-fair-mm"}

    # HARD no-fill boundary: edge null everywhere; NO forbidden fill/PnL field in the
    # serialized diagnostic; ``real_executable_edge_bps`` present only as the literal null.
    assert res.real_executable_edge_bps is None
    blob = json.dumps(diagnostic)
    for forbidden in FORBIDDEN_FILL_FIELDS:
        assert forbidden not in blob
    for report in diagnostic["per_agent"].values():
        assert report["real_executable_edge_bps"] is None


def test_runner_r15_join_matches_real_mapping_row(monkeypatch):
    # M1 spec-closure (AC-102): the E4-T4 headline (real-artifact join -> diagnostic) must
    # be LIVE on real captures. The decoder emits condition_id="" (the OrderFilled ABI has
    # no condition_id), so an un-enriched artifact's ("", token_id) can NEVER match the
    # pinned mapping's non-empty (condition_id, token_id) -> rows_matched==0, every per-agent
    # report collapses to INSUFFICIENT_DATA. build_trade_artifact enriches condition_id from
    # the mapping, so the join now matches on real data. RED before enrichment.
    from veridex.maker.capture import build_trade_artifact
    from veridex.maker.mapping import DEFAULT_MAPPING_PATH, load_resolved_market_lookup

    records, _hash = load_resolved_market_lookup(DEFAULT_MAPPING_PATH)
    # A REAL pinned (condition_id, token_id) whose fixture is in the pinned cp1 set.
    rec = next(r for r in records if r.fixture_id in CP1_18)

    # Decoder-shaped row: REAL token_id, but condition_id="" (as the ABI decode leaves it).
    raw_row = NormalizedTradeRow(
        ts=1, price=0.5, size=2.0, aggressor_side=AggressorSide.BUY,
        condition_id="", token_id=rec.token_id,
        block_number=100, tx_hash="0xabc", log_index=3,
    )
    artifact = build_trade_artifact(
        [raw_row],
        records=[{"token_id": rec.token_id, "condition_id": rec.condition_id}],
        manifest_meta=dict(_MANIFEST_META),
    )
    # Enrichment lifted the real condition_id onto the artifact row.
    assert artifact.rows[0].condition_id == rec.condition_id

    class _Art:
        artifact_hash = artifact.artifact_hash  # pin == what the enriched artifact recomputes to

    cfg = build_maker_run_config(fixture_ids=CP1_18, trade_artifact=_Art())
    monkeypatch.setattr(runner_mod, "load_trade_artifact", lambda *a, **k: artifact)
    monkeypatch.setattr(runner_mod, "build_cp1_maker_tape", lambda *a, **k: _fake_tape())  # keep fast
    res = runner_mod.run_maker_arena(
        cfg, expected_config_hash=cfg.config_hash(),
        trade_artifact_path=Path("pinned.json"), seal=False,
    )

    assert "R1_5" in str(res.rung)
    diagnostic = res.trade_aware_diagnostic
    assert diagnostic is not None

    # The join MATCHES on real data now (was 0 before enrichment).
    assert diagnostic["rows_matched"] > 0
    assert diagnostic["rows_total"] == diagnostic["rows_matched"] + diagnostic["rows_unmatched"]

    # Per-agent report POPULATED, not INSUFFICIENT_DATA (the M1 closure).
    for report in diagnostic["per_agent"].values():
        assert report["independent_reference_verdict"] != "INSUFFICIENT_DATA"

    # STILL honest: no fill / no executable edge; every fill field absent.
    assert res.real_executable_edge_bps is None
    blob = json.dumps(diagnostic)
    for forbidden in FORBIDDEN_FILL_FIELDS:
        assert forbidden not in blob
    for report in diagnostic["per_agent"].values():
        assert report["real_executable_edge_bps"] is None


def test_diagnostic_markout_is_per_market_not_cross_market(monkeypatch):
    # M8-class cross-market FV leakage guard for the R1.5 trade-aware diagnostic: a matched
    # trade in market A must have its post-trade FV markout measured against market A's OWN
    # fv series -- NEVER the merged/other-market tape. Two markets of ONE fixture carry
    # DIFFERENT fv trajectories; the only matched trade lives in market A (away).
    from veridex.maker.mapping import DEFAULT_MAPPING_PATH, load_resolved_market_lookup

    records, _hash = load_resolved_market_lookup(DEFAULT_MAPPING_PATH)
    rec = next(
        r for r in records if r.fixture_id == CP1_18[0] and r.market_ref == "1X2|away|full"
    )

    _common = {
        "fixture_id": CP1_18[0], "tick_seq": 0,
        "txline_market_key": "1X2_PARTICIPANT_RESULT||", "staleness_s": 0,
    }

    def _tape():
        return [
            # Market A (away, MATCHED): its OWN fv rises 0.50 -> 0.55 over the window.
            {**_common, "ts": 1000, "txline_side": "part2",
             "venue_market_ref": "1X2|away|full", "venue_side": "away", "fv": 0.50, "mid": 0.50},
            {**_common, "ts": 1100, "txline_side": "part2",
             "venue_market_ref": "1X2|away|full", "venue_side": "away", "fv": 0.55, "mid": 0.50},
            # Market B (home, UNMATCHED): a DIFFERENT trajectory whose ts=1120 tick would
            # corrupt a merged-tape fv_at at the trade's fv-after horizon (1000 + window 120).
            {**_common, "ts": 1120, "txline_side": "part1",
             "venue_market_ref": "1X2|home|full", "venue_side": "home", "fv": 0.90, "mid": 0.90},
        ]

    # BUY at ts=1000 near the quote (median mid 0.50), pinned to market A's real (cond, token).
    row = _row(ts=1000, price=0.50, aggressor_side=AggressorSide.BUY,
               condition_id=rec.condition_id, token_id=rec.token_id)

    class _Art:
        artifact_hash = recompute_artifact_hash([row])

    cfg = build_maker_run_config(fixture_ids=CP1_18, trade_artifact=_Art())
    monkeypatch.setattr(runner_mod, "load_trade_artifact", lambda *a, **k: _fake_artifact(rows=[row]))
    monkeypatch.setattr(runner_mod, "build_cp1_maker_tape", lambda *a, **k: _tape())
    res = runner_mod.run_maker_arena(
        cfg, expected_config_hash=cfg.config_hash(),
        trade_artifact_path=Path("pinned.json"), seal=False,
    )

    diagnostic = res.trade_aware_diagnostic
    assert diagnostic is not None
    assert diagnostic["rows_matched"] == 1

    # PER-MARKET markout: market A's BUY, fv 0.50 -> 0.55 => +500 bps. The merged-tape fv_at
    # would leak market B's ts=1120 fv (0.90) into the fv-after, giving the WRONG +4000 bps.
    for report in diagnostic["per_agent"].values():
        assert report["post_trade_fv_markout_bps_diagnostic"] == 500

    # No-fill boundary preserved.
    assert res.real_executable_edge_bps is None
    blob = json.dumps(diagnostic)
    for forbidden in FORBIDDEN_FILL_FIELDS:
        assert forbidden not in blob
    for report in diagnostic["per_agent"].values():
        assert report["real_executable_edge_bps"] is None


def test_runner_r15_no_artifact_has_no_diagnostic_and_stays_r1(monkeypatch):
    # No predeclared artifact -> no join, diagnostic stays None, rung MM-R1 (INSUFFICIENT
    # trade data for an R1.5 claim).
    monkeypatch.setattr(runner_mod, "build_cp1_maker_tape", lambda *a, **k: _fake_tape())
    res = runner_mod.run_maker_arena(_pinned_cfg(), seal=False)
    assert res.trade_aware_diagnostic is None
    assert "R1" in str(res.rung) and "R1_5" not in str(res.rung)
