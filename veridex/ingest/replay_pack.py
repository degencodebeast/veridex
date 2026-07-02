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
    """Sorted filenames the `fixtures` manifest references — the hash-scope contract."""
    names: list[str] = []
    for entry in fixtures:
        names.append(entry["records"])
        if "odds_updates" in entry:
            names.append(entry["odds_updates"])
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


def load_pack_marketstates(pack_dir: Path, fixture_id: int, *, batch_size: int = 1) -> list[MarketState]:
    """Read a fixture's odds file and feed the raw records through the SAME normalizer live uses."""
    path = pack_dir / f"odds_{fixture_id}.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"fixture_id {fixture_id} not found in pack {pack_dir} (missing {path.name})")
    records = [json.loads(line) for line in path.read_text().splitlines() if line]
    return list(marketstates_from_record_stream(records, batch_size=batch_size))


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
