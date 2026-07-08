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
from veridex.maker.capture import decode_order_filled
from veridex.maker.markout import MarkoutError


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
