import inspect, pytest
import veridex.maker.runner as runner_mod
from veridex.maker.runner import run_maker_arena
from veridex.maker.config import build_maker_run_config, MakerVoidError
from veridex.maker.contracts import Side

CP1_18 = (17588229,17588234,17588245,17588325,17588391,17588404,17926593,18167317,
          18172280,18172469,18175918,18175981,18175983,18176123,18179550,18179551,18179759,18179763)

def _pinned_cfg():
    return build_maker_run_config(fixture_ids=CP1_18)

def _fake_tape():
    return [{"ts": 0, "fixture_id": CP1_18[0], "tick_seq": 0,
             "txline_market_key": "1X2_PARTICIPANT_RESULT||", "txline_side": "part1",
             "venue_market_ref": "1X2|home|full", "venue_side": "home",
             "fv": 0.60, "mid": 0.58, "staleness_s": 0}]

def test_drifted_config_voids_before_any_mapping_or_tape_load(monkeypatch):
    calls = []
    monkeypatch.setattr(runner_mod, "load_resolved_market_lookup", lambda *a, **k: calls.append("map") or ([], ""))
    monkeypatch.setattr(runner_mod, "build_cp1_maker_tape", lambda *a, **k: calls.append("tape") or [])
    bad = build_maker_run_config(fixture_ids=CP1_18, markout_horizons_s=(1, 2, 3))
    with pytest.raises(MakerVoidError):
        run_maker_arena(bad, seal=False)
    assert calls == []                                   # verify VOIDed before any mapping/tape I/O

def test_valid_path_verifies_strictly_before_loading(monkeypatch):
    order = []
    real_verify = runner_mod.verify_pinned
    monkeypatch.setattr(runner_mod, "verify_pinned", lambda *a, **k: order.append("verify") or real_verify(*a, **k))
    monkeypatch.setattr(runner_mod, "build_cp1_maker_tape", lambda *a, **k: order.append("tape") or _fake_tape())
    run_maker_arena(_pinned_cfg(), seal=False)
    assert order[0] == "verify" and "tape" in order      # verify strictly precedes tape load

def test_seal_false_writes_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "RESULT_PATH", tmp_path / "maker-result.json", raising=False)
    monkeypatch.setattr(runner_mod, "build_cp1_maker_tape", lambda *a, **k: _fake_tape())
    res = run_maker_arena(_pinned_cfg(), seal=False)
    assert res.real_executable_edge_bps is None and res.fixture_universe_n == 1  # _fake_tape has 1 fixture
    assert not (tmp_path / "maker-result.json").exists()

def test_runner_has_no_network_or_gamma_import():
    src = inspect.getsource(runner_mod)
    for banned in ("httpx", "requests", "gamma", "resolver", "aiohttp"):
        assert banned not in src.lower()

def test_runner_voids_when_cfg_fixtures_disagree_with_mapping(monkeypatch):
    # cfg passes verify (CP1_18) and the mapping-hash check (monkeypatched to return the matching hash),
    # but the mapping's records carry a NON-canonical fixture -> the cross-check must VOID.
    from veridex.maker.mapping import ResolvedMarketRecord
    cfg = _pinned_cfg()
    bad_records = [ResolvedMarketRecord(condition_id="0xX", fixture_id=99999999, frame_rows=1,
        market_ref="1X2|home|full", side="home", source_artifact_content_hash=None,
        source_frames_file="f", token_id="1", venue="polymarket")]
    monkeypatch.setattr(runner_mod, "load_resolved_market_lookup",
                        lambda *a, **k: (bad_records, cfg.mapping_content_hash))
    monkeypatch.setattr(runner_mod, "build_cp1_maker_tape", lambda *a, **k: _fake_tape())
    with pytest.raises(MakerVoidError):
        run_maker_arena(cfg, seal=False)

def test_tape_has_no_network_or_gamma_import():
    import inspect, veridex.maker.tape as tape_mod
    src = inspect.getsource(tape_mod)
    for banned in ("httpx", "requests", "gamma", "resolver", "aiohttp"):
        assert banned not in src.lower()

def test_scoring_is_per_market_not_cross_fixture(monkeypatch):
    # Two fixtures whose fv series would give opposite markouts if pooled. Each quote must be scored
    # against its OWN fixture's future fv. We assert the run completes and reports both fixtures,
    # and (indirectly) that no cross-fixture contamination crashes or collapses the universe.
    def _two_fixture_tape(*a, **k):
        rows = []
        for fid, fv0, fv1 in ((111, 0.40, 0.60), (222, 0.60, 0.40)):
            rows.append({"ts": 0, "fixture_id": fid, "tick_seq": 0,
                         "txline_market_key": "1X2_PARTICIPANT_RESULT||", "txline_side": "part1",
                         "venue_market_ref": "1X2|home|full", "venue_side": "home",
                         "fv": fv0, "mid": 0.50, "staleness_s": 0})
            rows.append({"ts": 60, "fixture_id": fid, "tick_seq": 1,
                         "txline_market_key": "1X2_PARTICIPANT_RESULT||", "txline_side": "part1",
                         "venue_market_ref": "1X2|home|full", "venue_side": "home",
                         "fv": fv1, "mid": 0.50, "staleness_s": 0})
        return rows
    monkeypatch.setattr(runner_mod, "build_cp1_maker_tape", _two_fixture_tape)
    res = run_maker_arena(_pinned_cfg(), seal=False)
    assert res.fixture_universe_n == 2          # both fixtures represented
    assert res.real_executable_edge_bps is None
    # the run must not raise despite the same venue_market_ref appearing in two fixtures


