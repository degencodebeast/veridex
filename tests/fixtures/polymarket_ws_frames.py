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

# =======================================================================================
# W2 book-state-merge fixtures (hand-crafted, best_bid/best_ask kept CONSISTENT with the
# post-merge top-of-book so the self-validation checksum PASSES — except the deliberate
# mismatch fixture above). All operate on ``BOOK_ASSET_ID`` (seeded by ``BOOK_FRAME``:
# best_bid 0.50, best_ask 0.52, mid 0.51) unless noted. ``timestamp`` strictly ascends
# from ``BOOK_FRAME``'s ``123456789000`` so the stale-timestamp guard never trips.
# =======================================================================================
_MARKET = "0x5f65177b394277fd294cd75650044e32ba009a95022d88a0c1d565897d72f8f1"

# Add a bid at .51 → best_bid 0.50→0.51, mid 0.51→0.515 (a real mid move).
PRICE_CHANGE_ADD_BID_FRAME: dict[str, Any] = {
    "market": _MARKET,
    "price_changes": [
        {
            "asset_id": BOOK_ASSET_ID,
            "price": ".51",
            "size": "12",
            "side": "BUY",
            "hash": "a1",
            "best_bid": ".51",
            "best_ask": ".52",
        }
    ],
    "timestamp": "123456789100",
    "event_type": "price_change",
}

# Delete the best ask .52 (size=="0") → best_ask 0.52→0.53 (proves delete + mid move).
PRICE_CHANGE_DELETE_BEST_ASK_FRAME: dict[str, Any] = {
    "market": _MARKET,
    "price_changes": [
        {
            "asset_id": BOOK_ASSET_ID,
            "price": ".52",
            "size": "0",
            "side": "SELL",
            "hash": "a2",
            "best_bid": ".51",
            "best_ask": ".53",
        }
    ],
    "timestamp": "123456789200",
    "event_type": "price_change",
}

# One frame carrying BOTH a BUY and a SELL change for the same asset (side routing):
# BUY .505 → best_bid 0.505; SELL .515 → best_ask 0.515. Last change (SELL) carries the
# post-frame top-of-book best_bid/best_ask.
PRICE_CHANGE_BUY_AND_SELL_FRAME: dict[str, Any] = {
    "market": _MARKET,
    "price_changes": [
        {
            "asset_id": BOOK_ASSET_ID,
            "price": ".505",
            "size": "5",
            "side": "BUY",
            "hash": "a3",
            "best_bid": ".505",
            "best_ask": ".515",
        },
        {
            "asset_id": BOOK_ASSET_ID,
            "price": ".515",
            "size": "5",
            "side": "SELL",
            "hash": "a4",
            "best_bid": ".505",
            "best_ask": ".515",
        },
    ],
    "timestamp": "123456789150",
    "event_type": "price_change",
}

# A deep bid at .40 that does NOT move best_bid (0.50) → mid unchanged (no emit).
PRICE_CHANGE_NO_MOVE_FRAME: dict[str, Any] = {
    "market": _MARKET,
    "price_changes": [
        {
            "asset_id": BOOK_ASSET_ID,
            "price": ".40",
            "size": "5",
            "side": "BUY",
            "hash": "a5",
            "best_bid": ".50",
            "best_ask": ".52",
        }
    ],
    "timestamp": "123456789050",
    "event_type": "price_change",
}

# A book seeded with an EMPTY ask side (best_ask None → mid None; empty side never imputed).
BOOK_EMPTY_ASK_FRAME: dict[str, Any] = {
    "event_type": "book",
    "asset_id": BOOK_ASSET_ID,
    "market": "0xbd31dc8a20211944f6b70f31557f1001557b59905b7738480ca09bd4532f84af",
    "bids": [{"price": ".48", "size": "10"}],
    "asks": [],
    "timestamp": "123456789000",
    "hash": "0x0....",
}

# --- multi-asset routing seeds (for the verbatim PRICE_CHANGE_FRAME) --------------------
# Seed the BUY-side asset so that applying BUY .5/200 yields best_bid 0.5, best_ask 1
# (matching PRICE_CHANGE_FRAME's first change's best_bid/best_ask).
BOOK_FRAME_BUY_ASSET: dict[str, Any] = {
    "event_type": "book",
    "asset_id": PRICE_CHANGE_ASSET_ID_BUY,
    "market": _MARKET,
    "bids": [{"price": ".4", "size": "10"}],
    "asks": [{"price": "1", "size": "10"}],
    "timestamp": "1757908892000",
    "hash": "0x0....",
}

# Seed the SELL-side asset (empty bids) so that applying SELL .5/200 yields best_bid 0
# (empty), best_ask 0.5 (matching PRICE_CHANGE_FRAME's second change's best_bid/best_ask).
BOOK_FRAME_SELL_ASSET: dict[str, Any] = {
    "event_type": "book",
    "asset_id": PRICE_CHANGE_ASSET_ID_SELL,
    "market": _MARKET,
    "bids": [],
    "asks": [{"price": ".6", "size": "10"}],
    "timestamp": "1757908892000",
    "hash": "0x0....",
}
