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
from typing import Any

from pydantic import BaseModel

from veridex.ingest.live_client import marketstates_from_record_stream
from veridex.ingest.marketstate import MarketState
from veridex.ingest.recorder import read_session


class ReplayPack(BaseModel):
    pack_version: int = 1
    capture: dict[str, Any]  # {started_ts, ended_ts, endpoints, tool, gaps}
    fixtures: list[dict[str, Any]]  # [{fixture_id, records, odds_updates?}]
    closing_policy: str = "con-040_last_pre_inrunning"
    content_hash: str  # sha256 over canonically-ordered data-file bytes


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


def _compute_content_hash(pack_dir: Path, fixtures: list[dict[str, Any]]) -> str:
    """sha256 over length-prefixed (name, bytes) pairs for each MANIFEST-referenced data file,
    in sorted-filename order. Hash scope == the `fixtures` manifest exactly: a file present in
    `pack_dir` but not referenced by `fixtures` (e.g. a stale leftover from a prior build into
    the same directory) is excluded, so content_hash always describes exactly what `fixtures`
    lists — never more, never less. Length-prefixing (rather than a bare separator byte) makes
    the (name, bytes) decomposition provably injective.
    """
    digest = hashlib.sha256()
    for name in _manifest_filenames(fixtures):
        name_bytes = name.encode("utf-8")
        file_bytes = (pack_dir / name).read_bytes()
        digest.update(len(name_bytes).to_bytes(4, "big"))
        digest.update(name_bytes)
        digest.update(len(file_bytes).to_bytes(8, "big"))
        digest.update(file_bytes)
    return digest.hexdigest()


def pack_from_session(session_dir: Path, out_dir: Path) -> ReplayPack:
    """Pure file transform: recorder session -> self-describing, hashed ReplayPack.

    Splits enveloped records per fixture into ``out_dir/odds_<fid>.jsonl`` (one RAW
    native TxLINE record per line, unwrapped from its envelope), copies any
    ``updates_<fid>.json`` present, computes ``content_hash``, writes ``out_dir/pack.json``.
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

    capture = {
        "started_ts": meta.started_ts,
        "ended_ts": ended_ts,
        "endpoints": meta.endpoints,
        "tool": meta.tool_version,
        "gaps": gaps,
    }

    pack = ReplayPack(capture=capture, fixtures=fixtures, content_hash=_compute_content_hash(out_dir, fixtures))
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


def verify_content_hash(pack_dir: Path) -> bool:
    """Recompute content_hash from the manifest's referenced data files; compare to pack.json's
    stored value. A corrupt/missing manifest, or a manifest-referenced file that's gone, counts
    as a FAILED verification (returns False) rather than raising.
    """
    try:
        manifest = json.loads((pack_dir / "pack.json").read_text())
        return _compute_content_hash(pack_dir, manifest["fixtures"]) == manifest["content_hash"]
    except (json.JSONDecodeError, KeyError, FileNotFoundError):
        return False
