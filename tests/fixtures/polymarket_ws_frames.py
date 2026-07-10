"""VERBATIM Polymarket CLOB market-channel WS frames as offline test fixtures (W0).

Encoded exactly from the W0-verified schema note
(`.omc/research/polymarket-ws-market-channel-schema.md`) so the offline WS tests never
touch the network. All numeric fields stay STRINGS (as the wire delivers them) and prices
may lack a leading zero (e.g. ``".48"``) — the parser must coerce defensively downstream.

Discriminator is ``event_type`` (NOT ``type``). ``book`` sides use ``bids``/``asks``.
``price_change`` carries a ``price_changes`` array (possibly multiple ``asset_id``s per
frame); per-change ``size == "0"`` DELETES that level. ``tick_size_change`` is a SEPARATE
event. ``timestamp`` is a 13-digit ms string.
"""

from __future__ import annotations

from typing import Any

# Token ids used across the fixtures (long CLOB token ids, verbatim from the note).
BOOK_ASSET_ID = "65818619657568813474341868652308942079804919287380422192892211131408793125422"
PRICE_CHANGE_ASSET_ID_BUY = "71321045679252212594626385532706912750332728571942532289631379312455583992563"
PRICE_CHANGE_ASSET_ID_SELL = "52114319501245915516055106046884209969926127482827954674443846427813813222426"

# --- book (full snapshot) — VERBATIM ---------------------------------------------------
BOOK_FRAME: dict[str, Any] = {
    "event_type": "book",
    "asset_id": BOOK_ASSET_ID,
    "market": "0xbd31dc8a20211944f6b70f31557f1001557b59905b7738480ca09bd4532f84af",
    "bids": [{"price": ".48", "size": "30"}, {"price": ".49", "size": "20"}, {"price": ".50", "size": "15"}],
    "asks": [{"price": ".52", "size": "25"}, {"price": ".53", "size": "60"}, {"price": ".54", "size": "10"}],
    "timestamp": "123456789000",
    "hash": "0x0....",
}

# --- price_change (incremental; multiple asset_ids in one frame) — VERBATIM -------------
PRICE_CHANGE_FRAME: dict[str, Any] = {
    "market": "0x5f65177b394277fd294cd75650044e32ba009a95022d88a0c1d565897d72f8f1",
    "price_changes": [
        {
            "asset_id": PRICE_CHANGE_ASSET_ID_BUY,
            "price": "0.5",
            "size": "200",
            "side": "BUY",
            "hash": "56621a121a47ed9333273e21c83b660cff37ae50",
            "best_bid": "0.5",
            "best_ask": "1",
        },
        {
            "asset_id": PRICE_CHANGE_ASSET_ID_SELL,
            "price": "0.5",
            "size": "200",
            "side": "SELL",
            "hash": "1895759e4df7a796bf4f1c5a5950b748306923e2",
            "best_bid": "0",
            "best_ask": "0.5",
        },
    ],
    "timestamp": "1757908892351",
    "event_type": "price_change",
}

# --- price_change with a size=="0" DELETE (hand-crafted per W0 note; for W2) ------------
PRICE_CHANGE_DELETE_FRAME: dict[str, Any] = {
    "market": "0x5f65177b394277fd294cd75650044e32ba009a95022d88a0c1d565897d72f8f1",
    "price_changes": [
        {
            "asset_id": BOOK_ASSET_ID,
            "price": ".49",
            "size": "0",
            "side": "BUY",
            "hash": "0deadbeef",
            "best_bid": ".48",
            "best_ask": ".52",
        }
    ],
    "timestamp": "1757908892400",
    "event_type": "price_change",
}

# --- best_bid/best_ask MISMATCH case (hand-crafted checksum-divergence; for W2) ---------
PRICE_CHANGE_MISMATCH_FRAME: dict[str, Any] = {
    "market": "0x5f65177b394277fd294cd75650044e32ba009a95022d88a0c1d565897d72f8f1",
    "price_changes": [
        {
            "asset_id": BOOK_ASSET_ID,
            "price": ".51",
            "size": "5",
            "side": "BUY",
            "hash": "0badc0de",
            "best_bid": ".99",
            "best_ask": ".52",
        }
    ],
    "timestamp": "1757908892500",
    "event_type": "price_change",
}

# --- tick_size_change (separate event) — VERBATIM --------------------------------------
TICK_SIZE_CHANGE_FRAME: dict[str, Any] = {
    "event_type": "tick_size_change",
    "asset_id": BOOK_ASSET_ID,
    "market": "0xbd31dc8a20211944f6b70f31557f1001557b59905b7738480ca09bd4532f84af",
    "old_tick_size": "0.01",
    "new_tick_size": "0.001",
    "timestamp": "100000000",
}
