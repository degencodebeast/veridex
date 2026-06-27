"""B9 — package anchor wiring (REQ-112 / AC-112 / CON-004).

TDD suite for ``anchor_memo``. ALL tests in this module are offline — no network.
The mock RPC client is injected via the ``client=`` seam.

Live smoke (``test_live_anchor_memo``) is skipped unless ``SOLANA_KEYPAIR_PATH``
is set and the wallet is funded on devnet. Never run automatically in CI.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_HASH = "ab" * 32  # 64 lowercase hex chars — a well-formed manifest hash


def _make_keypair_file(tmp_path: Path) -> Path:
    """Write a fresh random Solana keypair JSON to a temp file and return its path."""
    from solders.keypair import Keypair  # type: ignore[import-untyped]

    kp = Keypair()
    kp_file = tmp_path / "test_keypair.json"
    kp_file.write_text(json.dumps(list(bytes(kp))))
    return kp_file


def _make_mock_client(sig_str: str = "MOCK_SIG_1234567890ABCDEF") -> tuple[Any, list[Any]]:
    """Return a (mock_client, captured_transactions) pair.

    The mock implements the ``AsyncClient`` duck-type:
    - ``get_latest_blockhash()`` → resp with ``.value.blockhash`` (a real ``Hash.default()``)
    - ``send_transaction(vtx)`` → resp with ``.value`` = captured sig MagicMock (str == sig_str)
    - ``confirm_transaction(sig, *, commitment)`` → no-op
    The caller owns the client lifecycle; ``close`` must NOT be called by ``anchor_memo``
    when the client is injected.
    """
    from solders.hash import Hash  # type: ignore[import-untyped]

    captured: list[Any] = []

    blockhash_resp = MagicMock()
    blockhash_resp.value.blockhash = Hash.default()

    mock_sig = MagicMock()
    mock_sig.__str__ = lambda _: sig_str

    send_resp = MagicMock()
    send_resp.value = mock_sig

    async def _fake_send(tx: Any, **_kwargs: Any) -> Any:
        captured.append(tx)
        return send_resp

    mock = AsyncMock()
    mock.get_latest_blockhash.return_value = blockhash_resp
    mock.send_transaction.side_effect = _fake_send
    mock.confirm_transaction.return_value = MagicMock()

    return mock, captured


# ---------------------------------------------------------------------------
# 1. Module-load laziness (structural — passes both before and after implementation)
# ---------------------------------------------------------------------------


def test_anchor_module_no_toplevel_solders_import() -> None:
    """``import veridex.chain.anchor`` must NOT pull solders/solana at module load.

    Verified via AST inspection of the source file — reliable regardless of
    what is already cached in ``sys.modules``.
    """
    import ast

    src = Path(__file__).parent.parent / "veridex" / "chain" / "anchor.py"
    tree = ast.parse(src.read_text())
    forbidden = ("solders", "solana")
    for node in ast.iter_child_nodes(tree):  # top-level nodes only
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith(forbidden), f"Forbidden module-level import: import {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert not mod.startswith(forbidden), f"Forbidden module-level import: from {mod} ..."


# ---------------------------------------------------------------------------
# 2. Validation — ValueError before any network (RED → GREEN)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_hash",
    [
        "",  # empty
        "ab" * 31,  # 62 chars — too short
        "ab" * 33,  # 66 chars — too long
        "zz" * 32,  # 64 chars but not valid hex
        "AB" * 31 + "XY",  # 64 chars, last two not hex
        "ab" * 31 + "g0",  # 64 chars, 'g' is not hex
    ],
    ids=["empty", "too-short", "too-long", "non-hex-64", "non-hex-suffix", "g-not-hex"],
)
async def test_anchor_memo_rejects_invalid_hash(bad_hash: str) -> None:
    """Non-64-hex ``manifest_hash`` raises ``ValueError`` before any network call."""
    from veridex.chain.anchor import anchor_memo

    with pytest.raises(ValueError):
        await anchor_memo(bad_hash, keypair_path="/dev/null", client=AsyncMock())


# ---------------------------------------------------------------------------
# 3. Happy path — mock client (RED → GREEN)
# ---------------------------------------------------------------------------


async def test_anchor_memo_valid_hash_returns_sig(tmp_path: Path) -> None:
    """64-hex hash + injected mock client → mock's signature string returned."""
    from veridex.chain.anchor import anchor_memo

    kp_file = _make_keypair_file(tmp_path)
    mock_client, _ = _make_mock_client("EXPECTED_SIG_ABC123")

    result = await anchor_memo(VALID_HASH, keypair_path=str(kp_file), client=mock_client)

    assert result == "EXPECTED_SIG_ABC123"


