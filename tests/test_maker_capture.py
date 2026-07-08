"""E3 — operator-gated clean-room ``OrderFilled`` capture tests.

These tests pin the clean-room decoder (E3-T1), the offline artifact builder with
secret hygiene (E3-T2), and the fail-closed operator entrypoint (E3-T3). No test
here performs a real network call: the HyperSync client is always injected or the
entrypoint is exercised on its fail-closed path.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

import veridex.maker.capture as capture_module
from veridex.maker.capture import (
    build_trade_artifact,
    capture_order_filled_artifact,
    decode_order_filled,
)
from veridex.maker.mapping import PINNED_MAPPING_HASH
from veridex.maker.markout import MarkoutError

_MANIFEST_META = dict(
    raw_artifact_hash=None,
    schema_version="v1",
    decoder_version="d1",
    decoder_commit=None,
    source="polymarket_ctf_exchange_v2_orderfilled",
    chain_id=137,
    contract_address="0xe11...",
    event_signature="OrderFilled(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)",
    from_block=1,
    to_block=2,
    reorg_buffer_confs=20,
    capture_ts=1,
    capture_tool_id="t1",
    provider_id="hs-prod",
    token_supplied_externally=True,
    fixture_count=18,
    side_count=54,
    cleanroom_attestation="clean-room; no GPL copied",
)


def _order_filled_log(**kw):
    base = dict(
        block_number=100,
        transaction_hash="0xabc",
        log_index=3,
        block_timestamp=1710000000,
        maker="0xM",
        taker="0xT",
        makerAssetId="0",
        takerAssetId="42",
        makerAmountFilled="500000",
        takerAmountFilled="1000000",
        side=0,
    )
    base.update(kw)
    return base


def test_decode_order_filled_yields_native_price_and_identity():
    log = dict(
        block_number=100,
        transaction_hash="0xabc",
        log_index=3,
        block_timestamp=1710000000,
        maker="0xM",
        taker="0xT",
        makerAssetId="0",
        takerAssetId="42",
        makerAmountFilled="500000",
        takerAmountFilled="1000000",
        side=0,
    )  # USDC 0.5 / shares 1.0 → price 0.5
    r = decode_order_filled(log)
    assert 0.0 <= r.price <= 1.0 and abs(r.price - 0.5) < 1e-9
    assert (
        r.token_id == "42"
        and r.tx_hash == "0xabc"
        and r.log_index == 3
        and r.block_number == 100
    )


def test_decode_order_filled_rejects_decimal_out_of_range_price():
    # usdc_leg > share_leg → price > 1 (decimal-odds) → rejected, never reaches math.
    log = _order_filled_log(makerAmountFilled="1400000", takerAmountFilled="1000000")
    with pytest.raises((MarkoutError, ValueError)):
        decode_order_filled(log)


def test_capture_module_imports_only_stdlib_and_veridex():
    """Clean-room / no-network source scan: no GPL / external_research / net SDK.

    The module must import only the standard library and ``veridex.*`` — asserting
    both that no copied ``poly_data`` / GPL code rode along and that no network SDK
    (``requests`` / ``httpx`` / ``websocket``) is imported.
    """
    source = Path(capture_module.__file__).read_text()
    assert "external_research" not in source
    assert "poly_data" not in source

    allowed_roots = set(sys.stdlib_module_names) | {"veridex"}
    tree = ast.parse(source)
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                imported_roots.add(node.module.split(".")[0])
    forbidden = imported_roots - allowed_roots
    assert not forbidden, f"capture.py imports non-stdlib/non-veridex modules: {forbidden}"


def test_build_trade_artifact_reconciles_and_has_no_token():
    import json

    matched = decode_order_filled(_order_filled_log(takerAssetId="42", log_index=3))
    duplicate = decode_order_filled(
        _order_filled_log(takerAssetId="42", log_index=3, takerAmountFilled="2000000")
    )  # same event key as `matched` → dropped as duplicate
    non_cp1 = decode_order_filled(_order_filled_log(takerAssetId="99", log_index=4))

    records = [{"token_id": "42"}]  # cp1 token set

    artifact = build_trade_artifact(
        [matched, duplicate, non_cp1],
        records=records,
        manifest_meta=dict(_MANIFEST_META),
    )

    assert artifact.rows_duplicate_dropped == 1
    assert artifact.rows_unmatched == 1
    assert artifact.rows_matched_cp1 == 1
    assert artifact.rows_malformed == 0
    assert artifact.rows_decoded == (
        artifact.rows_matched_cp1
        + artifact.rows_unmatched
        + artifact.rows_malformed
        + artifact.rows_duplicate_dropped
    )
    assert artifact.mapping_content_hash == PINNED_MAPPING_HASH
    assert "HYPERSYNC" not in json.dumps(artifact.model_dump())


def test_build_trade_artifact_enriches_condition_id_from_mapping():
    # The clean-room OrderFilled ABI has no condition_id, so a decoded row carries
    # condition_id="". When its token_id is present in the pinned mapping, build MUST
    # enrich the row's condition_id from that record so the runner's later
    # (condition_id, token_id) join can match on real data (AC-102).
    row = decode_order_filled(_order_filled_log(takerAssetId="42", log_index=7))
    assert row.condition_id == ""  # decoder leaves it empty (no condition_id in the ABI)

    records = [{"token_id": "42", "condition_id": "0xCONDMAPPED"}]
    artifact = build_trade_artifact(
        [row], records=records, manifest_meta=dict(_MANIFEST_META)
    )

    assert len(artifact.rows) == 1
    assert artifact.rows[0].token_id == "42"
    # Enriched from the mapping (was ""); the artifact_hash now deterministically
    # covers the real condition_id.
    assert artifact.rows[0].condition_id == "0xCONDMAPPED"


def test_capture_entrypoint_fails_closed_without_token(monkeypatch, tmp_path):
    monkeypatch.delenv("HYPERSYNC_API", raising=False)
    out_path = tmp_path / "artifact.json"
    with pytest.raises(RuntimeError):
        capture_order_filled_artifact(from_block=1, to_block=2, out_path=out_path)
    # fail-closed BEFORE any I/O: no artifact written.
    assert not out_path.exists()


class _FakeClient:
    """An injected, network-free log source returning canned decoded logs."""

    def __init__(self, logs):
        self._logs = logs

    def fetch_order_filled_logs(self, *, from_block, to_block):
        return list(self._logs)


def test_capture_entrypoint_with_injected_client_writes_tokenless_artifact(
    monkeypatch, tmp_path
):
    import json

    monkeypatch.delenv("HYPERSYNC_API", raising=False)
    logs = [_order_filled_log(takerAssetId="42", log_index=7)]
    out_path = tmp_path / "artifact.json"

    artifact = capture_order_filled_artifact(
        from_block=1,
        to_block=2,
        out_path=out_path,
        client=_FakeClient(logs),
        records=[{"token_id": "42"}],
        manifest_meta=dict(_MANIFEST_META),
    )

    assert artifact.rows_matched_cp1 == 1
    assert out_path.exists()
    written = out_path.read_text()
    # No operator token leaks into the persisted artifact.
    assert "HYPERSYNC" not in written
    assert "HYPERSYNC" not in json.dumps(artifact.model_dump())
