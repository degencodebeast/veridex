from veridex.strategies.market_quality import MarketQualityConfig


def test_different_bands_produce_different_filter_config_hash():
    a = MarketQualityConfig(band_lo=0.05, band_hi=0.95, min_tick_count=30, min_horizon_s=600)
    b = MarketQualityConfig(band_lo=0.10, band_hi=0.90, min_tick_count=30, min_horizon_s=600)
    assert a.filter_config_hash() != b.filter_config_hash()
    assert a.filter_config_hash() == a.filter_config_hash()  # stable
    assert len(a.filter_config_hash()) == 64
