"""T2 — pure session-file core for the continuous capture recorder (REQ-2D-002).

T0 read-only-to-trust-path tool: NO network, NO LLM imports, NO imports from
veridex/law, veridex/checks, veridex/verifier, or veridex/runtime/evidence. This module
only knows how to serialize/deserialize the on-disk session format:

    captures/<session_ts>/meta.json
    captures/<session_ts>/records.jsonl      (append-only, one JSON object per line)
    captures/<session_ts>/updates_<fid>.json (one per fixture, written at shutdown)

The async network shell lives in scripts/txline_live/record.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class SessionMeta(BaseModel):
    started_ts: int
    endpoints: list[str]
    tool_version: str
    # REQ-2D-002(e): capture window, fixture ids, record counts. All optional so a
    # crash-partial meta.json (written at session start, before any records or shutdown)
    # still parses via these defaults.
    ended_ts: int | None = None
    fixture_ids: list[int] = Field(default_factory=list)
    record_counts: dict[str, int] = Field(default_factory=dict)  # fixture id (str) -> count


def envelope_line(record: dict[str, Any], received_ts: int) -> str:
    """One JSON line (no trailing newline) wrapping a raw record with its receipt time."""
    return json.dumps({"received_ts": received_ts, "record": record})


def gap_line(from_ts: int, to_ts: int) -> str:
    """One JSON line marking an explicit stream gap — never a silent splice."""
    return json.dumps({"gap": {"from_ts": from_ts, "to_ts": to_ts}})


def finalize_meta(start_meta: SessionMeta, *, ended_ts: int, record_counts: dict[str, int]) -> SessionMeta:
    """Build the finalized SessionMeta written at shutdown (REQ-2D-002(e)).

    Carries forward the original started_ts/endpoints/tool_version and fills in the
    capture-window end plus the sorted fixture ids derived from ``record_counts`` keys
    (JSON object keys must be strings, so ``record_counts`` is fixture-id-as-string ->
    count; ``fixture_ids`` is the same set as sorted ints for easy scanning).
    """
    fixture_ids = sorted(int(k) for k in record_counts)
    return start_meta.model_copy(
        update={"ended_ts": ended_ts, "fixture_ids": fixture_ids, "record_counts": dict(record_counts)}
    )


def read_session(path: Path) -> tuple[SessionMeta, list[dict[str, Any]], list[dict[str, Any]]]:
    """Read ``<path>/meta.json`` + ``<path>/records.jsonl``.

    Returns ``(meta, enveloped_records, gaps)`` where ``enveloped_records`` are the parsed
    ``{"received_ts": ..., "record": {...}}`` dicts and ``gaps`` are the parsed
    ``{"from_ts": ..., "to_ts": ...}`` dicts.

    Crash-safe: a truncated final line (e.g. process killed mid-write) is dropped rather than
    raising.
    """
    meta = SessionMeta.model_validate_json((path / "meta.json").read_text())

    records: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    lines = (path / "records.jsonl").read_text().splitlines()
    for i, line in enumerate(lines):
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            if i != len(lines) - 1:
                raise  # only the final line may be a crash-truncated partial write
            continue
        if "gap" in entry:
            gaps.append(entry["gap"])
        else:
            records.append(entry)

    return meta, records, gaps
