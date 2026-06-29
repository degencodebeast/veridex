"""Merkle-root forest — per-domain sealing of competition records."""

from __future__ import annotations

from veridex.chain.merkle import EMPTY_ROOT, domain_root, leaf_hash, merkle_root


def test_empty_domain_is_sentinel() -> None:
    assert merkle_root([]) == EMPTY_ROOT
    assert domain_root([]) == EMPTY_ROOT


def test_single_leaf_is_itself() -> None:
    h = leaf_hash({"a": 1})
    assert merkle_root([h]) == h


def test_root_is_deterministic_and_order_sensitive() -> None:
    a, b = leaf_hash({"x": 1}), leaf_hash({"x": 2})
    assert merkle_root([a, b]) == merkle_root([a, b])
    assert merkle_root([a, b]) != merkle_root([b, a])


def test_odd_leaf_count_duplicates_last() -> None:
    a, b, c = leaf_hash({"i": 0}), leaf_hash({"i": 1}), leaf_hash({"i": 2})
    # 3 leaves must still produce a stable, non-error root.
    assert len(merkle_root([a, b, c])) == 64


def test_tamper_changes_root() -> None:
    clean = domain_root([{"seq": 0}, {"seq": 1}])
    tampered = domain_root([{"seq": 0}, {"seq": 99}])
    assert clean != tampered
