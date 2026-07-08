"""MM-R1.5 trade-artifact provenance layer — the HARD no-fill boundary.

A :class:`NormalizedTradeRow` is a decoded Polymarket ``OrderFilled`` event — a
trade between **other** venue participants, **never a Veridex fill**. It therefore
carries only market-observation fields plus chain-event identity (``block_number,
tx_hash, log_index``) and deliberately has **no** ``fill_price`` /
``real_executable_edge_bps`` / ``pnl`` / ``spread_capture`` field: any of those
would imply the row was our own execution, which it is not.

Prices are native probability / share prices in ``[0, 1]`` (matching the markout
math in :mod:`veridex.maker.markout`); a decimal-priced (``> 1``) row is rejected
at construction via :func:`~veridex.maker.markout.assert_native_prob`, so a
decimal-odds value can never silently reach downstream math.

:func:`recompute_artifact_hash` produces the trust-load-bearing artifact hash over
BOTH the economic fields AND the chain-event identity of every row, under a
deterministic sort; the canonical-dump helper is inlined here (NOT imported from
:mod:`veridex.runtime.evidence`) so the trade trust surface has no cross-module
dependency.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from veridex.maker.mapping import PINNED_MAPPING_HASH
from veridex.maker.markout import assert_native_prob
from veridex.maker.trades import AggressorSide

__all__ = [
    "NormalizedTradeRow",
    "TradeArtifact",
    "load_trade_artifact",
    "recompute_artifact_hash",
]

#: Manifest key substrings that would carry (or reference) an operator secret.
#: A key matching any of these is rejected outright. This is a *precise* denylist:
#: the manifest's own required boolean ``token_supplied_externally`` and the
#: row-level ``token_id`` are NOT secrets and are deliberately NOT matched (no
#: blanket ``*token*`` ban, which would reject those legitimate fields).
_SECRET_BEARING_KEY_SUBSTRINGS: tuple[str, ...] = (
    "hypersync_api",
    "api_key",
    "bearer_token",
    "authorization",
    "secret",
)


class NormalizedTradeRow(BaseModel):
    """A single decoded venue trade row with chain-event identity (never our fill).

    Attributes:
        ts: Event timestamp (epoch units as emitted by the source).
        price: Native probability / share price in ``[0, 1]``.
        size: Observed traded size (shares) — observational only, never
            exposure / fill-volume / PnL / rankable.
        aggressor_side: Side that crossed the spread.
        condition_id: Polymarket condition (market) identifier.
        token_id: Outcome-token identifier.
        block_number: Chain block number of the emitting log.
        tx_hash: Transaction hash of the emitting log.
        log_index: Log index within the transaction.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ts: int
    price: float
    size: float
    aggressor_side: AggressorSide
    condition_id: str
    token_id: str
    block_number: int
    tx_hash: str
    log_index: int

    @field_validator("price")
    @classmethod
    def _price_is_native_prob(cls, v: float) -> float:
        """Reject a non-``[0, 1]`` (decimal-odds) price at model construction."""
        return assert_native_prob(v, "price")

    def event_key(self) -> tuple[str, int]:
        """Return the chain-event identity key ``(tx_hash, log_index)``."""
        return (self.tx_hash, self.log_index)


