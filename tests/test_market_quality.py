from veridex.strategies.market_quality import (
    DEFAULT_MARKET_QUALITY_CONFIG as C,
)
from veridex.strategies.market_quality import (
    MarketQualityConfig,
    evaluate_market_quality,
)


def test_different_bands_produce_different_filter_config_hash():
    a = MarketQualityConfig(band_lo=0.05, band_hi=0.95, min_tick_count=30, min_horizon_s=600)
    b = MarketQualityConfig(band_lo=0.10, band_hi=0.90, min_tick_count=30, min_horizon_s=600)
    assert a.filter_config_hash() != b.filter_config_hash()
    assert a.filter_config_hash() == a.filter_config_hash()  # stable
    assert len(a.filter_config_hash()) == 64


def test_ou_0_5_near_certain_line_is_excluded_with_named_reason():
    r = evaluate_market_quality(
        market_ref="OU|0.5|full",
        implied_prob=0.985,
        tick_count=200,
        horizon_s=3600,
        mapping_valid=True,
        close_quality="priced",
        config=C,
    )
    assert r.eligible is False and r.near_certain is True and "near_certain" in r.reasons
    assert r.filter_config_hash == C.filter_config_hash()


def test_suspended_close_is_ineligible_and_surfaced_not_hidden():
    r = evaluate_market_quality(
        market_ref="OU|0.5|full",
        implied_prob=0.5,
        tick_count=200,
        horizon_s=3600,
        mapping_valid=True,
        close_quality="suspended",
        config=C,
    )
    assert r.eligible is False and "close_suspended" in r.reasons


def test_clean_1x2_market_is_eligible():
    r = evaluate_market_quality(
        market_ref="1X2|home|full",
        implied_prob=0.55,
        tick_count=200,
        horizon_s=3600,
        mapping_valid=True,
        close_quality="priced",
        config=C,
    )
    assert r.eligible is True and r.reasons == []
