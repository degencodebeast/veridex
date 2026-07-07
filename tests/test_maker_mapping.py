from veridex.maker.mapping import (
    load_resolved_market_lookup, recompute_records_hash,
    PINNED_MAPPING_HASH, DEFAULT_MAPPING_PATH,
)

def test_recomputed_hash_matches_pinned_and_is_records_only():
    records, recomputed = load_resolved_market_lookup(DEFAULT_MAPPING_PATH)
    assert len(records) == 54                       # 18 fixtures x 3 sides
    assert recomputed == PINNED_MAPPING_HASH         # canonical records-only sha256
    assert len({r.fixture_id for r in records}) == 18

def test_recompute_is_over_sorted_records_not_whole_file():
    records, _ = load_resolved_market_lookup(DEFAULT_MAPPING_PATH)
    reversed_records = [r.model_dump() for r in reversed(records)]
    assert recompute_records_hash(reversed_records) == PINNED_MAPPING_HASH