def _canonical_dump(payload: object) -> str:
    """Canonical, deterministic JSON encoding (inlined; no cross-module import).

    Matches the mapping builder's ``sort_keys=True, separators=(",", ":")`` scheme
    so the hash is byte-stable. Inlined here deliberately: the trade trust surface
    must NOT depend on :mod:`veridex.runtime.evidence`.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def recompute_artifact_hash(rows: list[NormalizedTradeRow]) -> str:
    """Recompute the trust-load-bearing artifact hash over normalized rows.

    The hash covers BOTH the economic fields (``ts, price, size, aggressor_side,
    condition_id, token_id``) AND the chain-event identity (``block_number,
    tx_hash, log_index``) of every row, so any change to either — including a
    differing ``log_index`` on otherwise-identical rows — produces a different
    digest. Rows are sorted deterministically by ``(block_number, log_index)``
    before hashing so file order does not affect the result.

    Args:
        rows: The normalized trade rows to hash.

    Returns:
        The lowercase hex sha256 digest over the canonical encoding of the
        sorted rows.
    """
    ordered = sorted(rows, key=lambda r: (r.block_number, r.log_index))
    payload = [_canonical_dump(r.model_dump(mode="json")) for r in ordered]
    encoded = _canonical_dump(payload).encode()
    return hashlib.sha256(encoded).hexdigest()


class TradeArtifact(BaseModel):
    """A pinned, provenance-bearing bundle of normalized venue trade rows.

    The manifest fields describe the offline capture (source contract, block
    range, decoder, provider, row-count accounting) and the ``rows`` carry the
    decoded trades. Trust is enforced by four validators:

    * ``artifact_hash`` must equal :func:`recompute_artifact_hash` over ``rows``
      (covers economic + chain-event identity);
    * the row counts must reconcile exactly
      (``rows_decoded == matched_cp1 + unmatched + malformed + duplicate_dropped``);
    * ``mapping_content_hash`` must equal the pinned records-only mapping hash;
    * no manifest key may carry an operator secret (precise denylist —
      ``token_supplied_externally`` and row-level ``token_id`` are allowed).

    The model is ``frozen`` and forbids extra fields, so a smuggled fill / PnL /
    edge kwarg (or an unlisted secret key) is rejected loudly.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_hash: str
    raw_artifact_hash: str | None
    schema_version: str
    decoder_version: str
    decoder_commit: str | None
    source: str
    chain_id: int
    contract_address: str
    event_signature: str
    from_block: int
    to_block: int
    reorg_buffer_confs: int
    capture_ts: int
    capture_tool_id: str
    provider_id: str
    token_supplied_externally: bool
    rows_decoded: int
    rows_matched_cp1: int
    rows_unmatched: int
    rows_malformed: int
    rows_duplicate_dropped: int
    mapping_content_hash: str
    fixture_count: int
    side_count: int
    cleanroom_attestation: str
    rows: tuple[NormalizedTradeRow, ...]

    @model_validator(mode="before")
    @classmethod
    def _reject_secret_bearing_keys(cls, data: Any) -> Any:
        """Reject any manifest key whose name references an operator secret."""
        if isinstance(data, dict):
            for key in data:
                lowered = str(key).lower()
                if any(sub in lowered for sub in _SECRET_BEARING_KEY_SUBSTRINGS):
                    raise ValueError(f"secret-bearing manifest key forbidden: {key!r}")
        return data

    @model_validator(mode="after")
    def _validate_provenance(self) -> "TradeArtifact":
        """Enforce hash coverage, row-count reconciliation, and pinned mapping."""
        expected_hash = recompute_artifact_hash(list(self.rows))
        if self.artifact_hash != expected_hash:
            raise ValueError(
                f"artifact_hash mismatch: manifest {self.artifact_hash!r} != "
                f"recomputed {expected_hash!r}"
            )
        reconciled = (
            self.rows_matched_cp1
            + self.rows_unmatched
            + self.rows_malformed
            + self.rows_duplicate_dropped
        )
        if self.rows_decoded != reconciled:
            raise ValueError(
                f"row-count reconciliation failed: rows_decoded={self.rows_decoded} "
                f"!= matched+unmatched+malformed+duplicate_dropped={reconciled}"
            )
        if self.mapping_content_hash != PINNED_MAPPING_HASH:
            raise ValueError(
                f"mapping_content_hash not pinned: {self.mapping_content_hash!r} != "
                f"{PINNED_MAPPING_HASH!r}"
            )
        return self


def load_trade_artifact(path: str | Path) -> TradeArtifact:
    """Load and validate a :class:`TradeArtifact` from a JSON file.

    Args:
        path: Path to the JSON artifact.

    Returns:
        The validated artifact. All four trust validators run during
        construction, so a tampered hash / reconciliation / mapping pin / secret
        key raises ``ValidationError`` rather than loading silently.
    """
    raw = json.loads(Path(path).read_text())
    return TradeArtifact(**raw)