def test_markout_ref_uses_full_fv_series_even_when_future_mid_is_stale(monkeypatch):
    # DEFECT (Codex M8): the markout REFERENCE fv series must be the FULL observed
    # TxLINE fv for the market (fv exists at every tick), independent of whether the
    # venue mid is fresh. The buggy code built ref_at from only the mid-present ("live")
    # rows, so a FUTURE tick whose venue mid is stale (mid=None) had its real future fv
    # dropped -- ref_at(ts+h) then silently fell back to an OLDER fv, corrupting the markout.
    #
    # Build ONE (fixture, venue_market_ref) group with a live row at ts=0 (fv=0.50, mid=0.50)
    # and a FUTURE row at ts=30 whose fv moved to 0.80 but whose venue mid is STALE (None).
    fid = CP1_18[0]
    base = {"fixture_id": fid, "txline_market_key": "1X2_PARTICIPANT_RESULT||",
            "txline_side": "part1", "venue_market_ref": "1X2|home|full", "venue_side": "home"}
    def _tape_with_stale_future_mid(*a, **k):
        return [
            {**base, "tick_seq": 0, "ts": 0,  "fv": 0.50, "mid": 0.50, "staleness_s": 0},
            {**base, "tick_seq": 1, "ts": 30, "fv": 0.80, "mid": None, "staleness_s": None},
        ]
    monkeypatch.setattr(runner_mod, "build_cp1_maker_tape", _tape_with_stale_future_mid)

    # Spy on the scorer to observe the reference series the markout actually consumes.
    # (The two-sided symmetric quote's AVERAGE markout is (ask-bid)/(2*ref_now), which
    # cancels ref_future -- so avg_markout_bps alone cannot distinguish fixed from buggy;
    # the +0.30 future move only shows up in the INDIVIDUAL bid/ask marks and in ref_at.)
    seen: dict = {"bid30": []}
    real_score = runner_mod.score_r1_markout
    def _spy(quote_sets, ref_at, horizons_s):
        marks, acc = real_score(quote_sets, ref_at, horizons_s)
        # Future reference at the stale-mid tick: 0.80 (real future fv) once fixed, but
        # the stale 0.50 fallback while buggy (the 0.80 was dropped with the mid=None row).
        seen["ref_future_at_30"] = ref_at("1X2|home|full", Side.BID, 30)
        seen["bid30"].extend(
            m.markout_bps for m in marks if m.side is Side.BID and m.horizon_s == 30
        )
        return marks, acc
    monkeypatch.setattr(runner_mod, "score_r1_markout", _spy)

    res = run_maker_arena(_pinned_cfg(), seal=False)
    cand = next(a for a in res.per_agent if a["agent_id"] == "txline-fair-mm")

    # The markout reference at the future stale-mid tick MUST be the real future fv 0.80,
    # not the 0.50 stale fallback -- proving the changed FV IS consumed by the markout.
    assert seen["ref_future_at_30"] == 0.80
    # Candidate bid @ 0.48 marked to future fv 0.80 over ref_now 0.50:
    #   (0.80 - 0.48) / 0.50 * 1e4 = 6400 bps.
    # With the bug (ref_future stalls at 0.50) this collapses to (0.50-0.48)/0.50*1e4 = 400.
    assert 6400 in seen["bid30"]
    assert 400 not in seen["bid30"]
    assert cand["avg_markout_bps"] is not None


def test_sealed_run_consumes_real_cp1_bytes(monkeypatch):
    # REAL path: do NOT patch build_cp1_maker_tape. Spy the pack loader to prove real bytes + verify=True.
    import veridex.maker.tape as tape_mod
    seen = {}
    real_loader = tape_mod.load_pack_marketstates
    def _spy(pack_dir, fixture_id, **kw):
        seen[fixture_id] = kw.get("verify", None)
        return real_loader(pack_dir, fixture_id, **kw)
    monkeypatch.setattr(tape_mod, "load_pack_marketstates", _spy)
    res = run_maker_arena(_pinned_cfg(), seal=False)
    assert seen and all(v is True for v in seen.values())          # every pack loaded with verify=True
    assert len(seen) == 18 and res.fixture_universe_n == 18        # all 18 REAL cp1 fixtures consumed
