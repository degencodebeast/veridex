import pytest
from veridex.maker.config import MakerRunConfig, verify_pinned, MakerVoidError

def _cfg(**kw):
    base = dict(fixture_ids=(17588229, 17588234), mapping_content_hash="faf4a840")
    base.update(kw); return MakerRunConfig(**base)

def test_config_hash_is_deterministic_and_input_sensitive():
    h1 = _cfg().config_hash()
    assert h1 == _cfg().config_hash()                                  # deterministic
    assert _cfg(markout_horizons_s=(30, 60)).config_hash() != h1       # horizon change moves hash
    assert _cfg(mapping_content_hash="deadbeef").config_hash() != h1   # mapping hash bound in

def test_verify_pinned_voids_on_drift_and_is_pure():
    cfg = _cfg()
    verify_pinned(cfg, cfg.config_hash())                              # matches → no raise
    with pytest.raises(MakerVoidError):
        verify_pinned(cfg, "0" * 64)                                   # drift → VOID

def test_config_is_frozen_extra_forbid():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        MakerRunConfig(fixture_ids=(1,), mapping_content_hash="x", bogus=1)