async def test_anchor_memo_payload_equals_manifest_hash(tmp_path: Path) -> None:
    """Gate 4: the Memo instruction ``data`` bytes == manifest_hash encoded as UTF-8.

    Verifies the on-chain payload is exactly the manifest hash — nothing else.
    The VersionedTransaction captured from the mock is inspected directly.
    """
    from veridex.chain.anchor import anchor_memo

    kp_file = _make_keypair_file(tmp_path)
    mock_client, captured = _make_mock_client()

    await anchor_memo(VALID_HASH, keypair_path=str(kp_file), client=mock_client)

    assert len(captured) == 1, "exactly ONE transaction must be sent"
    vtx = captured[0]

    # The VersionedTransaction message carries compiled instructions.
    ix_data = bytes(vtx.message.instructions[0].data)
    assert ix_data == VALID_HASH.encode("utf-8"), f"Memo payload must equal manifest hash bytes; got {ix_data!r}"


async def test_anchor_memo_sends_exactly_one_tx(tmp_path: Path) -> None:
    """ONE and only one Memo transaction is sent per call (CON-004)."""
    from veridex.chain.anchor import anchor_memo

    kp_file = _make_keypair_file(tmp_path)
    mock_client, captured = _make_mock_client()

    await anchor_memo(VALID_HASH, keypair_path=str(kp_file), client=mock_client)

    mock_client.send_transaction.assert_called_once()
    assert len(captured) == 1


async def test_anchor_memo_does_not_close_injected_client(tmp_path: Path) -> None:
    """Caller owns the injected client lifecycle — ``close`` must NOT be called."""
    from veridex.chain.anchor import anchor_memo

    kp_file = _make_keypair_file(tmp_path)
    mock_client, _ = _make_mock_client()

    await anchor_memo(VALID_HASH, keypair_path=str(kp_file), client=mock_client)

    mock_client.close.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Gate 4 via memo_payload_for_manifest helper (pre-existing; must stay green)
# ---------------------------------------------------------------------------


def test_memo_payload_for_manifest_is_manifest_hash() -> None:
    """``memo_payload_for_manifest`` returns exactly ``run_manifest_hash`` (gate 4 pin)."""
    from veridex.chain.anchor import memo_payload_for_manifest, run_manifest, run_manifest_hash

    manifest = run_manifest(
        run_id="b9-unit",
        fixture_or_window_id="17952170",
        agent_ids=["agno-1", "det-baseline"],
        action_evidence_root="ev_root",
        score_root="sc_root",
        proof_mode_map={"agno-1": "LLM/evidence-verified"},
        code_prompt_schema_versions={"action_schema": "sports_v0"},
    )
    h = run_manifest_hash(manifest)
    assert len(h) == 64
    assert memo_payload_for_manifest(manifest) == h


# ---------------------------------------------------------------------------
# 5. Live smoke — creds-gated, default-skipped (adds ONE skip to the suite)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("SOLANA_KEYPAIR_PATH") is None,
    reason="SOLANA_KEYPAIR_PATH not set — creds-gated devnet smoke; skip in CI",
)
async def test_live_anchor_memo() -> None:
    """Send a real SPL Memo tx on Solana devnet. Manual only — never in CI.

    Requires a funded devnet wallet at ``$SOLANA_KEYPAIR_PATH``.
    """
    from veridex.chain.anchor import anchor_memo, memo_payload_for_manifest, run_manifest

    manifest = run_manifest(
        run_id="b9-live-smoke",
        fixture_or_window_id="17952170",
        agent_ids=["smoke-agent"],
        action_evidence_root="ev_root_live",
        score_root="sc_root_live",
        proof_mode_map={"smoke-agent": "reproducible"},
        code_prompt_schema_versions={"action_schema": "sports_v0"},
    )
    manifest_hash = memo_payload_for_manifest(manifest)
    assert len(manifest_hash) == 64

    sig = await anchor_memo(manifest_hash)

    assert isinstance(sig, str)
    assert len(sig) > 10
    print(f"\nlive anchor sig: {sig}")
    print(f"explorer: https://explorer.solana.com/tx/{sig}?cluster=devnet")
