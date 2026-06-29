"""Per-domain Merkle roots — bind sealed competition records into anchorable commitments.

Pure / deterministic / dependency-light. Reuses the ONE canonical serializer so leaf hashes
match the rest of the evidence/proof chain cross-process. Used by the run manifest (the
``root_forest`` block) so validation + anchoring bind to sealed records, not just one flat hash.
"""

from __future__ import annotations

import hashlib
from typing import Any

from veridex.runtime.evidence import serialize_payload

#: Root of an empty domain (no records). Distinct, stable, never collides with a real leaf.
EMPTY_ROOT: str = hashlib.sha256(b"").hexdigest()


def leaf_hash(record: Any) -> str:
    """SHA-256 of one canonically-serialized record (a Merkle leaf)."""
    return hashlib.sha256(serialize_payload(record).encode("utf-8")).hexdigest()


def _pair_hash(left: str, right: str) -> str:
    """Hash two hex child nodes into their parent (concatenate the hex strings, then sha256)."""
    return hashlib.sha256((left + right).encode("utf-8")).hexdigest()


def merkle_root(leaves: list[str]) -> str:
    """Deterministic binary Merkle root over hex ``leaves`` (odd levels duplicate the last node).

    Args:
        leaves: Ordered leaf hashes (hex). Order is significant — the caller controls it.

    Returns:
        The 64-char hex Merkle root; ``EMPTY_ROOT`` for ``[]``; the single leaf for one leaf.
    """
    if not leaves:
        return EMPTY_ROOT
    level = list(leaves)
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])  # duplicate the last node on an odd level
        level = [_pair_hash(level[i], level[i + 1]) for i in range(0, len(level), 2)]
    return level[0]


def domain_root(records: list[Any]) -> str:
    """Merkle root over a domain's records (each canonically hashed, in the given order)."""
    return merkle_root([leaf_hash(r) for r in records])
