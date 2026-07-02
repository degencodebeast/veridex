"""WD-1 — Solana explorer URL for the anchored Memo tx."""

from __future__ import annotations

from veridex.chain.anchor import explorer_tx_url


def test_explorer_url_for_devnet() -> None:
    url = explorer_tx_url("5sig", cluster="devnet")
    assert url == "https://explorer.solana.com/tx/5sig?cluster=devnet"


def test_explorer_url_for_mainnet_has_no_cluster_query() -> None:
    url = explorer_tx_url("5sig", cluster="mainnet-beta")
    assert url == "https://explorer.solana.com/tx/5sig"


def test_explorer_url_none_when_unanchored() -> None:
    assert explorer_tx_url(None, cluster="devnet") is None
