import pytest
from veridex.maker.config import build_maker_run_config
from veridex.maker.mapping import PINNED_MAPPING_HASH

CP1_18 = (17588229,17588234,17588245,17588325,17588391,17588404,17926593,18167317,
          18172280,18172469,18175918,18175981,18175983,18176123,18179550,18179551,18179759,18179763)

def test_build_binds_pinned_mapping_hash_into_config():
    cfg = build_maker_run_config(fixture_ids=CP1_18)
    assert cfg.mapping_content_hash == PINNED_MAPPING_HASH   # bound in
    assert len(cfg.fixture_ids) == 18                        # n=18 (AC-020)

def test_mapping_drift_changes_config_hash():
    good = build_maker_run_config(fixture_ids=CP1_18).config_hash()
    from veridex.maker.config import MakerRunConfig
    tampered = MakerRunConfig(fixture_ids=CP1_18, mapping_content_hash="deadbeef").config_hash()
    assert tampered != good

def test_non_18_universe_rejected():
    with pytest.raises(ValueError):
        build_maker_run_config(fixture_ids=CP1_18[:17])
