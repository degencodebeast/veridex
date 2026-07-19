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
import shutil
import tempfile
import threading
import uuid
import weakref
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from veridex.ingest.capture_chain import (
    GENUINE_TXLINE_PROVENANCE,
    UNKNOWN_PROVENANCE,
    is_genuine_pack,
    read_pack_provenance,
)
from veridex.ingest.replay_pack import load_pack_marketstates, verify_content_hash

if TYPE_CHECKING:
    from veridex.ingest.marketstate import MarketState


class CatalogVerificationError(Exception):
    """Raised when :meth:`ReplayCatalog.register_pack` is asked to promote an UNVERIFIED pack.

    Verify-before-promote is the trust invariant: a pack whose ``content_hash`` does not recompute
    (tampered / corrupt / malformed manifest) is REJECTED — it is never promoted into the served
    catalog. This is a loud rejection (not a silent skip) because the caller explicitly asked to
    register THIS pack; a startup SCAN, by contrast, silently excludes a bad pack and keeps going.
    """


class CatalogAdmissionError(ValueError):
    """Raised when :meth:`ReplayCatalog.register_pack` refuses a pack on an ADMISSION-POLICY boundary.

    Distinct from :class:`CatalogVerificationError` (the pack failed hash-verification): here the pack
    verifies fine but violates the read-only-curated boundary — it either resolves UNDER the curated
    root, or its ``pack_id`` COLLIDES with an existing CURATED-SEED entry (a runtime-registered capture
    pack may never REPLACE a curated seed). Subclasses :class:`ValueError` so the boundary refusal reads
    as a caller/argument error to existing handlers.
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
    manifest, a manifest declaring NO fixtures, OR a manifest whose fixture list contains a malformed /
    non-int / boolean fixture id all return ``None`` (the pack is excluded — never served, and the
    catalog never silently under-reports a pack's fixtures). Provenance is derived HONESTLY: genuine
    only when :func:`is_genuine_pack` proves a
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
        # A valid fixture id is a real int — NEVER a JSON bool (``bool`` subclasses ``int``, so a
        # bare ``isinstance(fid, int)`` would admit ``true``/``false`` as fixture ids 1/0). A
        # malformed/non-int fixture entry EXCLUDES the whole pack (fail-closed, consistent with the
        # module's posture): the catalog must not silently UNDER-report a pack's declared fixtures.
        if not isinstance(fid, int) or isinstance(fid, bool):
            return None
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
        # The configured WRITABLE capture root. :meth:`register_pack` FAILS CLOSED unless a candidate
        # pack resolves to this root or a descendant (MAJOR-1 confinement) — a sibling path or a symlink
        # that escapes the capture root is refused. ``None`` means no writable root is configured, so
        # nothing is promotable and every register call is refused.
        self._capture_root = capture_root
        # Catalog-OWNED publication root (lazy): on admission :meth:`register_pack` copies the verified
        # bytes here (immutable publication, MAJOR-2) so a later mutation of the writable SOURCE can never
        # affect the catalogued/served pack. It is created under the system temp dir on first register and
        # rmtree'd when this catalog is garbage-collected (see :meth:`_take_ownership`).
        self._published_root: Path | None = None

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

        **Capture-root confinement (MAJOR-1).** A candidate must resolve to the configured WRITABLE
        capture root or a descendant, else it is REFUSED (fail-closed). ``.resolve()`` is used, so a
        SIBLING path AND a symlink that sits inside the capture root but points OUTSIDE it are both
        caught. With no capture root configured, nothing is promotable and every call is refused.

        **Immutable publication (MAJOR-2).** On admission the catalog TAKES OWNERSHIP of the verified
        bytes: the pack is copied into a catalog-owned, non-source directory and re-verified THERE, and
        the resulting entry points at the OWNED copy. A later mutation of the writable source therefore
        cannot affect the catalogued/served pack. This method writes ONLY to the catalog-owned
        publication root — it NEVER writes the curated root or the capture-root source.

        The curated root is READ-ONLY: registering a pack that resolves under the curated root is
        REFUSED, and a capture pack whose ``pack_id`` COLLIDES with an existing CURATED-SEED entry is
        REFUSED too — a runtime-registered writable-root pack may never REPLACE a curated seed's catalog
        entry (deployed capture must publish to the writable capture root, and startup already gives
        curated seeds precedence on collision). A genuinely NEW ``pack_id`` still registers, and
        re-registering a non-curated ``pack_id`` REFRESHES it.

        Args:
            pack_dir: Directory of the freshly-captured pack under the writable capture root.

        Returns:
            The promoted :class:`CatalogEntry` (its ``pack_dir`` is the catalog-owned immutable copy).

        Raises:
            CatalogAdmissionError: If ``pack_dir`` resolves under the read-only curated root, does NOT
                resolve under the configured writable capture root, or its ``pack_id`` collides with an
                existing curated-seed entry (all ``ValueError`` subtypes).
            CatalogVerificationError: If the pack fails hash-verification (not promoted).
        """
        pack_dir = Path(pack_dir)
        # Order matters: the curated-root guard runs FIRST so a curated-root candidate keeps its explicit
        # "curated" refusal message before the capture-root confinement (which reports a different cause).
        self._reject_curated_root_writes(pack_dir)
        self._require_within_capture_root(pack_dir)

        # Immutable publication: take ownership of the bytes, then verify the OWNED copy (stage → verify →
        # admit), so the admitted entry's bytes are byte-independent of the mutable source (MAJOR-2).
        entry = self._own_and_verify(pack_dir)
        if entry is None:
            raise CatalogVerificationError(
                f"refusing to register pack at {pack_dir}: content_hash verification failed "
                "(tampered, corrupt, or malformed) — unverified packs are never promoted"
            )
        owned_dir = entry.pack_dir

        with self._lock:
            # Boundary guard (under the lock so the decision reflects the live catalog): a runtime
            # capture-root registration may NEVER replace a curated-seed entry. Startup gives curated
            # seeds precedence via setdefault; this closes the same hole on the runtime promote path.
            existing = self._entries.get(entry.pack_id)
            if existing is not None and self._resolves_under_curated(existing.pack_dir):
                self._discard_owned(owned_dir)
                raise CatalogAdmissionError(
                    f"refusing to register {pack_dir}: pack_id {entry.pack_id!r} collides with a "
                    f"READ-ONLY curated-seed entry at {existing.pack_dir}; a runtime-registered capture "
                    "pack may never replace a curated seed"
                )
            # Copy-on-write: build a fresh mapping and swap the reference atomically. Never mutate the
            # existing dict in place, so lock-free readers never observe a half-updated catalog.
            # TODO(bounded-growth): on a REFRESH (re-registered pack_id) the SUPERSEDED owned copy is
            # intentionally NOT deleted here — a lock-free reader may still hold the old entry and be
            # reading its bytes. Superseded copies are reclaimed when the catalog is garbage-collected
            # (the weakref.finalize on _published_root). This trades bounded in-process disk growth for
            # in-flight-reader safety; a generational sweep could reclaim sooner if it ever matters.
            updated = dict(self._entries)
            updated[entry.pack_id] = entry
            self._entries = updated
        return entry

    def _reject_curated_root_writes(self, pack_dir: Path) -> None:
        """Refuse to register a pack that resolves under the READ-ONLY curated root (boundary guard)."""
        if self._resolves_under_curated(pack_dir):
            raise CatalogAdmissionError(
                f"refusing to register {pack_dir} under the READ-ONLY curated root {self._curated_root}; "
                "deployed capture must publish to the separate writable capture root"
            )

    def _require_within_capture_root(self, pack_dir: Path) -> None:
        """FAIL CLOSED unless ``pack_dir`` resolves to the configured writable capture root or below it.

        MAJOR-1 confinement: ``.resolve()`` follows symlinks, so a SIBLING path and a symlink inside the
        capture root that points OUTSIDE it are both refused. With no capture root configured, nothing is
        promotable — every registration is refused (there is no owned writable volume to promote from).
        """
        if self._capture_root is None:
            raise CatalogAdmissionError(
                f"refusing to register {pack_dir}: no writable capture root is configured, so no pack is "
                "promotable — a runtime-registered pack must live under the writable capture root"
            )
        try:
            resolved = pack_dir.resolve()
            capture = self._capture_root.resolve()
        except OSError:
            # Fail CLOSED: an unresolvable candidate cannot be proven to live under the capture root.
            raise CatalogAdmissionError(
                f"refusing to register {pack_dir}: its path could not be resolved for capture-root "
                "confinement"
            ) from None
        if not (resolved == capture or capture in resolved.parents):
            raise CatalogAdmissionError(
                f"refusing to register {pack_dir}: it does not resolve under the writable capture root "
                f"{self._capture_root} (a sibling path or symlink-escape is refused, fail-closed)"
            )

    def _own_and_verify(self, source_dir: Path) -> CatalogEntry | None:
        """Take ownership of ``source_dir`` and verify the OWNED copy; return its entry, else ``None``.

        The shared immutable-publication core of both the runtime promote path (:meth:`register_pack`)
        and the startup capture-root fold (:meth:`_fold_capture_root_at_startup`): stage an owned copy,
        verify THAT copy, and return an entry pointing at it. Any verification miss (``None``) OR a raise
        while verifying discards the staged copy, so a rejected/erroring admission never leaks a temp dir.
        """
        owned_dir = self._take_ownership(source_dir)
        try:
            entry = _build_verified_entry(owned_dir)
        except BaseException:
            self._discard_owned(owned_dir)
            raise
        if entry is None:
            self._discard_owned(owned_dir)
            return None
        return entry

    def _take_ownership(self, source_dir: Path) -> Path:
        """Copy ``source_dir`` into a catalog-OWNED directory and return the owned copy (MAJOR-2).

        Each admission lands in a fresh unique version directory under the catalog's publication root, so
        a re-registration never overwrites (or deletes) the bytes an in-flight reader may still hold. The
        publication root is created lazily under the system temp dir and rmtree'd when this catalog is
        garbage-collected. The owned copy keeps the source leaf name so ``pack_id`` is stable.
        """
        # Double-checked lazy init under the lock: two concurrent first admissions must not each mkdtemp
        # (which would orphan one publication root and leak it past GC of the losing reference).
        if self._published_root is None:
            with self._lock:
                if self._published_root is None:
                    root = Path(tempfile.mkdtemp(prefix="veridex-replay-published-"))
                    # Best-effort cleanup of the owned copies when the catalog is collected (never at import).
                    weakref.finalize(self, shutil.rmtree, str(root), ignore_errors=True)
                    self._published_root = root
        version_dir = self._published_root / uuid.uuid4().hex
        version_dir.mkdir()
        try:
            owned = version_dir / source_dir.name
            shutil.copytree(source_dir, owned)
        except BaseException:
            # A partial/failed copytree must not leave an orphaned version dir behind (temp-dir hygiene).
            shutil.rmtree(version_dir, ignore_errors=True)
            raise
        return owned

    @staticmethod
    def _discard_owned(owned_dir: Path) -> None:
        """Remove an owned copy (and its unique version dir) that was staged but never admitted."""
        shutil.rmtree(owned_dir.parent, ignore_errors=True)

    def _fold_capture_root_at_startup(self, capture_root: Path) -> None:
        """Fold previously-captured packs under the WRITABLE capture root into the catalog at STARTUP.

        Symmetric with :meth:`register_pack`'s immutable publication (MAJOR-2): each capture-root pack is
        served from a catalog-OWNED copy, so a later mutation of the writable capture volume cannot affect
        the served pack (a promoted pack rediscovered after a restart must not revert to a mutable-source
        reference). CURATED seeds already loaded WIN on a ``pack_id`` collision and are NEVER copied — the
        curated seed is the trusted, canonically-:ro-mounted seed and keeps pointing at the curated dir.

        Runs during construction (single-threaded, before the catalog is published to any reader), so it
        mutates ``self._entries`` in place rather than via copy-on-write.
        """
        for source_dir in _iter_pack_dirs(capture_root):
            # Curated (or an earlier capture) seed wins the pack_id — skip BEFORE copying so a colliding
            # capture pack never costs an owned-copy write.
            if source_dir.name in self._entries:
                continue
            # Capture-root confinement (symmetric with register_pack's _require_within_capture_root): a
            # DIRECTORY SYMLINK in the capture root that _iter_pack_dirs followed OUT of the volume
            # resolves outside the capture root — EXCLUDE it (fail-closed, skip). A startup scan drops a
            # bad/escaping pack rather than aborting the whole catalog, so it is never owned/served.
            if not self._resolves_under_capture(source_dir):
                continue
            entry = self._own_and_verify(source_dir)
            if entry is not None:
                self._entries[entry.pack_id] = entry

    def _resolves_under_curated(self, path: Path) -> bool:
        """Return ``True`` iff ``path`` is the curated root itself or lives beneath it (boundary test)."""
        if self._curated_root is None:
            return False
        try:
            resolved = path.resolve()
            curated = self._curated_root.resolve()
        except OSError:
            # Fail-OPEN here is intentional and safe: an unresolvable path is treated as NOT-under-curated
            # so it does not trip the read-only boundary guard. It is NOT a trust bypass — the pack still
            # goes through verify-before-promote (hash-verification) and the curated-seed collision guard;
            # this only decides whether the under-curated-root SHORTCUT refusal applies.
            return False
        return resolved == curated or curated in resolved.parents

    def _resolves_under_capture(self, path: Path) -> bool:
        """Return ``True`` iff ``path`` is the writable capture root or lives beneath it (resolve-following).

        The boolean form of the runtime :meth:`_require_within_capture_root` confinement, used by the
        startup fold to EXCLUDE (rather than raise on) a capture-root candidate that resolves outside the
        volume — e.g. a directory symlink escape. ``.resolve()`` follows symlinks; an unresolvable path or
        an unconfigured capture root is fail-closed to ``False`` (not provably under the capture root).
        """
        if self._capture_root is None:
            return False
        try:
            resolved = path.resolve()
            capture = self._capture_root.resolve()
        except OSError:
            return False
        return resolved == capture or capture in resolved.parents


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
        A :class:`ReplayCatalog` carrying only hash-verified packs. It retains the curated root so
        :meth:`ReplayCatalog.register_pack` can enforce the read-only-curated boundary, and the capture
        root so registration can CONFINE promotions to the writable capture root (MAJOR-1, fail-closed).
    """
    curated = Path(curated_root) if curated_root else None
    capture = Path(capture_root) if capture_root else None

    # Curated seeds are catalogued in place — they stay pointing at the READ-ONLY curated root (the
    # canonically-:ro-mounted trusted seed) and are NEVER copied (no per-startup ~600KB duplication).
    entries: dict[str, CatalogEntry] = {}
    if curated is not None:
        for pack_dir in _iter_pack_dirs(curated):
            entry = _build_verified_entry(pack_dir)
            if entry is not None:
                entries[entry.pack_id] = entry

    catalog = ReplayCatalog(entries, curated_root=curated, capture_root=capture)
    # WRITABLE capture-root packs are folded in through the OWNING path (immutable publication, MAJOR-2),
    # symmetric with register_pack — a captured pack rediscovered after a restart is served from a
    # catalog-owned copy, never a mutable capture-volume reference. Curated seeds win on collision.
    if capture is not None:
        catalog._fold_capture_root_at_startup(capture)
    return catalog


@dataclass(frozen=True)
class ResolvedReplaySource:
    """The FROZEN identity of a production-replay tape (R-4, Option B): server-derived + catalog-verified.

    Produced by :func:`resolve_replay_source` at the ADMISSION boundary and PERSISTED, then REUSED
    (never re-selected) at execution/retry via :func:`load_resolved_marketstates`. Because the identity
    is frozen once, an R-0b promotion between admission and load can never silently change the tape or
    make a sealed run's identity diverge from the bytes it replayed.

    Attributes:
        pack_id: The verified catalog key that was selected.
        fixture_id: The selected fixture within the pack (a VALID int, incl. ``0``).
        content_hash: The pack's verified ``content_hash`` at selection time (the tamper-evidence anchor).
        provenance: HONEST provenance label of the selected pack (observability).
        is_genuine: Whether the selected pack proved a coherent genuine state (observability).
    """

    pack_id: str
    fixture_id: int
    content_hash: str
    provenance: str
    is_genuine: bool

    def as_binding(self) -> dict[str, str | int]:
        """The minimal, durable identity triple to persist (pack_id + fixture_id + content_hash)."""
        return {"pack_id": self.pack_id, "fixture_id": self.fixture_id, "content_hash": self.content_hash}


class ReplayResolutionError(ValueError):
    """Fail-closed production-replay selection/resolution (Option B).

    Raised INSTEAD of any silent fallback (``build_demo_ticks`` / a filesystem path / another catalog
    entry). ``reason`` is a stable machine label the API boundary maps to a 4xx error body.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def resolve_replay_source(
    catalog: ReplayCatalog | None,
    *,
    pack_id: str | None,
    fixture_id: int | None,
) -> ResolvedReplaySource:
    """Select the production-replay pack from the verified catalog under a SINGLE atomic snapshot (Option B).

    ONE :meth:`ReplayCatalog.snapshot` drives BOTH the cardinality decision AND the selected entry, so a
    concurrent R-0b :meth:`ReplayCatalog.register_pack` can never make cardinality and selection disagree
    (never ``len()`` then a separate live ``get()``). Selection is fail-closed at every gate — there is no
    fallback to ``build_demo_ticks``, a filesystem path, or another entry after ANY failure.

    Selection rules:

    * ``pack_id`` given -> exact lookup IN THAT SNAPSHOT; unknown/unverified -> :class:`ReplayResolutionError`.
    * ``pack_id`` omitted + ZERO entries -> fail closed (``empty_catalog``).
    * ``pack_id`` omitted + exactly ONE entry -> select it.
    * ``pack_id`` omitted + MULTIPLE entries -> fail closed (``pack_id_required``) — never guess.
    * ``fixture_id`` given -> must belong to the selected entry; else fail closed (``unknown_fixture``).
      ``fixture_id`` is presence-aware: ``0`` is a VALID identity and is NEVER treated as "omitted".
    * ``fixture_id`` omitted -> the DETERMINISTIC, documented choice: the entry's LOWEST fixture id.

    Args:
        catalog: The live R-2 catalog (``None`` is treated as an empty catalog -> fail closed when unnamed).
        pack_id: The requested catalog key, or ``None`` to auto-select the single catalogued pack.
        fixture_id: The requested fixture (``None`` -> the entry's lowest fixture id; ``0`` is valid).

    Returns:
        The :class:`ResolvedReplaySource` frozen identity (pack_id + fixture_id + content_hash + provenance).

    Raises:
        ReplayResolutionError: Any fail-closed gate (empty catalog, pack_id required, unknown pack/fixture).
    """
    snapshot = catalog.snapshot() if catalog is not None else {}

    if pack_id is not None:
        entry = snapshot.get(pack_id)
        if entry is None:
            raise ReplayResolutionError(
                f"unknown or unverified replay pack_id: {pack_id!r}", reason="unknown_pack"
            )
    elif not snapshot:
        raise ReplayResolutionError(
            "no verified replay pack is catalogued (empty catalog)", reason="empty_catalog"
        )
    elif len(snapshot) > 1:
        raise ReplayResolutionError(
            f"pack_id required: {len(snapshot)} verified packs catalogued ({sorted(snapshot)})",
            reason="pack_id_required",
        )
    else:
        entry = next(iter(snapshot.values()))

    if fixture_id is not None:
        if fixture_id not in entry.fixtures:
            raise ReplayResolutionError(
                f"fixture_id {fixture_id} is not catalogued for pack {entry.pack_id!r}",
                reason="unknown_fixture",
            )
        chosen_fixture = fixture_id
    else:
        chosen_fixture = min(entry.fixtures)

    return ResolvedReplaySource(
        pack_id=entry.pack_id,
        fixture_id=chosen_fixture,
        content_hash=entry.content_hash,
        provenance=entry.provenance,
        is_genuine=entry.is_genuine,
    )


