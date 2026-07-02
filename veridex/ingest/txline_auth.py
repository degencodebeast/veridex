"""CON-041 live-feed access: guest JWT → on-chain subscribe() → /token/activate → API token.

Pure URL/header builders are offline-testable. Network + Solana are async-shell, lazy-imported,
and client-injectable. Credentials come from typed config ONLY — never the repo/logs/events
(CON-041). The on-chain subscribe is a REAL signed devnet tx (free World Cup = 0 TxL).

Trust-path note: ``ingest/`` is import-audited, so neither httpx nor solders is imported at
module load — both are imported lazily inside the async functions (CON-010).
"""

from __future__ import annotations

from typing import Any


def guest_start_url(base: str) -> str:
    """The guest-auth start endpoint URL."""
    return f"{base}/auth/guest/start"


def activate_url(base: str) -> str:
    """The token-activate endpoint URL."""
    return f"{base}/api/token/activate"


async def guest_start(*, base_url: str | None = None, client: Any = None) -> str:
    """POST ``/auth/guest/start`` and return the guest JWT (IP-bound)."""
    from veridex.config import get_settings

    resolved = base_url or get_settings().txline_auth_base_url
    own = client is None
    if own:
        import httpx  # noqa: PLC0415

        client = httpx.AsyncClient()
    try:
        resp = await client.post(guest_start_url(resolved))
        resp.raise_for_status()
        return str(resp.json()["jwt"])
    finally:
        if own:
            await client.aclose()


async def on_chain_subscribe(
    jwt: str,
    *,
    keypair_path: str | None = None,
    program_id: str | None = None,
    rpc_url: str | None = None,
    client: Any = None,
) -> str:
    """Send the on-chain ``subscribe()`` tx (free WC = 0 TxL, but a real signed devnet tx).

    Lazy ``solders``/``solana`` import (mirrors ``veridex.chain.anchor``). Returns the tx sig.
    Credentials/program id come from typed config via ``require_txline_subscribe`` when omitted.
    """
    from veridex.config import get_settings, require_txline_subscribe

    cfg = get_settings()
    kp_path, prog = (keypair_path, program_id)
    if kp_path is None or prog is None:
        kp_path, prog = require_txline_subscribe(cfg)
    url = rpc_url or cfg.solana_rpc_url

    import json as _json
    from pathlib import Path as _Path

    from solana.rpc.async_api import AsyncClient
    from solana.rpc.commitment import Confirmed
    from solders.instruction import AccountMeta, Instruction
    from solders.keypair import Keypair
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction, VersionedTransaction

    kp: Any = Keypair.from_bytes(bytes(_json.loads(_Path(kp_path).read_text())))
    program: Any = Pubkey.from_string(prog)
    ix: Any = Instruction(
        program_id=program,
        data=b"subscribe",
        accounts=[AccountMeta(pubkey=kp.pubkey(), is_signer=True, is_writable=False)],
    )
    own = client is None
    rpc: Any = AsyncClient(url) if own else client
    try:
        blockhash = (await rpc.get_latest_blockhash()).value.blockhash
        msg = Message.new_with_blockhash([ix], kp.pubkey(), blockhash)
        tx = Transaction([kp], msg, blockhash)
        vtx = VersionedTransaction.from_legacy(tx)
        sig = (await rpc.send_transaction(vtx)).value
        await rpc.confirm_transaction(sig, commitment=Confirmed)
        return str(sig)
    finally:
        if own:
            await rpc.close()


async def activate_token(jwt: str, subscribe_sig: str, *, base_url: str | None = None, client: Any = None) -> str:
    """POST ``/api/token/activate`` (Bearer guest JWT + subscribe sig) → API token (X-Api-Token)."""
    from veridex.config import get_settings

    resolved = base_url or get_settings().txline_auth_base_url
    own = client is None
    if own:
        import httpx  # noqa: PLC0415

        client = httpx.AsyncClient()
    try:
        resp = await client.post(
            activate_url(resolved),
            headers={"Authorization": f"Bearer {jwt}"},
            json={"subscribeSignature": subscribe_sig},
        )
        resp.raise_for_status()
        return str(resp.json()["apiToken"])
    finally:
        if own:
            await client.aclose()


async def acquire_live_credentials(
    *, base_url: str | None = None, auth_client: Any = None, rpc_client: Any = None
) -> tuple[str, str]:
    """End-to-end CON-041 flow → ``(jwt, api_token)``. Secrets from typed config only."""
    jwt = await guest_start(base_url=base_url, client=auth_client)
    sig = await on_chain_subscribe(jwt, client=rpc_client)
    api_token = await activate_token(jwt, sig, base_url=base_url, client=auth_client)
    return jwt, api_token
