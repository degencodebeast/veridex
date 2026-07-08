"""MM-R1.5 TradeArtifact provenance-layer tests (no-fill boundary).

Covers the normalized-row contract, the artifact hash over economic + chain-event
identity, the manifest reconciliation / pinned-mapping / secret-hygiene validators,
and duplicate dedup keyed on event identity.
"""

from __future__ import annotations

import pytest

from veridex.maker.mapping import PINNED_MAPPING_HASH
from veridex.maker.trade_artifact import (
    NormalizedTradeRow,
    TradeArtifact,
    recompute_artifact_hash,
)
from veridex.maker.trades import AggressorSide


def _row(**kw):
    base = dict(
        ts=1,
        price=0.5,
        size=2.0,
        aggressor_side=AggressorSide.BUY,
        condition_id="0xc",
        token_id="42",
        block_number=100,
        tx_hash="0xabc",
        log_index=3,
    )
    base.update(kw)
    return NormalizedTradeRow(**base)


def test_normalized_row_carries_event_identity_and_no_fill_fields():
    r = _row()
    assert r.event_key() == ("0xabc", 3)
    assert r.block_number == 100 and r.tx_hash == "0xabc" and r.log_index == 3
    with pytest.raises(Exception):
        _row(fill_price=0.5)  # extra="forbid"
    with pytest.raises(Exception):
        _row(price=1.4)  # decimal rejected


def test_artifact_hash_covers_event_identity():
    # two row-sets identical except log_index must hash differently
    a = recompute_artifact_hash([_row(log_index=3)])
    b = recompute_artifact_hash([_row(log_index=4)])
    assert a != b
    c = recompute_artifact_hash([_row(price=0.6)])
    assert c != a  # economic field also covered


def _artifact(rows, **kw):
    h = recompute_artifact_hash(rows)
    base = dict(
        artifact_hash=h,
        raw_artifact_hash=None,
        schema_version="v1",
        decoder_version="d1",
        decoder_commit=None,
        source="polymarket_ctf_exchange_v2_orderfilled",
        chain_id=137,
        contract_address="0xe11...",
        event_signature="OrderFilled(...)",
        from_block=1,
        to_block=2,
        reorg_buffer_confs=20,
        capture_ts=1,
        capture_tool_id="t1",
        provider_id="hs-prod",
        token_supplied_externally=True,
        rows_decoded=len(rows),
        rows_matched_cp1=len(rows),
        rows_unmatched=0,
        rows_malformed=0,
        rows_duplicate_dropped=0,
        mapping_content_hash=PINNED_MAPPING_HASH,
        fixture_count=18,
        side_count=54,
        cleanroom_attestation="clean-room; no GPL copied",
        rows=tuple(rows),
    )
    base.update(kw)
    return TradeArtifact(**base)


def test_trade_artifact_reconciles_and_forbids_token():
    a = _artifact([_row()])
    assert a.artifact_hash == recompute_artifact_hash([_row()])
    with pytest.raises(Exception):
        _artifact([_row()], artifact_hash="deadbeef")  # hash mismatch
    with pytest.raises(Exception):
        _artifact([_row()], rows_unmatched=5)  # reconciliation fails
    with pytest.raises(Exception):
        _artifact([_row()], mapping_content_hash="nope")  # mapping not pinned
    _artifact([_row()])  # token_supplied_externally=True is ALLOWED
    with pytest.raises(Exception):
        _artifact([_row()], hypersync_api="secret")  # secret-bearing key forbidden
    with pytest.raises(Exception):
        _artifact([_row()], api_key="AKIA...")  # secret-bearing key forbidden
