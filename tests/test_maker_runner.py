import inspect, pytest
import veridex.maker.runner as runner_mod
from veridex.maker.runner import run_maker_arena
from veridex.maker.config import build_maker_run_config, MakerVoidError

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
