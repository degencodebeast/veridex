"""Frozen maker run config + config hash + VOID-on-drift (PAT-001 for the maker lane).

``MakerRunConfig`` is the single, frozen, predeclared superset of every knob that
identifies a maker-arena run: which fixtures were scored, the mapping content hash
the run was pinned against, the markout horizons, the per-rung gate thresholds, the
participating agent config hashes, and the fill-assumption hash. It follows the
sealed-config pattern from ``veridex.backtest.event_probe.config.ProbeConfig``:

* ``config_hash()`` -- sha256 over the canonical (sorted-key, compact) JSON dump of
  every field. Changing ANY of them after results are observed changes this hash --
  the anti-drift guarantee. The canonical dump is a local :func:`_canonical_dump`,
  byte-identical to the runtime-evidence payload serializer but INLINED here, not
  imported, so this maker package imports nothing from the trust core (CON-012).
* ``verify_pinned(cfg, expected_hash)`` -- raises :class:`MakerVoidError` when the
  live hash diverges from the committed stamp. It is a pure comparison performing NO
  I/O, so the runner can VOID BEFORE touching any data / scores file (PAT-001).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def _canonical_dump(obj: Any) -> str:
    """Canonical JSON: sorted keys, compact separators.

    BYTE-IDENTICAL to the runtime-evidence payload serializer -- inlined HERE, not
    imported, so the maker config package imports NOTHING from the trust core (the
    runtime evidence module; CON-012 trust boundary). Reproducing the exact same dump
    keeps ``config_hash()`` in canonicalization-parity with the rest of the seal.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


class MakerVoidError(Exception):
    """Raised when the live config hash diverges from the pinned stamp (PAT-001).

    The runner raises this BEFORE any data / scores I/O so a drifted config never
    produces a reportable maker-arena result.
    """


class MakerRunConfig(BaseModel):
    """The single, frozen, predeclared superset of every maker-run identity knob.

    Frozen so a run cannot silently mutate a field after the hash is computed;
    ``extra="forbid"`` so a construction-time typo RAISES instead of silently keeping
    a default and yielding an identical ``config_hash`` (the exact drift the seal
    exists to prevent).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    protocol_id: str = "maker-arena-v1"
    fixture_ids: tuple[int, ...]
    mapping_content_hash: str
    markout_horizons_s: tuple[int, ...] = (30, 60, 300)
    rung_gate_thresholds: dict[str, int] = Field(default_factory=dict)
    agent_config_hashes: tuple[str, ...] = ()
    fill_assumption_hash: str | None = None

    def config_hash(self) -> str:
        """SHA-256 over the canonically-serialized config (stable, order-independent).

        Mirrors ``ProbeConfig.config_hash`` exactly: the same canonical dump
        (``json.dumps(sort_keys=True, separators=(",", ":"))``, inlined as
        :func:`_canonical_dump` to stay off the trust boundary) over ``model_dump()``.
        Tuple fields serialize as JSON arrays, so the dump is deterministic.
        """
        return hashlib.sha256(_canonical_dump(self.model_dump()).encode()).hexdigest()


def verify_pinned(cfg: MakerRunConfig, expected_hash: str) -> None:
    """VOID (:class:`MakerVoidError`) unless ``cfg`` recomputes to ``expected_hash``.

    A pure comparison performing NO I/O, so the runner can call it first and fail
    closed before reading any data or scores file (PAT-001).
    """
    actual = cfg.config_hash()
    if actual != expected_hash:
        raise MakerVoidError(
            f"VOID: MakerRunConfig hash diverged from the pinned stamp -- expected "
            f"{expected_hash}, got {actual}. The predeclared config changed since the "
            "stamp; do NOT report this result."
        )
