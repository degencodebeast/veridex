"""Merkle-root forest — per-domain sealing of competition records."""

from __future__ import annotations

from veridex.chain.merkle import (
    EMPTY_ROOT,
    build_root_forest,
    domain_root,
    leaf_hash,
    merkle_root,
)


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


def test_forest_has_six_domains_and_reserved_payout() -> None:
    forest = build_root_forest(
        event_log=[{"seq": 0}, {"seq": 1}],
        score_rows=[{"agent_id": "a"}],
        receipts=[],
        policy_results=[{"decision": "approved"}],
        competition=[{"competition_id": "c"}],
    )
    assert set(forest) == {"event_log", "score", "receipt", "policy", "competition", "payout_reserved"}
    assert forest["receipt"] == EMPTY_ROOT  # no receipts → sentinel
    assert forest["payout_reserved"] == EMPTY_ROOT  # 2D reserved, always empty in Plan A
    assert len(forest["event_log"]) == 64


def test_forest_binds_into_manifest() -> None:
    import asyncio

    from tests._arena_fixtures import _beta_agent, _ticks
    from veridex.runtime.competition import run_demo_competition
    from veridex.runtime.orchestrator import deterministic_agent

    result = asyncio.run(
        run_demo_competition(_ticks(), [deterministic_agent("a"), _beta_agent()], anchor_fn=None, run_id="fixed")
    )
    assert "root_forest" in result.manifest
    assert set(result.manifest["root_forest"]) == {
        "event_log",
        "score",
        "receipt",
        "policy",
        "competition",
        "payout_reserved",
    }
