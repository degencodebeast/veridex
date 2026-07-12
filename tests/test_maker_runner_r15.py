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
from veridex.maker.contracts import Side
from veridex.maker.diagnostic import FORBIDDEN_FILL_FIELDS
from veridex.maker.scorer import QuoteAccounting, QuoteMarkout
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


def _real_cp1_record(market_ref: str = "1X2|home|full"):
    """A REAL pinned cp1 mapping record for CP1_18[0] (so a trade join can MATCH it)."""
    from veridex.maker.mapping import DEFAULT_MAPPING_PATH, load_resolved_market_lookup

    records, _hash = load_resolved_market_lookup(DEFAULT_MAPPING_PATH)
    return next(
        r for r in records if r.fixture_id == CP1_18[0] and r.market_ref == market_ref
    )


def _matching_row(**kw):
    """A ``_row`` whose (condition_id, token_id) matches a REAL cp1 record -> rows_matched>0."""
    rec = _real_cp1_record()
    base = dict(condition_id=rec.condition_id, token_id=rec.token_id)
    base.update(kw)
    return _row(**base)


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


# --- E1 provenance boundary: a SYNTHETIC artifact cannot reach MM-R1.5 ------------
# Codex's exact defect: a fully hash-valid / reconciling / pinned-mapping artifact whose
# provenance manifest is HAND-AUTHORED (source="synthetic...", chain_id=999,
# contract="0xFAKE", cleanroom="not cleanroom", token_supplied_externally=False,
# fixture_count/side_count=999) wrapping REAL rows was ACCEPTED, loaded, and upgraded to
# MM-R1.5 with data_state="OK". With the provenance pin the artifact FAILS to load
# (ValidationError -> uniform MakerVoidError), so the run VOIDs BEFORE any trade I/O and
# can NEVER reach MM-R1.5 -- even though the artifact-content hash + config hash match.
def test_runner_cannot_reach_r15_with_fake_provenance(monkeypatch, tmp_path):
    import json as _json

    from veridex.maker.mapping import PINNED_MAPPING_HASH

    tape_calls = []
    monkeypatch.setattr(runner_mod, "build_cp1_maker_tape",
                        lambda *a, **k: tape_calls.append("t") or _fake_tape())

    rows = [_row()]
    h = recompute_artifact_hash(rows)  # a VALID content hash over REAL rows
    fake = dict(
        artifact_hash=h,
        raw_artifact_hash=None,
        schema_version="v1",
        decoder_version="d1",
        decoder_commit=None,
        # HAND-AUTHORED synthetic provenance (Codex's exact fake) -- not a real capture.
        source="synthetic_test_fixture_not_chain",
        chain_id=999,
        contract_address="0xFAKE",
        event_signature="Synthetic(uint256)",
        from_block=1,
        to_block=2,
        reorg_buffer_confs=20,
        capture_ts=1,
        capture_tool_id="t1",
        provider_id="hs-prod",
        token_supplied_externally=False,
        rows_decoded=len(rows),
        rows_matched_cp1=len(rows),
        rows_unmatched=0,
        rows_malformed=0,
        rows_duplicate_dropped=0,
        mapping_content_hash=PINNED_MAPPING_HASH,
        fixture_count=999,
        side_count=999,
        cleanroom_attestation="not cleanroom",
        rows=[r.model_dump(mode="json") for r in rows],
    )
    path = tmp_path / "synthetic-artifact.json"
    path.write_text(_json.dumps(fake))

    class _Art:  # the predeclared artifact IDENTITY = the (valid) content hash
        artifact_hash = h

    cfg = build_maker_run_config(fixture_ids=CP1_18, trade_artifact=_Art())
    # config-hash pin PASSES and artifact-content hash MATCHES -- the ONLY thing that can
    # stop the run reaching MM-R1.5 is the provenance pin at load.
    with pytest.raises(MakerVoidError):
        runner_mod.run_maker_arena(cfg, expected_config_hash=cfg.config_hash(),
                                   trade_artifact_path=path, seal=False)
    assert tape_calls == []   # VOID at load, BEFORE any trade I/O -> never reaches MM-R1.5


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
    # A REAL cp1-matching row so the join yields rows_matched>0 -> a genuine MM-R1.5 claim
    # (a hash-valid artifact with ZERO matched trades stays MM-R1; see M1 test below).
    matched = [_matching_row()]

    class _Art:
        artifact_hash = recompute_artifact_hash(matched)  # pin == what the artifact recomputes to

    cfg = build_maker_run_config(fixture_ids=CP1_18, trade_artifact=_Art())
    monkeypatch.setattr(runner_mod, "load_trade_artifact", lambda *a, **k: _fake_artifact(rows=matched))
    monkeypatch.setattr(runner_mod, "build_cp1_maker_tape", lambda *a, **k: _fake_tape())  # keep fast
    res = runner_mod.run_maker_arena(cfg, expected_config_hash=cfg.config_hash(),
                                     trade_artifact_path=Path("pinned.json"), seal=False)
    assert "R1_5" in str(res.rung) and res.real_executable_edge_bps is None
    r1 = runner_mod.run_maker_arena(_pinned_cfg(), seal=False)  # no artifact -> MM-R1
    assert "R1" in str(r1.rung) and "R1_5" not in str(r1.rung)


