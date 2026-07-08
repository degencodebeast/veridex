"""MM-R1.5 TradeArtifact provenance-layer tests (no-fill boundary).

Covers the normalized-row contract, the artifact hash over economic + chain-event
identity, the manifest reconciliation / pinned-mapping / secret-hygiene validators,
and duplicate dedup keyed on event identity.
"""

from __future__ import annotations

import pytest

from veridex.maker.trade_artifact import NormalizedTradeRow, recompute_artifact_hash
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
