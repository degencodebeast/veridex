"""R-2 — the trusted, hash-verified ReplayPack CATALOG (root of replay trust).

Startup scans a curated ``REPLAY_PACK_ROOT`` (READ-ONLY seed packs) plus an optional SEPARATE
writable capture root, HASH-VERIFIES every pack, and builds an ALLOWLISTED catalog mapping
``pack_id -> CatalogEntry{content_hash, provenance, is_genuine, fixtures}``. A pack whose stored
``content_hash`` does not recompute (tampered / corrupt / unverified) is EXCLUDED — fail-closed, never
added to the catalog and therefore never served. Both REAL and SYNTHETIC packs are listed WITH HONEST
provenance: a pack is labelled ``is_genuine`` only when :func:`~veridex.ingest.capture_chain.is_genuine_pack`
proves a hash-verified, coherent genuine state; a synthetic pack is listed as synthetic, and a pack that
merely *declares* ``genuine-txline`` without a coherent genuine state is fail-safe DOWNGRADED — never
served as genuine.

**The writable-capture-root ↔ read-only-catalog boundary (R-0b seam).** The curated ``REPLAY_PACK_ROOT``
is READ-ONLY and is NEVER written here. A SEPARATE writable capture root (the ``replay-capture`` volume)
receives R-0b's freshly-captured deployed packs. :meth:`ReplayCatalog.register_pack` is the runtime
register/refresh path: given a NEW pack under the writable capture root it HASH-VERIFIES the pack and,
only if verification passes, ATOMICALLY promotes it into the in-memory catalog with NO process restart.
An unverified new pack is REJECTED, never promoted. Promotion is copy-on-write under a lock (a fresh
mapping is built and the reference swapped), so concurrent readers see either the old or the new catalog
whole — never a torn read. Registration refuses a pack sitting under the curated root: deployed capture
publishes to the WRITABLE root, never the read-only curated root.

Trust-path module (``ingest/`` is import-audited): NO network, NO LLM SDK imports. It reuses the R-1
tamper-evidence machinery (:func:`~veridex.ingest.replay_pack.verify_content_hash`) and the MAJOR-1
provenance-honesty predicates (:func:`~veridex.ingest.capture_chain.is_genuine_pack`) rather than
re-deriving trust.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from veridex.ingest.capture_chain import (
    GENUINE_TXLINE_PROVENANCE,
    UNKNOWN_PROVENANCE,
    is_genuine_pack,
    read_pack_provenance,
)
from veridex.ingest.replay_pack import verify_content_hash


class CatalogVerificationError(Exception):
    """Raised when :meth:`ReplayCatalog.register_pack` is asked to promote an UNVERIFIED pack.

    Verify-before-promote is the trust invariant: a pack whose ``content_hash`` does not recompute
    (tampered / corrupt / malformed manifest) is REJECTED — it is never promoted into the served
    catalog. This is a loud rejection (not a silent skip) because the caller explicitly asked to
    register THIS pack; a startup SCAN, by contrast, silently excludes a bad pack and keeps going.
    """


@dataclass(frozen=True)
class CatalogEntry:
    """One allowlisted, hash-verified pack in the catalog.

    Attributes:
        pack_id: Stable id (the pack directory name) the served API / replay path addresses.
        content_hash: The pack's stored, VERIFIED ``content_hash`` (recompute-matched at admission).
        provenance: HONEST provenance label — ``genuine-txline`` only for a coherent genuine pack;
            a synthetic pack keeps its synthetic label; a pack that declares genuine without a
            coherent genuine state is fail-safe downgraded (never surfaced as genuine).
        is_genuine: ``True`` only when :func:`~veridex.ingest.capture_chain.is_genuine_pack` proves a
            hash-verified, coherent genuine state (real TxLINE capture). ``False`` for synthetic/test.
        fixtures: The fixture ids the pack manifest declares (the replayable fixtures of this pack).
        pack_dir: Absolute directory of the pack (curated seed OR promoted capture pack).
    """

    pack_id: str
    content_hash: str
    provenance: str
    is_genuine: bool
    fixtures: tuple[int, ...]
    pack_dir: Path


def _iter_pack_dirs(root: Path) -> Iterator[Path]:
    """Yield candidate pack directories under ``root`` (the root itself and one level down).

    Mirrors the readiness probe's candidate resolution: a pack is either ``root/pack.json`` (the
    curated root IS one pack, e.g. the pinned demo pack mounted at the catalog root) or
    ``root/<name>/pack.json`` (a root holding several packs). A missing root yields nothing.
    """
    if not root.is_dir():
        return
    if (root / "pack.json").is_file():
        yield root
    for manifest in sorted(root.glob("*/pack.json")):
        yield manifest.parent


def _build_verified_entry(pack_dir: Path) -> CatalogEntry | None:
    """Hash-verify ``pack_dir`` and, if verified, build its :class:`CatalogEntry`; else ``None``.

    Fail-closed at every gate: an unverified content_hash (tampered/corrupt), a malformed/missing
    manifest, or a manifest declaring NO fixtures all return ``None`` (the pack is excluded — never
    served). Provenance is derived HONESTLY: genuine only when :func:`is_genuine_pack` proves a
    coherent genuine state; a pack that merely *declares* ``genuine-txline`` without that coherent
    state is downgraded to ``unknown-provenance`` (a non-verified pack is NEVER surfaced as genuine).
    """
    # Hash-verification is the allowlist gate: recompute == stored, else EXCLUDE (fail-closed).
    if not verify_content_hash(pack_dir):
        return None
    try:
        manifest = json.loads((pack_dir / "pack.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    fixtures_raw = manifest.get("fixtures") if isinstance(manifest, dict) else None
    if not isinstance(fixtures_raw, list) or not fixtures_raw:
        return None
    fixture_ids: list[int] = []
    for entry in fixtures_raw:
        fid = entry.get("fixture_id") if isinstance(entry, dict) else None
        if isinstance(fid, int):
            fixture_ids.append(fid)
    if not fixture_ids:
        return None
    content_hash = manifest.get("content_hash")
    if not isinstance(content_hash, str) or not content_hash:
        return None

    genuine = is_genuine_pack(pack_dir)
    declared = read_pack_provenance(pack_dir)
    if genuine:
        # is_genuine_pack already proved provenance == GENUINE_TXLINE_PROVENANCE coherently.
        provenance = GENUINE_TXLINE_PROVENANCE
    elif declared == GENUINE_TXLINE_PROVENANCE:
        # Declares genuine but is NOT a coherent, hash-verified genuine pack -> fail-safe downgrade.
        # A non-genuine pack must NEVER be catalogued (or served) as genuine.
        provenance = UNKNOWN_PROVENANCE
    else:
        provenance = declared

    return CatalogEntry(
        pack_id=pack_dir.name,
        content_hash=content_hash,
        provenance=provenance,
        is_genuine=genuine,
        fixtures=tuple(fixture_ids),
        pack_dir=pack_dir,
    )


class ReplayCatalog:
    """A thread/async-safe, ALLOWLISTED catalog of hash-verified ReplayPacks (root of replay trust).

    Reads are lock-free: :meth:`get` / :meth:`snapshot` / :meth:`__contains__` read a single reference
    to an immutable mapping, so a concurrent :meth:`register_pack` — which builds a fresh mapping and
    swaps the reference under a lock (copy-on-write) — can never expose a torn/half-updated catalog.

    The catalog is built from the READ-ONLY curated root (and optionally previously-captured packs in
    the writable capture root). :meth:`register_pack` is the runtime promotion path for a freshly
    captured pack in the WRITABLE capture root; it NEVER writes the curated root.
    """

    def __init__(
        self,
        entries: Mapping[str, CatalogEntry],
        *,
        curated_root: Path | None = None,
        capture_root: Path | None = None,
    ) -> None:
        # Store an immutable snapshot; every mutation replaces this reference wholesale (never in place).
        self._entries: dict[str, CatalogEntry] = dict(entries)
        self._lock = threading.Lock()
        self._curated_root = curated_root
        self._capture_root = capture_root

    # -- lock-free reads (single atomic reference read of self._entries) --

    def get(self, pack_id: str) -> CatalogEntry | None:
        """Return the allowlisted entry for ``pack_id``, or ``None`` if it is not catalogued."""
        return self._entries.get(pack_id)

    def snapshot(self) -> dict[str, CatalogEntry]:
        """Return a COPY of the current catalog mapping (safe to iterate while writers register)."""
        return dict(self._entries)

    def pack_ids(self) -> list[str]:
        """Return the catalogued pack ids (sorted, deterministic)."""
        return sorted(self._entries)

    def __contains__(self, pack_id: object) -> bool:
        return pack_id in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    # -- locked, atomic copy-on-write promotion --

    def register_pack(self, pack_dir: Path) -> CatalogEntry:
        """Hash-verify a NEW capture-root pack and ATOMICALLY promote it into the catalog (no restart).

        Verify-before-promote: the pack's ``content_hash`` is recomputed and must match; an unverified
        (tampered/corrupt/malformed) pack raises :class:`CatalogVerificationError` and is NOT promoted.
        A verified pack is admitted under the same honest-provenance rules as startup (a non-genuine
        pack can never be promoted as genuine). Promotion is copy-on-write under a lock, so a concurrent
        reader sees the catalog either without or with the new pack whole — never a torn read. A
        re-registered ``pack_id`` REFRESHES its entry.

        The curated root is READ-ONLY: registering a pack that resolves under the curated root is
        REFUSED (deployed capture must publish to the writable capture root). This method never WRITES
        any file — it only reads and hash-verifies ``pack_dir``.

        Args:
            pack_dir: Directory of the freshly-captured pack under the writable capture root.

        Returns:
            The promoted :class:`CatalogEntry`.

        Raises:
            ValueError: If ``pack_dir`` resolves under the read-only curated root.
            CatalogVerificationError: If the pack fails hash-verification (not promoted).
        """
        pack_dir = Path(pack_dir)
        self._reject_curated_root_writes(pack_dir)

        entry = _build_verified_entry(pack_dir)
        if entry is None:
            raise CatalogVerificationError(
                f"refusing to register pack at {pack_dir}: content_hash verification failed "
                "(tampered, corrupt, or malformed) — unverified packs are never promoted"
            )

        with self._lock:
            # Copy-on-write: build a fresh mapping and swap the reference atomically. Never mutate the
            # existing dict in place, so lock-free readers never observe a half-updated catalog.
            updated = dict(self._entries)
            updated[entry.pack_id] = entry
            self._entries = updated
        return entry

    def _reject_curated_root_writes(self, pack_dir: Path) -> None:
        """Refuse to register a pack that resolves under the READ-ONLY curated root (boundary guard)."""
        if self._curated_root is None:
            return
        try:
            resolved = pack_dir.resolve()
            curated = self._curated_root.resolve()
        except OSError:
            return
        if resolved == curated or curated in resolved.parents:
            raise ValueError(
                f"refusing to register {pack_dir} under the READ-ONLY curated root {self._curated_root}; "
                "deployed capture must publish to the separate writable capture root"
            )


def build_catalog(
    curated_root: str | Path | None,
    *,
    capture_root: str | Path | None = None,
) -> ReplayCatalog:
    """Scan + hash-verify every pack under the curated root (and optional capture root) into a catalog.

    Startup entrypoint. Every pack under ``curated_root`` (the READ-ONLY ``REPLAY_PACK_ROOT`` seeds) is
    hash-verified; a verified pack is allowlisted with honest provenance, an unverified pack is silently
    EXCLUDED (fail-closed — never served). When ``capture_root`` is given, previously-captured packs in
    that SEPARATE writable root are folded in too (so captures survive a redeploy), curated seeds taking
    precedence on a ``pack_id`` collision (a writable-root pack can never shadow a trusted curated seed).

    Args:
        curated_root: The read-only ``REPLAY_PACK_ROOT`` directory (blank/None -> empty catalog).
        capture_root: Optional SEPARATE writable capture root to also scan at startup.

    Returns:
        A :class:`ReplayCatalog` carrying only hash-verified packs; it retains both roots so
        :meth:`ReplayCatalog.register_pack` can enforce the read-only-curated boundary.
    """
    curated = Path(curated_root) if curated_root else None
    capture = Path(capture_root) if capture_root else None

    entries: dict[str, CatalogEntry] = {}
    if curated is not None:
        for pack_dir in _iter_pack_dirs(curated):
            entry = _build_verified_entry(pack_dir)
            if entry is not None:
                entries[entry.pack_id] = entry
    if capture is not None:
        for pack_dir in _iter_pack_dirs(capture):
            entry = _build_verified_entry(pack_dir)
            if entry is not None:
                # Curated seed wins on collision — a writable-root pack never shadows a trusted seed.
                entries.setdefault(entry.pack_id, entry)

    return ReplayCatalog(entries, curated_root=curated, capture_root=capture)
