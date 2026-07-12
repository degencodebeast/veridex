"""E3-T7 — keyless read-only client for IDM-002 reads (REQ-018e).

MONEY-NETWORK BOUNDARY. This client is STRUCTURALLY read-only and STRUCTURALLY keyless:

* It exposes ONLY reads (order status, open orders, trades) — there is NO ``submit`` / ``sign`` /
  ``cancel`` method, so it cannot mutate custody or funds.
* It holds NO credentials and NO key material (no L1/L2 secret, no private key, no signer). It issues
  UNSIGNED public GETs through an INJECTED async ``http_get`` reader; it never attaches a ``POLY_*``
  auth header and never imports a local-key crypto library. IDM-002 reconciliation reads are public
  and MUST NOT require — or be able to use — a signing credential.
* It is provider-neutral: the concrete HTTP transport is injected, so this module imports no
  ``httpx``/venue client and stays offline-import-safe.

Keeping the read path keyless removes the read surface from the money boundary entirely: even a fully
compromised read client cannot sign or spend, because it never has anything to sign with.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from veridex.dust_execution.risk import FailClosed

#: An injected async public GET: ``(path, params) -> parsed JSON``. No auth header is ever added — the
#: reader is for PUBLIC reads only (IDM-002). Typed to a mapping result so no live client leaks across.
KeylessHttpGet = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class KeylessReadClient:
    """Read-only, credential-free venue reader for IDM-002 reconciliation reads.

    Constructed with ONLY a public ``http_get`` reader and a non-secret ``base_path`` — deliberately no
    ``api_key`` / ``secret`` / ``signer`` parameter exists, so the type cannot hold a credential. Every
    method is a GET; there is no write/sign surface.
    """

    def __init__(self, *, http_get: KeylessHttpGet, base_path: str = "") -> None:
        if http_get is None:  # fail closed: a reader with no transport cannot silently no-op reads.
            raise FailClosed("KeylessReadClient requires an injected http_get reader")
        self._http_get = http_get
        self._base_path = base_path.rstrip("/")

    def _path(self, suffix: str) -> str:
        return f"{self._base_path}{suffix}" if self._base_path else suffix

    async def get_order(self, venue_order_key: str) -> dict[str, Any]:
        """Read a single order's status by its venue order key (the V2 order hash join key)."""
        return await self._http_get(self._path("/data/order"), {"id": venue_order_key})

    async def get_open_orders(self, *, market: str | None = None) -> dict[str, Any]:
        """Read open orders, optionally scoped to a ``market`` (public, unsigned)."""
        params: dict[str, Any] = {}
        if market is not None:
            params["market"] = market
        return await self._http_get(self._path("/data/orders"), params)

    async def get_trades(self, venue_order_key: str) -> dict[str, Any]:
        """Read the trade/fill history for an order by its venue order key (IDM-002)."""
        return await self._http_get(self._path("/data/trades"), {"id": venue_order_key})


__all__ = ["KeylessHttpGet", "KeylessReadClient"]
