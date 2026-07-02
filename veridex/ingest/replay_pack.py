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


def _data_files(pack_dir: Path) -> list[Path]:
    """The pack's hashed data files (odds_*.jsonl, updates_*.json), sorted for a canonical order."""
    return sorted(
        p for p in pack_dir.iterdir() if p.name.startswith(("odds_", "updates_")) and p.suffix in {".jsonl", ".json"}
    )


def _compute_content_hash(pack_dir: Path) -> str:
    """sha256 over name + b"\\0" + bytes for each data file, concatenated in sorted-name order."""
    digest = hashlib.sha256()
    for path in _data_files(pack_dir):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
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

    pack = ReplayPack(capture=capture, fixtures=fixtures, content_hash=_compute_content_hash(out_dir))
    (out_dir / "pack.json").write_text(pack.model_dump_json())
    return pack


def load_pack_marketstates(pack_dir: Path, fixture_id: int, *, batch_size: int = 1) -> list[MarketState]:
    """Read a fixture's odds file and feed the raw records through the SAME normalizer live uses."""
    path = pack_dir / f"odds_{fixture_id}.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines() if line]
    return list(marketstates_from_record_stream(records, batch_size=batch_size))


def verify_content_hash(pack_dir: Path) -> bool:
    """Recompute content_hash from the pack's data files; compare to pack.json's stored value."""
    stored = json.loads((pack_dir / "pack.json").read_text())["content_hash"]
    return _compute_content_hash(pack_dir) == stored
