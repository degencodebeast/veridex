"""Pinned resolved-market mapping loader.

Loads the committed ``resolved-market-lookup.json`` artifact and recomputes its
canonical content hash over the ``records`` list ONLY (not the whole file). This
hash is trust-load-bearing: a run whose recomputed hash does not match the pinned
value must be treated as VOID, so the recompute here must reproduce the builder's
byte-for-byte serialization exactly.

The builder computes the hash as::

    rows = sorted(records, key=lambda r: (r["fixture_id"], r["side"]))
    sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

No live network access happens here: the mapping is consumed from committed bytes
only.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

PINNED_MAPPING_HASH = "faf4a840e4366fcc9e6aec406eb0134ef48e263984d4482deeecaef45ef01e8b"

# maker -> veridex -> repo root; then down into the committed artifact.
DEFAULT_MAPPING_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "txline_live"
    / "cp1"
    / "resolved-market-lookup.json"
)


class ResolvedMarketRecord(BaseModel):
    """One resolved (fixture, side) -> market mapping record.

    Mirrors the exact 9-field shape of each entry in the committed artifact's
    ``records`` list so that ``model_dump()`` round-trips to the same bytes the
    builder hashed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    condition_id: str
    fixture_id: int
    frame_rows: int
    market_ref: str
    side: str
    source_artifact_content_hash: str | None
    source_frames_file: str
    token_id: str
    venue: str


def recompute_records_hash(records: list[dict[str, Any]]) -> str:
    """Recompute the canonical records-only content hash.

    Args:
        records: The raw 9-field record dicts (order-independent; sorted here).

    Returns:
        The lowercase hex sha256 digest over the sorted, compact JSON encoding
        of the records list.
    """
    rows = sorted(records, key=lambda r: (r["fixture_id"], r["side"]))
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def load_resolved_market_lookup(
    path: str | Path,
) -> tuple[list[ResolvedMarketRecord], str]:
    """Load the resolved-market lookup artifact and recompute its content hash.

    Args:
        path: Path to the ``resolved-market-lookup.json`` artifact.

    Returns:
        A tuple of the parsed records and the recomputed records-only content
        hash. The hash is computed over the original raw record dicts.
    """
    raw = json.loads(Path(path).read_text())
    raw_records: list[dict[str, Any]] = raw["records"]
    parsed = [ResolvedMarketRecord(**record) for record in raw_records]
    return parsed, recompute_records_hash(raw_records)
