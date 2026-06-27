"""Live devnet Memo anchor over a tick bundle (T9). ONE tx; payload == run-manifest hash.

Reuses the TESTED veridex offline code (run_manifest / run_manifest_hash /
memo_payload_for_manifest / compute_evidence_hash) to drive a REAL Solana devnet Memo tx,
proving the offline spine produces an anchorable hash. Measures send->confirm latency.
Run: .venv/bin/python scripts/txline_live/anchor_memo_live.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from veridex.chain.anchor import memo_payload_for_manifest, run_manifest, run_manifest_hash  # noqa: E402
from veridex.runtime.evidence import compute_evidence_hash  # noqa: E402

from solana.rpc.api import Client  # noqa: E402
from solana.rpc.commitment import Confirmed  # noqa: E402
from solders.instruction import AccountMeta, Instruction  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.message import Message  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from solders.transaction import Transaction  # noqa: E402

RPC = "https://api.devnet.solana.com"
MEMO_PROGRAM = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
KEYPAIR = Path.home() / ".config" / "solana" / "proofarena-treasury-devnet.json"


def load_keypair(path: Path) -> Keypair:
    return Keypair.from_bytes(bytes(json.loads(path.read_text())))


def build_bundle_manifest() -> tuple[dict, str]:
    """Build a run manifest over a 30-event bundle, using the tested offline code."""
    # 30 synthetic-but-structured RunEvents (a 'bundle' of ticks+decisions).
    events = [
        {"sequence_no": i, "event_type": "tick" if i % 2 == 0 else "decision",
         "state_snapshot_json": json.dumps({"tick": i}), "action_payload_json": json.dumps({"type": "WAIT"})}
        for i in range(30)
    ]
    action_evidence_root = compute_evidence_hash(events)
    score_root = compute_evidence_hash(events[:1])  # placeholder score root
    manifest = run_manifest(
        run_id="t9-live-anchor",
        fixture_or_window_id="17588404",
        agent_ids=["agno-1", "det-baseline"],
        action_evidence_root=action_evidence_root,
        score_root=score_root,
        proof_mode_map={"agno-1": "LLM/evidence-verified", "det-baseline": "reproducible"},
        code_prompt_schema_versions={"action_schema": "sports_v0"},
    )
    return manifest, run_manifest_hash(manifest)


def main() -> None:
    client = Client(RPC)
    payer = load_keypair(KEYPAIR)
    print(f"payer: {payer.pubkey()}")
    bal = client.get_balance(payer.pubkey()).value
    print(f"balance: {bal/1e9:.4f} SOL")

    manifest, mhash = build_bundle_manifest()
    payload = memo_payload_for_manifest(manifest)
    assert payload == mhash, "payload must equal manifest hash (gate 4)"
    print(f"run-manifest hash (== memo payload): {payload}")
    assert len(payload) == 64

    ix = Instruction(
        program_id=MEMO_PROGRAM,
        data=payload.encode("utf-8"),
        accounts=[AccountMeta(pubkey=payer.pubkey(), is_signer=True, is_writable=False)],
    )

    t0 = time.time()
    blockhash = client.get_latest_blockhash().value.blockhash
    msg = Message.new_with_blockhash([ix], payer.pubkey(), blockhash)
    tx = Transaction([payer], msg, blockhash)
    sig = client.send_transaction(tx).value
    t_sent = time.time()
    print(f"sent ONE Memo tx: {sig}")
    client.confirm_transaction(sig, commitment=Confirmed, sleep_seconds=0.5)
    t_conf = time.time()

    print(f"latency: build+send={t_sent - t0:.3f}s  send->confirmed={t_conf - t_sent:.3f}s  total={t_conf - t0:.3f}s")
    print(f"explorer: https://explorer.solana.com/tx/{sig}?cluster=devnet")

    # verify the memo landed on-chain by reading the tx
    txinfo = client.get_transaction(sig, encoding="json", commitment=Confirmed, max_supported_transaction_version=0)
    logs = (txinfo.value.transaction.meta.log_messages if txinfo.value else []) or []
    memo_logged = any(payload[:16] in m for m in logs)
    print(f"memo payload present in on-chain logs: {memo_logged}")


if __name__ == "__main__":
    main()