def load_resolved_marketstates(
    catalog: ReplayCatalog | None,
    resolved: ResolvedReplaySource,
) -> list[MarketState]:
    """Load the FROZEN identity's tape, re-checking the bound ``content_hash`` against the live catalog.

    The frozen identity is NEVER re-selected here — it is looked up by its exact ``pack_id`` and the load
    is refused (fail closed) if the pack has left the catalog OR its current ``content_hash`` drifted from
    the frozen one (an R-0b re-publish of different bytes under the same id). This guarantees the replayed
    bytes always match the sealed identity: a bound run can never quietly serve a different tape.

    Args:
        catalog: The live R-2 catalog.
        resolved: The frozen :class:`ResolvedReplaySource` (from :func:`resolve_replay_source`, persisted).

    Returns:
        The pack fixture's ``MarketState`` tape (``verify=True`` — the runtime default), non-empty.

    Raises:
        ReplayResolutionError: The frozen pack/fixture is gone, or its bytes drifted from ``content_hash``.
    """
    snapshot = catalog.snapshot() if catalog is not None else {}
    entry = snapshot.get(resolved.pack_id)
    if entry is None:
        raise ReplayResolutionError(
            f"frozen replay pack_id no longer catalogued: {resolved.pack_id!r}", reason="pack_gone"
        )
    if entry.content_hash != resolved.content_hash:
        raise ReplayResolutionError(
            f"frozen content_hash for pack {resolved.pack_id!r} drifted from the catalogued bytes",
            reason="content_hash_drift",
        )
    if resolved.fixture_id not in entry.fixtures:
        raise ReplayResolutionError(
            f"frozen fixture_id {resolved.fixture_id} no longer catalogued for pack {resolved.pack_id!r}",
            reason="fixture_gone",
        )
    return load_pack_marketstates(Path(entry.pack_dir), resolved.fixture_id, verify=True)
