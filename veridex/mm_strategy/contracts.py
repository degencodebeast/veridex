"""Pure-tier strategy contracts (MM-R4-B skeleton).

Frozen pydantic v2 stub contracts for the deterministic strategy tier. Mirrors the
``dust_execution/contracts.py`` evidence-hash idiom but is intentionally minimal: just
enough shape for a trivial ``core.decide()`` to type-check and run purely.

Import whitelist (load-bearing): stdlib + pydantic + ``veridex.runtime.evidence`` ONLY.
``veridex.runtime.evidence.serialize_payload`` is the one canonical byte serializer and the
pure tier's SOLE ``veridex.*`` runtime import exception (every evidence hash goes through it,
so hashes are deterministic across processes).
"""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict

from veridex.runtime.evidence import serialize_payload


class _FrozenModel(BaseModel):
    """Shared base: immutable, no extra fields tolerated, canonical evidence hash.

    ``config_hash()`` hashes the canonical serialization of ``model_dump()`` via the shared
    ``veridex.runtime.evidence.serialize_payload`` (sorted keys, compact separators), so the
    same content yields the same hash in every process.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    def config_hash(self) -> str:
        """``sha256`` hexdigest over ``serialize_payload(model_dump())`` (canonical)."""
        canonical = serialize_payload(self.model_dump())
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Sentinel(_FrozenModel):
    """A frozen marker value for the pure tier — a stable, hashable no-op placeholder."""

    name: str = "SENTINEL"


class StrategyObservation(_FrozenModel):
    """The per-tick market view a pure strategy decides from (skeleton shape)."""

    token_id: str = "TOKEN"
    ts: int = 0


class StrategyState(_FrozenModel):
    """The carry-forward strategy state threaded through successive ``decide()`` calls."""

    tick_seq: int = 0


class StrategyDecision(_FrozenModel):
    """The pure strategy's output. The skeleton only ever emits a fixed ``HOLD``."""

    action: Literal["HOLD"] = "HOLD"
    reason: str = "skeleton-hold"
