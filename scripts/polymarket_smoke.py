#!/usr/bin/env python
"""OPERATOR-ONLY Polymarket 1-share FAK smoke (REQ-2D-403 operator step) — DO NOT auto-run.

This is the ONE step that actually moves real mainnet USDC. It is NOT part of the offline suite and
is NEVER invoked by an agent or the runner. It refuses to do anything unless the operator explicitly
sets ``POLYMARKET_SMOKE=yes`` in the environment, and it places the smallest possible order (1 share)
on an operator-chosen throwaway market from the real production egress.

What it does (only when armed):
  1. builds the vendored CLOB client from operator-supplied credentials;
  2. derives the API key (``init_client`` -> ``derive_api_key`` / ``create_api_key``);
  3. submits a 1-share FAK through :class:`~veridex.venues.polymarket.PolymarketAdapter` — which puts
     the NATIVE tick-rounded price on the wire (never decimal odds);
  4. reads the HONEST fill back via ``get_order`` and prints the reconciled receipt (the REAL matched
     size/price, never the request).

Everything venue/credential-related is imported LAZILY inside :func:`main` so importing this module
is offline-safe. Run it yourself; an agent must not.

Operator environment (all required unless noted):
  POLYMARKET_SMOKE=yes            arming gate — anything else aborts
  POLYMARKET_WRITE_ENABLED=true   the adapter write gate
  POLYMARKET_KEY                  Polymarket account address (a.k.a. polymarket_key)
  POLYMARKET_SECRET               wallet private key (NEVER commit this)
  POLYMARKET_FUNDER               funder address (optional; defaults to POLYMARKET_KEY)
  POLYMARKET_SIG_TYPE             1 = email wallet, 2 = browser wallet (default 2)
  POLYMARKET_SMOKE_TOKEN_ID       the throwaway market's outcome token id (the side you buy)
  POLYMARKET_SMOKE_TICK           the market's tick size, e.g. 0.01
  POLYMARKET_SMOKE_PRICE          decimal odds to submit the 1 share at
  POLYMARKET_SMOKE_SIDE           side label (default "yes")

Usage (operator shell only):
  POLYMARKET_SMOKE=yes POLYMARKET_WRITE_ENABLED=true POLYMARKET_KEY=0x... POLYMARKET_SECRET=0x... \
  POLYMARKET_SMOKE_TOKEN_ID=... POLYMARKET_SMOKE_TICK=0.01 POLYMARKET_SMOKE_PRICE=1.05 \
  python scripts/polymarket_smoke.py
"""

from __future__ import annotations

import asyncio
import os
import sys

_ARM_ENV = "POLYMARKET_SMOKE"
_ARM_VALUE = "yes"
_SMOKE_SIZE = 1.0  # one share — the smallest real order


class SmokeAborted(RuntimeError):
    """Raised when the smoke is not armed or an operator input is missing/invalid."""


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SmokeAborted(f"missing required env var {name}")
    return value


def _require_armed() -> None:
    """Fail closed unless the operator explicitly armed the smoke."""
    if os.environ.get(_ARM_ENV) != _ARM_VALUE:
        raise SmokeAborted(
            f"refusing to run: set {_ARM_ENV}={_ARM_VALUE} to place a REAL 1-share mainnet order"
        )
    if os.environ.get("POLYMARKET_WRITE_ENABLED", "").strip().lower() not in ("1", "true", "yes"):
        raise SmokeAborted("refusing to run: set POLYMARKET_WRITE_ENABLED=true to arm the adapter write gate")


async def _run_smoke() -> int:
    """Place the 1-share FAK and print the honest reconciled receipt. Returns a process exit code."""
    # Lazy imports keep module import offline-safe (no vendored/eth_account/httpx at import time).
    from veridex.config import Settings
    from veridex.venues._vendor.polymarket_clob.client import Polymarket
    from veridex.venues.base import Order
    from veridex.venues.polymarket import PolymarketAdapter
    from veridex.venues.polymarket_resolver import ResolvedMarket

    polymarket_key = _require_env("POLYMARKET_KEY")
    secret = _require_env("POLYMARKET_SECRET")
    funder = os.environ.get("POLYMARKET_FUNDER") or polymarket_key
    sig_type = int(os.environ.get("POLYMARKET_SIG_TYPE", "2"))
    token_id = _require_env("POLYMARKET_SMOKE_TOKEN_ID")
    tick = float(_require_env("POLYMARKET_SMOKE_TICK"))
    price = float(_require_env("POLYMARKET_SMOKE_PRICE"))
    side = os.environ.get("POLYMARKET_SMOKE_SIDE", "yes")

    client = Polymarket(polymarket_key=polymarket_key, secret=secret, sig_type=sig_type, funder=funder)
    await client.init_client()  # derive/create the API key from the production egress

    # The smoke buys a single side, so map that side's token as the "yes" token of a 1-outcome resolve.
    resolved = ResolvedMarket(
        condition_id=os.environ.get("POLYMARKET_SMOKE_CONDITION_ID", "operator-smoke"),
        token_id_yes=token_id,
        token_id_no=token_id,
        tick_size=tick,
    )
    adapter = PolymarketAdapter(
        resolved,
        client,
        side=side,
        settings=Settings(polymarket_write_enabled=True),
        write_client=client,
        dry_run=False,  # explicitly armed by the operator
    )

    order = Order(
        market_ref="operator-smoke",
        side=side,
        size=_SMOKE_SIZE,
        price=price,
        venue="polymarket",
        client_order_id="polymarket-smoke-1",
    )

    print(f"[smoke] submitting 1-share FAK: side={side} decimal_odds={price} token={token_id}")
    ack = await adapter.submit_order(order)
    print(f"[smoke] submit ack: venue_order_id={ack.venue_order_id!r} accepted={ack.accepted}")

    status = await adapter.get_order_status(ack.venue_order_id)
    print(
        "[smoke] HONEST fill: "
        f"status={status.status} filled_size={status.filled_size} "
        f"decimal_price={status.price} native_price={status.native_price}"
    )
    receipt = adapter.normalize_receipt("polymarket-smoke-1", order, status, mode="live_guarded")
    print(f"[smoke] receipt: {receipt.model_dump(mode='json')}")
    return 0


def main() -> int:
    """Entry point: fail closed unless armed, then run the smoke."""
    try:
        _require_armed()
        return asyncio.run(_run_smoke())
    except SmokeAborted as exc:
        print(f"[smoke] ABORTED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
