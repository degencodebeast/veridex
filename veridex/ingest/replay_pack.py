"""T3 — ReplayPack: tamper-evident, self-describing replay artifact (REQ-2D-301).

T0 read-only-to-trust-path tool: NO network, NO LLM imports, NO imports from
veridex/law, veridex/checks, veridex/verifier, or veridex/runtime/evidence. Pure file
transform: a recorder session (T2) becomes a pack that replays through the SAME
normalizer live TxLINE uses — "one projection" is core doctrine (spec §4.2).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from veridex.ingest.live_client import marketstates_from_record_stream
from veridex.ingest.marketstate import MarketState
from veridex.ingest.recorder import read_session

if TYPE_CHECKING:
    # Type-only import (avoids a runtime cycle: capture_chain imports this module). A PackAuthority is
    # a SEALED provenance capability — see :mod:`veridex.ingest.capture_chain`.
    from veridex.ingest.capture_chain import PackAuthority

#: The current authority-inclusive pack format. A v2 pack folds its authority block (below) INTO
#: ``content_hash``; a legacy v1 pack hashes DATA FILES ONLY (see :func:`_compute_content_hash`).
PACK_FORMAT_VERSION = 2

#: The authority-bearing ``capture`` fields a v2 ``content_hash`` binds, in a FIXED order. Read with
#: an explicit ``None`` default so DELETING a field (not just editing it) also changes the hash — a
#: dropped ``provenance``/``test_capture`` can never silently pass verification.
_AUTHORITY_FIELDS: tuple[str, ...] = ("provenance", "test_capture", "synthetic", "evidence_rung", "capture_method")

#: Domain separator between the data-file region and the authority region of a v2 digest, so a v2
#: hash can never collide with a v1 (data-only) hash over the same files.
_AUTHORITY_DOMAIN_SEP = b"\x00replaypack.authority.v2\x00"


class ReplayPack(BaseModel):
    pack_version: int = 1
    capture: dict[str, Any]  # {started_ts, ended_ts, endpoints, tool, gaps, + v2 authority fields}
    fixtures: list[dict[str, Any]]  # [{fixture_id, records, odds_updates?}]
    closing_policy: str = "con-040_last_pre_inrunning"
    content_hash: str  # v1: sha256 over data-file bytes; v2: data-file bytes + authority block


def _canonical_authority_bytes(capture: dict[str, Any]) -> bytes:
    """Deterministic serialization of the :data:`_AUTHORITY_FIELDS` for the v2 ``content_hash``.

    Each field is read with a ``None`` default (explicit-null) and the whole block is emitted with
    sorted keys + a stable separator, so any edit OR deletion of an authority field changes the
    bytes — the tamper-evidence that binds the provenance declaration to the pack identity.
    """
    normalized = {field: capture.get(field) for field in _AUTHORITY_FIELDS}
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _manifest_filenames(fixtures: list[dict[str, Any]]) -> list[str]:
    """Sorted filenames the `fixtures` manifest references — the hash-scope contract.

    An OPTIONAL ``venue_quotes`` leg (recorded venue quotes for the fixture) joins the hash scope
    when present, so tampering with the recorded quotes is detected exactly like the records/odds
    files. Venue quotes remain a NON-EVIDENCE sibling (:func:`load_pack_venue_quotes` marks each
    frame ``evidence=False``); being content-hashed here never makes them sealed evidence.
    """
    names: list[str] = []
    for entry in fixtures:
        names.append(entry["records"])
        if "odds_updates" in entry:
            names.append(entry["odds_updates"])
        if "venue_quotes" in entry:
            names.append(entry["venue_quotes"])
    return sorted(names)


def _compute_content_hash(
    pack_dir: Path,
    fixtures: list[dict[str, Any]],
    *,
    pack_version: int = 1,
    capture: dict[str, Any] | None = None,
) -> str:
    """sha256 over length-prefixed (name, bytes) pairs for each MANIFEST-referenced data file,
    in sorted-filename order. Hash scope == the `fixtures` manifest exactly: a file present in
    `pack_dir` but not referenced by `fixtures` (e.g. a stale leftover from a prior build into
    the same directory) is excluded, so content_hash always describes exactly what `fixtures`
    lists — never more, never less. Length-prefixing (rather than a bare separator byte) makes
    the (name, bytes) decomposition provably injective.

    Version-aware (MAJOR-1): for ``pack_version >= 2`` the canonical AUTHORITY block
    (:func:`_canonical_authority_bytes` over ``capture``) is folded in after the data region behind
    a domain separator, so relabeling a pack's provenance/test_capture/synthetic markers changes its
    identity. A legacy ``pack_version == 1`` hash covers DATA FILES ONLY (unchanged semantics) — such
    packs keep loading, but their authority is not hash-bound and can never read genuine.
    """
    file_bytes = {name: (pack_dir / name).read_bytes() for name in _manifest_filenames(fixtures)}
    return _content_hash_from_file_bytes(file_bytes, fixtures, pack_version=pack_version, capture=capture)


def _content_hash_from_file_bytes(
    file_bytes: dict[str, bytes],
    fixtures: list[dict[str, Any]],
    *,
    pack_version: int = 1,
    capture: dict[str, Any] | None = None,
) -> str:
    """The content-hash CORE over an IN-MEMORY ``{filename: bytes}`` snapshot (see :func:`_compute_content_hash`).

    Iterates the SAME canonical, duplicate-preserving filename sequence (:func:`_manifest_filenames`) as
    the on-disk path and mixes bytes identically, so the digest is byte-for-byte equal — only the byte
    SOURCE differs (memory vs disk). Exposed so a load can hash the EXACT bytes it parses/replays from one
    snapshot, closing the load-vs-hash TOCTOU (a second disk read can be swapped between the two)."""
    digest = hashlib.sha256()
    for name in _manifest_filenames(fixtures):
        name_bytes = name.encode("utf-8")
        data = file_bytes[name]
        digest.update(len(name_bytes).to_bytes(4, "big"))
        digest.update(name_bytes)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    if pack_version >= 2:
        authority_bytes = _canonical_authority_bytes(capture or {})
        digest.update(_AUTHORITY_DOMAIN_SEP)
        digest.update(len(authority_bytes).to_bytes(8, "big"))
        digest.update(authority_bytes)
    return digest.hexdigest()


def pack_from_session(
    session_dir: Path, out_dir: Path, *, authority: PackAuthority | None = None
) -> ReplayPack:
    """Pure file transform: recorder session -> self-describing, hashed ReplayPack.

    Splits enveloped records per fixture into ``out_dir/odds_<fid>.jsonl`` (one RAW
    native TxLINE record per line, unwrapped from its envelope), copies any
    ``updates_<fid>.json`` present, computes ``content_hash``, writes ``out_dir/pack.json``.

    Provenance authority (MAJOR-1 / D-residual): ``authority`` is a SEALED
    :class:`~veridex.ingest.capture_chain.PackAuthority` capability, NOT a raw dict — the open-dict
    input is gone, so an arbitrary caller can no longer smuggle genuine field VALUES into a pack. A
    GENUINE capability is constructible ONLY by the CLOSED producer paths (live capture + the
    verified-backfill banker); the ordinary/public builder can only ever receive a non-genuine
    (synthetic/test) capability, so it can NEVER mint a genuine pack from arbitrary records. THE SEAL
    IS RE-CHECKED HERE — the actual write/mint point — via
    :func:`~veridex.ingest.capture_chain._assert_authority_mintable`, not just at ``PackAuthority``
    construction: a duck-typed object exposing ``as_capture_fields()``, an ``object.__new__`` bypass of
    ``__post_init__``, or a subclass overriding a construction-time check all fail closed HERE (exact
    type membership + proven seal possession — see that function's docstring). When a capability is
    supplied, its five authority fields are merged into the ``capture`` block AND folded into a
    ``pack_version=2`` ``content_hash`` (tamper-evident). When ``authority`` is ``None`` the pack stays
    legacy ``pack_version=1`` with a DATA-ONLY hash — such a pack can never read genuine.
    """
    meta, records, gaps = read_session(session_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    by_fixture: dict[int, list[dict[str, Any]]] = {}
    ended_ts = meta.started_ts
    for envelope in records:
        raw = envelope["record"]
        # Fail loud on a malformed record (missing/non-coercible FixtureId). This intentionally
        # differs from marketstates_from_record_stream, which silently drops such records at
        # replay time — a corrupt CAPTURE should surface immediately at pack-build time rather
        # than silently producing a pack that quietly omits data.
        fid = int(raw["FixtureId"])
        by_fixture.setdefault(fid, []).append(raw)
        ended_ts = max(ended_ts, int(envelope["received_ts"]))

    fixtures: list[dict[str, Any]] = []
    for fid in sorted(by_fixture):
        records_filename = f"odds_{fid}.jsonl"
        (out_dir / records_filename).write_text("\n".join(json.dumps(r) for r in by_fixture[fid]) + "\n")

        fixture_entry: dict[str, Any] = {"fixture_id": fid, "records": records_filename}

        updates_src = session_dir / f"updates_{fid}.json"
        if updates_src.exists():
            updates_filename = f"updates_{fid}.json"
            (out_dir / updates_filename).write_bytes(updates_src.read_bytes())
            fixture_entry["odds_updates"] = updates_filename

        fixtures.append(fixture_entry)

    capture: dict[str, Any] = {
        "started_ts": meta.started_ts,
        "ended_ts": ended_ts,
        "endpoints": meta.endpoints,
        "tool": meta.tool_version,
        "gaps": gaps,
    }

    pack_version = 1
    if authority is not None:
        # D-residual (write-boundary enforcement, Codex re-review): the seal must be re-checked HERE —
        # the actual mint point — not only at PackAuthority construction, which a duck-typed object,
        # an `object.__new__` bypass, or a subclass overriding `_claims_genuine` can all dodge. Lazy
        # import (mirrors this module's other deferred seam imports) avoids a capture_chain <->
        # replay_pack import cycle (capture_chain imports `pack_from_session` at module load time).
        from veridex.ingest.capture_chain import _assert_authority_mintable  # noqa: PLC0415

        _assert_authority_mintable(authority)
        # Merge ONLY the recognized authority fields (the closed set that the v2 hash binds) from the
        # sealed capability, so a caller cannot smuggle extra unhashed keys into the authority region.
        fields = authority.as_capture_fields()
        capture.update({field: fields.get(field) for field in _AUTHORITY_FIELDS})
        pack_version = PACK_FORMAT_VERSION

    pack = ReplayPack(
        pack_version=pack_version,
        capture=capture,
        fixtures=fixtures,
        content_hash=_compute_content_hash(out_dir, fixtures, pack_version=pack_version, capture=capture),
    )
    (out_dir / "pack.json").write_text(pack.model_dump_json())
    return pack


def load_pack_marketstates(
    pack_dir: Path, fixture_id: int, *, batch_size: int = 1, verify: bool = True
) -> list[MarketState]:
    """Read a fixture's odds file and feed the raw records through the SAME normalizer live uses.

    Manifest-gated: the file to read comes from the pack's `fixtures` manifest entry for
    `fixture_id`, NEVER from a filename guessed off `fixture_id` alone. A file sitting in
    `pack_dir` that isn't referenced by the manifest (e.g. a stale leftover from a prior build
    into the same directory) is rejected even if it exists and is well-formed.

    `verify=True` (default) refuses to replay a pack whose stored content_hash doesn't match
    its data files — pass `verify=False` to opt out for trusted/perf-sensitive paths.
    """
    if verify and not verify_content_hash(pack_dir):
        raise ValueError(f"pack at {pack_dir} failed content_hash verification (tampered or corrupt)")

    manifest = json.loads((pack_dir / "pack.json").read_text())
    entry = next((f for f in manifest["fixtures"] if f["fixture_id"] == fixture_id), None)
    if entry is None:
        raise FileNotFoundError(f"fixture_id {fixture_id} not present in pack manifest at {pack_dir}")

    path = pack_dir / entry["records"]
    records = [json.loads(line) for line in path.read_text().splitlines() if line]
    return list(marketstates_from_record_stream(records, batch_size=batch_size))


def load_pack_venue_quotes(
    pack_dir: Path, fixture_id: int, *, verify: bool = True
) -> list[dict[str, Any]]:
    """Load a fixture's OPTIONAL ``venue_quotes`` leg as NON-EVIDENCE rows (each ``evidence=False``).

    Manifest-gated exactly like :func:`load_pack_marketstates`: the quote file comes from the pack's
    ``fixtures`` manifest entry for ``fixture_id`` (``entry["venue_quotes"]``), NEVER a filename
    guessed off ``fixture_id`` alone. A fixture with no quote leg yields ``[]``.

    Venue quotes are a content-hashed SIBLING artifact, never sealed evidence: every returned row is
    stamped ``evidence=False`` so a caller can never mistake a recorded quote for a sealed tick event
    (AC-015 — the quote leg joins ``content_hash`` but never the ``evidence_hash``).

    Args:
        pack_dir: Directory of the self-describing ReplayPack (must contain ``pack.json``).
        fixture_id: The fixture whose quote leg to load.
        verify: When ``True`` (default), refuse a pack whose stored ``content_hash`` no longer
            matches its data files (tampered/corrupt) — pass ``False`` for trusted/perf paths.

    Returns:
        The recorded quote rows, each with an added ``evidence`` key set to ``False``. Empty when the
        fixture carries no ``venue_quotes`` leg.

    Raises:
        ValueError: If ``verify`` is ``True`` and content-hash verification fails.
        FileNotFoundError: If ``fixture_id`` is absent from the pack manifest.
    """
    if verify and not verify_content_hash(pack_dir):
        raise ValueError(f"pack at {pack_dir} failed content_hash verification (tampered or corrupt)")

    manifest = json.loads((pack_dir / "pack.json").read_text())
    entry = next((f for f in manifest["fixtures"] if f["fixture_id"] == fixture_id), None)
    if entry is None:
        raise FileNotFoundError(f"fixture_id {fixture_id} not present in pack manifest at {pack_dir}")

    quotes_name = entry.get("venue_quotes")
    if quotes_name is None:
        return []

    path = pack_dir / quotes_name
    rows = [json.loads(line) for line in path.read_text().splitlines() if line]
    for row in rows:
        # NON-EVIDENCE marker: a recorded venue quote is never a sealed tick event.
        row["evidence"] = False
    return rows


class PackIntegrityError(ValueError):
    """A pack failed a LOAD-TIME integrity gate — content-hash binding or fixture->file mapping.

    Carries a machine-usable ``reason`` so a caller can surface a stable code (e.g. over HTTP) without
    string-matching the message. Subclasses :class:`ValueError` so existing ``except ValueError`` load
    guards still fail closed.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def load_pack_fixture_states_bound(
    pack_dir: Path,
    fixture_id: int,
    *,
    expected_content_hash: str,
    batch_size: int = 1,
) -> list[MarketState]:
    """Load a fixture's ``MarketState`` tape under ONE immutable in-memory snapshot, binding the replayed
    bytes to ``expected_content_hash`` AND the fixture->file mapping to ``fixture_id`` (fail-closed).

    Closes two load-side gaps left by :func:`verify_content_hash` + :func:`load_pack_marketstates`:

    * **TOCTOU** — the manifest and every referenced data file are read into memory EXACTLY ONCE; the
      content_hash is recomputed from those in-memory bytes and the records are parsed from the SAME
      bytes, so the bytes hashed are provably the bytes replayed (no second, swappable disk snapshot).
    * **fixture<->file mapping** — the ``fixture_id -> records file`` mapping is NOT covered by
      ``content_hash`` (which hashes only sorted ``(name, bytes)`` + the authority block). A hash-valid
      ``pack.json`` that swaps two fixtures' ids would point the frozen fixture at ANOTHER fixture's
      bytes. Every RAW record (across all referenced legs) and every NORMALIZED ``MarketState`` must
      carry ``fixture_id``.

    Args:
        pack_dir: Directory of the self-describing ReplayPack (must contain ``pack.json``).
        fixture_id: The FROZEN fixture the run is bound to replay.
        expected_content_hash: The FROZEN ``content_hash`` the replayed bytes must recompute to.
        batch_size: Records buffered per fixture before emitting a snapshot (default ``1`` — the runtime
            default; matches :func:`load_pack_marketstates`).

    Returns:
        The frozen fixture's non-empty ``MarketState`` tape.

    Raises:
        PackIntegrityError: ``reason='content_hash_drift'`` (unreadable/malformed pack, or the recomputed
            in-memory bytes != ``expected_content_hash``), ``reason='fixture_gone'`` (fixture absent from
            the manifest), ``reason='fixture_mapping_mismatch'`` (a raw or normalized record belongs to a
            different fixture — a hash-valid fixture<->file swap), or ``reason='empty_fixture'`` (the
            frozen fixture yields no snapshots).
    """
    # ONE immutable in-memory snapshot: read the manifest + every hash-referenced file EXACTLY once.
    try:
        manifest = json.loads((pack_dir / "pack.json").read_text())
        fixtures = manifest["fixtures"]
        file_bytes = {name: (pack_dir / name).read_bytes() for name in _manifest_filenames(fixtures)}
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise PackIntegrityError(
            f"unreadable or malformed pack at {pack_dir}: {exc}", reason="content_hash_drift"
        ) from exc

    # TOCTOU close: hash the EXACT in-memory bytes (not a second disk read) and bind to the frozen hash.
    recomputed = _content_hash_from_file_bytes(
        file_bytes,
        fixtures,
        pack_version=int(manifest.get("pack_version", 1)),
        capture=manifest.get("capture", {}),
    )
    if recomputed != expected_content_hash:
        raise PackIntegrityError(
            f"recomputed content_hash for pack at {pack_dir} does not match the frozen binding — "
            f"refusing to replay bytes that differ from the sealed identity",
            reason="content_hash_drift",
        )

    entry = next((f for f in fixtures if int(f["fixture_id"]) == fixture_id), None)
    if entry is None:
        raise PackIntegrityError(
            f"fixture_id {fixture_id} not present in pack manifest at {pack_dir}", reason="fixture_gone"
        )

    # RAW mapping guard across ALL referenced legs: the fixture->file mapping is unhashed, so a hash-valid
    # id/file swap points the frozen fixture at another fixture's file. Every raw record that carries a
    # ``FixtureId`` must be the frozen fixture's — fail closed on the first foreign record (parsed from the
    # SAME in-memory snapshot that was hashed).
    for leg in ("records", "odds_updates", "venue_quotes"):
        name = entry.get(leg)
        if name is None:
            continue
        for line in file_bytes[name].decode("utf-8").splitlines():
            if not line:
                continue
            raw_fid = json.loads(line).get("FixtureId")
            if raw_fid is not None and int(raw_fid) != fixture_id:
                raise PackIntegrityError(
                    f"pack at {pack_dir} maps fixture_id {fixture_id} onto a file whose records carry "
                    f"FixtureId {int(raw_fid)} — a hash-valid fixture<->file swap",
                    reason="fixture_mapping_mismatch",
                )

    records = [json.loads(line) for line in file_bytes[entry["records"]].decode("utf-8").splitlines() if line]
    states = list(marketstates_from_record_stream(records, batch_size=batch_size))

    # NORMALIZED mapping guard: every emitted snapshot is exactly what the run replays and seals, so each
    # MUST belong to the frozen fixture (defence in depth over the raw guard).
    if any(ms.fixture_id != fixture_id for ms in states):
        raise PackIntegrityError(
            f"pack at {pack_dir} produced normalized snapshots for a fixture other than {fixture_id}",
            reason="fixture_mapping_mismatch",
        )
    if not states:
        raise PackIntegrityError(
            f"frozen fixture {fixture_id} in pack at {pack_dir} yields no market snapshots",
            reason="empty_fixture",
        )
    return states


def verify_content_hash(pack_dir: Path) -> bool:
    """Recompute content_hash from the manifest's referenced data files; compare to pack.json's
    stored value. A corrupt/missing manifest, or a manifest-referenced file that's gone, counts
    as a FAILED verification (returns False) rather than raising.

    Version-aware (MAJOR-1): recomputes with the manifest's ``pack_version`` + ``capture`` block, so
    a v2 pack's AUTHORITY fields are re-checked too — a post-build relabel of provenance/test_capture
    that does NOT recompute the hash fails here.
    """
    try:
        manifest = json.loads((pack_dir / "pack.json").read_text())
        pack_version = int(manifest.get("pack_version", 1))
        recomputed = _compute_content_hash(
            pack_dir,
            manifest["fixtures"],
            pack_version=pack_version,
            capture=manifest.get("capture", {}),
        )
        return recomputed == manifest["content_hash"]
    except (json.JSONDecodeError, KeyError, FileNotFoundError, TypeError, ValueError):
        return False
