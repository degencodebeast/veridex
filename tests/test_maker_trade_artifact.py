"""MM-R1.5 TradeArtifact provenance-layer tests (no-fill boundary).

Covers the normalized-row contract, the artifact hash over economic + chain-event
identity, the manifest reconciliation / pinned-mapping / secret-hygiene validators,
and duplicate dedup keyed on event identity.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from veridex.maker.mapping import PINNED_MAPPING_HASH
from veridex.maker.trade_artifact import (
    PINNED_CHAIN_ID,
    PINNED_CLEANROOM_ATTESTATION,
    PINNED_CONTRACT_ADDRESS,
    PINNED_EVENT_SIGNATURE,
    PINNED_FIXTURE_COUNT,
    PINNED_SIDE_COUNT,
    PINNED_SOURCE,
    NormalizedTradeRow,
    TradeArtifact,
    dedup_normalized_rows,
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
    with pytest.raises(ValidationError):
        _row(fill_price=0.5)  # extra="forbid"
    # price=1.4 raises MarkoutError inside the field_validator, which pydantic wraps
    # into a ValidationError at construction -- so the observable type here is
    # ValidationError, not the raw MarkoutError.
    with pytest.raises(ValidationError):
        _row(price=1.4)  # decimal rejected


def test_artifact_hash_covers_event_identity():
    # two row-sets identical except log_index must hash differently
    a = recompute_artifact_hash([_row(log_index=3)])
    b = recompute_artifact_hash([_row(log_index=4)])
    assert a != b
    c = recompute_artifact_hash([_row(price=0.6)])
    assert c != a  # economic field also covered


def test_artifact_hash_is_order_independent():
    # Two rows that TIE on (block_number, log_index) but differ in tx_hash (the rest
    # of the identity) must not let input order change the digest: the sort key must
    # be a TOTAL order matching identity (block_number, log_index, tx_hash), else a
    # stable sort leaks file order into the trust-load-bearing hash.
    r1 = _row(tx_hash="0xAAA", log_index=7, block_number=200, price=0.5)
    r2 = _row(tx_hash="0xBBB", log_index=7, block_number=200, price=0.6)
    assert recompute_artifact_hash([r1, r2]) == recompute_artifact_hash([r2, r1])


def test_artifact_hash_order_independent_same_event_key():
    # Two rows sharing the FULL partial sort key (block_number, log_index, tx_hash)
    # but differing in economics (price/size) tie under that partial key, so a stable
    # sort leaks input order into the trust-load-bearing digest. The sort MUST key on
    # each row's full canonical content, which is total over any distinct rows.
    r1 = _row(tx_hash="0xabc", log_index=3, block_number=100, price=0.4, size=1.0)
    r2 = _row(tx_hash="0xabc", log_index=3, block_number=100, price=0.7, size=9.0)
    assert recompute_artifact_hash([r1, r2]) == recompute_artifact_hash([r2, r1])


def _artifact(rows, **kw):
    h = recompute_artifact_hash(rows)
    base = dict(
        artifact_hash=h,
        raw_artifact_hash=None,
        schema_version="v1",
        decoder_version="d1",
        decoder_commit=None,
        source=PINNED_SOURCE,
        chain_id=PINNED_CHAIN_ID,
        contract_address=PINNED_CONTRACT_ADDRESS,
        event_signature=PINNED_EVENT_SIGNATURE,
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
        fixture_count=PINNED_FIXTURE_COUNT,
        side_count=PINNED_SIDE_COUNT,
        cleanroom_attestation=PINNED_CLEANROOM_ATTESTATION,
        rows=tuple(rows),
    )
    base.update(kw)
    return TradeArtifact(**base)


def test_trade_artifact_reconciles_and_forbids_token():
    a = _artifact([_row()])
    assert a.artifact_hash == recompute_artifact_hash([_row()])
    with pytest.raises(ValidationError):
        _artifact([_row()], artifact_hash="deadbeef")  # hash mismatch
    with pytest.raises(ValidationError):
        _artifact([_row()], rows_unmatched=5)  # reconciliation fails
    with pytest.raises(ValidationError):
        _artifact([_row()], mapping_content_hash="nope")  # mapping not pinned
    _artifact([_row()])  # token_supplied_externally=True is ALLOWED
    with pytest.raises(ValidationError):
        _artifact([_row()], hypersync_api="secret")  # secret-bearing key forbidden
    with pytest.raises(ValidationError):
        _artifact([_row()], api_key="AKIA...")  # secret-bearing key forbidden


def test_trade_artifact_rejects_fake_provenance():
    # Codex's exact synthetic artifact: REAL, hash-valid, reconciling, pinned-mapping
    # rows wrapped in a HAND-AUTHORED provenance manifest that does NOT describe a real
    # Polymarket OrderFilled capture. A hash pin proves the bytes did not drift; it does
    # NOT prove the bytes are the required data source. The provenance pin must reject it.
    with pytest.raises(ValidationError):
        _artifact(
            [_row()],
            source="synthetic_test_fixture_not_chain",
            chain_id=999,
            contract_address="0xFAKE",
            event_signature="Synthetic(uint256)",
            cleanroom_attestation="not cleanroom",
            token_supplied_externally=False,
            fixture_count=999,
            side_count=999,
        )


@pytest.mark.parametrize(
    "fake_field",
    [
        {"source": "synthetic_test_fixture_not_chain"},
        {"chain_id": 999},
        {"contract_address": "0xFAKE"},
        {"event_signature": "Synthetic(uint256)"},
        {"token_supplied_externally": False},
        {"fixture_count": 999},
        {"side_count": 54 + 1},
        {"fixture_count": 17},
        {"cleanroom_attestation": "not cleanroom"},
    ],
)
def test_trade_artifact_rejects_each_single_fake_provenance_field(fake_field):
    # Each claim-bearing provenance field is individually pinned: flipping ANY one of
    # them (leaving the rest genuine + the hash/reconciliation/mapping valid) must
    # reject. A hash-valid artifact is necessary but NOT sufficient to claim R1.5.
    with pytest.raises(ValidationError):
        _artifact([_row()], **fake_field)


def test_trade_artifact_rejects_duplicate_event_keys():
    # A single (tx_hash, log_index) identifies exactly one on-chain log; two rows
    # for it (differing economics) is an integrity violation and risks E4
    # double-counting one on-chain trade. rows_decoded reconciles (2 matched) so
    # only the uniqueness validator can reject it.
    r1 = _row(tx_hash="0xabc", log_index=3, price=0.4, size=1.0)
    r2 = _row(tx_hash="0xabc", log_index=3, price=0.7, size=9.0)
    with pytest.raises(ValidationError):
        _artifact([r1, r2])


def test_dedup_by_event_key():
    # two rows sharing (tx_hash, log_index) → one kept, dropped == 1
    unique, dropped = dedup_normalized_rows([_row(), _row()])
    assert len(unique) == 1 and dropped == 1
    # different log_index → both kept, dropped == 0
    unique2, dropped2 = dedup_normalized_rows([_row(log_index=3), _row(log_index=4)])
    assert len(unique2) == 2 and dropped2 == 0