# --- E4-T4: real-artifact join -> trade-aware diagnostic, no-fill, full accounting ----
def test_runner_r15_joins_real_artifact_and_stays_no_fill(monkeypatch):
    # A REAL cp1-matching row so rows_matched>0 and the run earns MM-R1.5 (M1: a claimed
    # MM-R1.5 must be backed by at least one real matched trade, not a mere pinned artifact).
    matched = [_matching_row()]

    class _Art:
        artifact_hash = recompute_artifact_hash(matched)  # pin == what the artifact recomputes to

    cfg = build_maker_run_config(fixture_ids=CP1_18, trade_artifact=_Art())
    monkeypatch.setattr(runner_mod, "load_trade_artifact", lambda *a, **k: _fake_artifact(rows=matched))
    monkeypatch.setattr(runner_mod, "build_cp1_maker_tape", lambda *a, **k: _fake_tape())
    res = runner_mod.run_maker_arena(cfg, expected_config_hash=cfg.config_hash(),
                                     trade_artifact_path=Path("pinned.json"), seal=False)

    assert "R1_5" in str(res.rung)                 # verified matched trade artifact -> MM-R1.5
    diagnostic = res.trade_aware_diagnostic
    assert diagnostic is not None                  # the join produced a diagnostic container
    assert diagnostic["data_state"] == "REAL_TRADES"   # a real matched trade informed it
    assert diagnostic["rows_matched"] == 1

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
    # A REAL pinned (condition_id, token_id) for CP1_18[0]'s HOME market (the market present in
    # `_fake_tape`), so the enriched join matches AND the trade sits near that market's OWN
    # median mid (0.58) under the per-market near-quote reference.
    rec = next(
        r for r in records if r.fixture_id == CP1_18[0] and r.market_ref == "1X2|home|full"
    )

    # Decoder-shaped row: REAL token_id, but condition_id="" (as the ABI decode leaves it).
    # Price 0.58 == the home market's median mid in `_fake_tape` -> near the per-market quote.
    raw_row = NormalizedTradeRow(
        ts=1, price=0.58, size=2.0, aggressor_side=AggressorSide.BUY,
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


# --- M1 (honesty, AC-103/§4.3): a pinned artifact with ZERO cp1-matching trades must
# stay MM-R1 / INSUFFICIENT_DATA -- the tape-only convergence disjunct must NOT vouch for
# trade-data adequacy, and the rung must NOT upgrade to R1.5 on an empty/synthetic join.
def test_zero_matched_trades_is_insufficient_data_and_stays_r1(monkeypatch):
    monkeypatch.setattr(runner_mod, "build_cp1_maker_tape", lambda *a, **k: _fake_tape())

    # (i) A provenance-/hash-valid artifact whose row matches NO pinned cp1
    # (condition_id, token_id) (the default _row() uses condition_id="0xc", token_id="42").
    # Convergence over the fv/mid tape is non-None, but that is NOT a trade signal:
    # rows_matched==0 -> data_state INSUFFICIENT_DATA, rung stays MM-R1.
    nonmatching = [_row()]

    class _ArtNoMatch:
        artifact_hash = recompute_artifact_hash(nonmatching)

    cfg = build_maker_run_config(fixture_ids=CP1_18, trade_artifact=_ArtNoMatch())
    monkeypatch.setattr(runner_mod, "load_trade_artifact",
                        lambda *a, **k: _fake_artifact(rows=nonmatching))
    res = runner_mod.run_maker_arena(cfg, expected_config_hash=cfg.config_hash(),
                                     trade_artifact_path=Path("pinned.json"), seal=False)
    assert "R1" in str(res.rung) and "R1_5" not in str(res.rung)
    assert res.trade_aware_diagnostic is not None
    assert res.trade_aware_diagnostic["data_state"] == "INSUFFICIENT_DATA"
    assert res.trade_aware_diagnostic["rows_matched"] == 0
    assert res.real_executable_edge_bps is None

    # (ii) An EMPTY-rows artifact: no trade at all -> same MM-R1 / INSUFFICIENT_DATA.
    empty: list = []

    class _ArtEmpty:
        artifact_hash = recompute_artifact_hash(empty)

    cfg2 = build_maker_run_config(fixture_ids=CP1_18, trade_artifact=_ArtEmpty())
    monkeypatch.setattr(runner_mod, "load_trade_artifact",
                        lambda *a, **k: _fake_artifact(rows=empty))
    res2 = runner_mod.run_maker_arena(cfg2, expected_config_hash=cfg2.config_hash(),
                                      trade_artifact_path=Path("pinned.json"), seal=False)
    assert "R1" in str(res2.rung) and "R1_5" not in str(res2.rung)
    assert res2.trade_aware_diagnostic["data_state"] == "INSUFFICIENT_DATA"
    assert res2.trade_aware_diagnostic["rows_matched"] == 0
    assert res2.real_executable_edge_bps is None


# --- m1 (per-market near-quote reference): the "near the quote" band must be measured
# against EACH market's OWN median mid, never a single tape-wide median pooled across
# markets trading at different price levels.
def test_near_quote_reference_is_per_market_not_global(monkeypatch):
    home = _real_cp1_record("1X2|home|full")
    away = _real_cp1_record("1X2|away|full")

    # Home market trades ~0.85; away market trades ~0.15. A global pooled median would land
    # at one level and wrongly exclude the other market's near-quote trade.
    rows = [
        _row(ts=1000, price=0.85, aggressor_side=AggressorSide.BUY,
             condition_id=home.condition_id, token_id=home.token_id),
        _row(ts=1000, price=0.15, aggressor_side=AggressorSide.BUY,
             condition_id=away.condition_id, token_id=away.token_id),
    ]

    _common = {"fixture_id": CP1_18[0], "tick_seq": 0,
               "txline_market_key": "1X2_PARTICIPANT_RESULT||", "staleness_s": 0}

    def _tape():
        return [
            {**_common, "ts": 1000, "txline_side": "part1",
             "venue_market_ref": "1X2|home|full", "venue_side": "home", "fv": 0.85, "mid": 0.85},
            {**_common, "ts": 1100, "txline_side": "part1",
             "venue_market_ref": "1X2|home|full", "venue_side": "home", "fv": 0.85, "mid": 0.85},
            {**_common, "ts": 1000, "txline_side": "part2",
             "venue_market_ref": "1X2|away|full", "venue_side": "away", "fv": 0.15, "mid": 0.15},
            {**_common, "ts": 1100, "txline_side": "part2",
             "venue_market_ref": "1X2|away|full", "venue_side": "away", "fv": 0.15, "mid": 0.15},
        ]

    class _Art:
        artifact_hash = recompute_artifact_hash(rows)

    cfg = build_maker_run_config(fixture_ids=CP1_18, trade_artifact=_Art())
    monkeypatch.setattr(runner_mod, "load_trade_artifact", lambda *a, **k: _fake_artifact(rows=rows))
    monkeypatch.setattr(runner_mod, "build_cp1_maker_tape", lambda *a, **k: _tape())
    res = runner_mod.run_maker_arena(cfg, expected_config_hash=cfg.config_hash(),
                                     trade_artifact_path=Path("pinned.json"), seal=False)

    diagnostic = res.trade_aware_diagnostic
    assert diagnostic is not None
    assert diagnostic["rows_matched"] == 2
    # Per-market: BOTH trades sit near their OWN market's median mid -> near-count 2. A single
    # global median (~0.85) would exclude the away trade at 0.15 (wrongly counting only 1).
    for report in diagnostic["per_agent"].values():
        assert report["trades_near_quote_count"] == 2


# --- Codex Gate-#2 Major: candidate/naive toxicity loss must be a MEAN, not a
# cumulative SUM (E4 diagnostic honesty) ------------------------------------------
def _fixed_mark(markout_bps: int, *, market_key: str = "1X2|home|full") -> QuoteMarkout:
    """A ``QuoteMarkout`` with an explicit ``markout_bps``, everything else fixed."""
    return QuoteMarkout(
        fixture_id=0, tick_seq=0, side=Side.BID, market_key=market_key,
        horizon_s=30, markout_bps=markout_bps,
    )


def test_candidate_vs_naive_delta_is_mean_not_cumulative(monkeypatch):
    # `_build_trade_aware_diagnostic` derives `candidate_toxicity_loss_bps` /
    # `naive_toxicity_loss_bps` from `quality_by` (built by `_score_group` in the R1
    # scoring loop, one call per (market group, agent)). Monkeypatching `_score_group`
    # gives full control over each agent's per-quote toxicity values -- UNEQUAL scored
    # counts between naive (2 marks) and candidate (4 marks) so the cumulative-SUM delta
    # and the MEAN delta diverge numerically (not just by a common scalar factor):
    #   naive:     2 marks x toxicity 100  -> sum 200, mean 100
    #   candidate: 4 marks x toxicity  40  -> sum 160, mean  40
    #   sum-delta  = 160 - 200 = -40  (WRONG: scales with quote count)
    #   mean-delta =  40 - 100 = -60  (RIGHT: matches scorer.avg_toxicity_loss_bps's axis)
    def _fake_score_group(agent, rows, ref_at, horizons_s):
        if agent.agent_id == "naive-mm":
            marks = [_fixed_mark(-100)]                      # 1/group x 2 groups = 2 marks
        else:
            marks = [_fixed_mark(-40), _fixed_mark(-40)]      # 2/group x 2 groups = 4 marks
        return marks, QuoteAccounting(scored=len(marks), abstained=0)

    monkeypatch.setattr(runner_mod, "_score_group", _fake_score_group)

    class _Art:
        artifact_hash = recompute_artifact_hash([_row()])

    cfg = build_maker_run_config(fixture_ids=CP1_18, trade_artifact=_Art())
    monkeypatch.setattr(runner_mod, "load_trade_artifact", lambda *a, **k: _fake_artifact(rows=[_row()]))
    monkeypatch.setattr(runner_mod, "build_cp1_maker_tape", lambda *a, **k: _fake_tape())
    res = runner_mod.run_maker_arena(
        cfg, expected_config_hash=cfg.config_hash(),
        trade_artifact_path=Path("pinned.json"), seal=False,
    )

    diagnostic = res.trade_aware_diagnostic
    assert diagnostic is not None

    naive_toxicity = [100, 100]
    candidate_toxicity = [40, 40, 40, 40]
    expected_naive_mean = round(sum(naive_toxicity) / len(naive_toxicity))          # 100
    expected_candidate_mean = round(sum(candidate_toxicity) / len(candidate_toxicity))  # 40
    expected_mean_delta = expected_candidate_mean - expected_naive_mean              # -60
    wrong_cumulative_delta = sum(candidate_toxicity) - sum(naive_toxicity)           # -40

    for report in diagnostic["per_agent"].values():
        delta = report["candidate_vs_naive_toxicity_delta_bps_diagnostic"]
        assert delta == expected_mean_delta, (
            f"expected MEAN-loss delta {expected_mean_delta}, got {delta} "
            f"(a cumulative-sum delta would wrongly read {wrong_cumulative_delta})"
        )
        assert delta != wrong_cumulative_delta

    # No-fill boundary preserved.
    assert res.real_executable_edge_bps is None
    blob = json.dumps(diagnostic)
    for forbidden in FORBIDDEN_FILL_FIELDS:
        assert forbidden not in blob


def test_candidate_vs_naive_delta_none_when_one_side_has_no_scored_quotes(monkeypatch):
    # If either agent has zero scored quotes, the mean is undefined for that side ->
    # loss_by[agent] must be None, and the delta (a pure subtraction) must therefore
    # also be None -- never a fabricated comparison from a missing operand.
    def _fake_score_group(agent, rows, ref_at, horizons_s):
        if agent.agent_id == "naive-mm":
            return [], QuoteAccounting(scored=0, abstained=1)
        return [_fixed_mark(-40)], QuoteAccounting(scored=1, abstained=0)

    monkeypatch.setattr(runner_mod, "_score_group", _fake_score_group)

    class _Art:
        artifact_hash = recompute_artifact_hash([_row()])

    cfg = build_maker_run_config(fixture_ids=CP1_18, trade_artifact=_Art())
    monkeypatch.setattr(runner_mod, "load_trade_artifact", lambda *a, **k: _fake_artifact(rows=[_row()]))
    monkeypatch.setattr(runner_mod, "build_cp1_maker_tape", lambda *a, **k: _fake_tape())
    res = runner_mod.run_maker_arena(
        cfg, expected_config_hash=cfg.config_hash(),
        trade_artifact_path=Path("pinned.json"), seal=False,
    )

    diagnostic = res.trade_aware_diagnostic
    assert diagnostic is not None
    for report in diagnostic["per_agent"].values():
        assert report["candidate_vs_naive_toxicity_delta_bps_diagnostic"] is None
